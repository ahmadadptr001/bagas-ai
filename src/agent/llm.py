"""Klien endpoint API OpenAI-compatible (NVIDIA, OpenRouter) + jalur CLI
OpenCode, dengan penanganan error tanpa pengiriman ulang otomatis.

Modul ini melayani jalur model bagas-ai yang berbasis API: model `nvidia/*`
(integrate.api.nvidia.com), `openrouter/*` (openrouter.ai/api/v1, kunci
OPENROUTER_API_KEY), dan `opencode/*` (JALUR UTAMA: subprocess CLI
`opencode run --format json` — butuh binary di PATH, TANPA API key;
OPENCODE_API_KEY hanya dipakai cadangan HTTP bila CLI absen). Jalur
lainnya (model `web/*`) tak menyentuh berkas ini
sama sekali — ia lewat agent/connectors + Agent._run_connector, dan padanan
penanganan "sementara" di sana berbentuk lain sesuai medianya (WebBusyError
untuk server penuh, WebLimitError untuk kuota situs habis).

Permintaan non-busy dikirim sekali tanpa retry. Kondisi sibuk/overload
(503/529) di-retry sampai 10 kali berturut-turut dengan backoff; satu
keberhasilan mereset hitungan ke 0. Setelah 10 kegagalan berturut-turut,
ProviderBusyError dilempar ke UI. Batas baca NVIDIA terpisah; pembatalan
pengguna tetap responsif ketika koneksi sedang menunggu data.
"""
from __future__ import annotations

import json as _json
import re as _re
import time
import queue
import shutil
import subprocess
import threading
from typing import Any, Callable

from . import config

# openai di-IMPOR MALAS: paket ini menarik ratusan modul tipe (~1.7 dtk saat
# impor) yang tak dipakai jalur browser sama sekali. Menundanya sampai panggilan
# API PERTAMA membuat start bagas-ai jauh lebih cepat — dan bagi pengguna yang
# hanya memakai model web, openai tak pernah dimuat seumur sesi.
_openai = None


def _oa():
    global _openai
    if _openai is None:
        import openai as _mod
        _openai = _mod
    return _openai


def is_rate_limit(exc: Exception) -> bool:
    """True bila exc adalah RateLimitError openai (tanpa memaksa impor openai)."""
    return _openai is not None and isinstance(exc, _openai.RateLimitError)


class EmptyResponseError(Exception):
    """NVIDIA kadang membalas HTTP 200 dengan body KOSONG saat throttle.

    Diperlakukan sebagai kondisi sementara supaya di-retry dengan backoff,
    bukan dilaporkan ke pengguna sebagai "model tak menjawab".
    """


class Cancelled(Exception):
    """Pengguna membatalkan giliran (Esc / Ctrl+C).

    Dibedakan dari kegagalan sungguhan supaya UI tak menampilkannya sebagai
    error, dan supaya core tetap merapikan tool yang menggantung lalu menyimpan
    sesi — pembatalan tidak boleh membuat konteks percakapan rusak.

    Tinggal di sini, bukan di connectors/ atau core.py: ia dilempar lapisan
    connector, ditangkap core, lalu dikenali lagi CLI. Di salah satu lapisan itu
    dua lapisan lain harus mengimpor yang bukan urusannya, dan
    connectors -> core akan jadi impor melingkar.
    """


class ProviderQuotaError(Exception):
    """Kuota layanan benar-benar habis dan retry cepat tidak akan menolong."""


class ProviderReadTimeout(Exception):
    """Koneksi tidak mengirim data sampai batas baca, tanpa retry otomatis."""


class ProviderBusyError(Exception):
    """Penyedia menolak sementara; sudah di-retry 10 kali berturut-turut."""


# Retry khusus kondisi sibuk (503/529 / "overloaded"): maksimal 10 kali
# berturut-turut. Sukses di percobaan mana pun mereset hitungan ke 0 —
# jadi "10 kali gagal" selalu berarti 10 kegagalan BERTURUT-TURUT, bukan
# akumulasi sepanjang sesi. Error non-busy (auth, konteks, payload) TIDAK
# masuk jalur ini: tetap dilaporkan sekali tanpa pengiriman ulang.
_BUSY_MAX_RETRY = 10


def _read_timeout(provider: str) -> float:
    return (config.NVIDIA_READ_TIMEOUT if provider in ("", "nvidia")
            else config.TTFT_TIMEOUT)


def _jelaskan_timeout(exc: Exception, provider: str) -> Exception:
    import httpx
    cause = exc
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, httpx.ReadTimeout):
            name = provider or "nvidia"
            return ProviderReadTimeout(
                f"Koneksi ke {name} berhenti mengirim data saat menunggu respons "
                f"(batas baca {_read_timeout(provider):g} detik). "
                "Jawaban yang sudah tampil tetap disimpan; prompt tidak dikirim "
                "ulang otomatis. Coba lanjutkan setelah layanan/koneksi pulih.")
        cause = cause.__cause__ or cause.__context__
    return exc


def _stream_cancellable(create: Callable[[], Any], cancel_event: Any):
    """Baca jaringan di worker; callback/model state tetap di thread pemanggil.

    Hanya pembatalan pengguna yang menutup stream, bukan jeda antar-token.
    Worker yang masih menunggu header membersihkan respons saat ia tiba;
    batas baca jaringan tetap berlaku agar request tidak hidup selamanya.
    """
    if cancel_event is None:
        stream = create()
        try:
            yield from stream
        finally:
            try:
                stream.close()
            except Exception:
                pass
        return
    if cancel_event.is_set():
        raise Cancelled()
    events = queue.Queue(maxsize=16)
    stopped = threading.Event()
    stream_box = []
    close_lock = threading.Lock()
    closed = False

    def close():
        nonlocal closed
        with close_lock:
            if closed or not stream_box:
                return
            closed = True
        try:
            stream_box[0].close()
        except Exception:
            pass

    def put(kind, value=None):
        while not stopped.is_set():
            try:
                events.put((kind, value), timeout=0.1)
                return
            except queue.Full:
                pass

    def read():
        try:
            stream = create()
            stream_box.append(stream)
            if stopped.is_set():
                return
            for item in stream:
                if stopped.is_set():
                    break
                put("item", item)
        except BaseException as exc:
            put("error", exc)
        finally:
            close()
            put("done")

    threading.Thread(target=read, name="bagasai-api-reader", daemon=True).start()
    try:
        while True:
            if cancel_event.is_set():
                raise Cancelled()
            try:
                kind, value = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if cancel_event.is_set():
                raise Cancelled()
            if kind == "done":
                return
            if kind == "error":
                raise value
            yield value
    finally:
        stopped.set()
        # close() pada beberapa transport bisa menunggu bacaan socket.
        # Jangan tahan thread UI/agent ketika pengguna sudah membatalkan.
        threading.Thread(target=close, name="bagasai-api-close", daemon=True).start()


# StreamStalled & _StallTimeout DIHAPUS (2026-09-23) bersama watchdog antar-
# token yang melahirkannya. Keduanya ADA untuk membatalkan stream yang sedang
# mengalir ("diam >45 dtk") lalu meminta ulang dari nol — dan justru itu
# keluhannya: model yang butuh jeda panjang di tengah jawaban tampak
# "dibatalkan padahal sedang menjawab", tulisannya lenyap, lalu ia mulai lagi
# dari awal. Penjaga liveness sekarang tinggal batas baca httpx
# (TTFT_TIMEOUT), dan retry tak pernah lagi menyentuh jawaban yang sudah
# mengalir (lihat sudah_mengalir di _call_with_retry).


# Kata kunci pada PESAN error yang menandakan kondisi SEMENTARA (throttle /
# kapasitas / gangguan sesaat). NVIDIA sering mengirim "worker local total
# request limit reached" dengan kode status tak terduga, jadi klasifikasinya
# juga lewat isi pesan, bukan cuma tipe/kode.
_TRANSIENT_KEYWORDS = (
    "request limit", "rate limit", "too many request", "limit reached",
    "overloaded", "capacity", "try again", "temporarily", "unavailable",
    "timeout", "timed out", "connection", "throttl", "429", "server error",
    "bad gateway", "gateway timeout", "worker", "quota", "busy",
    # "Provider returned error" sengaja tidak dianggap transient. Core mencoba
    # payload yang lebih ringan (tanpa extra/media/schema tool), tetapi TIDAK
    # menyentuh riwayat — riwayat tak pernah dipangkas otomatis lagi.
)
# Kode status FATAL (percuma diulang): permintaan salah / auth / model tak ada.
# Selain ini, 5xx dianggap sementara.
_FATAL_STATUS = {400, 401, 403, 404, 405, 422}

# Kata kunci pada pesan HTTP 400 yang berarti permintaannya MELEWATI JENDELA
# KONTEKS model — bukan permintaan salah. Dibedakan supaya core bisa
# MENJELASKANNYA (beserta jalan keluar /compact) alih-alih melaporkan
# kegagalan misterius. Sejak 2026-09-22 kata kunci ini TIDAK memicu pemangkasan
# riwayat: daftarnya sengaja lebar ("request too large" juga muncul saat
# BODY-nya kebesaran), jadi ia terlalu lemah untuk dipakai membuang pekerjaan.
_KATA_KONTEKS_PENUH = (
    "context length", "context window", "maximum context", "context_limit",
    "too many tokens", "token limit", "exceeds the maximum",
    "input length", "input tokens exceed", "prompt is too long",
    "reduce the length", "percih panjang", "payload too large",
    "request too large", "too large",
)


def _error_text(exc: Exception) -> str:
    """Gabungkan bagian error SDK yang relevan tanpa header/request rahasia."""
    bagian = [str(getattr(exc, "message", "") or "")]
    body = getattr(exc, "body", None)
    if body:
        bagian.append(str(body))
    bagian.append(str(exc))
    return " ".join(x for x in bagian if x)


def _is_free_usage_limit(exc: Exception) -> bool:
    """True khusus limit gratis OpenCode/Console, bukan throttle sementara."""
    teks = _error_text(exc).lower().replace("_", "")
    return ("freeusagelimiterror" in teks
            or ("free usage" in teks and "limit" in teks))


def _is_provider_busy(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    # Jangan menyamarkan auth, payload salah, konteks penuh, atau kuota akun.
    if status in _FATAL_STATUS or isinstance(exc, (Cancelled, KonteksPenuh)):
        return False
    teks = _error_text(exc).lower()
    if any(word in teks for word in ("insufficient_quota", "freeusagelimiterror")):
        return False
    return status in (503, 529) or any(word in teks for word in (
        "service temporarily overloaded", "overloaded", "server is busy",
        "service at capacity",
    ))


def _quota_error(exc: Exception) -> ProviderQuotaError:
    status = getattr(exc, "status_code", None)
    kode = f"HTTP {status} " if status else ""
    return ProviderQuotaError(
        f"Kuota gratis OpenCode Zen untuk jaringan/akun ini sedang habis "
        f"({kode}FreeUsageLimitError). Ini batas layanan, bukan kerusakan "
        "tool Bagas-AI. Tunggu kuota tersedia lagi, masuk lewat `opencode "
        "auth login`/isi OPENCODE_API_KEY untuk kuota akun, atau pilih model "
        "web lewat /model."
    )


class KonteksPenuh(Exception):
    """Permintaan ditolak karena riwayat melebihi jendela konteks model.

    FATAL untuk retry biasa (mengulang payload yang sama pasti ditolak lagi).
    Core menanganinya dengan MELAPORKAN apa adanya + jalan keluar milik
    pengguna (`/compact` lalu `/new` + `/send-compact`) — bukan lagi dengan
    memangkas riwayat diam-diam. Dibedakan dari BadRequestError mentah supaya
    pesan ke pengguna bukan sekadar "400 Bad Request" tanpa jalan keluar."""

    def __init__(self, asli: str = "") -> None:
        super().__init__(asli or "riwayat melebihi jendela konteks model")
        self.asli = asli


def _teks_menyebut_konteks_penuh(teks: str) -> bool:
    """True bila teks galat menyebut kelebihan konteks/token."""
    t = (teks or "").lower()
    return any(k in t for k in _KATA_KONTEKS_PENUH)


def _apakah_konteks_penuh(exc: Exception) -> bool:
    """Deteksi HTTP 400/413 'konteks penuh' dari exception openai apa pun.

    Isi galatnya tersebar di .message / .body / str() tergantung jalur
    (OpenRouter membalas {"error":{"message":...,"code":400}}), jadi ketiganya
    digabung lalu dicocokkan kata kuncinya. Status 413 (Payload Too Large)
    langsung sah tanpa perlu cocok kata kunci."""
    o = _openai
    if o is None or not isinstance(exc, o.BadRequestError):
        return False
    if getattr(exc, "status_code", None) == 413:
        return True
    gabung = " ".join(str(getattr(exc, a, "") or "") for a in ("message", "body"))
    return _teks_menyebut_konteks_penuh(gabung + " " + str(exc))


def _is_transient(exc: Exception) -> bool:
    """True bila error layak dicoba ulang (rate limit / throttle / gangguan)."""
    if isinstance(exc, (Cancelled, KonteksPenuh)):
        # KonteksPenuh: mengulang payload yang sama PASTI ditolak lagi —
        # pemulihannya bukan retry, melainkan penjelasan + /compact (di core).
        return False
    if isinstance(exc, EmptyResponseError):
        return True
    # Tipe exception openai dicek HANYA bila openai sudah dimuat (pasti sudah,
    # karena exc ini datang dari panggilan yang memakai klien openai).
    o = _openai
    if o is not None and isinstance(
        exc,
        (o.RateLimitError, o.APIConnectionError, o.APITimeoutError,
         o.InternalServerError),
    ):
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    # Pesan throttle menang atas kode status (kodenya bisa aneh saat limit).
    if any(k in msg for k in _TRANSIENT_KEYWORDS):
        return True
    status = getattr(exc, "status_code", None)
    if status in _FATAL_STATUS:
        return False
    if isinstance(status, int) and status >= 500:
        return True
    # APIError umum tanpa kode jelas -> anggap sementara (lebih baik menunggu
    # daripada membatalkan tugas pengguna).
    if o is not None and isinstance(exc, o.APIError):
        return True
    return False


_HAS_TOOLTEXT = _re.compile(r"<tool_call>|<function\s*=", _re.IGNORECASE)


def _extract_text_tool_calls(text: str) -> list[dict[str, str]]:
    """Sebagian model MENULISKAN panggilan tool sebagai TEKS/XML (mis.
    `<function=write_file><parameter=content>...</parameter></function>` atau
    `<tool_call>{json}</tool_call>`) alih-alih memakai function-calling asli —
    lalu berhenti. Endpoint tak mem-parse itu, jadi tanpa penanganan hasilnya
    cuma teks sampah di layar. Fungsi ini menyelamatkannya jadi tool_calls
    sungguhan.

    HANYA blok LENGKAP (ada tag penutup) yang diterima: panggilan yang terpotong
    (mis. kena batas token) tak boleh dieksekusi setengah jadi.
    """
    calls: list[tuple[str, Any]] = []
    # Format A: <function=nama> ... <parameter=kunci>nilai</parameter> ... </function>
    for m in _re.finditer(r"<function\s*=\s*([^\s>]+)\s*>(.*?)</function>",
                          text, _re.DOTALL | _re.IGNORECASE):
        name = m.group(1).strip()
        args: dict[str, str] = {}
        for pm in _re.finditer(r"<parameter\s*=\s*([^\s>]+)\s*>(.*?)</parameter>",
                               m.group(2), _re.DOTALL | _re.IGNORECASE):
            val = pm.group(2)
            if val.startswith("\n"):
                val = val[1:]
            args[pm.group(1).strip()] = val.rstrip("\n")
        if name:
            # Beberapa model membungkus objek JSON langsung dengan <function>.
            # Jangan mengganti argumen itu dengan {} hanya karena bukan XML parameter.
            body = m.group(2).strip()
            calls.append((name, args if args or not body else body))
    # Format B: <tool_call>{"name":..,"arguments":..}</tool_call>
    if not calls:
        for m in _re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
                              text, _re.DOTALL | _re.IGNORECASE):
            try:
                obj = _json.loads(m.group(1))
            except ValueError:
                continue
            name = obj.get("name")
            a = obj.get("arguments", obj.get("parameters", {}))
            if name:
                calls.append((name, a))
    out: list[dict[str, str]] = []
    for i, (name, args) in enumerate(calls):
        out.append({
            "id": f"txt_{i}",
            "name": name,
            "arguments": args if isinstance(args, str) else _json.dumps(args, ensure_ascii=False),
        })
    return out


def _sleep_cancellable(seconds: float, cancel_event: Any) -> None:
    """Tidur `seconds` detik tapi tetap bisa dibatalkan (cek cancel_event)."""
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        time.sleep(min(0.2, remaining))


def _busy_backoff(attempt: int) -> float:
    """Tunggu antar retry busy: 2s, 4s, 6s, … maks 20s (linear berdempet)."""
    return min(2.0 * attempt, 20.0)


def _busy_error(provider: str, percobaan: int, exc: Exception) -> ProviderBusyError:
    label = {"nvidia": "NVIDIA", "openrouter": "OpenRouter",
             "opencode": "OpenCode"}.get(provider or "nvidia", provider)
    return ProviderBusyError(
        f"Layanan {label} sedang sibuk atau sementara tidak tersedia. "
        f"Sudah dicoba ulang {percobaan} kali berturut-turut tanpa hasil. "
        "Percakapan dan jawaban parsial tetap dipertahankan. "
        "Tunggu sebentar lalu lanjutkan, atau pilih model lain lewat /model. "
        "Menaikkan timeout tidak mengatasi penolakan dari server ini."
    )


def _call_with_retry(
    do: Callable[[], Any],
    *,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    sudah_mengalir: Callable[[], bool] | None = None,
    provider: str = "",
) -> Any:
    """Jalankan permintaan; retry HANYA untuk kondisi busy/sibuk (maks 10x).

    Error non-busy (auth, konteks penuh, payload salah, kuota) tetap naik
    tanpa pengiriman ulang. Retry busy berhenti lebih awal bila jawaban
    sudah mengalir (`sudah_mengalir`) — mengulang berarti menghapus
    jawaban yang sedang tampil. Percobaan ke-10 yang tetap gagal melempar
    ProviderBusyError (pesan merah di UI).
    """
    percobaan_busy = 0
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        try:
            hasil = do()
            # Sukses -> reset: hitungan "10 gagal" selalu BERTURUT-TURUT.
            percobaan_busy = 0
            return hasil
        except Cancelled:
            raise
        except Exception as exc:
            if _is_free_usage_limit(exc):
                raise _quota_error(exc) from exc
            if not _is_provider_busy(exc):
                raise
            # Jawaban parsial sudah di layar: jangan restart dari nol.
            if sudah_mengalir is not None and sudah_mengalir():
                raise _busy_error(provider, percobaan_busy or 1, exc) from exc
            percobaan_busy += 1
            if percobaan_busy >= _BUSY_MAX_RETRY:
                raise _busy_error(provider, percobaan_busy, exc) from exc
            tunggu = _busy_backoff(percobaan_busy)
            if on_retry:
                try:
                    on_retry(percobaan_busy, tunggu, exc)
                except Exception:  # noqa: BLE001 — UI callback tak boleh mematikan retry
                    pass
            _sleep_cancellable(tunggu, cancel_event)


# Satu klien PER PENYEDIA dipakai ulang di seluruh aplikasi.
_clients: dict[str, Any] = {}

# Header atribusi yang diminta OpenRouter (opsional tapi resmi): identitas
# aplikasi muncul di dashboard aktivitas openrouter.ai.
_OPENROUTER_HEADERS = {
    "HTTP-Referer": config.REPO_URL,
    "X-Title": config.APP_NAME,
}


def _headers_tanpa_auth(provider: str) -> dict[str, Any]:
    """Header per-request yang membuang Authorization untuk Zen TANPA key.

    Model opencode/* gratis jalan secara anonim (kuota per-IP), tapi klien
    dibuat dengan api_key dummy (lihat get_client). Omit() adalah cara resmi
    SDK openai untuk "jangan kirim header ini" — tanpanya SDK justru
    melempar TypeError "Could not resolve authentication method" saat
    Authorization-nya kosong. Kosong bila provider bukan opencode atau key
    tersedia (key asli tetap dikirim sebagai Bearer).
    """
    if provider == "opencode" and not config.OPENCODE_API_KEY:
        from openai._base_client import Omit  # noqa: PLC0415 — impor tunda, selaras _oa()
        return {"Authorization": Omit()}
    return {}


def get_client(provider: str = ""):
    """Klien OpenAI diarahkan ke endpoint penyedia (dibuat sekali per penyedia).

    provider="" atau "nvidia" -> integrate.api.nvidia.com (NVIDIA_API_KEY);
    provider="openrouter"     -> openrouter.ai/api/v1 (OPENROUTER_API_KEY);
    provider="opencode"       -> CADANGAN HTTP opencode.ai/zen/v1 (jalur
                                 utama opencode/* lewat CLI `opencode run`
                                 di stream_completion; klien ini hanya
                                 dipakai bila CLI absen tapi OPENCODE_API_KEY
                                 ada — akses anonim HTTP ditutup 2026-09-21).
    """
    p = provider if provider in ("nvidia", "openrouter", "opencode") else "nvidia"
    client = _clients.get(p)
    if client is None:
        if p == "openrouter":
            config.require_api_key("openrouter")
            client = _oa().OpenAI(
                base_url=config.OPENROUTER_BASE_URL,
                api_key=config.OPENROUTER_API_KEY,
                timeout=config.REQUEST_TIMEOUT,
                max_retries=0,  # tidak mengirim ulang permintaan otomatis
                default_headers=_OPENROUTER_HEADERS,
            )
        elif p == "opencode":
            # CADANGAN HTTP (bukan jalur utama — lihat stream_completion):
            # dipakai hanya bila binary `opencode` hilang tapi OPENCODE_API_KEY
            # ada. Akses ANONIM HTTP sudah ditutup penyedia (TERUKUR
            # 2026-09-21: HTTP 403 FreeTierError), jadi jalur ini butuh key
            # sungguhan. Dummy key + _headers_tanpa_auth di bawah hanya
            # memuaskan syarat minimum SDK openai saat key kosong.
            config.require_api_key("opencode")
            client = _oa().OpenAI(
                base_url=config.OPENCODE_BASE_URL,
                api_key=config.OPENCODE_API_KEY or "tanpa-key",
                timeout=config.REQUEST_TIMEOUT,
                max_retries=0,
            )
        else:
            config.require_api_key("nvidia")
            client = _oa().OpenAI(
                base_url=config.NVIDIA_BASE_URL,
                api_key=config.NVIDIA_API_KEY,
                timeout=config.REQUEST_TIMEOUT,
                max_retries=0,  # tidak mengirim ulang permintaan otomatis
            )
        _clients[p] = client
    return client


def _base_kwargs(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str | None,
    temperature: float | None,
    extra_body: dict[str, Any] | None,
    stream: bool,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        # Bawaannya NVIDIA_DEFAULT_MODEL, BUKAN config.CHAT_MODEL: CHAT_MODEL
        # berisi ID internal bagas-ai ("nvidia/nemotron") yang tak dikenal
        # server. Pemanggil normal selalu mengirim ModelSpec.api_model.
        "model": model or config.NVIDIA_DEFAULT_MODEL,
        "messages": messages,
        "temperature": (
            temperature if temperature is not None else config.TEMPERATURE
        ),
        "top_p": config.TOP_P,
        "stream": stream,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if extra_body:
        kwargs["extra_body"] = extra_body
    if stream:
        kwargs["stream_options"] = {"include_usage": True}
    return kwargs


# chat_completion() NON-STREAM DIHAPUS: tak punya satu pun pemanggil sejak
# seluruh giliran memakai stream_completion (token realtime + reasoning),
# dan salinan logika retry/konteks-penuhnya cuma menjadi tempat drift.
# Hidupkan kembali dari riwayat git bila suatu saat benar-benar diperlukan.


# _pasang_watchdog() DIHAPUS (2026-09-23). Dulu thread daemon ini menutup
# stream yang diam >STREAM_STALL_TIMEOUT (45 dtk) SESUDAH token pertama, dan
# penutupan itu sengaja diterjemahkan jadi error "sementara" supaya ikut jalur
# retry biasa. Niatnya melindungi dari server menggantung; akibatnya model yang
# sekadar butuh jeda panjang di tengah jawaban (menyusun tabel, menimbang
# tool berikutnya, atau bernalar tanpa mengirim delta) DIBATALKAN lalu diminta
# ulang dari nol. Penjaganya sekarang cuma batas baca httpx (read=TTFT_TIMEOUT)
# yang berlaku per operasi baca — jauh lebih longgar, dan kegagalannya pun tak
# lagi memicu pengulangan karena jawabannya sudah mengalir. Jangan pasang
# penjaga "diam N detik" lagi di sini: jeda panjang BUKAN bukti stream mati.


def _kaitan_terima_detail(cb: Callable[..., None]) -> bool:
    """True bila `cb(nama, detail)` diterima; False bila hanya `cb(nama)`.

    Dipakai jalur OpenCode: kaitan lama 1-argumen tetap hidup, tapi TypeError
    dari DALAM badan kaitan tidak boleh memicu panggilan ulang (efek samping
    ganda). Signature gagal diukur -> anggap 2-argumen (tanpa fallback).
    """
    try:
        from inspect import Parameter, signature
        params = signature(cb).parameters
    except (TypeError, ValueError):
        return True
    if any(p.kind == Parameter.VAR_POSITIONAL for p in params.values()):
        return True
    n_pos = sum(
        1 for p in params.values()
        if p.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
    )
    return n_pos >= 2


def _prompt_dari_messages(messages: list[dict[str, Any]]) -> str:
    """Ratakan riwayat chat bagas-ai jadi SATU string prompt untuk CLI.

    `opencode run` menerima prompt tunggal (argv / stdin), bukan daftar pesan
    OpenAI. Peran dilabeli supaya batas giliran tetap terbaca model:
      [SYSTEM] / [ASSISTANT] / [USER] / [TOOL <nama>]. Lampiran multimodal
    dilepas diam-diam — jalur CLI ini jalur teks.
    """
    blok: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        konten = msg.get("content")
        if isinstance(konten, list):
            teks = [
                bag.get("text") or "" for bag in konten
                if isinstance(bag, dict) and bag.get("type") == "text"
            ]
            konten = "\n".join(teks)
        konten = (konten or "").strip()
        if not konten and role != "assistant":
            continue
        if role == "system":
            label = "SYSTEM"
        elif role == "assistant":
            label = "ASSISTANT"
        elif role == "tool":
            label = f"TOOL {msg.get('name') or msg.get('tool_call_id') or ''}".strip()
        else:
            label = "USER"
        blok.append(f"[{label}]\n{konten}".rstrip())
    return "\n\n".join(blok)


class _UsageCLI:
    """Usage ala Chat Completions yang dibaca TokenUsage.add — dari event
    step_finish CLI OpenCode.

    TERUKUR 2026-09-23: field ada di `part.tokens` (bukan root event):
    {"total","input","output","reasoning","cache":{"write","read"}} dan
    cost di `part.cost`. cache.read = bagian prompt yang terkena cache.
    """

    def __init__(self, step: dict[str, Any]) -> None:
        part = step.get("part") if isinstance(step.get("part"), dict) else {}
        src = part or step
        tok = src.get("tokens") or step.get("tokens") or {}
        self.prompt_tokens = int(tok.get("input") or 0)
        self.completion_tokens = int(tok.get("output") or 0)
        try:
            self.cost = float(src.get("cost") or step.get("cost") or 0)
        except (TypeError, ValueError):
            self.cost = 0.0

        class _Det:
            pass

        cache = tok.get("cache")
        if isinstance(cache, dict):
            cache = cache.get("read") or 0
        elif not isinstance(cache, (int, float)):
            cache = tok.get("cache_read") or 0
        reason = tok.get("reasoning") or 0
        pdet = _Det()
        pdet.cached_tokens = int(cache or 0)
        self.prompt_tokens_details = pdet
        cdet = _Det()
        cdet.reasoning_tokens = int(reason or 0)
        self.completion_tokens_details = cdet


def _stream_opencode_cli(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    on_content: Any = None,
    on_tool_pending: Callable[..., None] | None = None,
    on_reasoning: Any = None,
    cancel_event: Any = None,
) -> tuple[str, list[dict[str, Any]], Any]:
    """Streaming lewat subprocess `opencode run --format json` (tanpa key).

    OpenCode MENGENDALIKAN loop agennya sendiri (tool dipanggil & dieksekusi
    di sana dengan `--auto`), jadi fungsi ini:
      * mengirim riwayat sebagai prompt stdin (argv Windows ~32k — stdin aman);
      * menyiarkan potongan teks dari event `text` via on_content;
      * menyalakan on_tool_pending saat event `tool_use` (umpan balik live);
      * menutup dengan teks akhir + tool_calls KOSONG — loop tool bagas-ai
        selesai satu giliran (hasil tool sudah diproses di dalam CLI).
    `tools` dari pemanggil DIABAIKAN: OpenCode memakai tool bawaannya.
    """
    exe = shutil.which("opencode")
    if not exe:
        raise ProviderBusyError(
            "Binary `opencode` tidak ditemukan di PATH. Install dulu "
            "(npm i -g opencode-ai), atau isi OPENCODE_API_KEY untuk "
            "jalur cadangan HTTP."
        )
    prompt = _prompt_dari_messages(messages)
    if not prompt:
        raise EmptyResponseError("Prompt kosong untuk CLI OpenCode.")
    api_model = (model or "").strip() or "opencode/big-pickle"
    cmd = [exe, "run", "-m", api_model, "--format", "json", "--auto"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(  # noqa: S603 — argumen di-build lokal, bukan shell
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags if subprocess.CREATE_NO_WINDOW else 0,
            cwd=None,
        )
    except OSError as exc:
        raise ProviderBusyError(
            f"Gagal menjalankan CLI OpenCode: {exc}"
        ) from exc

    antre: queue.Queue[Any] = queue.Queue()
    selesai = threading.Event()

    def _baca() -> None:
        try:
            assert proc.stdout is not None
            for baris in proc.stdout:
                antre.put(baris)
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001 — timeout menunggu: biarkan
                pass
            sisa = ""
            if proc.stderr is not None:
                sisa = proc.stderr.read() or ""
            if sisa.strip():
                antre.put(("__stderr__", sisa.strip()))
        except Exception as exc:  # noqa: BLE001
            antre.put(exc)
        finally:
            selesai.set()
            antre.put(None)

    def _matikan() -> None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(target=_baca, daemon=True, name="opencode-cli")
    thread.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        _matikan()
        raise ProviderBusyError("CLI OpenCode menutup stdin lebih dulu.") from None

    potongan: list[str] = []
    usage: Any = None
    tool_pending_seen = False
    teks_part: dict[str, int] = {}  # id part -> panjang teks yang sudah dikirim
    last_exc: Exception | None = None
    stderr_teks = ""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _matikan()
            raise Cancelled()
        try:
            item = antre.get(timeout=0.25)
        except queue.Empty:
            if selesai.is_set() and antre.empty():
                break
            continue
        if item is None:
            break
        if isinstance(item, Exception):
            last_exc = item
            break
        if isinstance(item, tuple) and item and item[0] == "__stderr__":
            stderr_teks = str(item[1])
            continue
        baris = str(item).strip()
        if not baris:
            continue
        try:
            ev = _json.loads(baris)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        # Beberapa build membalas {type:"error"/"message", error:...}
        if ev.get("type") in ("error", "message_error") or "error" in ev and (
            ev.get("type") in ("error",)
        ):
            err = ev.get("error") or ev.get("message") or ev
            last_exc = ProviderBusyError(
                f"OpenCode CLI: {err if isinstance(err, str) else _json.dumps(err)}"
            )
            continue
        tipe = str(ev.get("type") or "")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if tipe == "text":
            pid = str(part.get("id") or id(ev))
            teks = str(part.get("text") or "")
            sudah = teks_part.get(pid, 0)
            if len(teks) > sudah:
                delta = teks[sudah:]
                teks_part[pid] = len(teks)
                potongan.append(delta)
                if on_content is not None:
                    try:
                        on_content(delta)
                    except Exception:  # noqa: BLE001 — UI boleh gagal sendiri
                        pass
        elif tipe == "reasoning":
            pid = str(part.get("id") or id(ev))
            teks = str(part.get("text") or "")
            sudah = teks_part.get(pid, 0)
            if len(teks) > sudah:
                delta = teks[sudah:]
                teks_part[pid] = len(teks)
                if on_reasoning is not None:
                    try:
                        on_reasoning(delta)
                    except Exception:  # noqa: BLE001
                        pass
        elif tipe == "tool_use":
            tool_pending_seen = True
            part_state = part.get("state") if isinstance(part.get("state"), dict) else {}
            nama = str(
                part.get("tool") or part.get("name")
                or ev.get("tool") or ev.get("name") or "tool"
            )
            # Event tool_use OpenCode membawa SELURUH jejak: input, output,
            # metadata (filepath, diff, exists). Teruskan ke UI supaya TUI
            # bisa menggambar diff/write-block seperti jalur API — tanpa ini
            # jawaban OpenCode hanya baris status "Menerima instruksi tool".
            detail = {
                "tool": nama,
                "status": str(part_state.get("status") or ""),
                "input": (
                    part_state.get("input")
                    if isinstance(part_state.get("input"), dict) else {}
                ),
                "output": str(part_state.get("output") or ""),
                "metadata": (
                    part_state.get("metadata")
                    if isinstance(part_state.get("metadata"), dict) else {}
                ),
                "title": str(part_state.get("title") or ""),
            }
            if on_tool_pending is not None:
                # Panggil dengan detail bila kaitan menerimanya. Fallback ke
                # 1-argumen HANYA bila signature tak menerima detail —
                # TypeError dari DALAM badan kaitan tidak boleh mengulang
                # panggilan (efek samping ganda: status/on_tool dobel).
                try:
                    on_tool_pending(nama, detail)
                except TypeError:
                    if not _kaitan_terima_detail(on_tool_pending):
                        try:
                            on_tool_pending(nama)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001 — UI boleh gagal sendiri
                    pass
                # Narasi "✓ tool" via on_content DIHAPUS: ia menyisipkan kode
                # inline ke dalam aliran jawaban (merusak teks akhir). Jejak
                # tool sudah digambar UI lewat on_tool + on_tool_result.
        elif tipe == "step_finish":
            usage = _UsageCLI(ev)

    if cancel_event is not None and cancel_event.is_set():
        _matikan()
        raise Cancelled()
    if last_exc is not None and not potongan:
        raise last_exc

    content = "".join(potongan)
    # OpenCode sudah mengeksekusi tool-nya; teks yang berisi penanda tool
    # mentah (jarang, tapi bisa bocor dari provider) dibersihkan seperti
    # jalur HTTP — tanpa mengubahnya jadi tool_calls bagas-ai.
    if content and _HAS_TOOLTEXT.search(content):
        # Ganti penanda dengan SPASI (bukan ""): membuang blok yang
        # menempel di antara dua kata tanpa spasi menyatukan kata-kata
        # itu ("selesai.edit_file()menjadi" -> bug "kadang gak ada spasi").
        cleaned = _re.sub(r"<tool_call>.*?</tool_call>", " ", content,
                          flags=_re.DOTALL | _re.IGNORECASE)
        cleaned = _re.sub(r"<function\s*=.*?</function>", " ", cleaned,
                          flags=_re.DOTALL | _re.IGNORECASE)
        cleaned = _re.sub(r"<tool_call>.*$", " ", cleaned,
                          flags=_re.DOTALL | _re.IGNORECASE)
        cleaned = _re.sub(r"<function\s*=.*$", " ", cleaned,
                          flags=_re.DOTALL | _re.IGNORECASE)
        # Jangan jadikan tool_calls: loop agent di CLI sudah selesai.
        content = _re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if not content and not tool_pending_seen:
        detail = stderr_teks.strip().splitlines()
        pesan = detail[-1] if detail else "respons CLI kosong"
        raise EmptyResponseError(f"OpenCode CLI tidak menjawab ({pesan}).")
    return content, [], usage


def stream_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    extra_body: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    provider: str = "",
    api_style: str = "chat",
    on_content: Any = None,
    on_tool_pending: Callable[..., None] | None = None,
    on_reasoning: Any = None,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> tuple[str, list[dict[str, Any]], Any]:
    """Streaming chat completion tanpa pengiriman ulang otomatis.

    Memanggil `on_content(teks)` tiap potongan jawaban tiba (token realtime),
    `on_reasoning(teks)` untuk potongan "pikiran" bila disediakan, dan memeriksa
    `cancel_event` tiap chunk supaya responsif saat dibatalkan. Error koneksi
    atau throttle dilaporkan langsung tanpa mengirim ulang prompt.

    provider="opencode" -> SUBPROSES CLI `opencode run` (jalur utama; lihat
    _stream_opencode_cli). `api_style="chat"` (bawaan) -> /chat/completions;
    `api_style="responses"` -> /responses (cadangan HTTP untuk model Zen yang
    hanya dilayani di sana, mis. muse-spark-1.2-contributor-free).

    Mengembalikan (teks_final, daftar_tool_calls, usage).
    """
    if provider == "opencode" and config.opencode_cli_tersedia():
        # Cabang PALING ATAS: CLI menang atas HTTP (kunci OPENCODE_API_KEY
        # opsional), sebab akses anonim HTTP Zen sudah ditutup penyedia.
        # tools/temperature/extra_body/max_tokens diabaikan — protokolnya
        # milik `opencode run`, bukan Chat Completions.
        return _stream_opencode_cli(
            messages, model=model, on_content=on_content,
            on_tool_pending=on_tool_pending, on_reasoning=on_reasoning,
            cancel_event=cancel_event,
        )
    if provider == "openrouter" and model and model.endswith(":free"):
        extra_body = dict(extra_body or {})
        routing = dict(extra_body.get("provider") or {})
        routing["max_price"] = {"prompt": 0, "completion": 0, "request": 0, "image": 0}
        extra_body["provider"] = routing
    if api_style == "responses":
        return _stream_responses(
            messages, tools=tools, model=model, temperature=temperature,
            extra_body=extra_body, max_tokens=max_tokens, provider=provider,
            on_content=on_content, on_reasoning=on_reasoning,
            on_tool_pending=on_tool_pending,
            cancel_event=cancel_event, on_retry=on_retry)
    client = get_client(provider)
    kwargs = _base_kwargs(messages, tools, model, temperature, extra_body,
                          True, max_tokens)
    _noauth = _headers_tanpa_auth(provider)
    if _noauth:
        kwargs["extra_headers"] = _noauth
    try:
        import httpx  # dependensi openai — pasti ada sesudah get_client()
        kwargs["timeout"] = httpx.Timeout(
            connect=15.0, read=_read_timeout(provider), write=60.0, pool=15.0
        )
    except Exception:  # noqa: BLE001 — tanpa httpx pun tetap jalan (timeout klien)
        pass

    # Dibuat DI LUAR _do supaya penanda "sudah mengalir" bertahan antar
    # percobaan retry busy — tanpa ini tiap retry mengira stream masih kosong
    # lalu mengulang jawaban yang sudah tampil di layar.
    mengalir = {"ada": False}

    def _do() -> tuple[str, list[dict[str, Any]], Any]:
        try:
            stream = _stream_cancellable(
                lambda: client.chat.completions.create(**kwargs), cancel_event)
        except Exception as exc:  # noqa: BLE001
            if _apakah_konteks_penuh(exc):
                raise KonteksPenuh(str(exc)) from exc
            raise
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        usage = None
        finish_reason = None
        try:
            try:
                for chunk in stream:
                    if cancel_event is not None and cancel_event.is_set():
                        raise Cancelled()
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if not getattr(chunk, "choices", None):
                        continue
                    choice = chunk.choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    piece = getattr(delta, "content", None)
                    if piece:
                        mengalir["ada"] = True
                        content_parts.append(piece)
                        if on_content:
                            on_content(piece)
                    # Model bernalar mengalirkan "pikiran" di field terpisah
                    # (`reasoning_content` di nemotron/muse, `reasoning` di
                    # deepseek & OpenRouter). Ditangkap supaya TIDAK hilang:
                    # jadi jawaban cadangan bila `content` akhirnya kosong,
                    # sekaligus bukti bagi UI bahwa modelnya bekerja, bukan
                    # menggantung.
                    rpiece = (getattr(delta, "reasoning_content", None)
                              or getattr(delta, "reasoning", None))
                    if rpiece:
                        mengalir["ada"] = True
                        reasoning_parts.append(rpiece)
                        if on_reasoning:
                            on_reasoning(rpiece)
                        elif on_content:
                            on_content(rpiece)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        mengalir["ada"] = True
                        # DIKUNCI PER tc.index, dan itu bukan kerapian: satu
                        # giliran bisa memuat beberapa panggilan tool BERNAMA
                        # SAMA (TERUKUR: `bagi` di index 0 dan index 1).
                        # Menggabung tanpa index menghasilkan nama "bagibagi" —
                        # tool yang tak pernah ada, jadi seluruh giliran gagal.
                        slot = tool_slots.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn and fn.name:
                            slot["name"] += fn.name
                            if on_tool_pending:
                                on_tool_pending(slot["name"])
                        if fn and fn.arguments:
                            slot["arguments"] += fn.arguments
                    # Gateway bisa membiarkan SSE terbuka setelah pilihan selesai.
                    # Eksekusi tidak perlu menunggu socket ditutup/read timeout.
                    if finish_reason is not None:
                        break
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                # Provider kadang mengirim error 400/413 sebagai CHUNK PERTAMA
                # (bukan saat create()) — tanpa ini, "konteks penuh" jatuh ke
                # jalur generik dan pemulih pemangkasannya tak pernah jalan.
                if _apakah_konteks_penuh(exc):
                    raise KonteksPenuh(str(exc)) from exc
                explained = _jelaskan_timeout(exc, provider)
                if explained is not exc:
                    raise explained from exc
                raise
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = [tool_slots[i] for i in sorted(tool_slots)]
        # Penyelamatan: model menuliskan panggilan tool sebagai TEKS/XML alih-alih
        # function-calling asli. Bila tak ada tool_calls asli tapi kontennya
        # memuat pola `<tool_call>`/`<function=...>`, parse & jadikan tool_calls
        # sungguhan supaya benar-benar dieksekusi.
        if not tool_calls and content and _HAS_TOOLTEXT.search(content):
            parsed = _extract_text_tool_calls(content)
            if parsed:
                tool_calls = parsed
            # Buang blok XML tool dari konten supaya tak bocor ke layar — baik
            # yang sudah dieksekusi maupun yang TERPOTONG/gagal-parse. Panggilan
            # tool setengah jadi tak boleh tampil sebagai "jawaban". Spasi
            # pengganti (bukan "") mencegah dua kata menempel setelah blok dibuang.
            cleaned = _re.sub(r"<tool_call>.*?</tool_call>", " ", content,
                              flags=_re.DOTALL | _re.IGNORECASE)
            cleaned = _re.sub(r"<function\s*=.*?</function>", " ", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            # Sisa penanda tak berpasangan (terpotong) -> potong dari situ.
            cleaned = _re.sub(r"<tool_call>.*$", " ", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            cleaned = _re.sub(r"<function\s*=.*$", " ", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            content = _re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        # Model hanya "berpikir" tanpa jawaban akhir (mis. anggaran thinking
        # habis): pakai isi pikirannya supaya pengguna TETAP dapat respons,
        # bukan layar kosong.
        if not content and reasoning and not tool_calls:
            content = reasoning.strip()
        if not content and not tool_calls:
            # Benar-benar tak ada apa pun. Tanpa `finish_reason`, ini khas body
            # kosong saat throttle -> perlakukan sementara supaya diulang. Bila
            # ADA finish_reason (model memang berhenti), jangan spam retry:
            # kembalikan kosong, biar core yang memberi pesan cadangan.
            if finish_reason is None:
                raise EmptyResponseError(
                    "Stream kosong (kemungkinan rate limit 40 RPM)."
                )
        return content, tool_calls, usage

    return _call_with_retry(
        _do, cancel_event=cancel_event, on_retry=on_retry,
        sudah_mengalir=lambda: mengalir["ada"],
        provider=provider,
    )


# --- gaya Responses API (OpenCode Zen /responses) -----------------------------
#
# Sebagian model OpenCode Zen (mis. muse-spark-1.2-contributor-free) HANYA
# dilayani di endpoint /responses — TERUKUR 2026-08-29: /chat/completions
# membalas "Internal server error" untuk model itu, sedangkan /responses
# menjawab normal. Protokolnya beda keluarga (OpenAI Responses API, bukan
# Chat Completions), tapi gateway Zen MENERIMA peran chat ("system"/"user"/
# "assistant") di `input` — TERUKUR — jadi pesan tinggal dialihkan bentuknya:
#   - pesan polos      -> {"role": ..., "content": ...}
#   - tool_calls asis. -> satu item {"type":"function_call", call_id/name/args}
#   - hasil tool       -> {"type":"function_call_output", call_id, output}
# Definisi tool juga diratakan: {type, function:{name,...}} gaya chat DITOLAK
# ("missing required field `name`" — TERUKUR), bentuk resminya {type, name,
# description, parameters}.

_API_ROLES = frozenset(("system", "user", "assistant", "tool"))


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pesan gaya chat -> daftar item input Responses API."""
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role not in _API_ROLES:
            # Memory juga memuat record `diff` untuk tampilan/replay. Itu
            # sengaja tidak punya padanan role di Responses API.
            continue
        # Lampiran multimodal (image_url/video_url) tak didukung jalur ini:
        # di Zen hanya model /chat/completions yang menerima media. Di sini
        # medianya dilepas diam-diam — teksnya tetap dikirim.
        konten = msg.get("content")
        if isinstance(konten, list):
            teks = []
            for bag in konten:
                if isinstance(bag, dict) and bag.get("type") == "text":
                    teks.append(bag.get("text") or "")
            konten = "\n".join(teks)
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("tool_call_id") or ""),
                "output": str(konten or ""),
            })
            continue
        if role == "assistant" and msg.get("tool_calls"):
            # Satu pesan assistant bisa memuat BEBERAPA panggilan: di Responses
            # tiap panggilan adalah item sendiri. `call_id` (bukan `id`) adalah
            # kunci yang dikirim balik oleh function_call_output.
            for tc in msg["tool_calls"]:
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "arguments": str(fn.get("arguments") or ""),
                })
            if konten:
                items.append({"role": "assistant", "content": str(konten)})
            continue
        items.append({"role": str(role or "user"), "content": str(konten or "")})
    return items


def _responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Definisi tool gaya chat -> bentuk datar Responses API."""
    out: list[dict[str, Any]] = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue  # tool non-function tak ada padanannya di jalur ini
        out.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters")
            or {"type": "object", "properties": {}},
        })
    return out


class _UsageResponses:
    """Jembatan usage Responses -> atribut gaya chat yang dibaca TokenUsage.

    Nama medan utamanya memang beda (input/output vs prompt/completion), tapi
    detailnya kebetulan sama nama (cached_tokens, reasoning_tokens) — cukup
    dua alias + usaha kecil untuk `cost` (Zen melaporkannya sebagai STRING).
    """

    def __init__(self, u: Any) -> None:
        self.prompt_tokens = getattr(u, "input_tokens", 0) or 0
        self.completion_tokens = getattr(u, "output_tokens", 0) or 0
        try:
            self.cost = float(getattr(u, "cost", 0) or 0)
        except (TypeError, ValueError):
            self.cost = 0.0
        self.prompt_tokens_details = getattr(u, "input_tokens_details", None)
        self.completion_tokens_details = getattr(u, "output_tokens_details", None)


def _stream_responses(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    extra_body: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    provider: str = "",
    on_content: Any = None,
    on_tool_pending: Callable[..., None] | None = None,
    on_reasoning: Any = None,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> tuple[str, list[dict[str, Any]], Any]:
    """Streaming via /responses — protokol Responses API (lihat catatan blok).

    Sama semangatnya dengan stream_completion: token realtime lewat
    on_content/on_reasoning, panggilan tool diakumulasi per-index, dan hasil
    akhirnya (teks + tool_calls + usage) sama bentuknya dengan jalur chat
    supaya core tak perlu tahu bedanya."""
    client = get_client(provider)
    kwargs: dict[str, Any] = {
        "model": model or config.NVIDIA_DEFAULT_MODEL,
        "input": _responses_input(messages),
        "stream": True,
        "temperature": (
            temperature if temperature is not None else config.TEMPERATURE
        ),
        "top_p": config.TOP_P,
    }
    if max_tokens:
        # Responses menyebutnya max_output_tokens, bukan max_tokens.
        kwargs["max_output_tokens"] = max_tokens
    rt = _responses_tools(tools)
    if rt:
        kwargs["tools"] = rt
        kwargs["tool_choice"] = "auto"
    if extra_body:
        kwargs["extra_body"] = extra_body
    _noauth = _headers_tanpa_auth(provider)
    if _noauth:
        kwargs["extra_headers"] = _noauth
    try:
        import httpx  # dependensi openai — pasti ada sesudah get_client()
        kwargs["timeout"] = httpx.Timeout(
            connect=15.0, read=_read_timeout(provider), write=60.0, pool=15.0
        )
    except Exception:  # noqa: BLE001 — tanpa httpx pun tetap jalan
        pass

    # DI LUAR _do: penanda antar percobaan retry busy (lihat jalur chat).
    mengalir = {"ada": False}

    def _do() -> tuple[str, list[dict[str, Any]], Any]:
        try:
            stream = _stream_cancellable(
                lambda: client.responses.create(**kwargs), cancel_event)
        except Exception as exc:  # noqa: BLE001
            if _apakah_konteks_penuh(exc):
                raise KonteksPenuh(str(exc)) from exc
            raise
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Kunci = output_index item function_call: satu respons bisa memuat
        # beberapa panggilan, dan argumennya mengalir per-potongan.
        tool_slots: dict[int, dict[str, str]] = {}
        usage = None
        finish_reason = None
        try:
            try:
                for ev in stream:
                    if cancel_event is not None and cancel_event.is_set():
                        raise Cancelled()
                    tipe = getattr(ev, "type", "")
                    if tipe == "response.output_text.delta":
                        piece = getattr(ev, "delta", None)
                        if piece:
                            mengalir["ada"] = True
                            content_parts.append(piece)
                            if on_content:
                                on_content(piece)
                    elif tipe in ("response.reasoning_text.delta",
                                  "response.reasoning_summary_text.delta"):
                        piece = getattr(ev, "delta", None)
                        if piece:
                            mengalir["ada"] = True
                            reasoning_parts.append(piece)
                            if on_reasoning:
                                on_reasoning(piece)
                            elif on_content:
                                on_content(piece)
                    elif tipe == "response.output_item.added":
                        item = getattr(ev, "item", None)
                        if getattr(item, "type", "") == "function_call":
                            mengalir["ada"] = True
                            tool_slots[getattr(ev, "output_index",
                                               len(tool_slots))] = {
                                "id": str(getattr(item, "call_id", "") or ""),
                                "name": str(getattr(item, "name", "") or ""),
                                "arguments": "",
                            }
                            if on_tool_pending:
                                on_tool_pending(str(getattr(item, "name", "") or ""))
                    elif tipe == "response.function_call_arguments.delta":
                        idx = getattr(ev, "output_index", None)
                        slot = tool_slots.get(idx)
                        piece = getattr(ev, "delta", None)
                        if slot is not None and piece:
                            slot["arguments"] += piece
                    elif tipe in ("response.completed",
                                  "response.incomplete"):
                        resp = getattr(ev, "response", None)
                        if getattr(resp, "usage", None):
                            usage = _UsageResponses(resp.usage)
                        finish_reason = getattr(resp, "status", "completed")
                        break
                    elif tipe == "response.failed":
                        resp = getattr(ev, "response", None)
                        err = getattr(resp, "error", None)
                        raise Exception(
                            getattr(err, "message", None)
                            or f"respons gagal: {getattr(resp, 'status', '?')}")
                    elif tipe == "error":
                        raise Exception(str(getattr(ev, "message", "")
                                            or getattr(ev, "code", "error")))
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if _apakah_konteks_penuh(exc):
                    raise KonteksPenuh(str(exc)) from exc
                explained = _jelaskan_timeout(exc, provider)
                if explained is not exc:
                    raise explained from exc
                raise
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = [tool_slots[i] for i in sorted(tool_slots)]
        # Penyelamatan yang sama dengan jalur chat: model menuliskan panggilan
        # tool sebagai teks/XML alih-alih function-calling asli.
        if not tool_calls and content and _HAS_TOOLTEXT.search(content):
            parsed = _extract_text_tool_calls(content)
            if parsed:
                tool_calls = parsed
            cleaned = _re.sub(r"<tool_call>.*?</tool_call>", " ", content,
                              flags=_re.DOTALL | _re.IGNORECASE)
            cleaned = _re.sub(r"<function\s*=.*?</function>", " ", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            cleaned = _re.sub(r"<tool_call>.*$", " ", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            cleaned = _re.sub(r"<function\s*=.*$", " ", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            content = _re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if not content and reasoning and not tool_calls:
            content = reasoning.strip()
        if not content and not tool_calls:
            if finish_reason is None:
                raise EmptyResponseError(
                    "Stream /responses kosong (kemungkinan rate limit).")
        return content, tool_calls, usage

    return _call_with_retry(
        _do, cancel_event=cancel_event, on_retry=on_retry,
        sudah_mengalir=lambda: mengalir["ada"],
        provider=provider,
    )

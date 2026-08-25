"""Klien endpoint API OpenAI-compatible (NVIDIA & OpenRouter) + retry
tahan-banting.

Modul ini melayani jalur model bagas-ai yang berbasis API: model `nvidia/*`
(integrate.api.nvidia.com) dan `openrouter/*` (openrouter.ai/api/v1, kunci
OPENROUTER_API_KEY). Jalur lainnya (model `web/*`) tak menyentuh berkas ini
sama sekali — ia lewat agent/connectors + Agent._run_connector, dan padanan
penanganan "sementara" di sana berbentuk lain sesuai medianya (WebBusyError
untuk server penuh, WebLimitError untuk kuota situs habis).

Poin penting jalur API: free tier NVIDIA (~40 request/menit) membalas throttle
dengan bentuk yang BERMACAM-MACAM — bukan cuma HTTP 429/RateLimitError, tapi
juga pesan seperti "worker local total request limit reached", body KOSONG
dengan HTTP 200, atau 5xx sesaat. Semuanya ditahan di sini: tunggu lalu ulangi
langkah yang sama, supaya tugas pengguna berlanjut alih-alih dibatalkan.
"""
from __future__ import annotations

import json as _json
import re as _re
import threading
import time
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


class StreamStalled(Exception):
    """Stream MACET berulang (tak ada data melewati batas) dan sudah dicoba
    ulang beberapa kali tanpa hasil.

    Dilempar ke core supaya bisa menaikkan effort lalu mengulang dengan konteks
    yang sama — bukan menggantung selamanya di panggilan yang tak bergerak.
    """


class _StallTimeout(Exception):
    """Internal: watchdog antar-token menutup stream yang berhenti bergerak.

    Kelas sendiri, bukan menumpang pesan galat httpx: `_is_timeout` harus bisa
    mengenalinya dengan PASTI. Saat socket ditutup dari thread lain, galat yang
    muncul dari pembacaan bisa berupa apa saja (ReadError, StreamClosed,
    RuntimeError, dsb) — mencocokkan teksnya berarti watchdog kadang dianggap
    kegagalan fatal lalu tak pernah diulang.
    """


# Kata kunci pada PESAN error yang menandakan kondisi SEMENTARA (throttle /
# kapasitas / gangguan sesaat). NVIDIA sering mengirim "worker local total
# request limit reached" dengan kode status tak terduga, jadi klasifikasinya
# juga lewat isi pesan, bukan cuma tipe/kode.
_TRANSIENT_KEYWORDS = (
    "request limit", "rate limit", "too many request", "limit reached",
    "overloaded", "capacity", "try again", "temporarily", "unavailable",
    "timeout", "timed out", "connection", "throttl", "429", "server error",
    "bad gateway", "gateway timeout", "worker", "quota", "busy",
)
# Kode status FATAL (percuma diulang): permintaan salah / auth / model tak ada.
# Selain ini, 5xx dianggap sementara.
_FATAL_STATUS = {400, 401, 403, 404, 405, 422}

# Kata kunci pada pesan HTTP 400 yang berarti RIWAYAT MELEWATI JENDELA
# KONTEKS model — bukan permintaan salah. Dibedakan supaya core bisa BERTINDAK
# (pangkas riwayat lalu ulangi) alih-alih melaporkan kegagalan misterius.
_KATA_KONTEKS_PENUH = (
    "context length", "context window", "maximum context", "context_limit",
    "too many tokens", "token limit", "exceeds the maximum",
    "input length", "input tokens exceed", "prompt is too long",
    "reduce the length", "percih panjang", "payload too large",
    "request too large", "too large",
)


class KonteksPenuh(Exception):
    """Permintaan ditolak karena riwayat melebihi jendela konteks model.

    FATAL untuk retry biasa (mengulang payload yang sama pasti ditolak lagi)
    — tapi SEMBUH oleh core: riwayat dipangkas lalu giliran dilanjutkan.
    Dibedakan dari BadRequestError mentah supaya pesan ke pengguna bukan
    sekadar "400 Bad Request" tanpa jalan keluar."""

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
        # pemulihannya bukan retry, melainkan pangkas riwayat (di core).
        return False
    if isinstance(exc, (EmptyResponseError, _StallTimeout)):
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


def _is_timeout(exc: Exception) -> bool:
    """True bila error bertipe MACET (stream berhenti mengirim data)."""
    if isinstance(exc, _StallTimeout):
        return True
    o = _openai
    if o is not None and isinstance(exc, (o.APITimeoutError, o.APIConnectionError)):
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    return "timed out" in msg or "timeout" in msg


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
    calls: list[tuple[str, dict]] = []
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
            calls.append((name, args))
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
            if isinstance(a, str):
                try:
                    a = _json.loads(a)
                except ValueError:
                    a = {}
            if name:
                calls.append((name, a if isinstance(a, dict) else {}))
    out: list[dict[str, str]] = []
    for i, (name, args) in enumerate(calls):
        out.append({
            "id": f"txt_{i}",
            "name": name,
            "arguments": _json.dumps(args, ensure_ascii=False),
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


def _call_with_retry(
    do: Callable[[], Any],
    *,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    stall_escape: int | None = None,
) -> Any:
    """Jalankan `do()` dengan retry SABAR untuk error NVIDIA yang sementara.

    Backoff eksponensial (maks. 60 dtk/percobaan) sampai TOTAL tunggu melewati
    `config.RETRY_MAX_SECONDS`, baru menyerah. Tunggunya bisa dibatalkan. Saat
    akan mengulang, `on_retry(attempt, wait, exc)` dipanggil supaya UI bisa
    mengabari bahwa bagas-ai MENUNGGU lalu melanjutkan — bukan gagal.

    `stall_escape`: bila diberi N, error MACET (timeout/stream berhenti) yang
    terjadi N kali tak diulang lagi di sini melainkan dilempar sebagai
    StreamStalled, supaya core bisa mengubah setelan alih-alih menggantung
    berulang-ulang pada panggilan yang sama.
    """
    attempt = 0
    waited = 0.0
    delay = 3.0
    stalls = 0
    budget = config.RETRY_MAX_SECONDS
    while True:
        attempt += 1
        try:
            return do()
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if stall_escape is not None and _is_timeout(exc):
                stalls += 1
                if stalls >= stall_escape:
                    raise StreamStalled(
                        f"stream macet {stalls}x (tak ada data "
                        f">{config.STREAM_STALL_TIMEOUT:.0f} dtk)"
                    ) from exc
            if not _is_transient(exc) or waited >= budget:
                raise
            wait = min(delay, 60.0)
            delay *= 1.8
            waited += wait
            if on_retry:
                try:
                    on_retry(attempt, wait, exc)
                except Exception:  # noqa: BLE001 — UI gagal tak boleh membatalkan retry
                    pass
            _sleep_cancellable(wait, cancel_event)


# Satu klien PER PENYEDIA dipakai ulang di seluruh aplikasi.
_clients: dict[str, Any] = {}

# Header atribusi yang diminta OpenRouter (opsional tapi resmi): identitas
# aplikasi muncul di dashboard aktivitas openrouter.ai.
_OPENROUTER_HEADERS = {
    "HTTP-Referer": config.REPO_URL,
    "X-Title": config.APP_NAME,
}


def get_client(provider: str = ""):
    """Klien OpenAI diarahkan ke endpoint penyedia (dibuat sekali per penyedia).

    provider="" atau "nvidia" -> integrate.api.nvidia.com (NVIDIA_API_KEY);
    provider="openrouter"     -> openrouter.ai/api/v1 (OPENROUTER_API_KEY).
    """
    p = provider if provider in ("nvidia", "openrouter") else "nvidia"
    client = _clients.get(p)
    if client is None:
        if p == "openrouter":
            config.require_api_key("openrouter")
            client = _oa().OpenAI(
                base_url=config.OPENROUTER_BASE_URL,
                api_key=config.OPENROUTER_API_KEY,
                timeout=config.REQUEST_TIMEOUT,
                max_retries=0,  # retry ditangani _call_with_retry di atas
                default_headers=_OPENROUTER_HEADERS,
            )
        else:
            config.require_api_key("nvidia")
            client = _oa().OpenAI(
                base_url=config.NVIDIA_BASE_URL,
                api_key=config.NVIDIA_API_KEY,
                timeout=config.REQUEST_TIMEOUT,
                max_retries=0,  # retry ditangani _call_with_retry di atas
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


def _pasang_watchdog(stream: Any, state: dict[str, Any]) -> threading.Thread | None:
    """Jaga stream yang BERHENTI BERGERAK sesudah token pertama tiba.

    Kenapa perlu thread sendiri, bukan cukup timeout httpx: batas baca httpx
    berlaku untuk SATU operasi baca socket, jadi satu angka mengatur dua hal
    yang lamanya berbeda jauh — penantian token PERTAMA dan jeda ANTAR-token.
    TERUKUR 2026-08-23: deepseek-v4-flash butuh 106-169 dtk sampai kata
    pertama, sementara nemotron & muse-glimmer menjawab dalam hitungan detik.
    Klien lama memakai satu angka untuk keduanya (read=STREAM_STALL_TIMEOUT),
    sehingga permintaan yang sebenarnya SEHAT dibatalkan cuma karena modelnya
    lambat memulai. Menaikkan angka itu ke 300 dtk memperbaiki yang lambat
    memulai tapi MENGHILANGKAN perlindungan macet — stream yang menggantung di
    tengah jawaban jadi diam lima menit.

    Jadi keduanya dipisah: httpx menjaga TTFT (read=TTFT_TIMEOUT, sekaligus
    jaring terakhir bila thread ini gagal), dan thread ini menjaga jeda
    antar-token dengan MENUTUP stream begitu diam melewati batas. Penutupan itu
    membuat pembacaan di thread utama melempar galat, yang lalu diterjemahkan
    jadi _StallTimeout supaya ikut jalur retry biasa.
    """
    batas = config.STREAM_STALL_TIMEOUT
    if batas <= 0:
        return None

    def _jaga() -> None:
        while not state["selesai"]:
            time.sleep(0.5)
            if state["selesai"] or not state["mulai"]:
                continue  # belum ada token pertama -> itu wilayah TTFT, bukan macet
            if time.monotonic() - state["ts"] > batas:
                state["macet"] = True
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
                return

    t = threading.Thread(target=_jaga, name="bagasai-stall-watchdog", daemon=True)
    t.start()
    return t


def stream_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    extra_body: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    provider: str = "",
    on_content: Any = None,
    on_reasoning: Any = None,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> tuple[str, list[dict[str, Any]], Any]:
    """Streaming chat completion dengan retry tahan-banting.

    Memanggil `on_content(teks)` tiap potongan jawaban tiba (token realtime),
    `on_reasoning(teks)` untuk potongan "pikiran" bila disediakan, dan memeriksa
    `cancel_event` tiap chunk supaya responsif saat dibatalkan. Bila endpoint
    throttle di awal/tengah, seluruh panggilan diulang otomatis dengan backoff
    lewat `_call_with_retry`, dan `on_retry` mengabari UI.

    Mengembalikan (teks_final, daftar_tool_calls, usage).
    """
    client = get_client(provider)
    kwargs = _base_kwargs(messages, tools, model, temperature, extra_body,
                          True, max_tokens)
    try:
        import httpx  # dependensi openai — pasti ada sesudah get_client()
        kwargs["timeout"] = httpx.Timeout(
            connect=15.0, read=config.TTFT_TIMEOUT, write=60.0, pool=15.0
        )
    except Exception:  # noqa: BLE001 — tanpa httpx pun tetap jalan (timeout klien)
        pass

    def _do() -> tuple[str, list[dict[str, Any]], Any]:
        try:
            stream = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if _apakah_konteks_penuh(exc):
                raise KonteksPenuh(str(exc)) from exc
            raise
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        usage = None
        finish_reason = None
        state: dict[str, Any] = {
            "ts": time.monotonic(), "mulai": False, "macet": False,
            "selesai": False,
        }
        watchdog = _pasang_watchdog(stream, state)
        try:
            try:
                for chunk in stream:
                    if cancel_event is not None and cancel_event.is_set():
                        raise Cancelled()
                    state["ts"] = time.monotonic()
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
                        state["mulai"] = True
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
                        state["mulai"] = True
                        reasoning_parts.append(rpiece)
                        if on_reasoning:
                            on_reasoning(rpiece)
                        elif on_content:
                            on_content(rpiece)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        state["mulai"] = True
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
                        if fn and fn.arguments:
                            slot["arguments"] += fn.arguments
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                # Watchdog menutup socket -> galatnya bisa berbentuk apa saja.
                # Terjemahkan ke _StallTimeout supaya masuk jalur retry/naik-
                # kelas, bukan dilaporkan sebagai kerusakan tak dikenal.
                if state["macet"]:
                    raise _StallTimeout(
                        f"stream diam >{config.STREAM_STALL_TIMEOUT:.0f} dtk "
                        "sesudah token pertama"
                    ) from exc
                # Provider kadang mengirim error 400/413 sebagai CHUNK PERTAMA
                # (bukan saat create()) — tanpa ini, "konteks penuh" jatuh ke
                # jalur generik dan pemulih pemangkasannya tak pernah jalan.
                if _apakah_konteks_penuh(exc):
                    raise KonteksPenuh(str(exc)) from exc
                raise
        finally:
            state["selesai"] = True
            if watchdog is not None:
                watchdog.join(timeout=1.0)
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
            # tool setengah jadi tak boleh tampil sebagai "jawaban".
            cleaned = _re.sub(r"<tool_call>.*?</tool_call>", "", content,
                              flags=_re.DOTALL | _re.IGNORECASE)
            cleaned = _re.sub(r"<function\s*=.*?</function>", "", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            # Sisa penanda tak berpasangan (terpotong) -> potong dari situ.
            cleaned = _re.sub(r"<tool_call>.*$", "", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            cleaned = _re.sub(r"<function\s*=.*$", "", cleaned,
                              flags=_re.DOTALL | _re.IGNORECASE)
            content = cleaned.strip()
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
        stall_escape=max(1, config.MAX_STALLS_PER_CALL),
    )

"""Agent inti: loop chat + tool-calling (streaming). Dipakai semua antarmuka.

Menangani: system prompt dinamis, streaming (token realtime + pembatalan),
pemilihan model & effort yang tersimpan, dan penyimpanan sesi.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from . import config, llm, models, prefs, prompts
from .memory import Memory
from .session import Session
from .tools import base as tools

# --- Protokol tool untuk CONNECTOR web (Claude/Qwen web) ---
# AI web tak punya function-calling API, jadi kita ajari ia "memanggil" tool
# dengan menuliskan blok teks bertanda kurung siku ganda yang mudah di-parse,
# lalu bagas-ai mengeksekusi tool itu SUNGGUHAN di laptop (mesin tool yang sama
# dengan model NVIDIA) dan mengirim balik hasilnya — berulang sampai selesai.
# Penanda ditulis ulang oleh AI web dengan variasi kecil (spasi di dalam kurung,
# huruf kecil), jadi polanya dibuat longgar — kalau tidak, penanda lolos ke layar.
_OPEN_MARK = r"\[\[\s*TOOL\s*\]\]"
_CLOSE_MARK = r"\[\[\s*/\s*TOOL\s*\]\]"
# Sebagian model punya format pemanggilan tool BAWAAN dan memakainya walau
# diminta memakai penanda kita — Qwen, misalnya, mengeluarkan
# <tool_call>{...}</tool_call>. Menerima kedua bentuk jauh lebih murah daripada
# memaksa model mengubah kebiasaannya, dan isinya sama-sama JSON.
_ALT_OPEN = r"<\s*tool_call\s*>"
_ALT_CLOSE = r"<\s*/\s*tool_call\s*>"
_WEB_TOOL_RE = re.compile(
    _OPEN_MARK + r"(.*?)" + _CLOSE_MARK
    + r"|" + _ALT_OPEN + r"(.*?)" + _ALT_CLOSE,
    re.DOTALL | re.IGNORECASE)
# Pagar blok kode markdown (```json / ```): AI web sering merender usulan tool
# sebagai blok kode, jadi pagarnya harus dibuang sebelum JSON di-parse.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*")
# Penanda dari tool yang MENGHASILKAN GAMBAR (lihat tools/screen.py). File-nya
# dilampirkan ke pesan berikutnya supaya AI web benar-benar bisa MELIHATnya.
_IMAGE_MARK_RE = re.compile(r"^\[GAMBAR\][ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Pengingat singkat yang ditempel di TIAP giliran (selain yang pertama, yang
# sudah memuat protokol penuh). Tanpa ini, percakapan panjang membuat AI web
# lupa dan kembali menampilkan kode untuk disalin manual.
_WEB_REMINDER = (
    "[Pengingat: kalau permintaan ini perlu MENGUBAH file atau menjalankan "
    "sesuatu, keluarkan blok [[TOOL]] — jangan menampilkan kode untuk kusalin "
    "sendiri. Aku yang mengeksekusi dan mengirim balik hasilnya.]"
)
# Batas langkah tool per giliran web (jaring anti-loop-liar).
_WEB_MAX_STEPS = 24
# Batas panjang hasil tool yang dikirim balik ke AI web (hemat & fokus).
_WEB_RESULT_CAP = 6000


def _web_tool_protocol() -> str:
    """Instruksi + katalog tool untuk AI web agar bisa BERTINDAK (edit file,
    jalankan perintah, cari web, dll) seperti model NVIDIA."""
    lines = []
    for sc in tools.get_schemas():
        fn = sc.get("function", sc)
        name = fn.get("name", "")
        desc = (fn.get("description", "") or "").strip().split("\n")[0]
        params = fn.get("parameters", {}).get("properties", {}) or {}
        req = set(fn.get("parameters", {}).get("required", []) or [])
        pieces = []
        for pn, pinfo in params.items():
            tag = pn + ("*" if pn in req else "")
            pieces.append(f"{tag}:{pinfo.get('type', 'any')}")
        lines.append(f"- {name}({', '.join(pieces)}) — {desc}")
    tools_text = "\n".join(lines)
    return (
        "Konteks jujur soal percakapan ini, supaya tidak salah paham:\n\n"
        "Pesan-pesan ini dikirim & dibaca oleh **bagas-ai**, sebuah program agent "
        "open-source yang berjalan di laptop saya (saya pemiliknya, dan saya yang "
        "meminta ini). Kamu TIDAK mengeksekusi apa pun sendiri — kamu tetap model "
        "bahasa yang hanya menghasilkan teks, dan kamu memang tidak punya akses "
        "langsung ke laptop saya. Perannya begini:\n"
        "  1. Kamu MENGUSULKAN satu langkah dalam format teks di bawah.\n"
        "  2. Program bagas-ai di laptop saya membaca usulan itu, MENJALANKANNYA "
        "di sana (dengan izin & tanggung jawab saya sebagai pemilik), lalu\n"
        "  3. mengirimkan hasil nyatanya kembali ke percakapan ini sebagai teks.\n\n"
        "Jadi kamu berperan sebagai PERENCANA/otak, dan program lokal itu yang "
        "jadi tangannya. Kamu tidak perlu mengklaim punya akses apa pun — cukup "
        "usulkan langkahnya, dan hasil eksekusi akan kulaporkan balik apa adanya. "
        "Kalau sebuah usulan gagal dijalankan, kamu akan menerima pesan errornya.\n\n"
        "FORMAT USULAN LANGKAH — JSON WAJIB di dalam blok kode ```json (supaya "
        "isinya tidak berubah saat dirender; teks biasa merusak karakter seperti "
        "__nama__ menjadi tebal):\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "<nama_tool>", "args": {"<param>": "<nilai>"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "Contoh mengusulkan pembuatan file:\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "write_file", "args": {"path": "contoh.py", '
        '"content": "def halo():\\n    print(\'hai\')\\n"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "Aturan praktis:\n"
        "1. JSON harus valid (escape newline sebagai \\n, kutip sebagai \\\") dan "
        "SELALU dibungkus ```json ... ``` di dalam penanda [[TOOL]].\n"
        "2. Boleh beberapa blok sekaligus bila langkahnya independen.\n"
        "3. Setelah kukirim balik hasilnya (ditandai [[HASIL <nama_tool>]]), "
        "lanjutkan berdasarkan hasil itu.\n"
        "4. Kalau tugas sudah selesai, balas biasa TANPA blok [[TOOL]] — itu "
        "kuanggap jawaban akhir.\n"
        "5. Untuk membuat/mengubah file, usulkan write_file (bukan menampilkan "
        "kode untuk saya salin manual), karena tujuan saya memang agar bagas-ai "
        "yang menuliskannya langsung ke proyek.\n"
        "6. Path file relatif terhadap folder proyek yang disebut di konteks, "
        "dan pakai garis miring biasa (src/app/main.py) — JANGAN backslash, "
        "supaya tidak rusak saat dikirim.\n"
        "7. JANGAN memakai tool bawaanmu sendiri (pencarian web, analysis/REPL, "
        "artifact) di percakapan ini — semuanya lewat [[TOOL]] saja. Kalau "
        "sebuah langkah gagal, cukup usulkan langkah berikutnya; tak perlu "
        "minta maaf atau menjelaskan panjang lebar.\n"
        "8. Untuk membaca file, pakai read_file (bukan perintah shell seperti "
        "Get-Content/cat) supaya hasilnya rapi & utuh.\n\n"
        "HEMAT LANGKAH — ini penting, jangan buang giliran:\n"
        "- JANGAN membaca ulang file yang isinya SUDAH ada di percakapan ini.\n"
        "- Pakai peta proyek di bawah untuk tahu file mana yang relevan; jangan "
        "menjelajah folder satu per satu untuk hal yang sudah terlihat di peta.\n"
        "- JANGAN memverifikasi ulang langkah yang hasilnya sudah kukirim dan "
        "jelas berhasil (mis. membaca ulang file yang baru saja kamu tulis).\n"
        "- Gabungkan langkah-langkah yang saling bebas dalam SATU balasan "
        "(beberapa blok [[TOOL]] sekaligus), jangan satu per satu bergiliran.\n"
        "- Begitu informasinya cukup, langsung beri jawaban akhir. Jangan "
        "menambah langkah yang tak mengubah kesimpulan.\n"
        "- Ada tool take_screenshot untuk melihat layar pengguna saat debug "
        "tampilan; gambarnya otomatis terlampir ke pesan berikutnya sehingga "
        "kamu bisa melihatnya sendiri.\n\n"
        f"LANGKAH yang bisa diusulkan (tanda * = wajib):\n{tools_text}"
    )


# Backslash yang BUKAN escape JSON sah (mis. path Windows "src\entities" yang
# kehilangan gandanya saat dirender web) -> digandakan agar JSON bisa dibaca.
_BAD_ESCAPE_RE = re.compile(r'\\(?![\\/"bfnrtu]|u[0-9a-fA-F]{4})')


# Artefak PERENDERAN yang membuat JSON tak sah. Situs menata blok kode dengan
# spasi non-breaking & tanda kutip tipografis; JSON standar menolak keduanya,
# sehingga usulan tool yang sebenarnya benar gagal dibaca.
_JSON_ARTIFACTS = {
    "\xa0": " ", " ": " ", " ": " ", " ": " ", "​": "",
    "﻿": "", "“": '"', "”": '"', "‘": "'", "’": "'",
}


def _clean_json_text(raw: str) -> str:
    """Ganti artefak perenderan agar JSON-nya bisa dibaca apa adanya."""
    for buruk, baik in _JSON_ARTIFACTS.items():
        if buruk in raw:
            raw = raw.replace(buruk, baik)
    return raw


def _escape_control_in_strings(raw: str) -> str:
    """Escape baris-baru/tab MENTAH yang berada DI DALAM string JSON.

    Ini penting: isi file selalu multi-baris, dan model biasanya menuliskan
    baris-baru sungguhan di dalam "content" alih-alih \\n. JSON standar
    melarangnya, sehingga usulan write_file DIAM-DIAM gagal dibaca — akibatnya
    AI web seolah hanya bisa membaca & menjalankan perintah, tak pernah benar
    benar mengubah file."""
    out: list[str] = []
    dalam_string = False
    escape = False
    for ch in raw:
        if dalam_string:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                dalam_string = False
                out.append(ch)
                continue
            if ch in "\n\r\t":
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
                continue
            out.append(ch)
            continue
        if ch == '"':
            dalam_string = True
        out.append(ch)
    return "".join(out)


def _json_tool_obj(raw: str) -> dict | None:
    """Baca satu objek tool JSON dari teks.

    Beberapa perbaikan dicoba berurutan karena JSON yang ditulis model kerap
    rusak oleh hal-hal yang di luar kendalinya: perenderan situs, baris-baru
    mentah di dalam string, dan backslash yang kehilangan gandanya."""
    start = (raw or "").find("{")
    if start < 0:
        return None
    body = raw[start:]
    # Urutan perbaikan sengaja dari yang PALING TIDAK mengubah isi: teks apa
    # adanya dulu, baru normalisasi artefak render. Kalau dinormalkan lebih
    # dulu, spasi non-breaking di DALAM isi file ikut jadi spasi biasa dan
    # kode yang ditulis jadi berbeda dari yang dimaksud model.
    bersih = _clean_json_text(body)
    for candidate in (body,
                      _escape_control_in_strings(body),
                      bersih,
                      _escape_control_in_strings(bersih),
                      _BAD_ESCAPE_RE.sub(r"\\\\", bersih),
                      _BAD_ESCAPE_RE.sub(
                          r"\\\\", _escape_control_in_strings(bersih))):
        try:
            # raw_decode: berhenti di akhir objek JSON pertama, sisanya diabaikan.
            obj, _ = json.JSONDecoder().raw_decode(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and (obj.get("tool") or obj.get("name")):
            return _bersihkan_nilai(obj)
    return None


def _bersihkan_nilai(obj: Any) -> Any:
    """Bersihkan artefak render dari NILAI string hasil parsing.

    Spasi non-breaking & kutip tipografis di dalam isi file selalu berasal dari
    cara situs menata blok kode, bukan dari maksud model — kalau dibiarkan, ia
    tertulis ke file sebagai karakter tak terlihat yang bikin kode rusak dan
    sulit ditelusuri."""
    if isinstance(obj, str):
        return _clean_json_text(obj)
    if isinstance(obj, dict):
        return {k: _bersihkan_nilai(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bersihkan_nilai(v) for v in obj]
    return obj


def _parse_web_tool_calls(text: str, code_blocks: Any = ()) -> list[dict]:
    """Ambil daftar {'name','arguments'} dari blok [[TOOL]] pada balasan AI web.

    Toleran terhadap cara AI web merender blok: pagar markdown (```json), label
    bahasa yang bocor, spasi, teks tambahan, dan escape yang rusak.

    `code_blocks` = isi MENTAH blok kode dari DOM. Bila teks yang dirender gagal
    dibaca (paling sering: backslash pada path Windows hilang), usulan diambil
    dari sini karena isinya persis seperti yang ditulis AI web.
    """
    calls: list[dict] = []
    for m in _WEB_TOOL_RE.finditer(text or ""):
        # group(1) = bentuk [[TOOL]]…[[/TOOL]], group(2) = <tool_call>…</tool_call>
        body = _FENCE_RE.sub("", m.group(1) or m.group(2) or "").strip()
        obj = _json_tool_obj(body)
        if obj is not None:
            calls.append({"name": str(obj.get("tool") or obj.get("name")),
                          "arguments": obj.get("args") or obj.get("arguments") or {}})

    # Cadangan: sebagian/seluruh usulan gagal dibaca dari teks -> pakai isi
    # mentah blok kode (byte apa adanya, tak tersentuh perenderan markdown).
    n_markers = len(re.findall(_OPEN_MARK, text or "", re.IGNORECASE)) + \
        len(re.findall(_ALT_OPEN, text or "", re.IGNORECASE))
    if code_blocks and len(calls) < n_markers:
        from_code: list[dict] = []
        for raw in code_blocks:
            obj = _json_tool_obj(str(raw))
            if obj is not None:
                from_code.append(
                    {"name": str(obj.get("tool") or obj.get("name")),
                     "arguments": obj.get("args") or obj.get("arguments") or {}})
        if len(from_code) > len(calls):
            calls = from_code

    # Cadangan 2: sebagian model menulis usulan sebagai JSON BIASA tanpa penanda
    # apa pun (Qwen kerap begitu meski protokolnya sudah dijelaskan). Diterima
    # HANYA bila objeknya benar-benar berbentuk panggilan tool (punya nama tool
    # DAN args) dan balasannya nyaris tak berisi teks lain — supaya penjelasan
    # yang KEBETULAN memuat contoh JSON tidak ikut dieksekusi.
    if not calls:
        for raw in list(code_blocks or ()) + [text or ""]:
            obj = _json_tool_obj(str(raw))
            if obj is None:
                continue
            args = obj.get("args") or obj.get("arguments")
            if not isinstance(args, dict):
                continue
            sisa = _FENCE_RE.sub("", str(raw))
            sisa = re.sub(r"\{.*\}", "", sisa, flags=re.DOTALL).strip()
            if len(sisa) > 80:      # ada prosa panjang -> kemungkinan penjelasan
                continue
            calls.append({"name": str(obj.get("tool") or obj.get("name")),
                          "arguments": args})
            break

    return [c for c in calls if c["name"] and isinstance(c["arguments"], dict)]


# Penanda protokol yang boleh SAJA tersisa di teks (mis. blok rusak / tak
# berpasangan). Semuanya dibuang sebelum jawaban ditampilkan ke pengguna.
_WEB_MARKER_RE = re.compile(
    _OPEN_MARK + r"|" + _CLOSE_MARK + r"|" + _ALT_OPEN + r"|" + _ALT_CLOSE
    + r"|\[\[\s*/?\s*HASIL[^\]]*\]\]",
    re.IGNORECASE)


def _strip_web_markers(text: str) -> str:
    """Buang blok usulan tool + SISA penanda protokol dari teks jawaban.

    Tanpa ini, penanda seperti `[[/TOOL]]` bisa bocor ke layar saat blok tool
    rusak/tak berpasangan — pengguna melihat penanda alih-alih jawaban."""
    out = _WEB_TOOL_RE.sub("", text or "")
    out = _WEB_MARKER_RE.sub("", out)
    # Sisa pagar kode kosong akibat blok yang dibuang.
    out = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*$", "", out, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _take_image_marks(result: str) -> tuple[str, list[str]]:
    """Pisahkan penanda [GAMBAR] dari hasil tool.

    Return (teks tanpa penanda, daftar path gambar). Path-nya dilampirkan ke
    pesan berikutnya, jadi tak perlu ikut dikirim sebagai teks."""
    paths = [m.group(1).strip() for m in _IMAGE_MARK_RE.finditer(result or "")]
    if not paths:
        return result, []
    cleaned = _IMAGE_MARK_RE.sub("", result or "").rstrip()
    return cleaned, paths


def _looks_like_unapplied_code(text: str) -> bool:
    """True bila balasan menyajikan KODE untuk disalin manual, bukan menuliskannya.

    Dipakai untuk menegur sekali: pengguna memakai connector ini supaya
    perubahannya nyata di disk, bukan supaya kode ditempel di layar."""
    t = text or ""
    if "```" not in t and not re.search(r"^\s*(?:html|css|js|python)\d", t, re.M):
        return False
    # Cukup panjang untuk benar-benar berupa berkas/patch, bukan cuplikan sebaris.
    return len(t) > 400


def _strip_tool_json(text: str) -> str:
    """Buang objek JSON USULAN TOOL yang ditulis tanpa penanda.

    Sebagian model menulis usulannya sebagai blok kode polos. Tanpa dibuang,
    JSON mentahnya tercetak ke layar sebagai 'narasi' di setiap putaran dan
    memenuhi terminal."""
    out = text or ""
    i = 0
    while True:
        mulai = out.find("{", i)
        if mulai < 0:
            return re.sub(r"\n{3,}", "\n\n", out).strip()
        # Cari kurung penutup yang berpasangan (abaikan kurung di dalam string).
        depth, j, dalam_string, escape = 0, mulai, False, False
        while j < len(out):
            ch = out[j]
            if dalam_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    dalam_string = False
            elif ch == '"':
                dalam_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(out):
            return re.sub(r"\n{3,}", "\n\n", out).strip()
        blok = out[mulai:j + 1]
        if _json_tool_obj(blok) is not None:
            # Buang blok + label bahasa/nomor baris yang menempel sebelumnya.
            depan = re.sub(r"(?:```[a-zA-Z0-9_+-]*|\b[a-z]{2,10}\d*)\s*$", "",
                           out[:mulai])
            out = depan + out[j + 1:]
            i = len(depan)
        else:
            i = mulai + 1


def _web_reply_complete(text: str) -> bool:
    """False HANYA bila balasan tampak masih ditulis: ada pembuka [[TOOL]] yang
    belum ditutup. Dipakai agar bagas-ai tak menganggap balasan selesai saat blok
    usulan tool baru separuh dirender.

    PENTING: penutup nyasar (mis. AI menulis [[/TOOL]] sendirian) TIDAK boleh
    dianggap 'belum selesai' — dulu itu membuat penungguan berjalan sampai batas
    waktu 5 menit dan terminal terlihat macet."""
    t = text or ""
    opens = (len(re.findall(_OPEN_MARK, t, re.IGNORECASE))
             + len(re.findall(_ALT_OPEN, t, re.IGNORECASE)))
    closes = (len(re.findall(_CLOSE_MARK, t, re.IGNORECASE))
              + len(re.findall(_ALT_CLOSE, t, re.IGNORECASE)))
    return opens <= closes


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _est_messages(messages: list[dict[str, Any]]) -> int:
    total = sum(len(str(m.get("content", "") or "")) for m in messages)
    return total // 4


class Usage:
    """Akumulator token (energi AI)."""

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, usage: Any) -> None:
        if not usage:
            return
        self.prompt += getattr(usage, "prompt_tokens", 0) or 0
        self.completion += getattr(usage, "completion_tokens", 0) or 0

    def add_raw(self, prompt: int, completion: int) -> None:
        self.prompt += prompt
        self.completion += completion


class Agent:
    """Agent percakapan dengan kemampuan memanggil tools (streaming)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        tool_names: list[str] | None = None,
        max_iterations: int | None = None,
        session: Session | None = None,
    ) -> None:
        model_id = model or prefs.get_model() or config.CHAT_MODEL
        if "deepseek" in model_id.lower():
            # DeepSeek dihapus dari katalog (sering gagal dipakai) -> alihkan
            # preferensi lama ke model pengganti ACAK (tanpa pola, beban
            # menyebar antar model) & simpan agar migrasi sekali saja.
            picked = models.random_fallback()
            model_id = picked.id if picked else config.CHAT_MODEL
            prefs.save(model=model_id)
        self.model_spec = models.spec_for_id(model_id)
        self._init_effort()

        self.memory = Memory(system_prompt=prompts.build_system_prompt())
        self.tool_names = tool_names
        self.max_iterations = max_iterations or config.MAX_TOOL_ITERATIONS
        self.session = session

        self.tokens_session = Usage()
        self.tokens_last = Usage()
        self.tokens_live = 0  # nilai token realtime untuk tampilan

        # Auto-fallback: model yang sudah dicoba & berapa kali naik-kelas (per giliran).
        self._tried_models: set[str] = set()
        self._escalations = 0
        # Connector web: apakah konteks laptop/proyek sudah dikirim ke sesi web
        # ini (dikirim SEKALI sbg preamble pesan pertama; AI web ingat sepanjang chat).
        self._web_ctx_sent = False
        # Percakapan AI web yang DILANJUTKAN (dari sesi tersimpan / menu pilih
        # sesi). Bila ada, giliran pertama membuka chat itu — bukan chat baru —
        # sehingga konteks proyek yang sudah ada di sana tak perlu dikirim ulang.
        self._web_chat_id = ""
        if session is not None:
            saved = (getattr(session, "web_chats", None) or {}).get(
                self.model_spec.connector or "")
            if saved:
                self.use_web_chat(saved)

        # Token SESI bersifat persisten: saat --resume, lanjutkan hitungan
        # sesi sebelumnya (bukan mulai dari nol).
        if session and getattr(session, "tokens", None):
            self.tokens_session.prompt = int(session.tokens.get("prompt", 0) or 0)
            self.tokens_session.completion = int(
                session.tokens.get("completion", 0) or 0
            )

        if session and session.messages:
            self.memory.load(session.messages)

    # --- model & effort ---
    def _init_effort(self) -> None:
        if self.model_spec.supports_effort():
            saved = prefs.get_effort()
            self.effort = (
                saved if saved in self.model_spec.effort_options()
                else self.model_spec.default_effort()
            )
        else:
            self.effort = None

    @property
    def model(self) -> str:
        return self.model_spec.id

    def set_model(self, name: str) -> str:
        before = self.model_spec.connector
        self.model_spec = models.resolve(name)
        self.effort = self.model_spec.default_effort()
        if self.model_spec.connector != before:
            # Pindah layanan (mis. Claude web -> Qwen web, atau ke/dari model
            # NVIDIA): state percakapan web TIDAK boleh terbawa. Tanpa ini,
            # layanan baru dikira sudah menerima konteks (padahal chat-nya
            # kosong) dan ID chat milik layanan lama ikut terbawa.
            self._sync_web_state()
        prefs.save(model=self.model_spec.id, effort=self.effort)
        return self.model_spec.label

    def _sync_web_state(self) -> None:
        """Selaraskan kaitan chat web dengan LAYANAN yang sedang aktif."""
        svc = self.model_spec.connector
        saved = ""
        if svc and self.session is not None:
            saved = (getattr(self.session, "web_chats", None) or {}).get(svc, "")
        self._web_chat_id = saved
        # Konteks dianggap sudah terkirim HANYA bila kita menyambung chat lama
        # milik layanan ini; chat baru selalu perlu konteks lagi.
        self._web_ctx_sent = bool(saved)

    def set_effort(self, name: str) -> str | None:
        if not self.model_spec.supports_effort():
            return None
        if name not in self.model_spec.effort_options():
            raise ValueError(f"Effort '{name}' tidak dikenal untuk model ini.")
        self.effort = name
        prefs.save(effort=name)
        return name

    # --- auto-fallback saat AI ngeloop / performanya turun -------------------
    def _escalate(self, reason: str) -> str | None:
        """Naik kelas ketika AI ngelantur: NAIKKAN effort dulu (murah), lalu GANTI
        model. Memory/konteks TIDAK disentuh -> percakapan tetap nyambung, agent
        lanjut dari titik yang sama dengan otak yang lebih kuat/berbeda.

        Return deskripsi perubahan, atau None bila tak ada yang bisa dinaikkan.
        """
        if not config.AUTO_FALLBACK:
            return None
        if self._escalations >= config.MAX_ESCALATIONS:
            return None
        # 1) Naikkan effort (paling murah & cepat, model tetap sama).
        if self.model_spec.supports_effort() and self.effort:
            opts = list(self.model_spec.effort_options().keys())
            if self.effort in opts:
                i = opts.index(self.effort)
                if i < len(opts) - 1:
                    old, self.effort = self.effort, opts[i + 1]
                    self._escalations += 1
                    return f"effort {old} → {self.effort} ({reason})"
        # 2) Ganti ke model lain yang belum dicoba di giliran ini — dipilih
        #    ACAK (bukan urutan katalog) agar pengalihan tak berpola: kegagalan
        #    tidak selalu jatuh ke model yang itu-itu saja.
        self._tried_models.add(self.model_spec.id)
        spec = models.random_fallback(self._tried_models)
        if spec is None:
            return None
        old = self.model_spec.label
        self.model_spec = spec
        self._tried_models.add(spec.id)
        self._init_effort()
        self._escalations += 1
        return f"model {old} → {spec.label} ({reason})"

    # --- kaitan sesi terminal <-> percakapan di AI web ---
    def use_web_chat(self, chat_id: str) -> None:
        """Sambungkan sesi ini ke percakapan AI web yang SUDAH ADA.

        Konteks proyek & protokol tool sudah tersimpan di percakapan itu, jadi
        tak dikirim ulang (hemat & AI web langsung 'ingat' proyeknya)."""
        self._web_chat_id = chat_id or ""
        self._web_ctx_sent = bool(chat_id)

    def start_new_web_chat(self) -> None:
        """Lupakan kaitan chat web -> giliran berikutnya membuat chat BARU."""
        self._web_chat_id = ""
        self._web_ctx_sent = False
        if self.session is not None:
            svc = self.model_spec.connector
            if svc and svc in getattr(self.session, "web_chats", {}):
                self.session.web_chats.pop(svc, None)

    def _link_web_chat(self, chat_id: str) -> None:
        """Catat chat web ini sebagai milik sesi terminal saat ini (1 sesi
        terminal = 1 percakapan browser, juga dipakai saat --resume)."""
        self._web_chat_id = chat_id
        svc = self.model_spec.connector
        if self.session is not None and svc and chat_id:
            try:
                self.session.web_chats[svc] = chat_id
            except AttributeError:  # sesi lama tanpa atribut ini
                self.session.web_chats = {svc: chat_id}

    def refresh_system_prompt(self) -> None:
        """Bangun ulang system prompt (mis. setelah add-dir) & pasang ke memory."""
        self.memory.set_system(prompts.build_system_prompt())

    # --- sesi ---
    def reset(self) -> None:
        self.memory.reset()
        # Riwayat dikosongkan -> percakapan AI web lama tak lagi mewakili sesi
        # ini; giliran berikutnya memulai chat baru di situs.
        self.start_new_web_chat()
        self._persist()

    def _persist(self) -> None:
        if self.session:
            try:
                self.session.save(
                    self.memory.messages,
                    tokens={
                        "prompt": self.tokens_session.prompt,
                        "completion": self.tokens_session.completion,
                    },
                )
            except OSError:
                pass

    # --- inti ---
    def run(
        self,
        user_input: Any,
        *,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        cancel_event: Any = None,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        """Proses satu giliran (streaming). Kembalikan teks jawaban final.

        `on_notice(teks)` dipanggil saat bagas-ai OTOMATIS naik-kelas (ganti
        effort/model) karena terdeteksi ngeloop / performanya menurun.

        `on_message(teks)` dipanggil untuk narasi antar-langkah (ketika agent
        menjelaskan apa yang akan dilakukan sebelum memakai tool).
        `on_tool_result(nama, hasil)` dipanggil SETELAH sebuah tool selesai —
        dipakai UI untuk menampilkan hasil (mis. output perintah) secara ringkas.
        `on_retry(percobaan, tunggu, exc)` dipanggil saat NVIDIA rate-limit dan
        bagas-ai menunggu lalu MELANJUTKAN langkah yang sama.
        Bila `cancel_event` diset di tengah jalan, melempar llm.Cancelled.

        Bila model aktif adalah CONNECTOR web (mis. Claude/Qwen web), giliran ini
        TIDAK memakai API NVIDIA maupun tool-calling — melainkan diteruskan ke
        situs webnya lewat browser (`on_status`/`on_token` untuk progres & teks).
        """
        if self.model_spec.is_web:
            return self._run_connector(
                user_input, cancel_event=cancel_event,
                on_status=on_status, on_token=on_token,
                on_tool=on_tool, on_message=on_message,
                on_tool_result=on_tool_result, on_notice=on_notice,
            )
        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0
        # Jatah naik-kelas dihitung ulang tiap giliran (masalah bisa spesifik tugas).
        self._escalations = 0
        self._tried_models = {self.model_spec.id}
        try:
            return self._run_loop(
                on_tool, on_message, cancel_event, on_retry, on_tool_result,
                on_notice,
            )
        except BaseException:
            # Apa pun yang gagal di tengah giliran (rate limit, error tool,
            # pembatalan), rapikan state tool yang menggantung supaya instruksi
            # pengguna & konteks tetap tersimpan dan panggilan API berikutnya
            # tetap valid. Berlaku untuk SEMUA antarmuka (CLI, API, Telegram),
            # bukan hanya CLI. Lalu lempar ulang agar pemanggil bisa menangani.
            self.memory.repair_dangling_tools()
            self._persist()
            raise

    def _run_connector(
        self,
        user_input: Any,
        *,
        cancel_event: Any = None,
        on_status: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> str:
        """Jalankan giliran lewat AI web (browser) sebagai AGENT penuh.

        AI web tak punya function-calling, jadi kita ajari ia memakai TOOLS lewat
        protokol teks (_web_tool_protocol): ia menuliskan blok [[TOOL]]{...}[[/TOOL]],
        bagas-ai MENGEKSEKUSI tool itu sungguhan di laptop (mesin tool yang sama
        dengan model NVIDIA), lalu mengirim balik hasilnya — berulang sampai AI web
        menjawab tanpa blok tool (jawaban akhir). Dengan begitu Claude/Qwen web bisa
        mengedit file, menjalankan perintah, mencari web, dll — setara model NVIDIA.

        Web-AI menyimpan konteks percakapannya SENDIRI di sesi browser; memory
        bagas-ai tetap mencatat transkrip agar tampil di UI & tersimpan.
        """
        from . import connectors  # impor tunda: Playwright opsional

        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0
        user_text = str(user_input)

        if not connectors.playwright_available():
            answer = (
                "Fitur connector web butuh Playwright + browser Chromium yang "
                "belum terpasang. Jalankan:\n\n"
                "    pip install playwright\n"
                "    playwright install chromium\n\n"
                "lalu coba lagi. Atau kembali ke model NVIDIA dengan /model."
            )
            self.memory.add_assistant_text(answer)
            self._persist()
            return answer

        # Pesan PERTAMA sesi web memuat: protokol tool + konteks laptop/proyek
        # (keduanya SEKALI saja — AI web mengingatnya sepanjang chat).
        include_ctx = not self._web_ctx_sent
        first_msg = user_text
        if include_ctx:
            preamble = _web_tool_protocol()
            try:
                ctx = prompts.build_web_context()
            except Exception:  # noqa: BLE001
                ctx = ""
            if ctx:
                preamble += "\n\n" + ctx
            # Riwayat percakapan sejauh ini (tanpa permintaan yang sedang
            # dikirim) — supaya pindah model di tengah kerja tidak kehilangan
            # konteks: chat di situs baru selalu mulai kosong.
            try:
                digest = prompts.build_transcript_digest(
                    self.memory.messages[:-1])
            except Exception:  # noqa: BLE001
                digest = ""
            if digest:
                preamble += (
                    "\n\n# Percakapan kami sebelum ini (dengan asisten lain)\n"
                    "Lanjutkan dari sini — jangan mengulang yang sudah dibahas:\n"
                    + digest
                )
            first_msg = preamble + "\n\n==========\nPERMINTAAN SAYA:\n" + user_text
        else:
            # Percakapan panjang membuat AI web LUPA protokol dan kembali ke mode
            # mengobrol: menampilkan kode di jawaban alih-alih menuliskannya.
            # Pengingat singkat tiap giliran jauh lebih murah daripada mengirim
            # ulang seluruh protokol.
            first_msg = user_text + "\n\n" + _WEB_REMINDER

        conn = connectors.get_connector(self.model_spec.connector)
        prompt_chars = 0
        reply_chars = 0
        answer = ""

        def _sync_tokens() -> None:
            """Perbarui hitungan token (estimasi) agar penghitung di UI hidup
            selama giliran berjalan, bukan melompat di akhir."""
            self.tokens_live = (prompt_chars + reply_chars) // 4

        def _send(msg: str, new_chat: bool = False,
                  open_chat_id: str | None = None,
                  attachments: list[str] | None = None) -> str:
            nonlocal prompt_chars, reply_chars
            # Default: LANJUTKAN percakapan browser sesi ini. Ini KUNCI kontinuitas
            # di tengah tugas. Dulu hanya kirim PERTAMA yang menargetkan chat ini;
            # pesan susulan (hasil tool, teguran, perbaikan) dikirim dengan
            # open_chat_id kosong. Akibatnya bila browser mati & diluncurkan ulang
            # di tengah agentic-loop (mis. sesudah eksekusi lama yang bikin sesi
            # browser time-out), halaman baru mendarat di chat KOSONG dan susulan
            # diketik ke sana — AI web kehilangan seluruh konteks "progress tadi"
            # lalu kebingungan. Dengan menargetkan chat yang SAMA di tiap kirim,
            # relaunch kapan pun selalu kembali ke percakapan yang benar.
            if open_chat_id is None:
                open_chat_id = self._web_chat_id
            prompt_chars += len(msg)
            _sync_tokens()
            # "Server sedang sibuk" itu SEMENTARA (kuota kita aman) dan biasanya
            # pulih dalam hitungan detik, jadi ditangani di sini: tunggu lalu
            # kirim ULANG pesan yang sama. Menyerahkannya ke pengguna berarti
            # tugas yang sedang berjalan putus di tengah tanpa alasan nyata.
            # Jeda menaik supaya tak menambah beban server yang sedang penuh.
            jeda = (15, 40, 75)
            for percobaan in range(len(jeda) + 1):
                try:
                    out = conn.send(
                        msg, on_status=on_status, on_token=on_token,
                        cancel_event=cancel_event, new_chat=new_chat,
                        open_chat_id=open_chat_id,
                        complete_when=_web_reply_complete,
                        attachments=attachments,
                    )
                    break
                except connectors.WebBusyError:
                    if percobaan >= len(jeda):
                        raise           # sudah cukup sabar -> laporkan jujur
                    tunggu = jeda[percobaan]
                    on_status(f"{self.model_spec.label} sibuk — menunggu "
                              f"{tunggu}s lalu mencoba lagi "
                              f"({percobaan + 1}/{len(jeda)})")
                    # Tidur dipecah supaya Esc/batal tetap responsif; cancel_event
                    # yang menyala mengakhiri penungguan seketika.
                    habis = time.time() + tunggu
                    while time.time() < habis:
                        if cancel_event is not None and cancel_event.is_set():
                            raise llm.Cancelled()
                        time.sleep(0.2)
            reply_chars += len(out or "")
            _sync_tokens()
            # Tangkap kaitan chat begitu URL /chat/<id> tersedia (kadang baru
            # muncul sesudah balasan pertama). Sekali tertangkap, semua kirim &
            # relaunch berikutnya otomatis menargetkan percakapan yang sama.
            got = getattr(conn, "last_chat_id", "")
            if got and got != self._web_chat_id:
                self._link_web_chat(got)
            return out

        try:
            # SATU sesi terminal = SATU percakapan browser:
            #  - sudah punya kaitan chat (sesi lanjutan / --resume) -> BUKA chat itu
            #  - belum punya -> mulai chat BARU lalu catat kaitannya
            first_of_session = not self._web_ctx_sent or bool(self._web_chat_id)
            reply = _send(
                first_msg,
                new_chat=include_ctx,
                open_chat_id=self._web_chat_id if first_of_session else "",
            )
            if include_ctx:
                self._web_ctx_sent = True
            if first_of_session:
                # Catat kaitan sesi<->chat + rapikan chat lama buatan bagas-ai
                # supaya tak menumpuk di akun (chat pribadi tak tersentuh).
                try:
                    chat_id = getattr(conn, "last_chat_id", "")
                    if chat_id:
                        self._link_web_chat(chat_id)
                        if include_ctx:  # percakapan yang BARU dibuat
                            conn.record_chat(chat_id, user_text[:80])
                            if config.CONNECTOR_KEEP_CHATS > 0:
                                conn.prune_own_chats(config.CONNECTOR_KEEP_CHATS)
                except Exception:  # noqa: BLE001 - bersih-bersih tak boleh menggagalkan giliran
                    pass

            steps = 0
            repairs = 0   # berapa kali minta AI web mengirim ulang blok rusak
            # Jaring anti-ulang, sama seperti jalur model NVIDIA: hasil langkah
            # di-cache per (nama+argumen). Tanpa ini AI web bisa mengulang
            # langkah yang PERSIS SAMA berpuluh kali sampai batas langkah habis
            # — boros kuota & tak menghasilkan apa pun.
            seen_tools: dict[str, str] = {}
            dup_hits = 0
            # Langkah yang GAGAL/timeout BERTURUT-TURUT. Beda dari dup_hits: di sini
            # argumennya boleh berubah-ubah (mis. AI web menjalankan kode yang
            # sedikit divariasikan tapi tetap infinite-loop lalu timeout berulang),
            # sehingga cache anti-ulang tak menangkapnya dan tugas bisa memutar
            # sampai _WEB_MAX_STEPS (~12 menit timeout beruntun). Dihentikan lebih
            # awal supaya AI web menyimpulkan jujur alih-alih terus mencoba.
            fail_streak = 0
            force_final = False
            nudges = 0    # teguran "kode ditampilkan tapi tak ditulis ke file"
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise llm.Cancelled()
                calls = [] if force_final else _parse_web_tool_calls(
                    reply, getattr(conn, "last_code_blocks", ()))

                # Ada penanda [[TOOL]] tapi isinya tak terbaca (rusak saat
                # dirender web). Jangan tampilkan penanda mentah ke pengguna —
                # minta AI web mengirim ulang usulannya dengan format benar.
                if not calls and "[[TOOL]]" in (reply or "") and repairs < 2:
                    repairs += 1
                    reply = _send(
                        "[SISTEM] Blok usulan tool-mu tidak terbaca (JSON-nya "
                        "rusak saat dirender). Kirim ULANG langkah itu SAJA "
                        "dengan format persis:\n[[TOOL]]\n```json\n"
                        '{"tool": "...", "args": {...}}\n```\n[[/TOOL]]\n'
                        "Pakai garis miring biasa pada path, tanpa teks lain.")
                    continue

                # AI web menampilkan KODE tapi tak menuliskannya ke file: itu
                # mode mengobrol, bukan mengerjakan. Tegur SEKALI per giliran —
                # pengguna memakai connector ini justru agar perubahannya nyata.
                if (not calls and not force_final and nudges < 1
                        and steps == 0 and _looks_like_unapplied_code(reply)):
                    nudges += 1
                    reply = _send(
                        "[SISTEM] Kamu menampilkan kode tapi tidak menuliskannya "
                        "ke file, jadi tak ada yang berubah di laptopku. Kalau "
                        "kode itu memang perlu diterapkan, keluarkan sekarang "
                        "blok [[TOOL]] write_file untuk tiap file (isi LENGKAP, "
                        "bukan potongan). Kalau memang hanya penjelasan, ulangi "
                        "jawaban akhirmu tanpa blok tool.")
                    continue

                if not calls or steps >= _WEB_MAX_STEPS:
                    # Tak ada tool -> ini jawaban AKHIR. Bersihkan sisa penanda
                    # DAN usulan JSON tanpa penanda (mis. saat model tetap
                    # mengulang padahal sudah diminta menyimpulkan).
                    answer = _strip_tool_json(_strip_web_markers(reply))
                    if not answer:
                        # Seluruh balasan hanya berupa blok/penanda yang tak
                        # terbaca. Tampilkan CUPLIKAN aslinya — tanpa itu tak
                        # ada petunjuk apa pun untuk memperbaiki penyebabnya.
                        # Cuplikan DISANITASI: pagar kode (```) akan menutup
                        # blok lebih awal sehingga sisanya dirender kacau, dan
                        # penanda protokol yang lolos ke memory ikut terbawa ke
                        # ringkasan percakapan untuk model BERIKUTNYA.
                        mentah = " ".join((reply or "").split())[:300]
                        mentah = (mentah.replace("`", "'")
                                        .replace("[[", "⟦").replace("]]", "⟧"))
                        answer = (
                            "Balasan dari AI web tak bisa kubaca sebagai langkah "
                            "yang sah (formatnya rusak saat dirender). Coba "
                            "kirim ulang permintaanmu, atau perjelas langkah "
                            "yang kamu mau.\n\n"
                            f"Yang terbaca dari layar:\n```\n{mentah or '(kosong)'}\n```"
                        )
                    if steps >= _WEB_MAX_STEPS and calls:
                        answer += ("\n\n_(batas langkah tool tercapai — sebagian "
                                   "aksi mungkin belum tuntas.)_")
                    break

                # Narasi sebelum tool = teks di luar blok tool. JSON usulan yang
                # ditulis TANPA penanda juga dibuang, kalau tidak ia tercetak
                # mentah ke layar tiap putaran.
                narration = _strip_tool_json(_strip_web_markers(reply))
                if narration and on_message:
                    on_message(narration)

                # Eksekusi tiap tool & kumpulkan hasil untuk dikirim balik.
                result_blocks = []
                images: list[str] = []
                for c in calls:
                    if cancel_event is not None and cancel_event.is_set():
                        raise llm.Cancelled()
                    name, args = c["name"], c["arguments"]
                    if on_tool:
                        on_tool(name, args)
                    kunci = name + "::" + json.dumps(
                        args, sort_keys=True, ensure_ascii=False, default=str)
                    if kunci in seen_tools:
                        # Langkah PERSIS SAMA sudah pernah dijalankan: kembalikan
                        # hasil yang sama + tegur, jangan eksekusi ulang.
                        dup_hits += 1
                        result = (
                            "[SISTEM] Kamu SUDAH menjalankan langkah ini dengan "
                            "argumen yang sama persis; hasilnya identik dengan di "
                            "bawah. JANGAN mengulanginya — pakai hasil ini lalu "
                            "lanjut ke langkah BERIKUTNYA atau berikan jawaban "
                            "akhir.\n\n" + seen_tools[kunci]
                        )
                    else:
                        result = tools.execute(name, args)
                        seen_tools[kunci] = result
                        # Deret gagal beruntun (lihat fail_streak di atas). Penanda
                        # gagal seragam dari tools: "[GAGAL...]" (shell) & "GAGAL:"
                        # (files). Sukses apa pun menyetel ulang deretnya.
                        if "[GAGAL" in result or result.lstrip().startswith("GAGAL"):
                            fail_streak += 1
                        else:
                            fail_streak = 0
                        if on_tool_result:
                            on_tool_result(name, result)
                    steps += 1
                    # Tool yang menghasilkan GAMBAR (mis. screenshot): file-nya
                    # dilampirkan ke pesan berikutnya supaya AI web melihatnya
                    # sendiri, bukan cuma diberi tahu path-nya.
                    text_result, imgs = _take_image_marks(result)
                    if imgs and conn.supports_attachments():
                        images.extend(imgs)
                        text_result += ("\n(gambar terlampir pada pesan ini — "
                                        "lihat langsung, jangan minta dikirim ulang)")
                    clipped = text_result if len(text_result) <= _WEB_RESULT_CAP \
                        else (text_result[:_WEB_RESULT_CAP] + "\n…[hasil dipotong]")
                    result_blocks.append(
                        f"[[HASIL {name}]]\n{clipped}\n[[/HASIL]]")

                follow = (
                    "\n\n".join(result_blocks)
                    + "\n\nLanjutkan tugas berdasarkan hasil di atas. Kalau perlu "
                    "tool lagi, keluarkan blok [[TOOL]] berikutnya; kalau sudah "
                    "SELESAI, beri jawaban akhir biasa (tanpa blok tool)."
                )
                if dup_hits >= config.MAX_DUPLICATE_TOOL_CALLS:
                    # Terjebak mengulang langkah yang sama: matikan tool dan
                    # paksa menyimpulkan, daripada memutar sampai batas langkah.
                    force_final = True
                    follow += (
                        "\n\n[SISTEM] Kamu terus mengulang langkah yang sama. "
                        "STOP memakai tool. Berikan jawaban akhir dalam teks "
                        "biasa: jelaskan JUJUR apa yang sudah selesai, apa yang "
                        "belum, dan langkah tersisa yang perlu dilakukan."
                    )
                    if on_notice:
                        on_notice("langkah yang sama berulang — beralih ke "
                                  "kesimpulan")
                elif fail_streak >= config.MAX_DUPLICATE_TOOL_CALLS:
                    # Gagal/timeout beruntun (mis. kode yang dijalankan tak pernah
                    # berhenti): berhenti mencoba, minta kesimpulan jujur.
                    force_final = True
                    follow += (
                        "\n\n[SISTEM] Beberapa langkah tool GAGAL/timeout "
                        "berturut-turut. STOP menjalankan ulang kode itu. Berikan "
                        "jawaban akhir dalam teks biasa: jelaskan JUJUR apa yang "
                        "berhasil, apa yang gagal DAN kenapa (mis. kode yang "
                        "dijalankan tak berhenti / timeout), lalu langkah tersisa."
                    )
                    if on_notice:
                        on_notice("langkah gagal/timeout beruntun — beralih ke "
                                  "kesimpulan")
                reply = _send(follow, attachments=images)
        except llm.Cancelled:
            self.memory.repair_dangling_tools()
            self._persist()
            raise
        except connectors.WebBusyError as exc:
            # Sudah diulang beberapa kali dan servernya MASIH penuh. Katakan apa
            # adanya — jangan tampilkan pemberitahuan situs seolah jawaban model.
            answer = (
                f"🕒 **Server {self.model_spec.label} sedang penuh.**\n\n"
                f"> {exc}\n\n"
                "Sudah kucoba ulang beberapa kali dengan jeda, tapi masih penuh. "
                "Kirim ulang sebentar lagi, atau ketik `/model` untuk pindah ke "
                "model NVIDIA (gratis & tanpa browser) supaya bisa lanjut sekarang."
            )
        except connectors.WebLimitError as exc:
            # Kuota situs habis — sampaikan apa adanya (termasuk kapan pulih)
            # dan tawarkan jalan keluar, jangan sekadar "gagal".
            answer = (
                f"⛔ **{self.model_spec.label} sedang kena batas pemakaian.**\n\n"
                f"> {exc}\n\n"
                "Tunggu sampai waktu itu, atau ketik `/model` untuk pindah ke "
                "model NVIDIA (gratis & tanpa browser) supaya bisa lanjut kerja "
                "sekarang."
            )
        except connectors.BrowserError as exc:
            answer = f"[Connector {self.model_spec.label}] {exc}"
        except Exception as exc:  # noqa: BLE001 - laporkan apa adanya, jangan crash REPL
            answer = f"[Connector {self.model_spec.label}] gagal: {exc}"

        self.memory.add_assistant_text(answer)
        # Web-AI tak melaporkan token; pakai estimasi ~4 karakter per token dari
        # TOTAL lalu-lintas giliran ini (semua pesan terkirim + semua balasan),
        # bukan hanya jawaban akhir, supaya angkanya konsisten dgn model NVIDIA.
        self.tokens_last.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_session.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_live = self.tokens_last.total
        self._persist()
        return answer

    def _run_loop(
        self,
        on_tool: Callable[[str, dict[str, Any]], None] | None,
        on_message: Callable[[str], None] | None,
        cancel_event: Any,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> str:
        schemas = tools.get_schemas(self.tool_names)

        # Agent boleh memakai tool sampai tugas selesai, TAPI dengan jaring
        # pengaman anti-loop-liar agar tidak mengulang-ulang / ngelantur:
        #  - `seen_tools`  : cache hasil per (nama+argumen) -> panggilan PERSIS
        #                    SAMA tak dieksekusi ulang, cukup dikembalikan + ditegur.
        #  - `dup_hits`    : berapa kali pengulangan terjadi; bila melewati batas,
        #                    tool DIMATIKAN dan agent dipaksa menyimpulkan.
        #  - `total_calls` : total panggilan tool; ada anggaran maksimum.
        guard = 0
        safety = max(self.max_iterations, 60)
        seen_tools: dict[str, str] = {}
        dup_hits = 0
        total_calls = 0
        force_final = False
        # Sinyal "performa menurun": model menuliskan tool call sebagai TEKS/XML
        # (weak_hits) atau membalas kosong berulang (empty_hits).
        weak_hits = 0
        empty_hits = 0
        # Berapa kali giliran ini kena MACET total (StreamStalled) — dibatasi
        # agar tak berputar selamanya bila jaringan/model benar-benar mati.
        stall_rounds = 0
        while True:
            guard += 1
            if guard > safety:
                break
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

            # Dihitung ULANG tiap putaran: model/effort bisa BERUBAH di tengah
            # giliran akibat auto-fallback.
            extra = self.model_spec.extra_body_for(self.effort)
            prompt_est = _est_messages(self.memory.messages)
            state = {"completion_est": 0}
            self.tokens_live = self.tokens_last.total + prompt_est

            def on_content(piece: str) -> None:
                state["completion_est"] += _est_tokens(piece)
                self.tokens_live = (
                    self.tokens_last.total + prompt_est + state["completion_est"]
                )

            def _on_retry(attempt: int, wait: float, exc: Exception) -> None:
                # Panggilan diulang dari awal -> reset estimasi token parsial
                # agar tidak dobel-hitung, lalu teruskan info ke UI.
                state["completion_est"] = 0
                self.tokens_live = self.tokens_last.total + prompt_est
                if on_retry:
                    on_retry(attempt, wait, exc)

            # Saat stagnasi terdeteksi, matikan tool -> model TERPAKSA menjawab
            # dengan teks (menyimpulkan), tidak bisa mengulang tool lagi.
            active_tools = None if force_final else (schemas or None)
            try:
                content, tool_calls, usage = llm.stream_completion(
                    self.memory.messages,
                    tools=active_tools,
                    model=self.model_spec.id,
                    extra_body=extra,
                    on_content=on_content,
                    cancel_event=cancel_event,
                    on_retry=_on_retry,
                )
            except llm.StreamStalled:
                # ANTI-MACET: stream berhenti mengirim data berulang kali di model
                # ini. Batalkan sendiri, NAIK KELAS (effort/model), lalu ULANGI —
                # memory belum disentuh di putaran ini, jadi konteks tetap utuh.
                stall_rounds += 1
                if stall_rounds > 3:
                    final = (
                        "Maaf, model terus macet (tidak mengirim respons) meski "
                        "sudah kubatalkan & kuulang otomatis beberapa kali. "
                        "Kemungkinan jaringan/server NVIDIA sedang bermasalah — "
                        "coba lagi sebentar lagi, atau ganti model dengan /model."
                    )
                    self.memory.add_assistant_text(final)
                    self._persist()
                    return final
                changed = self._escalate("respons macet — stream diam terlalu lama")
                if on_notice:
                    on_notice(changed or
                              "respons macet — dibatalkan & diulang otomatis")
                continue

            # Konfirmasi token: pakai usage asli bila ada, jika tidak estimasi.
            if usage:
                self.tokens_last.add(usage)
                self.tokens_session.add(usage)
            else:
                self.tokens_last.add_raw(prompt_est, state["completion_est"])
                self.tokens_session.add_raw(prompt_est, state["completion_est"])
            self.tokens_live = self.tokens_last.total

            # --- Deteksi "performa menurun" ---
            # Model menuliskan tool call sebagai TEKS/XML (diselamatkan llm.py &
            # diberi id 'txt_') = tanda model ini lemah di function-calling.
            if tool_calls and any(
                str(tc.get("id", "")).startswith("txt_") for tc in tool_calls
            ):
                weak_hits += 1
            if not tool_calls and not (content and content.strip()):
                empty_hits += 1

            if not tool_calls:
                # Balasan kosong berulang -> coba naik-kelas dulu sebelum menyerah.
                if empty_hits >= 2 and not force_final:
                    changed = self._escalate("respons kosong berulang")
                    if changed:
                        empty_hits = 0
                        if on_notice:
                            on_notice(changed)
                        self.memory.add({
                            "role": "user",
                            "content": ("[SISTEM] Balasanmu kosong. Lanjutkan tugas "
                                        "dari konteks di atas dan berikan jawaban."),
                        })
                        continue
                # Jaring pengaman: jangan pernah "berhenti diam". Bila model tak
                # menghasilkan teks apa pun (mis. berhenti tanpa menjawab), beri
                # pesan cadangan yang jelas alih-alih layar kosong.
                final = content if (content and content.strip()) else (
                    "Hmm, aku berhenti tanpa sempat menyusun jawaban. Coba ulangi "
                    "atau perjelas permintaanmu. Kalau modelnya bertipe reasoning, "
                    "turunkan /effort agar tidak kehabisan anggaran berpikir."
                )
                self.memory.add_assistant_text(final)
                self._persist()
                return final

            # Narasi sebelum aksi tool (mis. "Baik, saya akan membuat file X").
            if content and content.strip() and on_message:
                on_message(content)

            # Ada tool call: catat lalu eksekusi.
            self.memory.add(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        {
                            "id": tc["id"] or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"] or "{}",
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                }
            )
            for i, tc in enumerate(tool_calls):
                if cancel_event is not None and cancel_event.is_set():
                    raise llm.Cancelled()
                name = tc["name"]
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if on_tool:
                    on_tool(name, args)
                # Dedup: panggilan PERSIS SAMA (nama+argumen) tak dieksekusi ulang.
                key = name + "::" + json.dumps(
                    args, sort_keys=True, ensure_ascii=False, default=str
                )
                if key in seen_tools:
                    dup_hits += 1
                    result = (
                        "[SISTEM] Kamu SUDAH memanggil tool ini dengan argumen yang "
                        "sama persis; hasilnya identik dengan di bawah. JANGAN "
                        "mengulanginya — gunakan hasil ini lalu lanjut ke langkah "
                        "berikutnya atau berikan jawaban akhir.\n\n" + seen_tools[key]
                    )
                else:
                    result = tools.execute(name, args)
                    seen_tools[key] = result
                    # Tampilkan hasil (mis. output perintah) HANYA saat benar-benar
                    # dieksekusi — bukan saat dedup mengembalikan cache + teguran.
                    if on_tool_result:
                        on_tool_result(name, result)
                total_calls += 1
                self.memory.add(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"] or f"call_{i}",
                        "content": result,
                    }
                )

            # --- Stagnasi / performa menurun ---
            # NGELOOP (mengulang tool sama) atau model lemah di function-calling:
            # COBA NAIK-KELAS DULU (effort ↑, lalu ganti model) dan LANJUTKAN dengan
            # KONTEKS YANG SAMA — memory tak direset, jadi progres tak hilang.
            looping = dup_hits >= config.MAX_DUPLICATE_TOOL_CALLS
            weak = weak_hits >= 2
            if not force_final and (looping or weak):
                reason = "terdeteksi mengulang langkah" if looping else \
                         "model lemah memanggil tool"
                changed = self._escalate(reason)
                if changed:
                    if on_notice:
                        on_notice(changed)
                    # Reset penghitung loop supaya otak baru dapat kesempatan bersih.
                    dup_hits = 0
                    weak_hits = 0
                    seen_tools.clear()
                    self.memory.add(
                        {
                            "role": "user",
                            "content": (
                                f"[SISTEM] Kamu tampak {reason}. Aku sudah menaikkan "
                                f"kemampuanmu ({changed}). Konteks & progres di atas "
                                f"TETAP berlaku — JANGAN ulangi dari nol. Lihat apa "
                                f"yang SUDAH selesai, lalu lanjutkan langkah "
                                f"BERIKUTNYA sampai tuntas."
                            ),
                        }
                    )
                    continue
            # Sudah tak bisa naik-kelas lagi (atau anggaran tool habis) -> minta
            # menyimpulkan dengan jujur.
            if not force_final and (
                looping or weak or total_calls >= config.MAX_TOOL_CALLS
            ):
                force_final = True
                self.memory.add(
                    {
                        "role": "user",
                        "content": (
                            "[SISTEM] Kamu tampak mengulang langkah / terlalu banyak "
                            "memakai tool. STOP memakai tool dan berikan jawaban akhir "
                            "dalam teks biasa. JUJUR: jelaskan apa yang SUDAH selesai "
                            "dan apa yang BELUM. JANGAN mengaku tuntas kalau memang "
                            "belum — sebutkan langkah tersisa yang perlu dilakukan."
                        ),
                    }
                )

        fallback = (
            "Maaf, proses berhenti karena mencapai batas iterasi tool. "
            "Coba persempit permintaanmu."
        )
        self.memory.add_assistant_text(fallback)
        self._persist()
        return fallback

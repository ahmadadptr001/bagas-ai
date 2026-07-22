"""Antarmuka CLI bagas-ai (sinkron & bersih).

Desain: rich memegang terminal penuh (warna/emoji/panel mulus, tanpa bocor kode
ANSI). Animasi loading realtime (spinner + token + waktu) NEMPEL inline pada tiap
task via rich Live. Input pakai prompt_toolkit (hanya saat idle) supaya
Ctrl+Backspace bisa hapus per-kata. Tanpa antrean — satu tugas satu waktu.
"""
from __future__ import annotations

import difflib
import re
import sys
import threading
import time

try:  # keyboard non-blocking (Windows) untuk toggle expand inline (Ctrl+R)
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows
    _msvcrt = None

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from InquirerPy import inquirer  # noqa: E402
from InquirerPy.base.control import Choice  # noqa: E402
from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.completion import Completer, Completion  # noqa: E402
from prompt_toolkit.formatted_text import HTML  # noqa: E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: E402
from prompt_toolkit.patch_stdout import patch_stdout  # noqa: E402
from prompt_toolkit.styles import Style as PTStyle  # noqa: E402
from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.markup import escape as _esc  # noqa: E402
from rich.padding import Padding  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402
from rich.theme import Theme  # noqa: E402

try:
    from pyfiglet import Figlet  # noqa: E402
except Exception:  # pragma: no cover
    Figlet = None  # type: ignore

from .. import config, interaction, llm, longmem, models, osinfo, prefs, projectindex, scripts, telegram_perms, updater, workspace  # noqa: E402
from .. import session as session_mod  # noqa: E402
from ..core import Agent  # noqa: E402
from ..session import Session  # noqa: E402

# Tema Markdown selaras palet "catppuccin" agar jawaban AI (heading, list, kutipan,
# kode, tautan) serasi dengan seluruh UI — bukan warna default rich yang kontras.
_MD_THEME = Theme({
    "markdown.h1": "bold #cba6f7",
    "markdown.h1.border": "#cba6f7",
    "markdown.h2": "bold #89b4fa",
    "markdown.h3": "bold #94e2d5",
    "markdown.h4": "bold #a6e3a1",
    "markdown.h5": "bold #f9e2af",
    "markdown.h6": "bold #fab387",
    "markdown.item.bullet": "bold #cba6f7",
    "markdown.item.number": "bold #89b4fa",
    "markdown.code": "#f5c2e7 on #313244",       # `inline code`
    "markdown.link": "#89b4fa underline",
    "markdown.link_url": "dim #74c7ec",
    "markdown.block_quote": "italic #f9e2af",
    "markdown.block_quote_border": "#585b70",
    "markdown.hr": "#45475a",
    "markdown.strong": "bold #f5e0dc",
    "markdown.emph": "italic #cdd6f4",
    "markdown.text": "#cdd6f4",
})
console = Console(theme=_MD_THEME)  # auto-detect VT -> warna/emoji mulus

# Tema penyorotan sintaks blok kode ```lang``` — 'dracula' paling dekat dengan
# nuansa catppuccin (pastel ungu/pink/hijau). Fallback aman bila tak tersedia.
try:  # pragma: no cover - bergantung versi pygments
    from pygments.styles import get_style_by_name as _gsbn
    _gsbn("dracula")
    _CODE_THEME = "dracula"
except Exception:  # pragma: no cover
    _CODE_THEME = "monokai"


def _md(text: str) -> Markdown:
    """Markdown bertema catppuccin (inline code pakai style `markdown.code`,
    blok kode ```lang``` disorot tema `dracula`).

    Escape ANSI dibuang DI SINI karena inilah satu-satunya pintu yang dilewati
    SEMUA teks model menuju layar: narasi antar-langkah dan jawaban akhir. Dulu
    penyaringnya hanya dipasang di region live, padahal jalur ini justru lebih
    berbahaya — region live ditimpa tiap frame, sedangkan riwayat terminal
    PERMANEN: satu \x1b[2J dari log yang disalin model menghapus scrollback
    giliran sebelumnya, dan \x1b[31m tanpa reset mewarnai semua teks sesudahnya
    sampai terminal di-reset manual."""
    return Markdown(_bersih_kendali(text), code_theme=_CODE_THEME)

# Padding tepi supaya konten tidak mepet ke pinggir terminal (kiri/kanan/bawah).
_LPAD = 2

# Perintah slash + deskripsi singkat (dipakai autocomplete "/..." & bantuan).
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("menu", "menu interaktif"),
    ("model", "pilih model + saran"),
    ("effort", "mode berpikir"),
    ("add-dir", "tambah folder konteks"),
    ("dirs", "folder konteks aktif"),
    ("rm-dir", "hapus folder konteks"),
    ("new", "mulai sesi baru"),
    ("delete", "hapus sesi"),
    ("reset", "kosongkan riwayat"),
    ("clear", "bersihkan layar"),
    ("web", "kelola sesi AI web (hapus chat menumpuk / logout)"),
    ("bot", "hidup/matikan bot Telegram di sesi ini"),
    ("permissions-bot", "atur izin siapa yang boleh kontrol via Telegram"),
    ("review", "cari bug & kesalahan sistem di seluruh proyek"),
    ("scan", "pindai ulang & segarkan peta proyek"),
    ("live", "hidup/matikan tampilan interaktif (Ctrl+R buka/tutup live)"),
    ("expand", "cetak ulang hasil penuh · /expand N untuk satu langkah"),
    ("memory", "memory jangka panjang"),
    ("scripts", "script memory"),
    ("models", "daftar semua model"),
    ("update", "cek pembaruan"),
    ("help", "bantuan"),
    ("exit", "keluar"),
]

# Instruksi untuk /review — audit bug & kesalahan sistem menyeluruh.
_REVIEW_PROMPT = (
    "Lakukan REVIEW/AUDIT menyeluruh pada proyek ini KHUSUS untuk menemukan BUG dan "
    "KESALAHAN SISTEM. Manfaatkan Peta Proyek yang sudah kamu punya untuk menentukan "
    "file paling berisiko lebih dulu, lalu baca file-file itu seperlunya (jangan baca "
    "semua kalau tak perlu). Telusuri terutama:\n"
    "- Bug logika & kasus tepi: off-by-one, None/null/undefined, pembagian nol, "
    "kondisi salah, loop tak berhenti, race condition, error/exception tak tertangani.\n"
    "- Kesalahan sistem/konfigurasi: import/modul salah, path/berkas salah, dependency "
    "hilang atau versi bentrok, variabel env yang belum diset, entry-point rusak.\n"
    "- Referensi rusak: fungsi/variabel/atribut yang dipanggil tapi tak ada, salah tipe, "
    "signature tak cocok.\n"
    "- Keamanan: kredensial/secret bocor, injeksi (SQL/shell), path traversal, input "
    "tak divalidasi.\n"
    "Untuk SETIAP temuan sebutkan: `file:baris`, tingkat keparahan (KRITIS/TINGGI/"
    "SEDANG/RENDAH), penjelasan singkat kenapa itu bug, dan saran perbaikan. URUTKAN "
    "dari paling parah. Kalau tak ada masalah serius, katakan terus terang. PENTING: "
    "ini fase pelaporan — JANGAN mengubah kode apa pun kecuali aku memintanya."
)


class SlashCompleter(Completer):
    """Sugesti perintah saat mengetik '/': '/ef' -> '/effort', dst.

    Hanya aktif untuk token perintah di awal baris (sebelum spasi), jadi tidak
    mengganggu saat mengetik pesan biasa atau argumen (mis. '/model llama').
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Hanya untuk perintah slash di awal baris & sebelum ada spasi/argumen.
        if not text.startswith("/") or " " in text:
            return
        prefix = text[1:].lower()
        for name, desc in SLASH_COMMANDS:
            if name.startswith(prefix):
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display=HTML(f"<b>/{name}</b>"),
                    display_meta=desc,
                )


def pout(renderable, *, bottom: int = 1) -> None:
    """Cetak renderable dengan padding kiri/kanan (+bawah) yang konsisten."""
    console.print(Padding(renderable, (0, _LPAD, bottom, _LPAD)))


def _update_notice() -> None:
    """Notifikasi ringkas bila versi usang (dari cache), lalu cek ulang di latar.

    Non-blocking: notifikasi diambil dari hasil cek TERAKHIR yang tersimpan,
    sedangkan pengecekan baru ke GitHub berjalan di latar untuk startup berikut.
    """
    try:
        cache = updater.read_cache()
        if cache.get("status") == "update_available":
            # Sampai sini artinya pemasangan paksa saat startup GAGAL
            # (jaringan/git) — beri jalan manual.
            n = cache.get("behind", "?")
            local, remote = cache.get("local", ""), cache.get("remote", "")
            ver = f" ({local} → {remote})" if local and remote else ""
            pout(
                f"[#f9e2af]⬆ Pembaruan bagas-ai tersedia[/] "
                f"[dim]— {n} commit lebih baru{ver} (pemasangan otomatis "
                f"gagal).[/dim]  Ketik [#94e2d5]/update[/] untuk mencoba lagi.",
                bottom=0,
            )
        # Segarkan cache di thread latar; startup berikutnya otomatis
        # MEMASANG pembaruan yang ditemukan (paksa, tanpa tanya).
        updater.background_refresh(min_interval=1800)
    except Exception:
        pass

# Gradasi ungu -> biru (magenta neon) untuk teks shadow.
_GRAD = ["#f0abfc", "#e879f9", "#c084fc", "#a855f7", "#7c3aed", "#4f46e5", "#2563eb"]


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _fmt_elapsed(sec: float) -> str:
    """Format durasi bertingkat: <60s -> '12.3s', lalu 'm s', 'h m', 'd h'."""
    if sec < 60:
        return f"{sec:.1f}s"
    total = int(sec)
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def _web_phase(msg: str) -> str:
    """Ringkas status connector web jadi KATA FASE pendek untuk baris status,
    supaya tampilannya seragam antar layanan web."""
    m = (msg or "").lower()
    if "menjawab" in m:
        return "menjawab"
    if "berpikir" in m:
        return "berpikir"
    if "login" in m or "sign-in" in m or "sign in" in m:
        return "menunggu login di jendela Chrome"
    if "mengetik" in m:
        return "mengirim pesan"
    # Sengaja hanya menangkap frasa UMUM-nya. Status yang lebih spesifik
    # ("menyiapkan jendela Chrome", "membuka percakapan baru", "menunggu giliran
    # browser sebelumnya selesai") dibiarkan lewat apa adanya lewat baris
    # terakhir — justru ketepatan itulah gunanya, supaya jeda panjang di fase
    # browser punya penjelasan alih-alih terlihat diam tanpa sebab.
    if "menyiapkan sesi" in m or "menghubungkan" in m:
        return "menyiapkan sesi web"
    return (msg or "").strip().rstrip("…") or "bekerja"


# Escape ANSI (CSI/OSC/dua-karakter) + karakter kendali lain. Keluaran tool nyata
# penuh dengannya — pip, npm, git, dan hampir semua CLI modern mewarnai
# keluarannya, dan model web pun kadang menyalin log berwarna ke jawabannya.
# Rich memperlakukan isi Text sebagai teks BIASA: byte ESC diteruskan apa adanya
# ke terminal, lalu terminal mengeksekusinya. Akibatnya warna region live berubah
# sendiri, kursor melompat, bahkan layar terhapus — persis "tampilan kacau" yang
# sulit ditelusuri karena sumbernya keluaran perintah, bukan kode UI.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI (warna, gerak kursor, hapus layar)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (judul jendela, hyperlink)
    r"|\x1b[@-Z\\-_]"                      # escape dua karakter
)
_KENDALI_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _bersih_kendali(s: str) -> str:
    """Buang escape ANSI & karakter kendali; CR jadi baris baru, TAB jadi spasi.

    CR ditangani TERSENDIRI, bukan lewat _KENDALI_RE: \\x0d jatuh persis di celah
    antara \\x0c dan \\x0e sehingga dulu lolos diam-diam. Ia karakter kendali
    sungguhan — memindahkan kursor ke kolom 0 — dan seluruh progress bar
    pip/npm/docker dibangun darinya, jadi ini bukan kasus langka. Diubah jadi
    baris baru (bukan dibuang) supaya tiap pembaruan progres tetap terbaca
    sebagai barisnya sendiri alih-alih menyambung jadi satu baris panjang.

    TAB ikut diganti karena lebar tampilannya ditentukan terminal (biasanya 8),
    sehingga perhitungan lebar Rich meleset dan kolom jadi tak sejajar."""
    if not s:
        return s
    s = _ANSI_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\t", "    ")
    return _KENDALI_RE.sub("", s)


def _oneline(t: Text) -> Text:
    """Baris untuk region live: JANGAN pernah wrap — wrap membuat tinggi region
    berubah antar-frame sehingga rich.Live menggambar ulang kacau (kedip/baris
    hantu, terutama di terminal sempit). Kelebihan dipotong dengan elipsis."""
    t.no_wrap = True
    t.overflow = "ellipsis"
    return t


# Warna gaya editor (GitHub-like): teks terang di atas bg gelap hijau/merah.
_ADD = "#c9f5cf on #123d1c"
_DEL = "#f5c9c9 on #3d1212"
_CTX = "grey50"
_GUT_A = "#5bd66f on #0d2a14"
_GUT_D = "#e06b6b on #2a0d0d"


def _row(lineno: str, sign: str, text: str, style: str) -> None:
    """Cetak satu baris gaya editor '123 + kode' dengan bg + margin tepi."""
    inner = max(20, min(console.width - 2 * _LPAD, 108))
    line = Text(" " * _LPAD)  # margin kiri tanpa background
    line.append(f" {lineno:>4} {sign} ", style=style)
    body = f"{text}".replace("\t", "    ")
    line.append(body, style=style)
    # Baris panjang DIPOTONG (bukan wrap): wrap membuat background hijau/merah
    # meluber tak beraturan ke baris berikutnya.
    line.truncate(_LPAD + inner, overflow="ellipsis")
    pad = (_LPAD + inner) - line.cell_len  # isi bg sampai batas kanan
    if pad > 0:
        line.append(" " * pad, style=style)
    line.no_wrap = True
    console.print(line)


# Tool yang MENGUBAH ISI file -> perubahannya ditampilkan sebagai diff berwarna
# SEBELUM file disentuh. Dulu hanya write_file, sehingga perubahan lewat
# edit_file/append_file lolos tanpa bisa ditinjau — padahal justru edit_file yang
# dianjurkan untuk file besar, jadi tanpa ini kebanyakan perubahan jadi tak terlihat.
_TOOL_DIFF = ("write_file", "edit_file", "append_file")


def _isi_sebelum_sesudah(name: str, path: str, args: dict):
    """(isi_lama, isi_baru, file_sudah_ada) untuk merender diff sebuah langkah.

    Isi barunya DIHITUNG dari argumen — untuk edit_file/append_file hasil akhir
    tak ada di args, jadi harus disimulasikan persis seperti yang akan dilakukan
    tool-nya (lihat tools/files.py)."""
    full = config.PROJECT_ROOT / path
    exists = full.exists()
    old = full.read_text(encoding="utf-8", errors="replace") if exists else ""
    a = args if isinstance(args, dict) else {}
    if name == "write_file":
        return old, a.get("content", "") or "", exists
    if name == "append_file":
        return old, old + (a.get("content", "") or ""), exists
    if name == "edit_file":
        lama = a.get("old_text", "") or ""
        baru = a.get("new_text", "") or ""
        if not lama or lama not in old:
            # Tool-nya akan menolak; jangan tampilkan diff yang menyesatkan.
            return old, old, exists
        jml = a.get("count", 1)
        try:
            jml = int(jml)
        except (TypeError, ValueError):
            jml = 1
        n = old.count(lama) if jml == -1 else jml
        return old, old.replace(lama, baru, n), exists
    return old, old, exists


def _print_diff(path: str, old: str, new: str, is_new: bool, limit: int = 200) -> None:
    """Tampilan editor: header status + line-numbered diff (bg hijau/merah)."""
    icon, label = ("✨", "dibuat") if is_new else ("📝", "diubah")
    console.print(f"\n  [bold]{icon} [cyan]{path}[/cyan][/bold] [dim]({label})[/dim]")
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     lineterm="", n=2))
    body = diff[2:] if len(diff) >= 2 and diff[0].startswith("---") else diff
    old_ln = new_ln = 0
    shown = 0
    for line in body:
        if shown >= limit:
            console.print("  [dim]... (diff dipotong)[/dim]")
            break
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
            if shown:
                console.print("  [dim]⋮[/dim]")
            continue
        tag, content = line[:1], line[1:]
        if tag == "+":
            _row(str(new_ln), "+", content, _ADD)
            new_ln += 1
        elif tag == "-":
            _row(str(old_ln), "-", content, _DEL)
            old_ln += 1
        else:
            _row(str(new_ln), " ", content, _CTX)
            old_ln += 1
            new_ln += 1
        shown += 1


def _print_delete(path: str, content: str, limit: int = 80) -> None:
    console.print(f"\n  [bold]🗑 [cyan]{path}[/cyan][/bold] [dim](dihapus)[/dim]")
    for i, line in enumerate(content.splitlines(), start=1):
        if i > limit:
            console.print("  [dim]... (dipotong)[/dim]")
            break
        _row(str(i), "-", line, _DEL)


def show_logo() -> None:
    """Wordmark modern: figlet bergradasi + garis aksen gradasi + tagline bersih
    (tanpa doodle ASCII)."""
    m = " " * _LPAD  # indent kiri agar tidak mepet
    console.print()
    if Figlet is not None:
        try:
            art = Figlet(font="ansi_shadow").renderText("bagas-ai")
            lines = [ln for ln in art.split("\n") if ln.strip()]
        except Exception:
            lines = ["b a g a s - a i"]
    else:
        lines = ["b a g a s - a i"]
    width = max((len(ln) for ln in lines), default=24)
    # Terminal lebih sempit dari seni figlet? wrap bikin logo jadi sampah —
    # jatuh ke wordmark teks biasa yang selalu muat.
    if width + _LPAD > console.width:
        lines = ["b a g a s - a i"]
        width = len(lines[0])
    for i, ln in enumerate(lines):
        t = Text(m + ln, style=f"bold {_GRAD[min(i, len(_GRAD) - 1)]}")
        t.no_wrap = True
        console.print(t)
    # Garis aksen gradasi di bawah wordmark (aksen modern pengganti doodle).
    seg = max(12, min(width, 56))
    per = max(1, seg // len(_GRAD))
    bar = Text(m)
    for col in _GRAD:
        bar.append("━" * per, style=col)
    console.print(bar)
    sub = Text(m)
    sub.append("AI agent serbaguna", style="bold #cdd6f4")
    sub.append("  ·  terminal · telegram · multitasking", style="dim")
    console.print(_oneline(sub))


# ---------------------------------------------------------------------------
# Indikator "berpikir" realtime (rich Live) — nempel inline pada task
# ---------------------------------------------------------------------------
# Kata FASE per-tool: bikin indikator status menjelaskan APA yang sedang
# dikerjakan (bukan cuma "berpikir"). Tanpa tool aktif -> "berpikir".
_PHASE = {
    "write_file": "menulis",
    "delete_file": "menghapus",
    "read_file": "membaca",
    "list_dir": "menelusuri",
    "web_search": "mencari",
    "run_command": "menjalankan",
    "run_python": "menjalankan",
    "run_script": "menjalankan",
    "save_script": "menyimpan",
    "remember": "mengingat",
}


class Status:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.start = time.time()
        self.tool: str | None = None
        self.phase = "berpikir"
        self.step = 0
        self.disp = 0.0
        self.retry_until = 0.0
        self.retry_msg = ""
        self.cancelling = False

    def note_retry(self, wait: float, msg: str) -> None:
        """Tandai bahwa bagas-ai sedang menunggu rate limit lalu melanjutkan."""
        self.retry_until = time.time() + wait
        self.retry_msg = msg

    def note_cancelling(self) -> None:
        """Tandai bahwa pembatalan (Ctrl+C) sedang diproses di latar belakang."""
        self.cancelling = True

    def note_step(self, name: str) -> None:
        """Mulai satu langkah tool: set fase sesuai jenis tool & naikkan nomor."""
        self.tool = name
        self.phase = _PHASE.get(name, "bekerja")
        self.step += 1

    def note_thinking(self) -> None:
        """Kembali ke fase 'berpikir' (tak ada tool aktif)."""
        self.tool = None
        self.phase = "berpikir"

    def note_phase(self, text: str) -> None:
        """Set fase status langsung (dipakai connector web: 'menjawab', dsb)."""
        if self.tool is None and text:
            self.phase = text

    def __rich__(self) -> Text:
        el = time.time() - self.start
        now = time.time()
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]

        dot = "[#45475a]•[/]"

        # Mode membatalkan: Ctrl+C ditekan, menunggu langkah aman berhenti.
        if self.cancelling:
            t = Text()
            t.append(f"  {frame} ", style="bold #f38ba8")
            t.append("membatalkan — menunggu langkah aman berhenti", style="#f38ba8")
            t.append("     Ctrl+C lagi = paksa", style="dim italic")
            return _oneline(t)

        # Mode menunggu rate limit: tampilkan hitung mundur + jaminan lanjut.
        if now < self.retry_until:
            left = self.retry_until - now
            t = Text()
            t.append(f"  {frame} ", style="bold #f9e2af")
            t.append("layanan sibuk — menunggu lalu melanjutkan", style="#f9e2af")
            t.append(f"  {left:.0f}s", style="bold #fab387")
            if self.retry_msg:
                t.append(f"  ·  {self.retry_msg}", style="dim #f9e2af")
            t.append("     Ctrl+C batal", style="dim italic")
            return _oneline(t)

        target = float(self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30  # easing -> angka mengalir
        if abs(target - self.disp) < 1:
            self.disp = target
        t = Text()
        t.append(f"  {frame} ", style="bold #cba6f7")
        t.append(self.phase, style="#cba6f7")
        t.append(f"  {_fmt_elapsed(el)}", style="bold #89b4fa")
        t.append("   ")
        t.append_text(Text.from_markup(dot))
        t.append(f"  ⚡ {_fmt(int(self.disp))}", style="#f9e2af")
        t.append(" token", style="dim")
        if self.tool:
            t.append("   ")
            t.append_text(Text.from_markup(dot))
            t.append(f"  🔧 {self.tool}", style="#f5c2e7")
        if self.step:
            t.append("   ")
            t.append_text(Text.from_markup(dot))
            t.append(f"  langkah {self.step}", style="dim #94e2d5")
        t.append("     Ctrl+C batal", style="dim italic")
        return _oneline(t)


# Tips singkat yang BERGANTIAN muncul di bawah status selama AI bekerja
# (seperti Claude CLI) — biar waktu menunggu tetap informatif.
_TIPS = (
    "/model mengganti otak AI kapan saja — preferensimu tersimpan",
    "/effort mengatur kedalaman berpikir: langsung → mendalam",
    "/bot menyalakan kontrol lewat Telegram — perintah dari HP",
    "/scan menyegarkan peta proyek · /review memburu bug proyek",
    "perintah menetap (mis. npm run dev) otomatis jalan di latar",
    "Ctrl+C sekali = batalkan dengan aman; ketik 'lanjutkan' untuk meneruskan",
    "bagas-ai --resume melanjutkan sesi terakhirmu di folder ini",
    "/expand N membuka hasil lengkap sebuah langkah setelah selesai",
    "kalau model macet/ngeloop, bagas-ai membatalkan & naik kelas sendiri",
    "/memory menyimpan fakta jangka panjang lintas sesi",
    "/live mengalihkan tampilan inline ↔ klasik bila terminal bermasalah",
)


class TurnView:
    """Tampilan SATU GILIRAN yang hidup INLINE (rich.Live, TANPA layar-penuh),
    persis alur terminal biasa. Seluruh giliran (narasi, langkah, jawaban)
    dirender di region yang terus diperbarui; hasil tiap langkah bisa DIBUKA/
    ditutup secara realtime dengan Ctrl+R (seperti Claude). Saat giliran selesai,
    region ini 'membeku' jadi bagian riwayat terminal (transient=False)."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    # Region live SENGAJA dijaga PENDEK: kalau seluruh giliran (narasi + banyak
    # langkah + jawaban panjang) ditaruh di region live, tingginya melebihi layar
    # -> rich.Live menggambar ulang semuanya tiap frame => KEDIP & scroll rusak
    # (terasa saat sesi/jawaban besar). Item lama "dibekukan" ke riwayat terminal.
    MAX_LIVE_STEPS = 5

    def __init__(self, agent: Agent, commit=None) -> None:
        self.agent = agent
        self.commit = commit          # commit(renderables) -> cetak ke riwayat
        self.start = time.time()
        self._lock = threading.Lock()
        self.items: list[tuple[str, object]] = []  # ("step",rec) yang masih live
        self.all_steps: list[dict] = []            # SEMUA langkah (untuk ringkasan)
        self._said = False                         # header "🤖" sekali per giliran
        self.answer: str | None = None
        self.expanded = False          # Ctrl+R toggle: buka/tutup hasil semua langkah
        self._clickable = False        # True bila mouse aktif -> tampilkan petunjuk klik
        self.done = False
        self.cancelling = False
        self.retry_until = 0.0
        self.retry_msg = ""
        self.phase = "berpikir"
        self.tool: str | None = None
        self.disp = 0.0
        self.phase_since = self.start   # kapan fase SEKARANG mulai (untuk ETA)
        # ETA connector web belajar dari riwayat waktu turn pengguna sendiri —
        # jujur: deskripsi masa lalu, bukan janji. None bila bukan connector web
        # ATAU sampel riwayat belum cukup (lalu UI sengaja tak menampilkan ETA).
        self._web_service = getattr(agent.model_spec, "connector", "") or ""
        self._web_med = None
        if self._web_service:
            try:
                from .. import web_timing
                self._web_med = web_timing.medians(self._web_service)
            except Exception:  # noqa: BLE001
                self._web_med = None
        # Jumlah karakter jawaban yang SUDAH mengalir di giliran ini. Inilah
        # sinyal yang membuat ETA benar-benar hidup: bukan menebak durasi dari
        # median, tapi mengukur kemajuan nyata terhadap perkiraan panjang akhir.
        self._web_chars = 0
        # Perkiraan PERTAMA yang ditampilkan (detik, total durasi menjawab) —
        # dipakai menilai akurasi sesudah giliran selesai.
        self._web_pred_first = 0.0
        # Ekor jawaban yang sedang ditulis, untuk pratinjau bergulir di region live.
        self._stream = ""

    # --- mutasi (dipanggil dari worker) ---
    def add_narasi(self, text: str) -> None:
        """Narasi langsung DIBEKUKAN ke riwayat (bisa panjang) -> region live tetap
        pendek & tak berkedip."""
        if not (text and text.strip()):
            return
        if self.commit:
            out = []
            if not self._said:
                out.append(Padding(Text("🤖 bagas-ai", style="bold #89b4fa"),
                                   (1, 0, 0, 2)))
                self._said = True
            out.append(Padding(_md(text.strip()), (0, 3, 1, 3)))
            self.commit(out)

    def _overflow(self) -> list:
        """Langkah lama yang keluar dari jatah region live (untuk dibekukan)."""
        out = []
        with self._lock:
            while len(self.items) > self.MAX_LIVE_STEPS:
                out.append(self.items.pop(0))
        return out

    def start_step(self, n: int, name: str, label: str) -> dict:
        rec = {"n": n, "name": name, "label": label, "result": "",
               "failed": False, "running": True, "expanded": False}
        with self._lock:
            self.items.append(("step", rec))
            self.all_steps.append(rec)
        self.tool = name
        self.phase = _PHASE.get(name, "bekerja")
        self.phase_since = time.time()
        # Bekukan langkah lama ke riwayat agar region live tak tumbuh tanpa batas.
        for kind, val in self._overflow():
            if kind == "step" and self.commit:
                self.commit(self._render_step(val))
        return rec

    def end_step(self, rec: dict, result: str, failed: bool) -> None:
        rec["result"] = result or ""
        rec["failed"] = failed
        rec["running"] = False
        # Pra-hitung baris hasil SEKALI di sini — _render_step dipanggil ~12x/dtk
        # per frame; tanpa cache ini regex+splitlines diulang terus tiap frame.
        text = re.sub(r"^exit_code=\S+\n?", "", (result or "").strip())
        # Dibersihkan DI SINI, di jalur pra-hitung yang sebenarnya dipakai.
        # Membersihkan hanya di _render_step tidak ada gunanya: cabang itu cuma
        # jalan bila `_lines` belum ada, sedangkan tiap langkah yang selesai
        # selalu melewati baris ini lebih dulu.
        rec["_lines"] = _bersih_kendali(text).splitlines()
        rec["_nlines"] = sum(1 for ln in rec["_lines"] if ln.strip())
        self.tool = None
        self.phase = "berpikir"
        self.phase_since = time.time()

    def note_retry(self, wait: float, msg: str) -> None:
        self.retry_until = time.time() + wait
        self.retry_msg = msg

    def note_phase(self, text: str) -> None:
        """Set fase status langsung (dipakai connector web: 'menjawab', dsb).
        Diabaikan saat ada tool berjalan supaya fase tool tak tertimpa."""
        if self.tool is None and text and text != self.phase:
            # Giliran connector bisa berisi BEBERAPA fase 'menjawab' (jawaban
            # awal, lalu balasan atas hasil tool). Tiap kali fase menjawab
            # dimulai lagi, hitungan karakter & perkiraan pertama disetel ulang
            # supaya ETA menghitung jawaban yang SEKARANG, bukan akumulasi.
            if text == "menjawab":
                self._web_chars = 0
                self._web_pred_first = 0.0
                self._stream = ""
            self.phase = text
            self.phase_since = time.time()   # patok waktu fase baru (untuk ETA)

    # Berapa baris jawaban yang sedang ditulis ditampilkan di region live.
    # Kecil DENGAN SENGAJA: region live harus jauh lebih pendek dari layar, kalau
    # tidak rich.Live tak bisa menghapus baris yang sudah lewat atas layar dan
    # muncul baris hantu/dobel.
    PREVIEW_LINES = 6
    # Ekor teks yang disimpan untuk pratinjau. Tak perlu menyimpan seluruh
    # jawaban (bisa ratusan ribu karakter) — yang ditampilkan hanya ekornya.
    _PREVIEW_KEEP = 4000

    def note_stream(self, delta: str) -> None:
        """Potongan jawaban baru saja mengalir dari situs (dari on_token).

        Dua gunanya: menghitung kemajuan untuk ETA, dan menyimpan EKOR teks
        supaya pengguna bisa melihat jawaban terbentuk saat itu juga alih-alih
        menatap spinner sampai jawaban selesai sepenuhnya."""
        if not delta:
            return
        self._web_chars += len(delta)
        # Dibersihkan SEBELUM masuk buffer: jawaban model bisa memuat log
        # berwarna yang disalin apa adanya, dan byte ESC di region live akan
        # dieksekusi terminal (warna berubah sendiri, kursor melompat).
        buf = self._stream + _bersih_kendali(delta)
        # Dipotong dari depan: yang ditonton pengguna selalu bagian terbaru.
        self._stream = buf[-self._PREVIEW_KEEP:]

    def _preview_rows(self) -> list:
        """Beberapa baris TERAKHIR jawaban yang sedang ditulis, untuk region live.

        Jawaban lengkap TIDAK dirender di sini melainkan dicetak ke riwayat
        sesudah giliran selesai — inilah yang membuat jawaban sepanjang apa pun
        tak merusak scroll. Yang tampil saat berjalan cuma jendela bergulir
        setinggi PREVIEW_LINES, jadi tinggi region live tetap tetap."""
        teks = self._stream
        if not teks.strip():
            return []
        # Perhitungan jatah baris sendiri DIHAPUS dari sini: ia memakai jumlah
        # BLOK sebagai pengganti jumlah BARIS — kekeliruan yang sama persis
        # dengan yang diperbaiki _muat_layar — dan menjadikan dua sumber
        # kebenaran untuk satu invarian, sehingga menyetel PREVIEW_LINES terasa
        # tak berpengaruh. Kini cukup satu penjaga: _muat_layar yang MENGUKUR.
        jatah = self.PREVIEW_LINES
        baris = teks.split("\n")
        # Baris kosong di ekor bikin pratinjau tampak "melompat" tanpa isi.
        while baris and not baris[-1].strip():
            baris.pop()
        if not baris:
            return []
        rows = []
        for b in baris[-jatah:]:
            rows.append(_oneline(Text("  │ " + b.rstrip(), style="#7f849c")))
        return rows

    def toggle(self) -> None:
        """Ctrl+R (cadangan): buka/tutup SEMUA hasil sekaligus."""
        self.expanded = not self.expanded

    def toggle_step(self, n: int) -> bool:
        """Klik: buka/tutup hasil langkah #n saja. True bila langkah ada."""
        with self._lock:
            for kind, val in self.items:
                if kind == "step" and val["n"] == n and not val["running"]:
                    val["expanded"] = not val["expanded"]
                    return True
        return False

    # --- render satu langkah ---
    def _render_step(self, rec: dict, live_cap: int = 0) -> list:
        """`live_cap` > 0 = sedang dirender di region LIVE: batasi jumlah baris
        hasil ter-expand agar total region < tinggi layar (lihat _blocks)."""
        n = rec["n"]
        label = rec["label"] or ""
        if len(label) > 64:
            label = label[:61] + "…"
        running = rec["running"]
        failed = rec["failed"]
        if running:
            frame = self.FRAMES[int((time.time() - self.start) * 10) % len(self.FRAMES)]
            icon = f"[#f9e2af]{frame}[/]"
        else:
            icon = "[#f38ba8]✗[/]" if failed else "[#a6e3a1]✓[/]"
        phase = _PHASE.get(rec["name"], "langkah")
        head = _oneline(Text.from_markup(
            f"  {icon} [#cdd6f4]{phase}[/]  [white]{_esc(label)}[/]"
            f"   [dim #94e2d5]#{n}[/]"
        ))
        out = [head]
        # Pakai baris pra-hitung dari end_step (fallback hitung bila belum ada).
        lines = rec.get("_lines")
        if lines is None:
            text = re.sub(r"^exit_code=\S+\n?", "", (rec["result"] or "").strip())
            # Keluaran perintah nyaris selalu berwarna (pip/npm/git) -> escape-nya
            # WAJIB dibuang sebelum masuk region live, kalau tidak terminal ikut
            # mengeksekusinya dan tampilan berantakan.
            lines = _bersih_kendali(text).splitlines()
        nlines = rec.get("_nlines")
        if nlines is None:
            nlines = sum(1 for ln in lines if ln.strip())
        if running:
            out.append(Text("     menjalankan…", style="italic #6c7086"))
        elif not lines:
            pass
        elif rec["expanded"] or self.expanded:
            cap = live_cap if live_cap > 0 else 40
            shown = lines[:cap]
            body = Text("\n".join("     " + ln for ln in shown),
                        style="#f5c9c9" if failed else "#a6adc8")
            # Tiap baris hasil juga anti-wrap: baris super panjang (log/minified)
            # yang wrap membuat tinggi region berubah -> kedip/baris hantu.
            out.append(_oneline(body))
            if len(lines) > cap:
                out.append(_oneline(Text(
                    f"     … {len(lines) - cap} baris lagi (/expand {n})",
                    style="dim")))
        else:
            unit = "hasil" if rec["name"] == "web_search" else "baris"
            tag = "[#f38ba8]gagal[/] · " if failed else ""
            out.append(_oneline(Text.from_markup(
                f"     [dim]{tag}{nlines} {unit}[/]")))
        return out

    def _blocks(self) -> list:
        """Urutan (tag, renderable) untuk render & pemetaan-klik. tag =
        ('step', n) bila baris itu milik langkah #n, else ('other', None).

        HANYA berisi langkah yang masih 'live' (maks. MAX_LIVE_STEPS) + footer,
        supaya region live PENDEK -> tak berkedip & terminal tetap bisa di-scroll.
        Narasi & jawaban dibekukan ke riwayat, bukan dirender di sini."""
        blocks: list = []
        with self._lock:
            items = list(self.items)
        # Region live WAJIB lebih pendek dari layar: bila lebih tinggi, rich.Live
        # tak bisa menghapus baris yang sudah lewat atas layar -> baris hantu/
        # dobel. Saat ada langkah ter-expand, jatah barisnya dihitung dari tinggi
        # terminal dan dibagi rata antar langkah yang terbuka.
        n_exp = sum(1 for kind, val in items
                    if kind == "step" and not val["running"]
                    and (val["expanded"] or self.expanded))
        live_cap = 0
        if n_exp:
            try:
                avail = console.size.height
            except Exception:  # noqa: BLE001
                avail = 30
            # Lantai `max(3, ...)` DIBUANG: itulah yang membuat jatah tetap
            # dilanggar di layar sempit (terukur 26 baris di terminal 24).
            # Dengan lantai 1, kelebihan tinggi ditebus dengan MEMANGKAS isi tiap
            # langkah — bukan dengan _muat_layar membuang langkah utuh, yang bagi
            # pengguna terlihat seperti langkah menghilang begitu saja setelah
            # sengaja dibuka lewat Ctrl+R.
            live_cap = max(1, (avail - 4 - 2 * len(items)) // n_exp)
        for kind, val in items:
            if kind == "step":
                for r in self._render_step(val, live_cap=live_cap):
                    blocks.append((("step", val["n"]), r))
        # Jawaban yang SEDANG ditulis: tampilkan ekornya supaya pengguna tak
        # menatap spinner tanpa isi sampai jawaban rampung. Dilewati saat ada
        # langkah ter-expand — keduanya berebut tinggi layar, dan isi langkah
        # yang sengaja dibuka pengguna lebih berhak.
        if not self.done and not n_exp:
            blocks.extend((("other", None), r) for r in self._preview_rows())
        # Footer (spinner/status) HANYA selama berjalan. Saat done, region yang
        # membeku ke riwayat cukup berisi langkah — tanpa "membatalkan…"/spinner
        # basi, dan ringkasan dicetak SETELAH jawaban (urutan benar).
        if not self.done:
            blocks.append((("other", None), self._footer()))
        return self._muat_layar(blocks)

    # Taksiran tinggi per blok bila pengukuran GAGAL. Sengaja terlalu besar:
    # menaksir kekecilan berarti penjaga tinggi mati diam-diam dan baris hantu
    # kembali tanpa satu pun tanda, sedangkan menaksir kebesaran cuma memangkas
    # lebih banyak dari perlu — arah kesalahan yang aman.
    _TAKSIR_BLOK = 4

    def _tinggi_blok(self, rend) -> int:
        """Tinggi NYATA (baris) satu renderable.

        Diukur, bukan ditaksir: satu blok TIDAK sama dengan satu baris — hasil
        langkah yang terbuka adalah SATU Text berisi banyak baris (terukur: 16
        blok = 26 baris). Menghitung blok itulah sebabnya penjaga tinggi yang
        lama meleset."""
        try:
            return len(console.render_lines(rend, console.options, pad=False))
        except Exception:  # noqa: BLE001 - konsol tiruan/uji
            return self._TAKSIR_BLOK

    def _muat_layar(self, blocks: list) -> list:
        """Pastikan region live MUAT di layar, buang langkah tertua bila perlu.

        Region live yang lebih tinggi dari layar membuat rich.Live tak bisa
        menghapus baris yang sudah lewat atas layar -> baris hantu, teks dobel,
        scroll rusak.

        Tinggi tiap blok diukur SEKALI lalu dijumlahkan; pembuangan cuma
        mengurangi jumlah itu. Versi sebelumnya merender ULANG seluruh region
        tiap putaran pembuangan — terukur 3 panggilan render_lines untuk satu
        _blocks(), padahal _blocks() dipanggil ~12x/detik oleh __rich__ DAN
        sekali lagi oleh pemetaan klik.

        Yang dibuang adalah blok TERTUA: langkah terbaru yang sedang berjalan
        itulah yang ditonton, dan riwayat penuhnya tetap ada di scrollback serta
        /expand. Footer (blok terakhir) tak pernah dibuang — di situlah status &
        spinner."""
        if not blocks:
            return blocks
        try:
            layar = console.size.height
        except Exception:  # noqa: BLE001
            return blocks
        maks = max(4, layar - 2)   # sisakan ruang untuk prompt & baris perintah
        tinggi = [self._tinggi_blok(r) for _, r in blocks]
        total = sum(tinggi)
        i = 0
        while i < len(blocks) - 1 and total > maks:
            total -= tinggi[i]
            i += 1
        return blocks[i:]

    def __rich__(self):
        return Group(*[r for _, r in self._blocks()])

    def _footer(self):
        el = time.time() - self.start
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]
        now = time.time()
        if self.cancelling:
            return _oneline(Text.from_markup(
                f"  [bold #f38ba8]{frame}[/] [#f38ba8]membatalkan — "
                f"menunggu langkah aman berhenti[/]   [dim italic]Ctrl+C lagi = paksa[/]"))
        if now < self.retry_until:
            left = self.retry_until - now
            return _oneline(Text.from_markup(
                f"  [bold #f9e2af]{frame}[/] [#f9e2af]layanan sibuk — menunggu lalu "
                f"melanjutkan[/] [bold #fab387]{left:.0f}s[/]   [dim italic]Ctrl+C batal[/]"))
        target = float(self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30
        if abs(target - self.disp) < 1:
            self.disp = target
        tok = _fmt(int(self.disp))
        if self.done:
            with self._lock:
                stps = list(self.all_steps)
            n_step = len(stps)
            if not n_step:
                return Text("")  # chat murni: tanpa footer
            n_file = sum(1 for s in stps if s["name"] in ("write_file", "delete_file"))
            n_fail = sum(1 for s in stps if s["failed"])
            seg = [f"{n_step} langkah"]
            if n_file:
                seg.append(f"{n_file} file")
            if n_fail:
                seg.append(f"[#f38ba8]{n_fail} gagal[/]")
            seg += [_fmt_elapsed(el), f"⚡ {tok} token"]
            return _oneline(Text.from_markup(
                "  [dim]" + " · ".join(seg) + "[/]   [dim]·[/]   "
                "[#94e2d5]/expand N[/][dim] lihat penuh[/]"))
        extra = f"   [dim]·[/]   [#f5c2e7]🔧 {self.tool}[/]" if self.tool else ""
        eff = getattr(self.agent, "effort", None)
        effseg = f"   [dim]·[/]   [#f5c2e7]◇ effort {eff}[/]" if eff else ""
        status = _oneline(Text.from_markup(
            f"  [bold #cba6f7]{frame}[/] [#cba6f7]{self.phase}[/]   [dim]·[/]   "
            f"[#89b4fa]{_fmt_elapsed(el)}[/]   [dim]·[/]   [#f9e2af]⚡ {tok}[/] "
            f"[dim]token[/]{effseg}{extra}"
            f"   [dim italic]Ctrl+C batal[/]"))
        rows = [status]
        # Baris ETA sadar-fase (hanya connector web dgn riwayat cukup).
        eta = self._web_eta_line(now) if self._web_service else None
        if eta is not None:
            rows.append(eta)
        # Tips bergantian (tiap 10 dtk) — baru muncul setelah beberapa detik agar
        # giliran singkat tak sempat kedip-kedip tips.
        if el > 4:
            tip = _TIPS[int(el / 10) % len(_TIPS)]
            rows.append(_oneline(Text(f"  ✦ tips: {tip}", style="dim italic")))
        return rows[0] if len(rows) == 1 else Group(*rows)

    @staticmethod
    def _bar(frac: float, width: int = 14) -> str:
        """Pill TIPIS: dibangun dari glyph garis (U+2501/U+2500), bukan blok
        penuh (U+2588/U+2591).

        Blok penuh setinggi satu baris sel penuh sehingga terlihat seperti
        batang tebal; glyph garis hanya menggambar satu goresan di tengah sel,
        jadi bar-nya terbaca tipis & rendah seperti pill. Ujungnya diberi
        setengah-garis (U+257A/U+2578 tebal, U+2576/U+2574 tipis) supaya kedua
        sisi tampak membulat alih-alih terpotong siku.

        Sengaja memakai box-drawing yang ADA di hampir semua font terminal —
        glyph pill Powerline (U+E0B4/E0B6) bergantung Nerd Font dan akan jadi
        kotak-tofu di font bawaan."""
        frac = max(0.0, min(frac, 1.0))
        isi = int(round(frac * width))
        # Dirakit per-sel supaya lebarnya SELALU `width`. Versi sebelumnya
        # menyusun ujung + tengah secara terpisah dan meleset satu karakter di
        # 0%/100%, sehingga bar berkedut saat mendekati ujung.
        def sel(i: int, terisi: bool) -> str:
            if i == 0:
                return "╺" if terisi else "╶"
            if i == width - 1:
                return "╸" if terisi else "╴"
            return "━" if terisi else "─"

        kiri = "".join(sel(i, True) for i in range(isi))
        kanan = "".join(sel(i, False) for i in range(isi, width))
        return f"[#a6e3a1]{kiri}[/][#45475a]{kanan}[/]"

    def _web_eta_line(self, now: float):
        """Baris ETA SADAR-FASE untuk connector web — dijaga JUJUR:

        - fase 'berpikir' (tak terprediksi): cuma hint deskriptif dari median
          riwayat ('biasanya mulai menjawab ~Xs'), TANPA hitung-mundur;
        - fase 'menjawab' (ada sinyal token nyata): bar + '≈Xs lagi (perkiraan)',
          bar dibatasi 95% supaya tak pernah klaim 100% sebelum benar-benar
          selesai; begitu lewat perkiraan ia berganti 'lebih lama dari biasanya…'
          alih-alih angka yang meleset.

        None -> tak ada baris (fase lain, atau sampel riwayat belum cukup)."""
        med = self._web_med
        if not med:
            return None
        ph_el = now - self.phase_since
        if self.phase == "berpikir":
            s = med["start"]
            # Sebagian situs (mis. kimi.com) membuat wadah balasan SEKETIKA
            # setelah kirim, jadi fase 'berpikir' terukur ~0 detik dan seluruh
            # penantian nyata jatuh ke fase 'menjawab'. Menampilkan "biasanya
            # mulai menjawab ~0s" cuma kebisingan yang meremehkan lama tunggu —
            # lebih baik diam dan biarkan bar fase berikutnya yang bicara.
            if s < 1.0:
                return None
            if ph_el <= max(s * 1.5, s + 3):
                txt = f"biasanya mulai menjawab ~{s:.0f}s"
            else:
                txt = f"biasanya ~{s:.0f}s — kali ini agak lama, ditunggu ya"
            return _oneline(Text.from_markup(f"     [dim #6c7086]{txt}[/]"))
        if self.phase == "menjawab":
            frac, eta = self._web_progress(ph_el)
            if eta is None:
                return None
            bar = self._bar(min(frac, 0.95))
            if eta >= 1:
                # "≤" bukan "≈": angkanya JANJI batas atas yang dikalibrasi,
                # bukan tebakan titik. Hitung-mundur satu angka tak bisa dibuat
                # akurat 80-90% (panjang jawaban belum ada saat ditanya);
                # janji satu sisi bisa — lihat web_timing._TARGET.
                tail = f"≤{eta:.0f}s lagi"
            else:
                tail = "hampir selesai…"
            akur = med.get("akurasi")
            # Angka DIUKUR dari giliran-giliran sebelumnya, bukan klaim: berapa
            # persen janji yang benar-benar ditepati. Kalau kenyataannya 60%,
            # yang tertulis 60% — dan kuantilnya menyetel diri naik.
            jejak = f" · janji tepat {akur * 100:.0f}%" if akur is not None else ""
            return _oneline(Text.from_markup(
                f"     {bar}  [dim #6c7086]{tail}{jejak}[/]"))
        return None

    def _web_progress(self, ph_el: float) -> tuple[float, float | None]:
        """(kemajuan 0-1, detik tersisa) untuk fase 'menjawab'.

        Perkiraan dihitung dari KEMAJUAN NYATA, bukan median durasi:

            sisa = (perkiraan panjang akhir - karakter yang sudah mengalir)
                   / throughput

        Median durasi ditinggalkan karena sebarannya sangat lebar (terukur
        5.75s-28.12s pada layanan yang sama) — sebabnya panjang jawaban yang
        berbeda-beda, bukan layanannya yang tak menentu. Throughput jauh lebih
        stabil, dan panjang akhir diperkirakan lewat KUANTIL BERSYARAT
        (web_timing.kuantil_panjang) yang menajam sendiri seiring makin banyak
        teks yang terlihat — kuantil, bukan rata-rata, supaya hasilnya jadi
        BATAS ATAS yang bisa dikalibrasi.

        `chars` bersatuan teks POLOS (dari on_token), sesatuan dengan panjang
        yang dicatat web_timing — jangan campur dengan panjang markdown.

        Kembali (frac, None) bila belum layak menampilkan apa pun."""
        med = self._web_med or {}
        chars = self._web_chars
        rate = med.get("rate")
        lengths = med.get("lengths") or []

        # Throughput dari riwayat belum ada? Pakai laju giliran INI, tapi tunggu
        # beberapa detik dulu — laju di detik pertama masih sangat berisik.
        if not rate:
            if ph_el < 3 or chars <= 0:
                return 0.0, None
            rate = chars / ph_el

        if chars <= 0 or not lengths:
            # Belum ada teks / belum ada riwayat panjang: jatuh kembali ke median
            # durasi supaya tetap ada gambaran kasar, dan katakan apa adanya.
            a = med.get("answer") or 0.0
            if a <= 0:
                return 0.0, None
            return min(ph_el / a, 0.95), max(a - ph_el, 0.0)

        try:
            from .. import web_timing
            # Kuantil (bukan rata-rata) supaya angkanya jadi BATAS ATAS yang
            # bisa dikalibrasi; nilainya menyetel diri tiap giliran.
            total = web_timing.kuantil_panjang(
                lengths, chars, med.get("kuantil", 0.80))
        except Exception:  # noqa: BLE001
            return 0.0, None

        sisa_chars = max(total - chars, 0.0)
        eta = sisa_chars / rate if rate > 0 else 0.0
        frac = chars / total if total > 0 else 0.0

        # Simpan janji PERTAMA (sebagai total durasi) untuk dinilai nanti.
        if not self._web_pred_first and eta >= 1:
            self._web_pred_first = ph_el + eta
        return frac, eta


# ---------------------------------------------------------------------------
# Komponen tampilan
# ---------------------------------------------------------------------------
def _banner(agent: Agent, resumed: bool) -> Panel:
    spec = agent.model_spec
    # Seluruh model berbasis browser, jadi penanda jenis lama (reasoning /
    # multimodal / chat) tak lagi membedakan apa pun. Yang berguna sekarang:
    # menegaskan bahwa model ini berjalan lewat browser.
    kind = "🌐 via browser"
    # Kolom label rata kanan (abu) + nilai berwarna -> sejajar & profesional.
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="#7f849c", min_width=7)
    grid.add_column(overflow="fold")
    eff = (f"   [#7f849c]·[/]   [#f5c2e7]◇ {agent.effort}[/]"
           if agent.effort else "")
    tag = "dilanjutkan" if resumed else "sesi baru"
    grid.add_row("Model", f"[bold #89b4fa]{spec.label}[/]   [dim]{kind}[/]{eff}")
    grid.add_row("Folder", f"[#a6e3a1]{config.PROJECT_ROOT}[/]")
    grid.add_row("Sesi", f"[#f9e2af]{agent.session.id}[/]   [dim]· {tag}[/]")

    head = Text.assemble(
        ("● ", "bold #a6e3a1"), ("siap", "bold #a6e3a1"),
        ("   dimana pun, dari terminal ini", "dim italic"),
    )
    hint = Text.from_markup(
        "[dim]ketik pesan untuk mengobrol[/dim]   "
        "[#94e2d5]/menu[/] [dim]menu[/dim]   "
        "[#94e2d5]/model[/] [dim]ganti model[/dim]   "
        "[#f38ba8]/exit[/] [dim]keluar[/dim]"
    )
    body = Group(head, Text(), grid, Rule(style="#313244"), hint)
    return Panel(body, border_style="#cba6f7", box=box.ROUNDED, padding=(1, 2),
                 title="[bold #cba6f7]⬢ bagas-ai[/]", title_align="left")


def _models_panel(current_id: str) -> Panel:
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=False)
    tbl.add_column("#", style="dim", justify="right")
    tbl.add_column("alias", style="bold cyan")
    tbl.add_column("model", style="white")
    tbl.add_column("kemampuan", style="dim")
    tbl.add_column("aktif", justify="center")
    for i, key, spec in models.catalog():
        mark = "[bold green]●[/bold green]" if spec.id == current_id else ""
        tbl.add_row(str(i), key, spec.label, spec.note or "-", mark)
    return Panel(tbl, title="[bold]🔀 Model tersedia[/bold]", border_style="cyan",
                 box=box.ROUNDED)




# ---------------------------------------------------------------------------
# Loop utama
# ---------------------------------------------------------------------------
def main(resume: bool = False) -> None:
    console.clear()
    show_logo()          # tampil segera setelah preload -> pengguna tahu app hidup
    console.print()

    # Persiapan cepat (deteksi OS, baca sesi, peta proyek). Bar loading BERTAHAP
    # sudah ditampilkan saat impor pustaka (di __main__._preload_with_bar) — fase
    # yang benar-benar lama. Sisa kerja di sini ringan; untuk pemindaian proyek
    # BESAR yang butuh baca banyak file, tampilkan bar tersendiri.
    os_status = osinfo.sync_to_memory()

    resumed = False
    if resume:
        session = session_mod.latest()
        if session:
            resumed = True
        else:
            session = Session.create()
    else:
        session = Session.create()

    # Peta proyek: JANGAN memblokir startup — pengguna harus bisa LANGSUNG
    # mengetik. Pakai cache disk apa adanya (instan, mungkin sedikit basi), lalu
    # periksa kesegaran & bangun ulang DI THREAD LATAR; system prompt disegarkan
    # otomatis begitu peta terbaru siap.
    #
    # DIBUNGKUS dengan sengaja: instalasi PARSIAL/basi (update tak tuntas -> modul
    # tak lagi cocok satu sama lain, mis. cli.py memanggil fungsi yang belum ada
    # di projectindex.py versi lama) TAK boleh membuat bagas-ai gagal start dengan
    # traceback mentah. Petanya toh dibangun ulang di latar; cukup lanjut & beri
    # tahu sekali supaya penyebabnya (perlu reinstall bersih) jelas.
    try:
        _primed_map = projectindex.prime(config.PROJECT_ROOT)
    except Exception as _prime_exc:  # noqa: BLE001
        _primed_map = ""
        console.print(
            "  [yellow]⚠ peta proyek dilewati saat start[/] "
            f"[dim](instalasi tampaknya belum tuntas: {type(_prime_exc).__name__}). "
            "Tutup bagas-ai lalu reinstall/`bagasai update` bila ini berulang.)[/]")

    agent = Agent(session=session)   # instan: pakai peta cache / tanpa peta dulu

    def _bg_build_map() -> None:
        try:
            # Spek laptop: deteksi LOKAL sekali saja (tanpa LLM) -> memory.
            hw_status = osinfo.sync_hardware_to_memory()
            fresh = projectindex.refresh(config.PROJECT_ROOT)
            if fresh != _primed_map or hw_status == "added":
                agent.refresh_system_prompt()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_bg_build_map, daemon=True).start()
    # Pemanasan impor `openai` DIHAPUS bersama klien API — pustaka itu tak lagi
    # dipakai sama sekali. (Padanannya untuk jalur browser sudah ada di tempat
    # lain: sesi Playwright dihidupkan sekali lalu dipertahankan antar giliran.)
    pout(_banner(agent, resumed), bottom=0)
    if resumed:
        console.print(Padding(Rule("[dim]percakapan sebelumnya[/dim]",
                                    style="#313244"), (1, 0, 0, 0)))
        for m in agent.memory.messages:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                console.print(f"\n  [bold #cba6f7]❯[/] [#cba6f7]{content}[/]")
            elif role == "assistant" and content:
                console.print("\n  [bold #89b4fa]🤖 bagas-ai[/]")
                console.print(Padding(_md(content), (0, 3, 1, 3)))
        console.print(Rule("[dim]lanjut di bawah[/dim]", style="#313244"))
    if os_status in ("added", "updated"):
        verb = "terdeteksi & disimpan" if os_status == "added" else "diperbarui"
        pout(f"[dim]🖥  OS {verb}: {osinfo.summary()} — perintah terminal akan "
             f"disesuaikan.[/dim]", bottom=0)
    # Peta proyek: dari cache instan (disegarkan di latar), atau sedang dibangun
    # pertama kali di latar — dua-duanya TANPA menunda prompt.
    _pn = _primed_map.count("\n- ")
    if _pn:
        pout(f"[dim]🗺  peta proyek siap (~{_pn} file) — disegarkan di latar; "
             f"ketik [/][#94e2d5]/scan[/][dim] untuk paksa pindai ulang.[/]",
             bottom=0)
    else:
        pout("[dim]🗺  peta proyek dibangun di latar — langsung ngetik aja, "
             "tak perlu menunggu.[/dim]", bottom=0)
    _update_notice()  # info bila versi usang (dari cache) + cek ulang di latar
    console.print()

    live_holder: dict = {"live": None}
    status_obj = Status(agent)
    tg_service: dict = {"svc": None}   # layanan bot Telegram di dalam sesi ini
    # Total token PERSISTEN lintas semua sesi ("dimanapun").
    # "sesi" (agent.tokens_session) kini persisten per-sesi (ikut saat --resume),
    # dan sudah termasuk di total global. Agar tidak dobel saat resume, base =
    # total global dikurangi token sesi yang sudah dihitung.
    grand = {"base": prefs.get_total_tokens() - agent.tokens_session.total}

    def _save_total() -> None:
        prefs.set_total_tokens(grand["base"] + agent.tokens_session.total)

    # --- Jejak langkah + hasil yang bisa di-expand ---------------------------
    # Tiap pemanggilan tool = satu "langkah" bernomor. Hasil PENUH tiap langkah
    # disimpan di `steps` agar bisa ditampilkan ulang lengkap lewat `/expand N`
    # (terminal bergulir tak bisa buka-tutup output lama di tempat, jadi expand =
    # cetak ulang hasil penuh atas permintaan). `step_ctr` bikin nomor unik &
    # stabil sepanjang sesi; `cur_step` menjembatani on_tool -> on_tool_result.
    steps: dict[int, dict] = {}
    step_ctr = {"n": 0}
    cur_step: dict = {}
    # Mode tampilan giliran: True = TUI interaktif (langkah bisa diklik SELAGI
    # berjalan); False = tampilan rich biasa (mengalir, tanpa layar-penuh).
    tui_mode = {"on": True}

    def _step_label(name: str, args: dict) -> str:
        a = args if isinstance(args, dict) else {}
        if name == "run_command":
            return a.get("command", "") or "perintah"
        if name == "run_python":
            return "kode Python"
        if name == "run_script":
            return f"skrip {a.get('name', '')}"
        if name == "read_file":
            return a.get("path", "")
        if name == "list_dir":
            return a.get("path", ".") or "."
        if name == "web_search":
            return a.get("query", "")
        if name == "write_file":
            return a.get("path", "")
        if name == "delete_file":
            return a.get("path", "")
        if name == "save_script":
            return a.get("name", "")
        if name == "remember":
            return a.get("fact", "") or "fakta"
        return name

    # Saat prompt pilihan (ask_user) aktif, POLLER input di loop giliran (msvcrt/
    # mouse) HARUS berhenti membaca — kalau tidak, ketikan user DICURI poller dan
    # dropdown inquirer rusak (keduanya membaca console yang sama).
    input_paused = {"on": False}
    # Event Telegram yang datang saat console "dipinjam" menu — dicetak nanti.
    _tg_pending: list[tuple[str, str]] = []

    def _with_console(fn, *a, **k):
        """Jalankan aksi ber-dropdown (inquirer) dengan console dipinjam penuh:
        poller input berhenti & pesan thread lain (Telegram) ditahan dulu —
        mencegah menu rusak oleh cetakan yang menyela."""
        input_paused["on"] = True
        try:
            return fn(*a, **k)
        finally:
            input_paused["on"] = False
            _tg_flush()

    def choice_handler(question: str, options: list[str], multiple: bool) -> str:
        input_paused["on"] = True
        live = live_holder.get("live")
        if live:
            live.stop()
        console.print(f"\n[bold yellow]❔ {question}[/bold yellow]")
        try:
            if multiple:
                res = inquirer.checkbox(
                    message=question, choices=options, pointer="❯",
                    instruction="(spasi pilih, enter konfirmasi)").execute()
                answer = ", ".join(res) if res else "(tidak memilih)"
            else:
                answer = inquirer.select(
                    message=question, choices=options, pointer="❯").execute()
        except (KeyboardInterrupt, EOFError):
            answer = "(dibatalkan)"
        finally:
            input_paused["on"] = False
            _tg_flush()
        console.print(f"[dim]-> {answer}[/dim]")
        if live:
            live.start()
        return answer

    interaction.set_choice_handler(choice_handler)

    # Tool yang hasilnya berupa teks substansial & layak di-expand penuh.
    _EXPANDABLE = {"run_command", "run_python", "run_script",
                   "read_file", "list_dir", "web_search"}

    def on_tool(name: str, args: dict) -> None:
        """Mulai satu langkah: set fase + timer, dan untuk tulis/hapus tampilkan diff."""
        status_obj.note_step(name)
        step_ctr["n"] += 1
        cur_step.clear()
        cur_step.update(n=step_ctr["n"], name=name, args=args, start=time.time())
        p = args.get("path") if isinstance(args, dict) else None
        # Diff/preview substantif ditampilkan SEBELUM aksi (konten inti perubahan).
        if name in _TOOL_DIFF and p:
            old, new, exists = _isi_sebelum_sesudah(name, p, args)
            _print_diff(p, old, new, is_new=not exists)
        elif name == "delete_file" and p:
            full = config.PROJECT_ROOT / p
            content = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
            _print_delete(p, content)

    def finish_step(name: str, result: str) -> None:
        """Selesaikan langkah: catat hasil penuh (untuk /expand) + cetak baris jejak.

        Baris jejak = ceklis ringkas (ikon, fase, target, durasi, #nomor). Hasil
        yang layak di-expand diberi petunjuk `/expand N`; hasil gagal ditandai.
        """
        n = cur_step.get("n", step_ctr["n"])
        args = cur_step.get("args", {})
        dur = time.time() - cur_step.get("start", time.time())
        text = (result or "").strip()
        failed = text.startswith("[GAGAL") or text.startswith("[error]")

        # Simpan hasil PENUH agar bisa dibuka lagi via /expand.
        steps[n] = {"name": name, "label": _step_label(name, args),
                    "result": result or "", "failed": failed, "dur": dur}
        # Batasi memori: simpan 200 langkah terakhir saja.
        if len(steps) > 200:
            for old_n in sorted(steps)[:-200]:
                steps.pop(old_n, None)

        label = _step_label(name, args)
        if len(label) > 64:
            label = label[:61] + "…"
        icon = "[#f38ba8]✗[/]" if failed else "[#a6e3a1]✓[/]"
        phase = _PHASE.get(name, "selesai")
        dur_s = f"{dur:.1f}s" if dur >= 0.05 else ""
        head = (f"  {icon} [#cdd6f4]{phase}[/]  [white]{_esc(label)}[/]"
                f"   [dim]{dur_s}[/]   [dim #94e2d5]#{n}[/]")
        console.print(head)

        # Baris kedua: ringkasan hasil ringkas (buka & klik lewat penampil).
        body = re.sub(r"^exit_code=\S+\n?", "", text)
        nlines = len([ln for ln in body.splitlines() if ln.strip()])
        if failed:
            console.print("     [#f38ba8]gagal[/]")
        elif name in _EXPANDABLE and nlines > 0:
            unit = "hasil" if name == "web_search" else "baris"
            console.print(f"     [dim]{nlines} {unit}[/]")
        elif name == "write_file":
            # Tampilkan status cek sintaks bila ada di hasil.
            m = re.search(r"\[cek sintaks\]\s*(.+)", text)
            if m:
                ok = m.group(1).startswith("OK")
                col = "#a6e3a1" if ok else "#f38ba8"
                console.print(f"     [{col}]{_esc(m.group(1).strip())}[/]")
        # Langkah tool selesai -> kembali ke fase "berpikir" untuk generasi berikut.
        status_obj.note_thinking()

    def show_expand(n: int | None) -> None:
        """Tampilkan ulang hasil PENUH sebuah langkah (perintah `/expand N`)."""
        if not steps:
            console.print("  [dim]belum ada langkah untuk di-expand.[/dim]\n")
            return
        if n is None:
            n = max(steps)
        rec = steps.get(n)
        if not rec:
            console.print(f"  [yellow]Langkah #{n} tak ada. Yang tersedia: "
                          f"{', '.join('#' + str(k) for k in sorted(steps))}[/yellow]\n")
            return
        text = (rec["result"] or "").strip() or "(tidak ada output)"
        lines = text.splitlines()
        cap = 400
        if len(lines) > cap:
            text = "\n".join(lines[:cap]) + f"\n… [dipotong, {len(lines) - cap} baris lagi]"
        color = "#f38ba8" if rec["failed"] else "#a6e3a1"
        icon = "✗" if rec["failed"] else "✓"
        title = f"[{color}]{icon} #{n} · {rec['name']}[/] [dim]· {_esc(rec['label'])[:56]}[/]"
        panel = Panel(Text(text), title=title, title_align="left",
                      border_style=color, box=box.ROUNDED, padding=(0, 1))
        console.print(Padding(panel, (0, 3, 1, 3)))

    def open_step_viewer() -> None:
        """Cetak ulang hasil PENUH semua langkah giliran terakhir (inline, teks).
        Saat giliran berjalan, buka/tutup realtime cukup pakai Ctrl+R."""
        if not steps:
            console.print("  [dim]belum ada langkah untuk dibuka.[/dim]\n")
            return
        for k in sorted(steps):
            show_expand(k)

    def _reset_web_hub_if_stuck(wt: threading.Thread) -> None:
        """Pasca-Ctrl+C pada giliran web: beri worker jeda singkat untuk lepas;
        bila masih menggantung (macet di dalam browser), RESET hub agar giliran
        berikutnya tak ikut mengantre di belakang job macet (akar bug 'tiap
        Ctrl+C lalu chat baru, sesi browser nyangkut tak selesai')."""
        if not wt.is_alive():
            return
        wt.join(timeout=2.0)
        if wt.is_alive():
            try:
                from ..connectors import browser as _br
                _br.reset_hub()
            except Exception:  # noqa: BLE001
                pass

    def _connect_web(prev_model_id: str) -> None:
        """Alur CONNECT saat model web DIPILIH (bukan saat pesan pertama):
        belum pernah login -> diarahkan ke Chrome untuk login SEKALI; sudah
        pernah -> langsung tersambung ke sesi chat. Gagal/dibatalkan -> kembali
        ke model sebelumnya supaya pengguna tak terjebak di model yang mati."""
        spec = agent.model_spec
        try:
            from .. import connectors
        except Exception:  # noqa: BLE001
            connectors = None
        if connectors is None or not connectors.playwright_available():
            console.print(
                "  [yellow]⚠ Connector butuh Playwright:[/] [bold]pip install "
                "playwright[/] lalu [bold]playwright install chromium[/]\n")
            _revert_model(prev_model_id)
            return

        state = {"status": f"menghubungkan ke {spec.label}…"}
        cancel_event = threading.Event()
        result: dict = {"login": None, "error": None}

        def worker() -> None:
            try:
                result["login"] = connectors.get_connector(spec.connector).connect(
                    on_status=lambda m: state.__setitem__("status", m),
                    cancel_event=cancel_event,
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        def render():
            return Text.from_markup(
                f"  [#94e2d5]◐[/] [dim]{_esc(state['status'])}[/]")

        wt = threading.Thread(target=worker, daemon=True)
        interrupted = False
        try:
            with Live(render(), console=console, refresh_per_second=8,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    try:
                        live.update(render())
                        wt.join(timeout=0.1)
                    except KeyboardInterrupt:
                        if not interrupted:
                            interrupted = True
                            cancel_event.set()
                            state["status"] = "membatalkan…"
                        else:
                            break
        except KeyboardInterrupt:
            interrupted = True
            cancel_event.set()

        if result["login"] is not None:
            _pick_web_chat(connectors.get_connector(spec.connector))
            if result["login"]:
                console.print(
                    f"  [#a6e3a1]✓ login berhasil — terhubung ke "
                    f"[bold]{_esc(spec.label)}[/bold]. Jendela diminimalkan; "
                    f"chat & jawaban di terminal ini.[/]\n")
            else:
                console.print(
                    f"  [#a6e3a1]✓ terhubung — sesi login [bold]"
                    f"{_esc(spec.label)}[/bold] masih aktif, langsung ke chat.[/]\n")
            return
        err = result["error"]
        why = ("dibatalkan" if interrupted or isinstance(err, llm.Cancelled)
               else f"gagal: {err}")
        console.print(f"  [yellow]⚠ koneksi {_esc(spec.label)} {_esc(str(why))}[/]")
        _revert_model(prev_model_id)

    def _pick_web_chat(conn) -> None:
        """Menu PILIH SESI di AI web setelah model web dipilih.

        Melanjutkan percakapan lama berarti konteks proyek yang sudah dikirim di
        sana tetap dipakai — AI web tak perlu 'membaca ulang' proyek dari nol
        (berguna untuk --resume). Satu sesi terminal terikat ke satu chat."""
        if not conn.supports_resume():
            return
        rows = conn.own_chats()
        linked = getattr(agent, "_web_chat_id", "")
        if not rows and not linked:
            return  # belum ada chat lama -> langsung chat baru saja

        def _when(ts) -> str:
            try:
                return time.strftime("%d/%m %H:%M", time.localtime(float(ts)))
            except (TypeError, ValueError):
                return ""

        choices = [Choice("__new__", "✨ Mulai percakapan BARU di web")]
        for r in rows[:15]:
            mark = "  ← terpakai sesi ini" if r.get("id") == linked else ""
            title = (r.get("title") or "(tanpa judul)")[:52]
            choices.append(Choice(r["id"], f"{title:<54}{_when(r.get('ts'))}{mark}"))
        try:
            sel = inquirer.select(
                message="Lanjutkan percakapan web yang mana?",
                choices=choices, pointer="❯", default=linked or "__new__",
                long_instruction="Melanjutkan chat lama = konteks proyek tak perlu "
                                 "dikirim ulang.",
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return
        if sel == "__new__":
            agent.start_new_web_chat()
            console.print("  [dim]→ percakapan web BARU akan dibuat saat kamu "
                          "mengirim pesan pertama.[/dim]")
            return
        agent.use_web_chat(sel)
        title = next((r.get("title") for r in rows if r.get("id") == sel), sel)
        console.print(f"  [#a6e3a1]✓ melanjutkan:[/] [bold]{_esc(str(title))}[/] "
                      f"[dim]— konteks proyek sudah ada di percakapan itu.[/]")

    def _delete_web_chats_of(sessions_deleted: list) -> None:
        """Hapus percakapan AI web milik sesi terminal yang baru saja dihapus
        (satu sesi terminal = satu percakapan browser)."""
        pairs: dict[str, list[str]] = {}
        for s in sessions_deleted:
            for svc, cid in (getattr(s, "web_chats", None) or {}).items():
                if cid:
                    pairs.setdefault(svc, []).append(cid)
        if not pairs:
            return
        try:
            from .. import connectors
            if not connectors.playwright_available():
                return
        except Exception:  # noqa: BLE001
            return
        total = sum(len(v) for v in pairs.values())

        def _do() -> int:
            n = 0
            for svc, ids in pairs.items():
                try:
                    conn = connectors.get_connector(svc)
                    if conn.supports_chat_admin():
                        n += conn.delete_chats(ids)
                        conn.forget_chats(set(ids))
                except Exception:  # noqa: BLE001 - lanjut ke service berikutnya
                    pass
            return n

        n, err = _web_busy(f"menghapus {total} percakapan web terkait…", _do)
        if err is None and n:
            console.print(f"  [dim]🌐 {n} percakapan di AI web ikut dihapus.[/dim]")

    def _revert_model(prev_model_id: str) -> None:
        # Seluruh model kini berbasis browser, jadi tak ada lagi model "pasti
        # jalan tanpa koneksi" untuk dijadikan pelabuhan. Yang masuk akal adalah
        # kembali ke model SEBELUMNYA apa adanya; bila ID-nya sudah tak dikenal
        # (mis. peninggalan katalog lama), spec_for_id memetakannya ke bawaan.
        if not models.is_known_id(prev_model_id):
            prev_model_id = config.CHAT_MODEL
        try:
            console.print(
                f"  [dim]kembali ke model: {agent.set_model(prev_model_id)}[/dim]\n")
        except ValueError:
            console.print()

    def process(text: str) -> None:
        """Jalankan satu giliran INLINE (tanpa layar-penuh, tetap di alur terminal
        biasa). Seluruh giliran dirender di satu region rich.Live yang hidup &
        membeku jadi riwayat saat selesai. Hasil langkah bisa dibuka/tutup realtime
        dengan Ctrl+R. Ctrl+C membatalkan. Bila gagal, jatuh ke process_classic.

        Model CONNECTOR web (Claude/Qwen web) memakai jalur yang SAMA: ia kini
        bisa memanggil tool (edit file, jalankan perintah, dll) lewat protokol
        teks, jadi langkah-langkahnya tampil rapi di terminal."""
        steps.clear()
        step_ctr["n"] = 0
        cur_step.clear()
        turn_start = time.time()
        if not tui_mode["on"]:
            process_classic(text)
            return

        # Saklar hidup callback: worker daemon yang DITINGGAL (Ctrl+C dua kali)
        # tidak boleh lagi mencetak ke terminal setelah kita kembali ke prompt.
        cbs_alive = {"on": True}

        def _commit(renderables) -> None:
            """Bekukan konten ke riwayat terminal (tercetak DI ATAS region live)."""
            if not cbs_alive["on"]:
                return
            for r in renderables:
                console.print(r)

        view = TurnView(agent, commit=_commit)
        ctr = {"n": 0}

        def _on_tool(name: str, args: dict) -> None:
            if not cbs_alive["on"]:
                return
            ctr["n"] += 1
            n = ctr["n"]
            label = _step_label(name, args)
            rec = view.start_step(n, name, label)
            cur_step.clear()
            cur_step["rec"] = rec
            cur_step["n"] = n
            # Diff tulis/hapus dicetak (otomatis di ATAS region live) sbg konteks
            # perubahan, lalu menjadi bagian riwayat terminal.
            p = args.get("path") if isinstance(args, dict) else None
            if name in _TOOL_DIFF and p:
                full = config.PROJECT_ROOT / p
                exists = full.exists()
                old = full.read_text(encoding="utf-8", errors="replace") if exists else ""
                new = args.get("content", "") if isinstance(args, dict) else ""
                _print_diff(p, old, new, is_new=not exists)
            elif name == "delete_file" and p:
                full = config.PROJECT_ROOT / p
                content = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
                _print_delete(p, content)

        def _on_result(name: str, result: str) -> None:
            if not cbs_alive["on"]:
                return
            rec = cur_step.get("rec")
            n = cur_step.get("n", ctr["n"])
            failed = (result or "").strip().startswith(("[GAGAL", "[error]"))
            if rec is not None:
                view.end_step(rec, result, failed)
            steps[n] = {"name": name, "label": _step_label(name, {}),
                        "result": result or "", "failed": failed, "dur": 0.0}
            if rec is not None:
                steps[n]["label"] = rec["label"]
            step_ctr["n"] = n

        def _on_msg(content: str) -> None:
            if cbs_alive["on"]:
                view.add_narasi(content)

        def _on_retry(attempt: int, wait: float, exc: Exception) -> None:
            if cbs_alive["on"]:
                view.note_retry(wait, f"percobaan ke-{attempt}")

        def _on_status(msg: str) -> None:
            """Status connector web (menyiapkan sesi / berpikir / menjawab)."""
            if cbs_alive["on"]:
                view.note_phase(_web_phase(msg))

        def _on_notice(msg: str) -> None:
            """bagas-ai naik-kelas / anti-macet otomatis — beri tahu pengguna.
            Deskripsi naik-kelas selalu memuat '→' (mis. 'effort a → b')."""
            label = ("⚡ naik kelas otomatis:" if "→" in msg
                     else "🛟 anti-macet:")
            _commit([Text.from_markup(
                f"  [#f9e2af]{label}[/] [dim]{_esc(msg)} "
                f"— konteks dipertahankan[/]")])

        cancel_event = threading.Event()
        result: dict = {"answer": None, "error": None}

        def worker() -> None:
            try:
                result["answer"] = agent.run(
                    text, on_tool=_on_tool, on_message=_on_msg,
                    on_retry=_on_retry, cancel_event=cancel_event,
                    on_tool_result=_on_result, on_notice=_on_notice,
                    on_status=_on_status,
                    # Aliran teks jawaban: dipakai untuk ETA sekaligus pratinjau
                    # bergulir, supaya jawaban terlihat terbentuk saat itu juga.
                    on_token=view.note_stream,
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        # Coba aktifkan MOUSE inline (klik hasil untuk buka/tutup) tanpa layar-penuh.
        mouse = None
        try:
            from . import winmouse
            if winmouse.available():
                m = winmouse.MouseReader()
                if m.enable():
                    mouse = m
                    view._clickable = True
        except Exception:  # noqa: BLE001
            mouse = None

        def _hit_step(click_y: int) -> int | None:
            """Petakan baris klik (koordinat buffer) ke #langkah, atau None."""
            bottom = mouse.cursor_row() if mouse else None
            if bottom is None:
                return None
            blocks = view._blocks()
            opts = console.options
            heights = []
            for _, r in blocks:
                try:
                    heights.append(len(console.render_lines(r, opts, pad=False)))
                except Exception:  # noqa: BLE001
                    heights.append(1)
            total = sum(heights)
            top = bottom - total + 1
            offset = click_y - top
            acc = 0
            for (tag, _), h in zip(blocks, heights):
                if acc <= offset < acc + h:
                    return tag[1] if tag[0] == "step" else None
                acc += h
            return None

        worker_thread = threading.Thread(target=worker, daemon=True)
        interrupted = False
        # Capture mouse MENELAN event scroll wheel -> terminal tak bisa digulung.
        # Saat pengguna terdeteksi men-scroll, capture DILEPAS sementara (wheel
        # kembali dilayani terminal secara native) lalu dipasang lagi otomatis.
        mouse_pause = {"until": 0.0}
        try:
            with Live(view, console=console, refresh_per_second=12,
                      transient=False, vertical_overflow="visible") as live:
                live_holder["live"] = live
                worker_thread.start()
                while worker_thread.is_alive():
                    try:
                        if input_paused["on"]:
                            # ask_user sedang tampil -> JANGAN baca console; biarkan
                            # inquirer yang menerima seluruh ketikan/klik. Lepaskan
                            # juga capture mouse agar prompt & scroll normal.
                            if mouse is not None and mouse.active:
                                try:
                                    mouse.disable()
                                except Exception:  # noqa: BLE001
                                    pass
                                mouse_pause["until"] = 0.0
                            worker_thread.join(timeout=0.1)
                        elif mouse is not None:
                            # Jeda-scroll usai? pasang lagi capture klik.
                            if (not mouse.active
                                    and time.time() >= mouse_pause["until"]):
                                try:
                                    mouse.enable()
                                except Exception:  # noqa: BLE001
                                    pass
                            if not mouse.active:
                                # Capture DILEPAS (pengguna sedang men-scroll):
                                # wheel dilayani terminal; keyboard via msvcrt.
                                if _msvcrt is not None and _msvcrt.kbhit():
                                    ch = _msvcrt.getwch()
                                    if ch == "\x12":
                                        view.toggle()
                                    elif ch == "\x03":
                                        raise KeyboardInterrupt
                                else:
                                    time.sleep(0.03)
                                continue
                            got = False
                            for ev in mouse.poll():
                                got = True
                                if ev[0] == "wheel":
                                    # Pengguna men-scroll: lepaskan capture agar
                                    # wheel menggulung terminal seperti biasa.
                                    try:
                                        mouse.disable()
                                    except Exception:  # noqa: BLE001
                                        pass
                                    mouse_pause["until"] = time.time() + 4.0
                                elif ev[0] == "click":
                                    n = _hit_step(ev[2])
                                    if n is not None:
                                        view.toggle_step(n)
                                elif ev[0] == "key":
                                    if ev[1] == "\x12":       # Ctrl+R (buka semua)
                                        view.toggle()
                                    elif ev[1] == "\x03":     # Ctrl+C
                                        raise KeyboardInterrupt
                            if not got:
                                time.sleep(0.02)
                        elif _msvcrt is not None:
                            if _msvcrt.kbhit():
                                ch = _msvcrt.getwch()
                                if ch == "\x12":
                                    view.toggle()
                                elif ch == "\x03":
                                    raise KeyboardInterrupt
                            else:
                                time.sleep(0.03)
                        else:
                            worker_thread.join(timeout=0.1)
                    except KeyboardInterrupt:
                        if not interrupted:
                            interrupted = True
                            cancel_event.set()
                            view.cancelling = True
                        else:
                            break
                # Selesai: tandai & render sekali lagi supaya footer final tampil.
                # Jawaban TIDAK ditaruh di region live (bisa sangat panjang ->
                # bikin kedip & scroll rusak); dicetak ke riwayat setelah Live tutup.
                view.done = True
                # Nilai perkiraan ETA terhadap kenyataan. Ditempel ke giliran
                # yang barusan tercatat connector, supaya angka "tepat N%" di
                # layar adalah hasil UKUR, bukan klaim.
                if view._web_service and view._web_pred_first:
                    try:
                        from .. import web_timing
                        web_timing.note_promise(view._web_service,
                                                view._web_pred_first)
                    except Exception:  # noqa: BLE001 - statistik tak boleh ganggu
                        pass
                live.refresh()
        except KeyboardInterrupt:
            interrupted = True
            cancel_event.set()
        finally:
            cbs_alive["on"] = False   # worker yatim tak boleh mencetak lagi
            live_holder["live"] = None
            if mouse is not None:
                try:
                    mouse.disable()
                except Exception:  # noqa: BLE001
                    pass
        # Giliran web yang dibatalkan: pastikan sesi browser tak tertinggal macet.
        if interrupted and agent.model_spec.is_web:
            _reset_web_hub_if_stuck(worker_thread)

        err = result["error"]
        ans = (result["answer"] or "").strip()
        if isinstance(err, (KeyboardInterrupt, llm.Cancelled)) or (
                interrupted and not ans and err is None):
            # Benar-benar terputus (tak ada jawaban yang sempat jadi).
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        # Cabang khusus rate-limit API DIHAPUS: batas pemakaian kini datang dari
        # SITUS AI web, dan itu sudah ditangani lebih baik di core sebagai
        # WebLimitError/WebBusyError — lengkap dengan kapan bisa dipakai lagi
        # dan ulang-otomatis. Sisanya jatuh ke cabang error umum di bawah.
        elif err is not None:
            console.print(f"\n  [red]✖ error:[/red] {err}\n")
        else:
            # Jawaban dicetak SEBAGAI RIWAYAT biasa (di luar region live) supaya
            # sepanjang apa pun tak bikin kedip dan terminal tetap bisa di-scroll.
            # Ini juga menyelamatkan jawaban yang SELESAI tepat saat Ctrl+C ditekan
            # (sudah tersimpan di memory — tampilkan, jangan dibuang).
            if ans:
                console.print()
                # Header bot cukup SEKALI per giliran — kalau narasi sudah
                # menampilkannya, jawaban akhir tak perlu header kedua.
                if not view._said:
                    console.print("  [bold #89b4fa]🤖 bagas-ai[/]")
                console.print(Padding(_md(ans), (0, 3, 1, 3)))
            # Ringkasan giliran SETELAH jawaban (urutan yang benar).
            stps = view.all_steps
            if stps:
                n_file = sum(1 for s in stps
                             if s["name"] in ("write_file", "delete_file"))
                n_fail = sum(1 for s in stps if s["failed"])
                seg = [f"{len(stps)} langkah"]
                if n_file:
                    seg.append(f"{n_file} file")
                if n_fail:
                    seg.append(f"[#f38ba8]{n_fail} gagal[/]")
                seg += [_fmt_elapsed(time.time() - view.start),
                        f"⚡ {_fmt(agent.tokens_last.total)} token"]
                console.print(Padding(Text.from_markup(
                    "[dim]" + " · ".join(seg) + "[/]   [dim]·[/]   "
                    "[#94e2d5]/expand N[/][dim] lihat penuh[/]"), (0, 3, 1, 3)))
        _reindex_if_edited()

    def _reindex_if_edited() -> None:
        """Bila giliran barusan menulis/menghapus file, segarkan PETA PROYEK &
        system prompt supaya pemahaman bagas-ai selalu sesuai kode terbaru."""
        if any(s.get("name") in ("write_file", "delete_file")
               for s in steps.values()):
            try:
                projectindex.invalidate()   # jangan pakai memo basi pasca-edit
                agent.refresh_system_prompt()
            except Exception:  # noqa: BLE001
                pass

    def process_classic(text: str) -> None:
        nonlocal status_obj
        status_obj = Status(agent)
        header = {"shown": False}
        # Nomor langkah & hasil di-reset tiap giliran -> nomor tetap kecil (1..k)
        # dan `/expand N` merujuk langkah giliran TERAKHIR yang barusan terlihat.
        steps.clear()
        step_ctr["n"] = 0
        cur_step.clear()
        turn_start = time.time()

        def say(content: str) -> None:
            """Tampilkan ucapan/narasi bagas-ai: 1 header per giliran, indentasi rapi."""
            if not content or not content.strip():
                return
            console.print()
            if not header["shown"]:
                console.print("  [bold #89b4fa]🤖 bagas-ai[/]")
                header["shown"] = True
            console.print(Padding(_md(content.strip()), (0, 3, 1, 3)))

        def on_retry(attempt: int, wait: float, exc: Exception) -> None:
            """Dipertahankan demi kecocokan; jalur web tak memakai on_retry —
            penantian saat server penuh ditangani di dalam core (WebBusyError
            -> tunggu lalu ulangi) sehingga tak pernah sampai ke sini."""
            status_obj.note_retry(wait, f"percobaan ke-{attempt}")

        # Jalankan jawaban AI di THREAD LATAR BELAKANG supaya thread utama bebas
        # menangkap Ctrl+C secara responsif. Ctrl+C pertama -> minta batal secara
        # halus (cancel_event); Ctrl+C kedua -> tinggalkan worker (daemon) & kembali
        # ke prompt tanpa menunggu.
        cancel_event = threading.Event()
        result: dict = {"answer": None, "error": None}

        def worker() -> None:
            try:
                result["answer"] = agent.run(
                    text, on_tool=on_tool, on_message=say,
                    on_retry=on_retry, cancel_event=cancel_event,
                    on_tool_result=finish_step,
                    on_status=lambda m: status_obj.note_phase(_web_phase(m)),
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        worker_thread = threading.Thread(target=worker, daemon=True)
        interrupted = False
        forced = False
        try:
            with Live(status_obj, console=console, refresh_per_second=12,
                      transient=True) as live:
                live_holder["live"] = live
                worker_thread.start()
                while worker_thread.is_alive():
                    try:
                        worker_thread.join(timeout=0.1)
                    except KeyboardInterrupt:
                        if not interrupted:
                            interrupted = True
                            cancel_event.set()
                            status_obj.note_cancelling()
                        else:
                            # Ctrl+C kedua: jangan tunggu lagi, tinggalkan worker.
                            forced = True
                            break
        except KeyboardInterrupt:
            # Ctrl+C di jendela sempit di luar join() (mis. saat Live start /
            # thread mulai): perlakukan sebagai pembatalan, jangan sampai lolos
            # & menjatuhkan REPL.
            interrupted = True
            cancel_event.set()
        finally:
            live_holder["live"] = None
        # Giliran web yang dibatalkan: pastikan sesi browser tak tertinggal macet.
        if (interrupted or forced) and agent.model_spec.is_web:
            _reset_web_hub_if_stuck(worker_thread)

        err = result["error"]
        if forced or interrupted or isinstance(err, (KeyboardInterrupt, llm.Cancelled)):
            # Memory sudah dirapikan & disimpan di dalam agent.run().
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        # Cabang khusus rate-limit API DIHAPUS: batas pemakaian kini datang dari
        # SITUS AI web, dan itu sudah ditangani lebih baik di core sebagai
        # WebLimitError/WebBusyError — lengkap dengan kapan bisa dipakai lagi
        # dan ulang-otomatis. Sisanya jatuh ke cabang error umum di bawah.
        elif err is not None:
            console.print(f"\n  [red]✖ error:[/red] {err}\n")
        else:
            say(result["answer"])
            _turn_footer(turn_start)
        _reindex_if_edited()

    def _turn_footer(turn_start: float) -> None:
        """Ringkasan giliran: langkah, file disentuh, waktu, token — hanya bila
        ada kerja tool (chat biasa tetap bersih tanpa footer)."""
        if not steps:
            return
        n_step = len(steps)
        n_file = sum(1 for s in steps.values()
                     if s["name"] in ("write_file", "delete_file"))
        n_fail = sum(1 for s in steps.values() if s["failed"])
        el = _fmt_elapsed(time.time() - turn_start)
        tok = _fmt(agent.tokens_last.total)
        parts = [f"{n_step} langkah"]
        if n_file:
            parts.append(f"{n_file} file")
        if n_fail:
            parts.append(f"[#f38ba8]{n_fail} gagal[/]")
        parts.append(el)
        parts.append(f"⚡ {tok} token")
        body = " [dim]·[/] ".join(parts)
        hint = "   [dim]·[/]   [#94e2d5]/expand N[/][dim] lihat hasil penuh[/]"
        console.print(Padding(
            Text.from_markup(f"[dim]{body}[/dim]{hint}"), (0, 3, 1, 3)))

    # --- aksi menu (inquirer) ---
    def pick_model() -> str | None:
        """Menu pilih model. Return ID model SEBELUMNYA bila yang dipilih adalah
        connector web (pemanggil lalu menjalankan _connect_web), selain itu None."""
        def _describe(spec) -> str:
            # Satu baris: nama (rata) + badge kemampuan + SARAN "cocok untuk apa".
            # Semua model kini web, jadi lencana reasoning/multimodal tak lagi
            # membedakan apa pun — cukup satu penanda bahwa ini lewat browser.
            badge = " 🌐" if spec.is_web else "  "
            note = f"  —  {spec.note}" if spec.note else ""
            return f"{spec.label:<28}{badge}{note}"

        choices = [
            Choice(key, _describe(spec)) for _, key, spec in models.catalog()
        ]
        try:
            sel = inquirer.select(
                message="Pilih model (tiap model ada sarannya)",
                choices=choices, pointer="❯",
                default=next((k for _, k, s in models.catalog()
                              if s.id == agent.model), None),
            ).execute()
            prev = agent.model
            console.print(f"[green]✓ Model: {agent.set_model(sel)}[/green] "
                          f"[dim]({agent.model})[/dim]")
            if agent.model_spec.is_web:
                return prev
        except (KeyboardInterrupt, EOFError):
            pass
        return None

    def pick_effort() -> None:
        """/effort — untuk model web berarti MENGKLIK tombol mode berpikir di UI
        situsnya, bukan mengirim parameter API.

        Cabang model ber-API-key (menu effort dari reasoning_style, set_effort)
        DIHAPUS bersama katalog NVIDIA: seluruh model kini web, jadi satu-satunya
        jalur yang tersisa adalah pick_web_option."""
        pick_web_option()

    def pick_web_option() -> None:
        """/effort untuk model web: pilih tombol di UI situs (varian model /
        mode berpikir) lalu program yang mengekliknya di browser."""
        spec = agent.model_spec
        try:
            from .. import connectors
            conn = connectors.get_connector(spec.connector)
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]connector tak siap: {_esc(str(exc))}[/red]")
            return
        opts = conn.web_options()
        if not opts:
            console.print(
                f"  [dim]{_esc(spec.label)} tak punya tombol model/berpikir yang "
                "bisa diatur dari sini.[/dim]")
            return
        # inquirer TIDAK memproses markup rich — tulis polos, kalau tidak tag
        # seperti [dim] ikut tampil mentah di layar.
        width = max((len(t) for t, _ in opts), default=0)
        choices = [Choice(text, f"{text:<{width}}  —  {desc}")
                   for text, desc in opts]
        try:
            sel = inquirer.select(
                message=f"Tombol {spec.label} (diklik di UI web)",
                choices=choices, pointer="❯",
                long_instruction="Program akan mengklik tombol ini langsung di situsnya.",
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return

        state = {"msg": f"mengklik '{sel}' di {spec.label}…"}
        result: dict = {"ok": None, "error": None}

        def worker() -> None:
            try:
                result["ok"] = conn.set_web_option(sel)
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        wt = threading.Thread(target=worker, daemon=True)
        FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        try:
            with Live(_oneline(Text()), console=console, refresh_per_second=10,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    live.update(_oneline(Text.from_markup(
                        f"  [#cba6f7]{frame}[/] [dim]{_esc(state['msg'])}[/]")))
                    wt.join(timeout=0.1)
        except KeyboardInterrupt:
            wt.join(timeout=2.0)
            if wt.is_alive():
                try:
                    from ..connectors import browser as _br
                    _br.reset_hub()
                except Exception:  # noqa: BLE001
                    pass
            console.print("  [yellow]◼ dibatalkan[/yellow]\n")
            return

        if result["error"] is not None:
            console.print(f"  [yellow]⚠ {_esc(str(result['error']))}[/yellow]\n")
        else:
            console.print(f"  [#a6e3a1]✓ {_esc(str(result['ok']))}[/]\n")

    def _web_service_pick() -> str:
        """Pilih service web mana yang dikelola (kalau lebih dari satu punya
        profil login tersimpan). Return "" bila batal / tak ada."""
        from ..connectors import browser as _br
        svcs = []
        for _, _key, spec in models.catalog():
            if spec.connector and spec.connector not in svcs:
                if _br.profile_dir(spec.connector).exists():
                    svcs.append(spec.connector)
        if not svcs:
            console.print("  [dim]belum ada sesi AI web (belum pernah login).[/dim]\n")
            return ""
        if len(svcs) == 1:
            return svcs[0]
        try:
            return inquirer.select(
                message="Kelola sesi web milik layanan mana?",
                choices=[Choice(s, s) for s in svcs], pointer="❯").execute()
        except (KeyboardInterrupt, EOFError):
            return ""

    def _web_busy(msg: str, fn):
        """Jalankan aksi browser dengan baris status hidup (bisa Ctrl+C)."""
        result: dict = {"val": None, "err": None}

        def worker() -> None:
            try:
                result["val"] = fn()
            except BaseException as exc:  # noqa: BLE001
                result["err"] = exc

        wt = threading.Thread(target=worker, daemon=True)
        FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        try:
            with Live(_oneline(Text()), console=console, refresh_per_second=10,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    live.update(_oneline(Text.from_markup(
                        f"  [#cba6f7]{frame}[/] [dim]{_esc(msg)}[/]")))
                    wt.join(timeout=0.1)
        except KeyboardInterrupt:
            _reset_web_hub_if_stuck(wt)
            console.print("  [yellow]◼ dibatalkan[/yellow]\n")
            return None, KeyboardInterrupt()
        return result["val"], result["err"]

    def manage_web_sessions() -> None:
        """/web — kelola sesi AI web: hapus percakapan yang menumpuk di akun,
        atau logout (hapus profil login browser)."""
        from .. import connectors
        from ..connectors import browser as _br

        if not connectors.playwright_available():
            console.print("  [yellow]⚠ Connector butuh Playwright.[/]\n")
            return
        svc = _web_service_pick()
        if not svc:
            return
        conn = connectors.get_connector(svc)
        own = conn.own_chats()
        console.print(
            f"  [dim]Layanan:[/] [bold]{_esc(conn.label)}[/]   "
            f"[dim]chat tercatat dibuat bagas-ai:[/] [#94e2d5]{len(own)}[/]\n")

        choices = [
            Choice("prune", "🧹 Hapus chat lama buatan bagas-ai (sisakan N terbaru)"),
            Choice("pick", "🗂  Pilih chat untuk dihapus (daftar dari akun)"),
            Choice("allown", "🧨 Hapus SEMUA chat buatan bagas-ai"),
            Choice("logout", "🔌 Logout & hapus profil login browser"),
            Choice("cancel", "↩ Batal"),
        ]
        try:
            act = inquirer.select(message=f"Kelola sesi {conn.label}",
                                  choices=choices, pointer="❯").execute()
        except (KeyboardInterrupt, EOFError):
            return
        if act == "cancel":
            return

        if act == "logout":
            try:
                if not inquirer.confirm(
                        message=f"Hapus profil login {conn.label}? "
                                "(harus login ulang nanti)", default=False).execute():
                    return
            except (KeyboardInterrupt, EOFError):
                return
            ok = _br.forget_profile(svc)
            console.print(
                f"  [#a6e3a1]✓ profil {_esc(conn.label)} dihapus — login ulang "
                f"saat dipakai lagi.[/]\n" if ok else
                f"  [yellow]⚠ sebagian file profil masih terkunci; tutup Chrome "
                f"lalu coba lagi.[/]\n")
            return

        if not conn.supports_chat_admin():
            console.print(f"  [yellow]⚠ {_esc(conn.label)} belum mendukung "
                          "pengelolaan chat dari bagas-ai.[/]\n")
            return

        if act == "prune":
            if not own:
                console.print("  [dim]belum ada chat buatan bagas-ai.[/dim]\n")
                return
            try:
                keep_s = inquirer.text(
                    message="Sisakan berapa chat terbaru?", default="10").execute()
                keep = max(0, int(str(keep_s).strip() or "10"))
            except (KeyboardInterrupt, EOFError, ValueError):
                return
            if len(own) <= keep:
                console.print(f"  [dim]tak ada yang perlu dihapus "
                              f"({len(own)} <= {keep}).[/dim]\n")
                return
            n, err = _web_busy(f"menghapus {len(own) - keep} chat lama…",
                               lambda: conn.prune_own_chats(keep))
            if err is None:
                console.print(f"  [#a6e3a1]✓ {n} chat lama dihapus, "
                              f"{keep} terbaru disimpan.[/]\n")
            elif not isinstance(err, KeyboardInterrupt):
                console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")
            return

        if act == "allown":
            if not own:
                console.print("  [dim]belum ada chat buatan bagas-ai.[/dim]\n")
                return
            try:
                if not inquirer.confirm(
                        message=f"Hapus SEMUA {len(own)} chat buatan bagas-ai "
                                f"di {conn.label}?", default=False).execute():
                    return
            except (KeyboardInterrupt, EOFError):
                return
            n, err = _web_busy(f"menghapus {len(own)} chat…",
                               lambda: conn.prune_own_chats(0))
            if err is None:
                console.print(f"  [#a6e3a1]✓ {n} chat dihapus.[/]\n")
            elif not isinstance(err, KeyboardInterrupt):
                console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")
            return

        # act == "pick": ambil daftar chat dari akun lalu pilih yang mau dihapus.
        chats, err = _web_busy("mengambil daftar chat dari akun…", conn.list_chats)
        if err is not None:
            if not isinstance(err, KeyboardInterrupt):
                console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")
            return
        if not chats:
            console.print("  [dim]tak ada chat di akun ini.[/dim]\n")
            return
        own_ids = {r.get("id") for r in own}
        opts = []
        for c in chats[:80]:
            mark = " [dibuat bagas-ai]" if c.get("id") in own_ids else ""
            when = str(c.get("updated") or c.get("created") or "")[:10]
            opts.append(Choice(c["id"],
                               f"{(c.get('title') or '')[:56]:<58}{when}{mark}"))
        try:
            picked = inquirer.checkbox(
                message=f"Pilih chat untuk DIHAPUS ({len(chats)} total)",
                choices=opts, pointer="❯",
                instruction="(spasi pilih, enter konfirmasi)").execute()
        except (KeyboardInterrupt, EOFError):
            return
        if not picked:
            console.print("  [dim](tidak ada yang dihapus)[/dim]\n")
            return
        n, err = _web_busy(f"menghapus {len(picked)} chat…",
                           lambda: conn.delete_chats(list(picked)))
        if err is None:
            conn.forget_chats(set(picked))
            console.print(f"  [#a6e3a1]✓ {n} chat dihapus.[/]\n")
        elif not isinstance(err, KeyboardInterrupt):
            console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")

    def delete_sessions() -> None:
        sessions = session_mod.list_sessions()
        if not sessions:
            console.print("[dim](tidak ada sesi di folder ini)[/dim]")
            return
        try:
            if len(sessions) == 1:
                s = sessions[0]
                if inquirer.confirm(
                        message=f"Hapus sesi {s.id} ({session_mod.user_msg_count(s)} pesan)?",
                        default=False).execute():
                    if session_mod.delete(s):
                        console.print("[green]✓ 1 sesi dihapus.[/green]")
                        _delete_web_chats_of([s])
                return
            choices = [Choice(s.path.name,
                              f"{s.id}  ({session_mod.user_msg_count(s)} pesan)"
                              + ("  (aktif)" if s.id == agent.session.id else ""))
                       for s in sessions]
            picked = inquirer.checkbox(message="Pilih sesi untuk DIHAPUS",
                                       choices=choices, pointer="❯",
                                       instruction="(spasi pilih, enter konfirmasi)").execute()
        except (KeyboardInterrupt, EOFError):
            return
        removed = [s for s in sessions
                   if s.path.name in picked and session_mod.delete(s)]
        console.print(f"[green]✓ {len(removed)} sesi dihapus.[/green]" if removed
                      else "[dim](tidak ada yang dihapus)[/dim]")
        # Satu sesi terminal = satu percakapan browser -> ikut dihapus.
        _delete_web_chats_of(removed)

    def show_help() -> None:
        c = "#94e2d5"
        pout(Panel(
            "[dim]ketik pesan biasa untuk mengobrol dengan bagas-ai[/dim]\n\n"
            f"[{c}]/menu[/]     menu interaktif        [{c}]/model[/]    pilih model + saran\n"
            f"[{c}]/effort[/]   mode berpikir          [{c}]/new[/]      sesi baru\n"
            f"[{c}]/add-dir[/]  tambah folder konteks  [{c}]/dirs[/]     folder konteks aktif\n"
            f"[{c}]/rm-dir[/]   lepas folder konteks   [{c}]/delete[/]   hapus sesi\n"
            f"[{c}]/memory[/]   memori jangka panjang  [{c}]/scripts[/]  skrip tersimpan\n"
            f"[{c}]/reset[/]    kosongkan riwayat      [{c}]/clear[/]    bersihkan layar\n"
            f"[{c}]/review[/]   cari bug seluruh proyek [{c}]/scan[/]     segarkan peta proyek\n"
            f"[{c}]/bot[/]      bot Telegram on/off    [{c}]/permissions-bot[/] izin bot\n"
            f"[{c}]/live[/]     interaktif on/off      [{c}]/expand[/]   buka hasil (klik/tutup)\n"
            f"[{c}]/models[/]   daftar semua model     [{c}]/update[/]   cek pembaruan\n"
            f"[#f38ba8]/exit[/]     keluar",
            title="[bold #cba6f7]❔ Bantuan[/]", title_align="left",
            border_style="#cba6f7", box=box.ROUNDED, padding=(1, 2)))

    def do_update() -> None:
        console.print("\n  [dim]🔄 memeriksa pembaruan di GitHub…[/dim]")
        try:
            res = updater.check()
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memeriksa:[/red] {e}\n")
            return
        st = res.get("status")

        if st == "up_to_date":
            console.print(
                f"  [bold #a6e3a1]✓ bagas-ai sudah versi terbaru.[/]  "
                f"[dim]({res.get('local','')})[/dim]\n"
            )
            return
        if st == "no_git":
            console.print("  [red]✖ git tidak ditemukan[/red] — pasang git dulu agar bisa memperbarui.\n")
            return
        if st == "no_repo":
            console.print("  [yellow]ℹ Tak bisa menentukan sumber pembaruan (REPO_URL kosong).[/yellow]\n")
            return
        if st == "no_upstream":
            console.print("  [yellow]ℹ Tidak ada remote/upstream yang dilacak.[/yellow]\n")
            return
        if st == "fetch_error":
            console.print(f"  [red]✖ gagal fetch:[/red] {res.get('detail','')}\n")
            return

        if st == "setup_needed":
            # Instalasi tanpa repo git penopang (salinan pip / installer dari
            # folder). Bisa disiapkan otomatis: clone lalu reinstall.
            body = Text()
            body.append("Auto-update belum disiapkan untuk instalasi ini.\n\n",
                        style="bold #f9e2af")
            body.append(f"Sumber : {res.get('repo_url','')}\n", style="dim")
            body.append(f"Branch : {res.get('branch','')}", style="dim")
            pout(Panel(body, title="[bold #cba6f7]🔄 Siapkan pembaruan[/]",
                       title_align="left", border_style="#cba6f7",
                       box=box.ROUNDED, padding=(1, 2)))
            try:
                go = inquirer.confirm(message="Siapkan & perbarui sekarang?",
                                      default=True).execute()
            except (KeyboardInterrupt, EOFError):
                go = False
            if not go:
                console.print("  [dim](dilewati)[/dim]\n")
                return
            console.print("  [dim]⏳ menyiapkan repo & memasang pembaruan…[/dim]")
        elif st == "update_available":
            n = res.get("behind", "?")
            log = res.get("log", "")
            body = Text()
            body.append(f"{n} pembaruan tersedia  ", style="bold #f9e2af")
            body.append(f"({res.get('local','')} → {res.get('remote','')})\n\n",
                        style="dim")
            if log:
                for line in log.splitlines():
                    body.append("  • ", style="#89b4fa")
                    body.append(line + "\n")
            pout(Panel(body, title="[bold #cba6f7]🔄 Pembaruan bagas-ai[/]",
                       title_align="left", border_style="#cba6f7",
                       box=box.ROUNDED, padding=(1, 2)))
            try:
                go = inquirer.confirm(message="Terapkan pembaruan sekarang?",
                                      default=True).execute()
            except (KeyboardInterrupt, EOFError):
                go = False
            if not go:
                console.print("  [dim](dilewati)[/dim]\n")
                return
            console.print("  [dim]⏳ menarik & memasang pembaruan…[/dim]")
        else:
            console.print(f"  [red]✖ status tak terduga:[/red] {st}\n")
            return

        try:
            out = updater.apply()
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memperbarui:[/red] {e}\n")
            return
        ost = out.get("status")
        if ost == "pull_error":
            console.print(f"  [red]✖ git pull gagal:[/red] {out.get('detail','')}\n")
            return
        if ost == "clone_error":
            console.print(f"  [red]✖ clone gagal:[/red] {out.get('detail','')}\n")
            return
        if ost != "updated":
            console.print(f"  [red]✖ gagal ({ost}):[/red] {out.get('detail','')}\n")
            return
        if out.get("note"):
            note = f"\n  [#f9e2af]ℹ {_esc(out['note'])}[/]"
        elif not out.get("reinstalled"):
            note = f"  [dim](catatan pip: {_esc(out.get('pip_detail', ''))})[/dim]"
        else:
            note = ""
        console.print(
            "  [bold #a6e3a1]✓ bagas-ai diperbarui![/]  "
            "[dim]jalankan ulang[/dim] [#94e2d5]bagas-ai[/] "
            "[dim]agar perubahan aktif.[/dim]" + note + "\n"
        )

    def _dir_tree_panel(p, title: str) -> None:
        body = Text()
        body.append(f"{p}\n\n", style="bold #a6e3a1")
        body.append(workspace.tree(p), style="dim")
        pout(Panel(body, title=title, title_align="left",
                   border_style="#a6e3a1", box=box.ROUNDED, padding=(1, 2)))

    def do_add_dir(path: str) -> None:
        try:
            p = workspace.add(path)
        except ValueError as e:
            console.print(f"  [red]✖[/red] {e}\n")
            return
        agent.refresh_system_prompt()  # bagas-ai langsung "paham" folder ini
        _dir_tree_panel(p, "[bold #a6e3a1]📂 Folder konteks ditambahkan[/]")
        console.print(
            "  [dim]bagas-ai kini memahami & bisa baca/tulis file di folder ini "
            "(pakai path absolut).[/dim]\n"
        )

    def do_rm_dir(path: str) -> None:
        if workspace.remove(path):
            agent.refresh_system_prompt()
            console.print(f"  [#a6e3a1]✓ Folder konteks dilepas:[/] [dim]{path}[/dim]\n")
        else:
            console.print(f"  [yellow]ℹ Folder itu tidak ada di daftar konteks.[/yellow]\n")

    def show_dirs() -> None:
        dirs = workspace.list_dirs()
        if not dirs:
            console.print(
                "  [dim]Belum ada folder konteks tambahan.[/dim]  "
                "Pakai [#94e2d5]/add-dir <path>[/] untuk menambah.\n"
            )
            return
        body = Text()
        body.append("Folder yang bagas-ai pahami (selain root project):\n\n",
                    style="dim")
        for d in dirs:
            body.append("  📂 ", style="#a6e3a1")
            body.append(f"{d}\n")
        body.append("\nLepas dengan /rm-dir <path>.", style="dim")
        pout(Panel(body, title="[bold #a6e3a1]📂 Folder konteks[/]",
                   title_align="left", border_style="#a6e3a1",
                   box=box.ROUNDED, padding=(1, 2)))

    def do_action(action: str) -> bool:
        nonlocal agent, session
        if action in ("exit", "quit"):
            return True
        if action == "model":
            prev = _with_console(pick_model)
            if prev is not None:
                _connect_web(prev)
        elif action == "effort":
            _with_console(pick_effort)
        elif action == "web":
            _with_console(manage_web_sessions)
        elif action == "dirs":
            show_dirs()
        elif action == "delete":
            _with_console(delete_sessions)
        elif action == "new":
            _save_total()  # persist kontribusi sesi lama ke total global
            session = Session.create()
            agent = Agent(session=session)
            grand["base"] = prefs.get_total_tokens()  # sesi baru mulai dari total
            console.clear()
            show_logo()
            console.print()
            pout(_banner(agent, False), bottom=0)
            console.print()
        elif action == "reset":
            agent.reset()
            console.print("[dim](riwayat dikosongkan)[/dim]")
        elif action == "clear":
            console.clear()
            show_logo()
            console.print()
            pout(_banner(agent, False), bottom=0)
            console.print()
        elif action == "memory":
            facts = longmem.all_facts()
            pout(Panel("\n".join(f"• {f}" for f in facts) or "[dim]kosong[/dim]",
                       title="[bold #a6e3a1]🧠 Memory jangka panjang[/]",
                       title_align="left", border_style="#a6e3a1",
                       box=box.ROUNDED, padding=(1, 2)))
        elif action == "scripts":
            items = scripts.index_list()
            txt = "\n".join(f"• [#89b4fa]{it['name']}[/]: {it.get('description') or '-'}"
                            for it in items) or "[dim]belum ada[/dim]"
            pout(Panel(txt, title="[bold #89b4fa]📜 Script memory[/]",
                       title_align="left", border_style="#89b4fa",
                       box=box.ROUNDED, padding=(1, 2)))
        elif action == "help":
            show_help()
        elif action == "update":
            _with_console(do_update)
        elif action == "models":
            pout(_models_panel(agent.model))
        elif action == "scan":
            do_scan()
        elif action == "bot":
            do_bot()
        elif action in ("permissions-bot", "perms-bot", "permissions"):
            _with_console(do_permissions_bot)
        return False

    def do_scan() -> None:
        console.print("  [dim]🔍 memindai proyek & menyusun peta…[/dim]")
        try:
            txt = projectindex.ensure(force=True)
            agent.refresh_system_prompt()
            nfiles = txt.count("\n- ")
            console.print(
                f"  [#a6e3a1]✓ peta proyek diperbarui[/] [dim]· ~{nfiles} file · "
                f"{len(txt):,} karakter — bagas-ai kini paham struktur terbaru tanpa "
                f"baca ulang.[/]\n".replace(",", "."))
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memindai:[/red] {e}\n")

    # --- Bot Telegram DI DALAM sesi CLI -------------------------------------
    def _tg_event(kind: str, text: str) -> None:
        """Tampilkan aktivitas bot Telegram di terminal (dipanggil dari thread bot).

        Saat dropdown/menu (inquirer) sedang menggambar, mencetak dari thread
        lain MERUSAK tampilannya — event DITAHAN dulu lalu dicetak setelah
        menu selesai (lihat _with_console / choice_handler)."""
        if input_paused["on"]:
            _tg_pending.append((kind, text))
            if len(_tg_pending) > 50:
                _tg_pending.pop(0)
            return
        _tg_emit(kind, text)

    def _tg_flush() -> None:
        while _tg_pending:
            k, t = _tg_pending.pop(0)
            _tg_emit(k, t)

    def _tg_emit(kind: str, text: str) -> None:
        t = _esc(text or "")
        if kind == "in":
            console.print(f"\n  [#89b4fa]📲 Telegram ▸[/] [#cdd6f4]{t}[/]")
        elif kind == "out":
            snip = t if len(t) <= 600 else t[:600] + "…"
            console.print(f"  [#a6e3a1]  ↳ balasan:[/] [dim]{snip}[/]")
        elif kind == "perm":
            console.print(f"\n  [#f9e2af]🔔 {t}[/]")
        elif kind == "error":
            console.print(f"  [red]📲 error:[/] {t}")
        else:
            console.print(f"  [dim]📲 {t}[/]")

    def do_bot() -> None:
        svc = tg_service.get("svc")
        # Toggle-off juga untuk svc yang MASIH proses menyala (alive tapi belum
        # running) — kalau tidak, /bot berikutnya membuat service KEDUA dan dua
        # polling bentrok ("Conflict: terminated by other getUpdates").
        if svc is not None and (svc.running or svc.alive()):
            console.print("  [dim]📲 mematikan bot Telegram…[/dim]")
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                pass
            tg_service["svc"] = None
            console.print("  [#f9e2af]○ bot Telegram MATI.[/]\n")
            return
        if not config.TELEGRAM_BOT_TOKEN:
            console.print("  [red]✖ TELEGRAM_BOT_TOKEN belum diisi di .env[/] "
                          "[dim]— dapatkan dari @BotFather, lalu isi di "
                          f"{config.ENV_FILE}.[/]\n")
            return
        console.print("  [dim]📲 menyalakan bot Telegram…[/dim]")
        try:
            from .telegram_bot import TelegramService  # lazy: hindari impor berat
            svc = TelegramService()
            ok = svc.start(on_event=_tg_event)
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal:[/] {e}\n")
            return
        if ok:
            tg_service["svc"] = svc
            ids = sorted(telegram_perms.allowed_ids())
            idtxt = (str(ids) if ids
                     else "belum ada — kirim pesan pertama dari HP, kamu otomatis jadi pemilik")
            console.print(
                f"  [#a6e3a1]✓ bot Telegram AKTIF[/] [dim]— kontrol bagas-ai dari HP-mu "
                f"selama sesi ini hidup. Folder: {config.PROJECT_ROOT}\n"
                f"     ID diizinkan: {idtxt}. Aktivitas tampil di sini. "
                f"Atur izin: [/][#94e2d5]/permissions-bot[/][dim].[/]\n")
        elif svc.error is not None:
            console.print(f"  [red]✖ gagal menyalakan bot:[/] {svc.error}\n")
        elif svc.alive():
            # Belum 'running' tapi thread masih menyala (jaringan lambat) -> SIMPAN
            # supaya tak jadi bot yatim & tetap bisa dimatikan via /bot.
            tg_service["svc"] = svc
            console.print("  [#f9e2af]… bot lambat menyala[/] [dim]— tunggu sebentar "
                          "lalu coba kirim pesan; /bot lagi untuk mematikan.[/]\n")
        else:
            console.print("  [red]✖ bot berhenti tak terduga saat menyala.[/]\n")

    def do_permissions_bot() -> None:
        env_ids = set(config.TELEGRAM_ALLOWED_IDS)
        while True:
            pend = telegram_perms.pending()
            allowed = sorted(telegram_perms.allowed_ids())
            head = Text.from_markup(
                f"[bold #89b4fa]🔐 Izin bot Telegram[/]\n"
                f"[dim]Diizinkan:[/] {allowed or '(belum ada)'}\n"
                f"[dim]Menunggu izin:[/] {len(pend)}")
            pout(Panel(head, border_style="#89b4fa", box=box.ROUNDED, padding=(1, 2)))
            choices = []
            for cid, info in pend.items():
                choices.append(Choice(("approve", int(cid)),
                                      f"✅ Izinkan {info.get('name', '?')} (id {cid})"))
                choices.append(Choice(("deny", int(cid)), f"🗑 Tolak id {cid}"))
            for cid in allowed:
                if cid in env_ids:
                    continue  # dari .env -> ubah di .env, bukan di sini
                choices.append(Choice(("revoke", cid), f"🚫 Cabut izin id {cid}"))
            choices.append(Choice(("add", None), "➕ Tambah ID manual"))
            choices.append(Choice(("done", None), "↩ Selesai"))
            try:
                act = inquirer.select(message="Pilih aksi izin", choices=choices,
                                      pointer="❯").execute()
            except (KeyboardInterrupt, EOFError):
                return
            kind, cid = act
            if kind == "done":
                return
            if kind == "approve":
                telegram_perms.add_allowed(cid)
                console.print(f"  [green]✓ id {cid} kini diizinkan[/]")
            elif kind == "deny":
                telegram_perms.deny(cid)
                console.print(f"  [yellow]🗑 id {cid} ditolak[/]")
            elif kind == "revoke":
                telegram_perms.remove_allowed(cid)
                console.print(f"  [yellow]🚫 izin id {cid} dicabut[/]")
            elif kind == "add":
                try:
                    val = inquirer.text(message="ID Telegram (angka):").execute()
                except (KeyboardInterrupt, EOFError):
                    continue
                if val and val.strip().lstrip("-").isdigit():
                    telegram_perms.add_allowed(int(val.strip()))
                    console.print(f"  [green]✓ id {val.strip()} ditambahkan[/]")
                else:
                    console.print("  [yellow]ID harus angka.[/]")

    def open_menu() -> bool:
        try:
            action = inquirer.select(
                message="Menu bagas-ai", pointer="❯",
                choices=[Choice("model", "🔀 Ganti model"),
                         Choice("effort", "🎚 Mode / effort"),
                         Choice("new", "✨ Sesi baru"),
                         Choice("delete", "🗑 Hapus sesi"),
                         Choice("web", "🌐 Sesi AI web (hapus chat / logout)"),
                         Choice("memory", "🧠 Memory"),
                         Choice("scripts", "📜 Scripts"),
                         Choice("reset", "🧹 Reset riwayat"),
                         Choice("clear", "🖥 Bersihkan layar"),
                         Choice("update", "🔄 Cek pembaruan"),
                         Choice("help", "❔ Bantuan"),
                         Choice("exit", "🚪 Keluar"),
                         Choice("cancel", "↩ Batal")]).execute()
        except (KeyboardInterrupt, EOFError):
            return False
        if action == "cancel":
            return False
        return do_action(action)

    # --- input (prompt_toolkit hanya saat idle) ---
    # ATURAN UTAMA: Backspace polos = hapus 1 HURUF, SELALU, di terminal mana pun.
    #
    # Kenyataannya tombol Backspace bisa terkirim sebagai `backspace` (\x7f) ATAU
    # `c-h` (\x08) tergantung terminal/OS — dan tak bisa dibedakan dari Ctrl+
    # Backspace secara andal. Maka kita SENGAJA membiarkan KEDUA kode itu memakai
    # perilaku default prompt_toolkit (hapus 1 huruf) dan TIDAK PERNAH mengikatnya
    # ke hapus-kata. Ini menjamin Backspace polos tak akan pernah menghapus sekata.
    #
    # Hapus 1 KATA hanya lewat kombinasi yang MUSTAHIL sama dengan Backspace polos:
    #   - Ctrl+W        (c-w)
    #   - Alt+Backspace (escape, backspace)
    kb = KeyBindings()

    def _del_word(event):
        pos = event.current_buffer.document.find_start_of_previous_word()
        if pos is not None:
            event.current_buffer.delete_before_cursor(count=-pos)

    kb.add("c-w")(_del_word)                   # Ctrl+W
    kb.add("escape", "backspace")(_del_word)   # Alt+Backspace
    # Gaya status bar: latar gelap "catppuccin" + aksen warna per segmen.
    _pt_style = PTStyle.from_dict({
        "bottom-toolbar": "bg:#181825 #cdd6f4 noreverse",
        "sep": "#45475a",
        "brand": "#cba6f7 bold",
        "model": "#89b4fa bold",
        "eff": "#f5c2e7",
        "sesi": "#f9e2af",
        "total": "#a6e3a1",
        "cmd": "#94e2d5",
        "exit": "#f38ba8",
        "muted": "#7f849c",
        # Menu autocomplete "/..." — selaras tema catppuccin.
        "completion-menu": "bg:#1e1e2e #cdd6f4",
        "completion-menu.completion": "bg:#1e1e2e #cdd6f4",
        "completion-menu.completion.current": "bg:#cba6f7 #1e1e2e bold",
        "completion-menu.meta.completion": "bg:#181825 #7f849c",
        "completion-menu.meta.completion.current": "bg:#45475a #cdd6f4",
    })
    session_pt: PromptSession = PromptSession(
        key_bindings=kb,
        style=_pt_style,
        completer=SlashCompleter(),
        complete_while_typing=True,  # sugesti muncul otomatis saat mengetik
    )

    # Status bar token PERMANEN di paling bawah (selalu terlihat & rapi).
    def bottom_toolbar():
        s = agent.tokens_session
        total = grand["base"] + s.total
        spec = agent.model_spec
        # Seluruh model berbasis browser -> penanda selalu sama.
        kind = "🌐" if spec.is_web else "🤖"
        eff = ""
        sep = " <sep>│</sep> "
        return HTML(
            " <brand>⬢ bagas-ai</brand>"
            + sep
            + f"{kind} <model>{spec.label}</model>{eff}"
            + sep
            + f"<sesi>⚡ {_fmt(s.total)}</sesi> <muted>sesi</muted>"
            + sep
            + f"<total>🔋 {_fmt(total)}</total> <muted>total</muted>"
            + sep
            + "<cmd>/menu</cmd> <muted>·</muted> <exit>/exit</exit> "
        )

    while True:
        try:
            # patch_stdout: aktivitas bot Telegram (dari thread latar) tercetak
            # RAPI di atas prompt, tak merusak baris input.
            with patch_stdout(raw=True):
                raw = session_pt.prompt(
                    HTML('<style fg="#cba6f7"><b>❯</b></style> '),
                    bottom_toolbar=bottom_toolbar)
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        text = raw.strip()
        if not text:
            continue
        if text.startswith("/"):
            cmd = text[1:].strip().lower()
            if cmd == "menu":
                if _with_console(open_menu):
                    break
            elif cmd == "review":
                # Audit bug/kesalahan sistem menyeluruh — dijalankan sbg giliran.
                console.print("  [dim]🔎 mereview proyek untuk bug & kesalahan "
                              "sistem…[/dim]")
                try:
                    process(_REVIEW_PROMPT)
                except KeyboardInterrupt:
                    console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
                _save_total()
            elif cmd.startswith("model ") or cmd == "model":
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    prev_model = agent.model
                    try:
                        console.print(f"[green]✓ Model: {agent.set_model(parts[1])}[/green]")
                    except ValueError as e:
                        console.print(f"[red]{e}[/red]")
                    else:
                        # Model connector web: CONNECT sekarang juga (login sekali
                        # bila belum pernah; sudah pernah -> langsung ke sesi chat).
                        if agent.model_spec.is_web:
                            _connect_web(prev_model)
                else:
                    prev_model = _with_console(pick_model)
                    if prev_model is not None:
                        _connect_web(prev_model)
            elif cmd == "add-dir" or cmd.startswith("add-dir "):
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    do_add_dir(parts[1].strip().strip('"').strip("'"))
                else:
                    console.print("  [yellow]Pakai: /add-dir <path folder>[/yellow]\n")
            elif cmd == "rm-dir" or cmd.startswith("rm-dir "):
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    do_rm_dir(parts[1].strip().strip('"').strip("'"))
                else:
                    console.print("  [yellow]Pakai: /rm-dir <path folder>[/yellow]\n")
            elif cmd == "live":
                tui_mode["on"] = not tui_mode["on"]
                if tui_mode["on"]:
                    console.print("  [#a6e3a1]✓ tampilan interaktif AKTIF[/] "
                                  "[dim]— hasil langkah bisa dibuka/tutup realtime "
                                  "dengan Ctrl+R selagi AI berjalan (tetap inline).[/]\n")
                else:
                    console.print("  [#f9e2af]○ tampilan interaktif MATI[/] "
                                  "[dim]— pakai tampilan mengalir biasa; buka hasil "
                                  "lewat /expand N.[/]\n")
            elif cmd == "expand" or cmd.startswith("expand "):
                parts = text.split(maxsplit=1)
                arg = parts[1].strip().lstrip("#") if len(parts) == 2 else ""
                if not arg:
                    # Tanpa nomor -> cetak ulang semua hasil giliran terakhir.
                    open_step_viewer()
                elif arg.isdigit():
                    show_expand(int(arg))
                else:
                    console.print("  [yellow]Pakai: /expand (semua) "
                                  "atau /expand <nomor>[/yellow]\n")
            else:
                if do_action(cmd):
                    break
            continue
        try:
            process(text)
        except KeyboardInterrupt:
            # Jaring pengaman terakhir: Ctrl+C tak boleh menjatuhkan REPL.
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        _save_total()

    _save_total()
    if tg_service.get("svc") is not None:
        # stop() aman dipanggil apa pun keadaannya (running / masih menyala).
        try:
            tg_service["svc"].stop()
        except Exception:  # noqa: BLE001
            pass
    # Tutup browser connector dengan RAPI supaya Chrome tak mengira dirinya
    # crash & menawarkan "Restore pages?" saat dipakai lagi.
    try:
        from ..connectors import browser as _br
        _br.shutdown()
    except Exception:  # noqa: BLE001
        pass
    console.clear()
    console.print("\n  [#cba6f7]⬢ bagas-ai[/]  [dim]— sampai jumpa! 👋[/dim]\n")


if __name__ == "__main__":
    main()

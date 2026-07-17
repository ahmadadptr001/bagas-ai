"""Antarmuka CLI bagasAI (sinkron & bersih).

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
from openai import RateLimitError  # noqa: E402
from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.completion import Completer, Completion  # noqa: E402
from prompt_toolkit.formatted_text import HTML  # noqa: E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: E402
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

from .. import config, interaction, llm, longmem, models, osinfo, prefs, projectindex, scripts, updater, workspace  # noqa: E402
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
    blok kode ```lang``` disorot tema `dracula`)."""
    return Markdown(text, code_theme=_CODE_THEME)

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
            n = cache.get("behind", "?")
            local, remote = cache.get("local", ""), cache.get("remote", "")
            ver = f" ({local} → {remote})" if local and remote else ""
            pout(
                f"[#f9e2af]⬆ Pembaruan bagasAI tersedia[/] "
                f"[dim]— {n} commit lebih baru{ver}.[/dim]  "
                f"Ketik [#94e2d5]/update[/] untuk memperbarui.",
                bottom=0,
            )
        # Segarkan cache untuk startup berikutnya (jalan di thread latar).
        updater.background_refresh()
    except Exception:
        pass

# Gradasi ungu -> biru (magenta neon) untuk teks shadow.
_GRAD = ["#f0abfc", "#e879f9", "#c084fc", "#a855f7", "#7c3aed", "#4f46e5", "#2563eb"]

# ASCII "virus" (spike protein) — aksen di atas logo.
_VIRUS = r"""
        .  o   o  .
      o   \  |  /   o
   o ---- ( ((*)) ) ---- o
      o   /  |  \   o
        '  o   o  '
"""


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


# Warna gaya editor (GitHub-like): teks terang di atas bg gelap hijau/merah.
_ADD = "#c9f5cf on #123d1c"
_DEL = "#f5c9c9 on #3d1212"
_CTX = "grey50"
_GUT_A = "#5bd66f on #0d2a14"
_GUT_D = "#e06b6b on #2a0d0d"


def _row(lineno: str, sign: str, text: str, style: str) -> None:
    """Cetak satu baris gaya editor '123 + kode' dengan bg + margin tepi."""
    inner = min(console.width - 2 * _LPAD, 108)
    line = Text(" " * _LPAD)  # margin kiri tanpa background
    line.append(f" {lineno:>4} {sign} ", style=style)
    body = f"{text}".replace("\t", "    ")
    line.append(body, style=style)
    pad = (_LPAD + inner) - line.cell_len  # isi bg sampai batas kanan
    if pad > 0:
        line.append(" " * pad, style=style)
    console.print(line)


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
    m = " " * _LPAD  # indent kiri agar tidak mepet
    # Virus (hijau neon)
    for ln in _VIRUS.split("\n"):
        if ln.strip():
            console.print(Text(m + ln, style="bold green"))
    # Teks "bagasAI" gaya ANSI Shadow, gradasi per baris (efek glow/shadow)
    if Figlet is not None:
        try:
            art = Figlet(font="ansi_shadow").renderText("bagasAI")
            lines = [ln for ln in art.split("\n") if ln.strip()]
        except Exception:
            lines = ["b a g a s A I"]
    else:
        lines = ["b a g a s A I"]
    for i, ln in enumerate(lines):
        console.print(Text(m + ln, style=f"bold {_GRAD[min(i, len(_GRAD) - 1)]}"))
    sub = Text(m + "  ")
    sub.append("AI agent serbaguna", style="bold white")
    console.print(sub)


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
        """Tandai bahwa bagasAI sedang menunggu rate limit lalu melanjutkan."""
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
            return t

        # Mode menunggu rate limit: tampilkan hitung mundur + jaminan lanjut.
        if now < self.retry_until:
            left = self.retry_until - now
            t = Text()
            t.append(f"  {frame} ", style="bold #f9e2af")
            t.append("NVIDIA sibuk — menunggu lalu melanjutkan", style="#f9e2af")
            t.append(f"  {left:.0f}s", style="bold #fab387")
            if self.retry_msg:
                t.append(f"  ·  {self.retry_msg}", style="dim #f9e2af")
            t.append("     Ctrl+C batal", style="dim italic")
            return t

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
        return t


class TurnView:
    """Tampilan SATU GILIRAN yang hidup INLINE (rich.Live, TANPA layar-penuh),
    persis alur terminal biasa. Seluruh giliran (narasi, langkah, jawaban)
    dirender di region yang terus diperbarui; hasil tiap langkah bisa DIBUKA/
    ditutup secara realtime dengan Ctrl+R (seperti Claude). Saat giliran selesai,
    region ini 'membeku' jadi bagian riwayat terminal (transient=False)."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.start = time.time()
        self._lock = threading.Lock()
        self.items: list[tuple[str, object]] = []  # ("narasi",str)|("step",rec)
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

    # --- mutasi (dipanggil dari worker) ---
    def add_narasi(self, text: str) -> None:
        if text and text.strip():
            with self._lock:
                self.items.append(("narasi", text.strip()))

    def start_step(self, n: int, name: str, label: str) -> dict:
        rec = {"n": n, "name": name, "label": label, "result": "",
               "failed": False, "running": True, "expanded": False}
        with self._lock:
            self.items.append(("step", rec))
        self.tool = name
        self.phase = _PHASE.get(name, "bekerja")
        return rec

    def end_step(self, rec: dict, result: str, failed: bool) -> None:
        rec["result"] = result or ""
        rec["failed"] = failed
        rec["running"] = False
        self.tool = None
        self.phase = "berpikir"

    def note_retry(self, wait: float, msg: str) -> None:
        self.retry_until = time.time() + wait
        self.retry_msg = msg

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
    def _render_step(self, rec: dict) -> list:
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
        head = Text.from_markup(
            f"  {icon} [#cdd6f4]{phase}[/]  [white]{_esc(label)}[/]"
            f"   [dim #94e2d5]#{n}[/]"
        )
        out = [head]
        text = re.sub(r"^exit_code=\S+\n?", "", (rec["result"] or "").strip())
        lines = [ln for ln in text.splitlines()]
        nlines = len([ln for ln in lines if ln.strip()])
        if running:
            out.append(Text("     menjalankan…", style="italic #6c7086"))
        elif not text:
            pass
        elif rec["expanded"] or self.expanded:
            cap = 40
            shown = lines[:cap]
            body = Text("\n".join("     " + ln for ln in shown),
                        style="#f5c9c9" if failed else "#a6adc8")
            out.append(body)
            if len(lines) > cap:
                out.append(Text(f"     … {len(lines) - cap} baris lagi (/expand {n})",
                                style="dim"))
        else:
            unit = "hasil" if rec["name"] == "web_search" else "baris"
            tag = "[#f38ba8]gagal[/] · " if failed else ""
            out.append(Text.from_markup(f"     [dim]{tag}{nlines} {unit}[/]"))
        return out

    def _blocks(self) -> list:
        """Urutan (tag, renderable) untuk render & pemetaan-klik. tag =
        ('step', n) bila baris itu milik langkah #n, else ('other', None)."""
        blocks: list = []
        with self._lock:
            items = list(self.items)
            answer = self.answer
        header_shown = False
        for kind, val in items:
            if kind == "narasi":
                if not header_shown:
                    blocks.append((("other", None),
                                   Padding(Text("🤖 bagasAI", style="bold #89b4fa"),
                                           (1, 0, 0, 2))))
                    header_shown = True
                blocks.append((("other", None), Padding(_md(val), (0, 3, 1, 3))))
            else:
                for r in self._render_step(val):
                    blocks.append((("step", val["n"]), r))
        if answer:
            blocks.append((("other", None),
                           Padding(Text("🤖 bagasAI", style="bold #89b4fa"), (1, 0, 0, 2))))
            blocks.append((("other", None), Padding(_md(answer), (0, 3, 1, 3))))
        blocks.append((("other", None), self._footer()))
        return blocks

    def __rich__(self):
        return Group(*[r for _, r in self._blocks()])

    def _footer(self):
        el = time.time() - self.start
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]
        now = time.time()
        if self.cancelling:
            return Text.from_markup(
                f"  [bold #f38ba8]{frame}[/] [#f38ba8]membatalkan — "
                f"menunggu langkah aman berhenti[/]   [dim italic]Ctrl+C lagi = paksa[/]")
        if now < self.retry_until:
            left = self.retry_until - now
            return Text.from_markup(
                f"  [bold #f9e2af]{frame}[/] [#f9e2af]NVIDIA sibuk — menunggu lalu "
                f"melanjutkan[/] [bold #fab387]{left:.0f}s[/]   [dim italic]Ctrl+C batal[/]")
        target = float(self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30
        if abs(target - self.disp) < 1:
            self.disp = target
        tok = _fmt(int(self.disp))
        if self.done:
            with self._lock:
                stps = [v for k, v in self.items if k == "step"]
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
            return Text.from_markup(
                "  [dim]" + " · ".join(seg) + "[/]   [dim]·[/]   "
                "[#94e2d5]/expand N[/][dim] lihat penuh[/]")
        extra = f"   [dim]·[/]   [#f5c2e7]🔧 {self.tool}[/]" if self.tool else ""
        eff = getattr(self.agent, "effort", None)
        effseg = f"   [dim]·[/]   [#f5c2e7]◇ effort {eff}[/]" if eff else ""
        return Text.from_markup(
            f"  [bold #cba6f7]{frame}[/] [#cba6f7]{self.phase}[/]   [dim]·[/]   "
            f"[#89b4fa]{_fmt_elapsed(el)}[/]   [dim]·[/]   [#f9e2af]⚡ {tok}[/] "
            f"[dim]token[/]{effseg}{extra}"
            f"   [dim italic]Ctrl+C batal[/]")


# ---------------------------------------------------------------------------
# Komponen tampilan
# ---------------------------------------------------------------------------
def _banner(agent: Agent, resumed: bool) -> Panel:
    spec = agent.model_spec
    kind = ("🧠 reasoning" if spec.reasoning
            else "🖼 multimodal" if spec.multimodal else "🤖 chat")
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
                 title="[bold #cba6f7]⬢ bagasAI[/]", title_align="left")


def _models_panel(current_id: str) -> Panel:
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=False)
    tbl.add_column("#", style="dim", justify="right")
    tbl.add_column("alias", style="bold cyan")
    tbl.add_column("model", style="white")
    tbl.add_column("kemampuan", style="dim")
    tbl.add_column("aktif", justify="center")
    for i, key, spec in models.catalog():
        tags = []
        if spec.multimodal:
            tags.append("multimodal")
        if spec.reasoning:
            tags.append("thinking")
        mark = "[bold green]●[/bold green]" if spec.id == current_id else ""
        tbl.add_row(str(i), key, spec.label, ", ".join(tags) or "-", mark)
    return Panel(tbl, title="[bold]🔀 Model tersedia[/bold]", border_style="cyan",
                 box=box.ROUNDED)




# ---------------------------------------------------------------------------
# Loop utama
# ---------------------------------------------------------------------------
def main(resume: bool = False) -> None:
    config.require_api_key()
    console.clear()

    # Deteksi OS & sinkronkan ke memory SEBELUM agent dibangun, agar system
    # prompt (yang memuat OS) sudah benar. add/update/lewati ditangani di sini.
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

    agent = Agent(session=session)

    show_logo()
    console.print()
    pout(_banner(agent, resumed), bottom=0)
    if resumed:
        console.print(Padding(Rule("[dim]percakapan sebelumnya[/dim]",
                                    style="#313244"), (1, 0, 0, 0)))
        for m in agent.memory.messages:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                console.print(f"\n  [bold #cba6f7]❯[/] [#cba6f7]{content}[/]")
            elif role == "assistant" and content:
                console.print("\n  [bold #89b4fa]🤖 bagasAI[/]")
                console.print(Padding(_md(content), (0, 3, 1, 3)))
        console.print(Rule("[dim]lanjut di bawah[/dim]", style="#313244"))
    if os_status in ("added", "updated"):
        verb = "terdeteksi & disimpan" if os_status == "added" else "diperbarui"
        pout(f"[dim]🖥  OS {verb}: {osinfo.summary()} — perintah terminal akan "
             f"disesuaikan.[/dim]", bottom=0)
    # Peta proyek: sudah dibangun/di-cache saat Agent dibuat -> bagasAI paham
    # proyek tanpa baca ulang tiap giliran/ganti model/resume.
    try:
        _pmap = projectindex.ensure()
        _pn = _pmap.count("\n- ")
        if _pn:
            pout(f"[dim]🗺  peta proyek siap (~{_pn} file) — bagasAI sudah paham "
                 f"strukturnya; ketik [/][#94e2d5]/scan[/][dim] untuk menyegarkan.[/]",
                 bottom=0)
    except Exception:  # noqa: BLE001
        pass
    _update_notice()  # info bila versi usang (dari cache) + cek ulang di latar
    console.print()

    live_holder: dict = {"live": None}
    status_obj = Status(agent)
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

    def choice_handler(question: str, options: list[str], multiple: bool) -> str:
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
        if name == "write_file" and p:
            full = config.PROJECT_ROOT / p
            exists = full.exists()
            old = full.read_text(encoding="utf-8", errors="replace") if exists else ""
            new = args.get("content", "") if isinstance(args, dict) else ""
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

    def process(text: str) -> None:
        """Jalankan satu giliran INLINE (tanpa layar-penuh, tetap di alur terminal
        biasa). Seluruh giliran dirender di satu region rich.Live yang hidup &
        membeku jadi riwayat saat selesai. Hasil langkah bisa dibuka/tutup realtime
        dengan Ctrl+R. Ctrl+C membatalkan. Bila gagal, jatuh ke process_classic."""
        steps.clear()
        step_ctr["n"] = 0
        cur_step.clear()
        turn_start = time.time()
        if not tui_mode["on"]:
            process_classic(text)
            return

        view = TurnView(agent)
        ctr = {"n": 0}

        def _on_tool(name: str, args: dict) -> None:
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
            if name == "write_file" and p:
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
            view.add_narasi(content)

        def _on_retry(attempt: int, wait: float, exc: Exception) -> None:
            view.note_retry(wait, f"percobaan ke-{attempt}")

        cancel_event = threading.Event()
        result: dict = {"answer": None, "error": None}

        def worker() -> None:
            try:
                result["answer"] = agent.run(
                    text, on_tool=_on_tool, on_message=_on_msg,
                    on_retry=_on_retry, cancel_event=cancel_event,
                    on_tool_result=_on_result,
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
        try:
            with Live(view, console=console, refresh_per_second=12,
                      transient=False, vertical_overflow="visible") as live:
                live_holder["live"] = live
                worker_thread.start()
                while worker_thread.is_alive():
                    try:
                        if mouse is not None:
                            got = False
                            for ev in mouse.poll():
                                got = True
                                if ev[0] == "click":
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
                view.done = True
                if result["answer"] and not result["error"]:
                    view.answer = (result["answer"] or "").strip() or None
                live.refresh()
        except KeyboardInterrupt:
            interrupted = True
            cancel_event.set()
        finally:
            live_holder["live"] = None
            if mouse is not None:
                try:
                    mouse.disable()
                except Exception:  # noqa: BLE001
                    pass

        err = result["error"]
        if interrupted or isinstance(err, (KeyboardInterrupt, llm.Cancelled)):
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        elif isinstance(err, RateLimitError):
            console.print("\n  [yellow]⏳ rate limit NVIDIA (~40 permintaan/menit) — "
                          "tunggu ~1 menit lalu coba lagi[/yellow]\n")
        elif err is not None:
            console.print(f"\n  [red]✖ error:[/red] {err}\n")
        # Sukses: seluruh giliran (narasi, langkah, jawaban, footer) sudah
        # membeku dari region live -> tak perlu cetak apa pun lagi.
        _reindex_if_edited()

    def _reindex_if_edited() -> None:
        """Bila giliran barusan menulis/menghapus file, segarkan PETA PROYEK &
        system prompt supaya pemahaman bagasAI selalu sesuai kode terbaru."""
        if any(s.get("name") in ("write_file", "delete_file")
               for s in steps.values()):
            try:
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
            """Tampilkan ucapan/narasi bagasAI: 1 header per giliran, indentasi rapi."""
            if not content or not content.strip():
                return
            console.print()
            if not header["shown"]:
                console.print("  [bold #89b4fa]🤖 bagasAI[/]")
                header["shown"] = True
            console.print(Padding(_md(content.strip()), (0, 3, 1, 3)))

        def on_retry(attempt: int, wait: float, exc: Exception) -> None:
            """NVIDIA rate-limit: bagasAI menunggu lalu MELANJUTKAN, bukan gagal."""
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

        err = result["error"]
        if forced or interrupted or isinstance(err, (KeyboardInterrupt, llm.Cancelled)):
            # Memory sudah dirapikan & disimpan di dalam agent.run().
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        elif isinstance(err, RateLimitError):
            console.print("\n  [yellow]⏳ rate limit NVIDIA (~40 permintaan/menit) — "
                          "tunggu ~1 menit lalu coba lagi[/yellow]\n")
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
    def pick_model() -> None:
        def _describe(spec) -> str:
            # Satu baris: nama (rata) + badge kemampuan + SARAN "cocok untuk apa".
            badge = ""
            if spec.reasoning:
                badge += "🧠"
            if spec.multimodal:
                badge += "🖼"
            badge = f" {badge}" if badge else "  "
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
            console.print(f"[green]✓ Model: {agent.set_model(sel)}[/green] "
                          f"[dim]({agent.model})[/dim]")
        except (KeyboardInterrupt, EOFError):
            pass

    def pick_effort() -> None:
        if not agent.model_spec.supports_effort():
            console.print(
                f"  [dim]Model [bold]{agent.model_spec.label}[/bold] menjawab langsung "
                "— tidak punya mode berpikir yang bisa diatur.[/dim]"
            )
            return
        choices = [
            Choice(key, f"{icon}  {title:<9} —  {desc}")
            for key, title, desc, icon in agent.model_spec.effort_info()
        ]
        try:
            sel = inquirer.select(
                message="Mode berpikir — seberapa dalam bagasAI menalar?",
                choices=choices, default=agent.effort, pointer="❯",
                long_instruction="Makin dalam = makin cermat tapi lebih lambat & boros token.",
            ).execute()
            agent.set_effort(sel)
            title = dict((k, t) for k, t, _, _ in agent.model_spec.effort_info()).get(sel, sel)
            console.print(f"  [green]✓ Mode berpikir: [bold]{title}[/bold][/green]")
        except (KeyboardInterrupt, EOFError):
            pass

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
                    session_mod.delete(s)
                    console.print("[green]✓ 1 sesi dihapus.[/green]")
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
        count = sum(1 for s in sessions if s.path.name in picked and session_mod.delete(s))
        console.print(f"[green]✓ {count} sesi dihapus.[/green]" if count
                      else "[dim](tidak ada yang dihapus)[/dim]")

    def show_help() -> None:
        c = "#94e2d5"
        pout(Panel(
            "[dim]ketik pesan biasa untuk mengobrol dengan bagasAI[/dim]\n\n"
            f"[{c}]/menu[/]     menu interaktif        [{c}]/model[/]    pilih model + saran\n"
            f"[{c}]/effort[/]   mode berpikir          [{c}]/new[/]      sesi baru\n"
            f"[{c}]/add-dir[/]  tambah folder konteks  [{c}]/dirs[/]     folder konteks aktif\n"
            f"[{c}]/rm-dir[/]   lepas folder konteks   [{c}]/delete[/]   hapus sesi\n"
            f"[{c}]/memory[/]   memori jangka panjang  [{c}]/scripts[/]  skrip tersimpan\n"
            f"[{c}]/reset[/]    kosongkan riwayat      [{c}]/clear[/]    bersihkan layar\n"
            f"[{c}]/review[/]   cari bug seluruh proyek [{c}]/scan[/]     segarkan peta proyek\n"
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
                f"  [bold #a6e3a1]✓ bagasAI sudah versi terbaru.[/]  "
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
            pout(Panel(body, title="[bold #cba6f7]🔄 Pembaruan bagasAI[/]",
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
        note = "" if out.get("reinstalled") else (
            f"  [dim](catatan pip: {out.get('pip_detail','')})[/dim]")
        console.print(
            "  [bold #a6e3a1]✓ bagasAI diperbarui![/]  "
            "[dim]jalankan ulang[/dim] [#94e2d5]bagasAI[/] "
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
        agent.refresh_system_prompt()  # bagasAI langsung "paham" folder ini
        _dir_tree_panel(p, "[bold #a6e3a1]📂 Folder konteks ditambahkan[/]")
        console.print(
            "  [dim]bagasAI kini memahami & bisa baca/tulis file di folder ini "
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
        body.append("Folder yang bagasAI pahami (selain root project):\n\n",
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
            pick_model()
        elif action == "effort":
            pick_effort()
        elif action == "dirs":
            show_dirs()
        elif action == "delete":
            delete_sessions()
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
            do_update()
        elif action == "models":
            pout(_models_panel(agent.model))
        elif action == "scan":
            do_scan()
        return False

    def do_scan() -> None:
        console.print("  [dim]🔍 memindai proyek & menyusun peta…[/dim]")
        try:
            txt = projectindex.ensure(force=True)
            agent.refresh_system_prompt()
            nfiles = txt.count("\n- ")
            console.print(
                f"  [#a6e3a1]✓ peta proyek diperbarui[/] [dim]· ~{nfiles} file · "
                f"{len(txt):,} karakter — bagasAI kini paham struktur terbaru tanpa "
                f"baca ulang.[/]\n".replace(",", "."))
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memindai:[/red] {e}\n")

    def open_menu() -> bool:
        try:
            action = inquirer.select(
                message="Menu bagasAI", pointer="❯",
                choices=[Choice("model", "🔀 Ganti model"),
                         Choice("effort", "🎚 Mode / effort"),
                         Choice("new", "✨ Sesi baru"),
                         Choice("delete", "🗑 Hapus sesi"),
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
        kind = "🧠" if spec.reasoning else ("🖼" if spec.multimodal else "🤖")
        eff = f" <eff>◇ {agent.effort}</eff>" if agent.effort else ""
        sep = " <sep>│</sep> "
        return HTML(
            " <brand>⬢ bagasAI</brand>"
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
                if open_menu():
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
                    try:
                        console.print(f"[green]✓ Model: {agent.set_model(parts[1])}[/green]")
                    except ValueError as e:
                        console.print(f"[red]{e}[/red]")
                else:
                    pick_model()
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
    console.clear()
    console.print("\n  [#cba6f7]⬢ bagasAI[/]  [dim]— sampai jumpa! 👋[/dim]\n")


if __name__ == "__main__":
    main()

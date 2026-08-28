"""MessageList widget — area gulir untuk seluruh isi percakapan.

Menampilkan gema pengguna, jawaban AI (markdown), langkah tool,
pemberitahuan, diff, dan gambar. Gulir otomatis ke bawah.

CATATAN BUG YANG SUDAH DIPERBAIKI (jangan diulang):

1. Dulu ini ``Widget`` yang membungkus ``RichLog`` anak dan menyimpannya di
   ``self._log`` pada ``on_mount``. Setiap metode diawali ``if not self._log:
   return`` sehingga SEMUA tulisan sebelum mount HILANG TANPA JEJAK. Sekarang
   kelas ini TURUNAN ``RichLog`` langsung — RichLog sendiri sudah menunda
   tulisan sampai ukurannya diketahui (``_deferred_renders``).
2. ``RichLog.min_width`` bawaannya 78. Semua tulisan dirender minimal 78 kolom
   lalu dipotong horizontal di terminal sempit. Sekarang ``min_width=1`` dan
   setiap tulisan diberi ``width=`` selebar area isi.
3. ``RichLog`` bawaannya ``can_focus=True``: klik di riwayat merebut fokus dari
   kotak input dan ketikan berikutnya hilang. Sekarang ``can_focus=False``.
4. RichLog menyimpan hasil render sebagai ``Strip`` dan TIDAK pernah
   membungkus ulang saat lebar berubah. Semua tulisan sekarang lewat
   ``_emit()`` yang juga menyimpan renderable-nya, jadi ``on_resize`` bisa
   menggambar ulang (dengan jeda) agar teks ikut lebar baru.
5. ``append_notice`` dulu hanya memberi spasi di baris PERTAMA
   (``f"  {text}"``), sehingga seluruh keluaran berkotak (mis. ``/help``)
   miring dan patah. Sekarang setiap baris diberi indentasi.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from collections import deque
from typing import TYPE_CHECKING

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual.events import Resize
from rich.style import Style as RichStyle
from textual.strip import Strip
from textual.widgets import RichLog

from ...ui import tema

if TYPE_CHECKING:
    from ...core import Agent

# Renderable yang disimpan untuk digambar ulang saat lebar layar berubah.
_MAKS_RIWAYAT = 1500
# Batas baris di dalam RichLog (menahan pemakaian memori sesi panjang).
_MAKS_BARIS_LOG = 12000
# Jeda gambar-ulang setelah resize berhenti (detik).
_JEDA_RESIZE = 0.2


def _bersih_kendali(text: str) -> str:
    """Buang escape ANSI supaya tidak bocor sebagai teks mentah."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _indent(text: str, pad: str = "  ") -> str:
    """Beri indentasi pada SETIAP baris (bukan cuma baris pertama)."""
    return "\n".join(pad + b for b in text.split("\n"))


def _pagari_pohon(text: str) -> str:
    """Bungkus diagram pohon gundul ke dalam blok ```text.

    Hanya membungkus bila ada minimal 2 baris berkarakter penghubung pohon
    DAN belum ada fence ```/~~~ di dalamnya.
    """
    lines = text.split("\n")
    if any(re.match(r"^\s*(```|~~~)", b) for b in lines):
        return text
    tree_lines = sum(1 for b in lines
                     if re.match(r"^\s*[│├└┃┣┗]", b) and re.search(r"[├└┣┗]", b))
    if tree_lines < 2:
        return text
    return "```text\n" + text + "\n```"


class MessageList(RichLog, can_focus=False):
    """Riwayat percakapan yang bisa digulir; mengisi ruang sisa layar."""

    def __init__(self, agent: "Agent | None" = None, **kwargs):
        super().__init__(
            markup=False,      # isi sudah berupa Text/Markdown ber-style;
                               # markup=True bikin "[foto]" ditelan sebagai tag
            highlight=False,    # jangan pakai ReprHighlighter
            wrap=True,
            auto_scroll=True,
            min_width=1,        # jangan paksa 78 kolom
            max_lines=_MAKS_BARIS_LOG,
            **kwargs,
        )
        self._agent = agent
        self._items: deque[RenderableType] = deque(maxlen=_MAKS_RIWAYAT)
        self._lebar_terakhir = 0
        self._timer_resize = None
        self._menggambar_ulang = False
        # Keadaan aliran token
        self._stream_buf = ""
        self._stream_lock = threading.Lock()
        self._stream_rendered = False
        self._stream_preview_len = 0
        # Seleksi salin-otomatis (lihat "Salin otomatis" di bawah).
        # _salin_jangkar: baris jangkar saat tombol kiri ditahan (None = tak
        # sedang menyeret). _salin_lo/_salin_hi: rentang tersorot — TETAP
        # hidup setelah tombol dilepas supaya sorotannya terlihat, lenyap saat
        # klik berikutnya.
        self._salin_jangkar: int | None = None
        self._salin_lo: int | None = None
        self._salin_hi: int | None = None

    # --- Inti penulisan ------------------------------------------------

    def _lebar(self) -> int | None:
        """Lebar render = lebar area isi. None bila ukuran belum diketahui."""
        try:
            w = self.scrollable_content_region.width
        except Exception:  # noqa: BLE001 — belum ter-mount
            return None
        return max(20, w) if w else None

    def _emit(self, renderable: RenderableType, simpan: bool = True) -> None:
        """Satu-satunya jalan tulis ke log.

        ``simpan=False`` untuk isi sementara yang tak perlu digambar ulang.
        """
        if simpan:
            self._items.append(renderable)
        try:
            self.write(renderable, width=self._lebar())
        except Exception:  # noqa: BLE001 — jangan sampai UI mati karena render
            pass

    # --- Gambar ulang saat lebar berubah -------------------------------

    def on_resize(self, event: Resize) -> None:
        """Jadwalkan gambar ulang saat lebar berubah.

        JANGAN panggil ``super().on_resize()``: Textual mengirim event ke
        setiap handler di MRO, jadi ``RichLog.on_resize`` (yang melepas
        tulisan tertunda) tetap ikut jalan. Memanggilnya lagi akan
        menggandakan isi.
        """
        lebar = event.size.width
        if lebar and lebar != self._lebar_terakhir:
            if self._lebar_terakhir:
                self._jadwalkan_gambar_ulang()
            self._lebar_terakhir = lebar

    def _jadwalkan_gambar_ulang(self) -> None:
        """Tunda gambar ulang sampai resize berhenti (hemat saat drag)."""
        if self._timer_resize is not None:
            try:
                self._timer_resize.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._timer_resize = self.set_timer(_JEDA_RESIZE,
                                                self._gambar_ulang)
        except Exception:  # noqa: BLE001 — belum ter-mount
            self._timer_resize = None

    def _gambar_ulang(self) -> None:
        """Bersihkan lalu tulis ulang riwayat pada lebar baru."""
        self._timer_resize = None
        # Nomor baris berubah setelah dibungkus ulang di lebar baru — sorotan
        # lama bisa menunjuk baris yang keliru, lebih baik lenyap.
        self._salin_jangkar = None
        self._salin_lo = self._salin_hi = None
        if self._menggambar_ulang or not self._items:
            return
        self._menggambar_ulang = True
        try:
            items = list(self._items)
            self.clear()
            lebar = self._lebar()
            for it in items:
                try:
                    self.write(it, width=lebar)
                except Exception:  # noqa: BLE001
                    pass
            self._items.clear()
            self._items.extend(items)
            self.scroll_end(animate=False)
        finally:
            self._menggambar_ulang = False

    # --- Salin otomatis saat teks diblok ---------------------------------
    #
    # RichLog (Textual 8.2.8) tidak punya seleksi teks bawaan, dan terminal
    # sendiri tak bisa menyalin teks milik aplikasi TUI penuh-layar (mouse
    # kita yang menangkapnya). Karena itu seleksi dibuat sendiri SEBARIS:
    # tahan tombol kiri lalu seret — baris yang dilintas tersorot — lepaskan
    # dan isinya LANGSUNG tersalin ke clipboard.
    #
    # Kenapa per baris penuh (bukan per karakter): lebar sel terminal tak
    # seragam (emoji, huruf CJK lebar dua), jadi pemetaan kolom mouse ->
    # karakter hanya bisa ditebak. Baris penuh tak pernah salah isi; polanya
    # sama dengan copy-mode tmux.

    def _baris_konten(self, y: float) -> int:
        """Ubah y mouse (relatif widget) menjadi nomor baris konten."""
        try:
            geser = int(self.scroll_offset.y)
        except Exception:  # noqa: BLE001 — belum ter-mount
            geser = 0
        n = max(0, int(y) + geser)
        return min(n, max(0, len(self.lines) - 1))

    def on_mouse_down(self, event) -> None:
        if getattr(event, "button", -1) != 1:
            return
        # Klik baru selalu memulai seleksi segar — sorotan lama (bila ada)
        # ikut lenyap.
        self._salin_jangkar = self._baris_konten(event.y)
        self._salin_lo = self._salin_hi = self._salin_jangkar
        self.refresh()

    def on_mouse_move(self, event) -> None:
        if getattr(event, "button", -1) != 1 or self._salin_jangkar is None:
            return
        b = self._baris_konten(event.y)
        if b != self._salin_hi:
            lo, hi = sorted((self._salin_jangkar, b))
            if (lo, hi) != (self._salin_lo, self._salin_hi):
                self._salin_lo, self._salin_hi = lo, hi
                self.refresh()

    def on_mouse_up(self, event) -> None:
        if getattr(event, "button", -1) != 1 or self._salin_jangkar is None:
            return
        jangkar, self._salin_jangkar = self._salin_jangkar, None
        lo, hi = sorted((jangkar, self._baris_konten(event.y)))
        if lo == hi:
            # Klik polos tanpa seret: tak ada yang disalin; sorotan (yang
            # memang cuma satu baris tadi) dibersihkan.
            self._salin_lo = self._salin_hi = None
            self.refresh()
            return
        teks = self._teks_salin(lo, hi)
        if teks:
            self._salin_ke_clipboard(teks, hi - lo + 1)
            # Sorotan DIBIARKAN tampil: satu-satunya umpan balik visual apa
            # yang barusan tersalin.

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        lo, hi = self._salin_lo, self._salin_hi
        if lo is None or hi is None:
            return strip
        try:
            baris = int(y + self.scroll_offset.y)
            if lo <= baris <= hi:
                return strip.apply_style(RichStyle(reverse=True))
        except Exception:  # noqa: BLE001 — sorotan tak boleh mematikan render
            pass
        return strip

    def _teks_salin(self, lo: int, hi: int) -> str:
        """Teks polos baris lo..hi (Strip.text), tanpa baris kosong pengapit."""
        try:
            potong = self.lines[lo:hi + 1]
        except Exception:  # noqa: BLE001 — indeks basi (log sudah terpangkas)
            return ""
        baris = [s.text.rstrip() for s in potong if s is not None]
        while baris and not baris[0].strip():
            baris.pop(0)
        while baris and not baris[-1].strip():
            baris.pop()
        return "\n".join(baris)

    def _salin_ke_clipboard(self, teks: str, jumlah: int) -> None:
        """Salin lewat OSC 52 DAN clip.exe — dua jalur, bukan salah satu.

        OSC 52 hanya dijawab terminal yang mendukungnya; clip.exe pasti ada
        di Windows tapi tak berguna di terminal jarak jauh. Keduanya dijalani
        berurutan: yang mana pun berhasil, clipboard pengguna terisi."""
        tersalin = False
        try:
            self.app.copy_to_clipboard(teks)
            tersalin = True
        except Exception:  # noqa: BLE001 — driver belum siap / tak dukung
            pass
        if sys.platform == "win32":
            try:
                # UTF-16 + BOM: clip.exe mengenalinya, jadi karakter non-ASCII
                # (termasuk emoji) tak berubah jadi '?'.
                subprocess.run(
                    ["clip.exe"], input=teks.encode("utf-16"),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False)
                tersalin = True
            except Exception:  # noqa: BLE001 — clipboard gagal: cukup diam
                pass
        if not tersalin:
            return
        try:
            self.app.notify(
                f"{jumlah} baris tersalin — tempel dengan klik-kanan / "
                "Ctrl+Shift+V", timeout=4)
        except Exception:  # noqa: BLE001 — notify opsional
            pass

    # --- Pesan ---------------------------------------------------------

    def append_user_message(self, text: str, prefix: str = "❯ ") -> None:
        """Gema pesan pengguna.

        Prompt tampil sebagai BLOK bergaris vertikal + latar — jelas
        dibedakan dari jawaban AI dan teks lain. (Prompt yang sedang
        mengantre TIDAK lewat sini; ia tampil di QueueStrip dulu,
        diredupkan, sampai benar-benar dijalankan.)
        """
        from ...ui.textual_theme import campur

        gema = tema.p("gema_bg")
        garis_st = f"bold {tema.p('aksen')}"
        isi_st = f"bold {tema.p('teks')}"
        bg = campur(gema, tema.p("aksen"), 0.10)

        t = Text()
        # Jarak kecil dari jawaban sebelumnya (bukan pesan pertama).
        if self._items:
            t.append("\n")
        for i, baris in enumerate(text.split("\n")):
            if i:
                t.append("\n")
            # Garis vertikal aksen di tiap baris — baris lanjutan tetap
            # sejajar, blok multi-baris tampak sebagai satu kartu utuh.
            t.append("▌", style=f"{garis_st} on {bg}")
            t.append(" ", style=f"on {bg}")
            isi = f"{prefix}{baris}" if i == 0 else f"  {baris}"
            t.append(isi, style=f"{isi_st} on {bg}")
            t.append(" ", style=f"on {bg}")  # rapian tepi kanan
        # Jarak kecil ke jawaban/tool yang menyusul di bawahnya.
        t.append("\n")
        self._emit(t)

    def append_ai_message(self, text: str) -> None:
        """Jawaban AI, dirender sebagai markdown."""
        cleaned = _pagari_pohon(_bersih_kendali(text))
        try:
            self._emit(Markdown(cleaned))
        except Exception:  # noqa: BLE001 — markdown cacat
            self._emit(Text(cleaned))

    def append_notice(self, text: str, style: str | None = None) -> None:
        """Pemberitahuan sistem (info, peringatan, keluaran /help, dll)."""
        s = style or f"italic {tema.p('aksen_terang')}"
        self._emit(Text(_indent(str(text)), style=s, no_wrap=False))

    def append_tool_step(self, name: str, args: dict, result: str | None = None,
                         duration: float | None = None,
                         success: bool = True) -> None:
        """Satu langkah tool: nama, durasi, dan cuplikan hasil."""
        icon = "✓" if success else "✗"
        color = tema.p("aksen") if success else tema.p("exit_footer")
        t = Text()
        t.append(f"  {icon} ", style=f"bold {color}")
        t.append(name, style=f"bold {tema.p('aksen2')}")
        if duration is not None:
            if duration < 1.0:
                t.append(f" ({duration * 1000:.0f}ms)", style=tema.p("redup"))
            else:
                t.append(f" ({duration:.1f}s)", style=tema.p("redup"))
        self._emit(t)

        if result:
            batas = 200
            datar = result.replace("\n", " ").rstrip()
            if len(datar) > batas:
                t2 = Text(f"    ↳ {datar[:batas]}", style=tema.p("redup"))
                t2.append(f" … (+{len(datar) - batas} huruf)",
                          style=f"dim {tema.p('redup')}")
                self._emit(t2)
            elif datar:
                self._emit(Text(f"    ↳ {datar}", style=tema.p("redup")))

    def append_reasoning(self, text: str) -> None:
        """Isi penalaran (diredupkan)."""
        self._emit(Text(_indent(f"💭 {text}"),
                        style=f"dim italic {tema.p('redup')}"))

    # --- Diff ----------------------------------------------------------

    def append_diff(self, path: str, old: str, new: str, is_new: bool = False,
                    limit: int = 200) -> None:
        """Diff gaya GitHub dengan pewarnaan sintaks pada baris tambahan."""
        import difflib

        diff = list(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        ))
        if not diff:
            return

        syntax_lines = self._syntax_highlight(path, new) if new else None
        add_style = "#a3ccad on #0d2312"
        del_style = "#cca3a3 on #230d0d"
        ctx_style = "grey50"
        gut_add = "#4f8f5c on #08170b"
        gut_del = "#96565a on #170808"

        header = Text()
        header.append(f"  📝 {path}", style=f"bold {tema.p('aksen2')}")
        if is_new:
            header.append(" (baru)", style=tema.p("aksen_terang"))
        self._emit(header)

        lineno_old = lineno_new = new_idx = 0
        for line in diff[:limit]:
            if line.startswith(("+++", "---")):
                continue
            if line.startswith("@@"):
                self._emit(Text(f"  {line.rstrip()}", style="bold #888888"))
                continue
            if line.startswith("+"):
                lineno_new += 1
                t = Text(f"  {lineno_new:>4} + ", style=gut_add)
                if syntax_lines and new_idx < len(syntax_lines):
                    body = syntax_lines[new_idx].copy()
                    body.style = add_style
                    t.append_text(body)
                    new_idx += 1
                else:
                    t.append(line[1:], style=add_style)
                self._emit(t)
            elif line.startswith("-"):
                lineno_old += 1
                t = Text(f"  {lineno_old:>4} - ", style=gut_del)
                t.append(line[1:], style=del_style)
                self._emit(t)
            else:
                lineno_old += 1
                lineno_new += 1
                new_idx += 1
                t = Text(f"  {lineno_new:>4}   ", style=ctx_style)
                t.append(line[1:] if line.startswith(" ") else line,
                         style=ctx_style)
                self._emit(t)

        if len(diff) > limit:
            self._emit(Text(f"  … (diff dipotong: {len(diff)} baris)",
                            style=tema.p("redup")))

    def _syntax_highlight(self, path: str, code: str):
        """Kembalikan daftar ``Text`` per baris berwarna, atau None."""
        if not code or len(code) > 400_000:
            return None
        try:
            from rich.syntax import Syntax
            from pygments.lexers import guess_lexer_for_filename

            lexer = guess_lexer_for_filename(path, code)
            if not lexer or lexer.name in ("Text only", "text"):
                return None
            syn = Syntax(code, lexer.name, theme="gruvbox-dark",
                         line_numbers=False, word_wrap=False)
            hasil = []
            for line_text in syn.highlight(code).split("\n"):
                clean = Text(line_text.plain, no_wrap=True)
                for span in line_text.spans:
                    st = span.style
                    if not getattr(st, "color", None):
                        continue
                    try:
                        warna = f"color {st.color.get_truecolor().hex}"
                    except Exception:  # noqa: BLE001
                        warna = f"color {st.color}"
                    clean.stylize(warna, span.start, span.end)
                hasil.append(clean)
            return hasil
        except Exception:  # noqa: BLE001 — pygments opsional
            return None

    # --- Gambar & rencana ---------------------------------------------

    def append_image_preview(self, pixel_data: list,
                             title: str = "Pratinjau") -> None:
        """Pratinjau gambar sebagai blok warna."""
        if not pixel_data:
            return
        garis = tema.p("tepi")
        max_w = max(len(r) for r in pixel_data)

        atas = Text(f"  ╭─ {title} ", style=garis)
        atas.append("─" * max(0, max_w * 2 - len(title) - 4), style=garis)
        atas.append("╮", style=garis)
        self._emit(atas)

        for row in pixel_data:
            t = Text("  │", style=garis)
            for r, g, b in row:
                t.append("  ", style=f"on rgb({r},{g},{b})")
            if max_w - len(row) > 0:
                t.append("  " * (max_w - len(row)))
            t.append("│", style=garis)
            self._emit(t)

        bawah = Text("  ╰", style=garis)
        bawah.append("─" * (max_w * 2 + 1), style=garis)
        bawah.append("╯", style=garis)
        self._emit(bawah)

    def append_plan(self, steps: list[dict], title: str = "Rencana") -> None:
        """Daftar langkah rencana."""
        if not steps:
            return
        garis = tema.p("tepi")
        self._emit(Text(f"  ╭─── {title} ───╮", style=garis))
        for step in steps:
            status = step.get("status", "pending")
            if status == "done":
                ikon, style = "✓", f"bold {tema.p('aksen')}"
            elif status == "active":
                ikon, style = "▸", f"bold {tema.p('aksen_terang')}"
            else:
                ikon, style = "·", tema.p("redup")
            self._emit(Text(f"  │ {ikon} {step.get('text', '')}", style=style))
        self._emit(Text(f"  ╰{'─' * 20}╯", style=garis))

    # --- Bersihkan -----------------------------------------------------

    def clear_messages(self) -> None:
        """Kosongkan seluruh riwayat."""
        with self._stream_lock:
            self._stream_buf = ""
            self._stream_rendered = False
            self._stream_preview_len = 0
        self._items.clear()
        try:
            self.clear()
        except Exception:  # noqa: BLE001
            pass

    # --- Aliran token --------------------------------------------------

    def begin_stream(self) -> None:
        """Mulai sesi aliran untuk satu jawaban AI."""
        with self._stream_lock:
            self._stream_buf = ""
            self._stream_rendered = False
            self._stream_preview_len = 0

    def append_token(self, piece: str) -> None:
        """Kumpulkan satu token. Dipanggil dari thread pekerja."""
        with self._stream_lock:
            self._stream_buf += piece

    def end_stream(self) -> str:
        """Akhiri aliran, kembalikan seluruh teks untuk render markdown."""
        with self._stream_lock:
            text = self._stream_buf
            self._stream_buf = ""
            self._stream_rendered = False
            self._stream_preview_len = 0
            return text

    @property
    def stream_length(self) -> int:
        """Panjang buffer aliran (aman lintas thread)."""
        with self._stream_lock:
            return len(self._stream_buf)

    def get_stream_tail(self, n: int = 600) -> str:
        """``n`` huruf terakhir dari buffer aliran (aman lintas thread)."""
        with self._stream_lock:
            return self._stream_buf[-n:] if self._stream_buf else ""

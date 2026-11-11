"""Penalaran aktif: satu baris header dan viewport kecil yang bisa digulir.

Isi lengkap disimpan untuk riwayat; footer tidak ikut memanjang saat model
mengirim penalaran panjang. Tinggi viewport mengikuti ukuran terminal.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, RichLog

from ...ui import tema
from .animasi import tampilkan_halus

# Jarak minimal antar render saat token penalaran mengalir (detik).
_JEDA_RENDER = 0.12
# Batas tenang agar jeda antar-token tidak membuat render berkedip.
_JEDA_DIAM = 1.2
# Baris isi maksimum saat blok dibuka (batas atas, sebelum disesuaikan
# dengan tinggi terminal). Sejalan dengan _PIKIR_BARIS di cli.py.
_PIKIR_BARIS = 3


class ThinkingBlock(Widget):
    """Blok penalaran yang bisa dibuka/tutup, di atas ChatBox."""

    DEFAULT_CSS = """
    ThinkingBlock {
        height: auto;
        padding: 0 1;
        display: none;
    }
    ThinkingBlock #thinking-content { height: 1; width: 100%; }
    ThinkingBlock #thinking-body {
        display: none; height: 3; min-height: 1; width: 100%;
        padding: 0 1; border-left: solid $t-tepi_redup;
        background: transparent; color: $t-redup;
        scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1;
        scrollbar-background: $t-menu_bg; scrollbar-color: $t-tepi_redup;
        scrollbar-color-hover: $t-tepi; scrollbar-gutter: stable;
        overflow-x: hidden; overflow-y: auto;
    }
    """

    collapsed: reactive[bool] = reactive(True)

    def __init__(self, max_lines: int = _PIKIR_BARIS, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._body: RichLog | None = None
        self._text = ""
        self._max_lines = max_lines
        self._timer = None
        self._kotor = False
        self._mengalir = False  # token penalaran sedang masuk?
        self._waktu_token = 0.0  # monotonic() token penalaran terakhir

    def compose(self):
        yield Static("", id="thinking-content")
        yield RichLog(id="thinking-body", min_width=1, wrap=True,
                      auto_scroll=False, max_lines=12000)

    def on_mount(self):
        self._content = self.query_one("#thinking-content", Static)
        self._content.tooltip = "Buka/tutup penalaran"
        self._body = self.query_one("#thinking-body", RichLog)
        self._body.can_focus = False

    def on_click(self, event) -> None:
        """Buka/tutup saat diklik."""
        if event.widget is self._content:
            event.stop()
            self.toggle()

    def on_resize(self, event) -> None:
        """Bungkus ulang teks pada lebar baru."""
        if self.display and not self.collapsed:
            self.call_after_refresh(self._gambar)

    # --- API publik ----------------------------------------------------

    def toggle(self) -> None:
        """Buka/tutup blok. Aman dipanggil walau blok sedang tersembunyi."""
        if not self._text:
            return
        self.collapsed = not self.collapsed
        tampilkan_halus(self)
        self._gambar()

    def update_thinking(self, text: str) -> None:
        """Ganti seluruh teks penalaran."""
        if not text:
            self.hide()
            return
        self._text = text
        self._mengalir = True
        self._waktu_token = time.monotonic()
        tampilkan_halus(self)
        self._jadwalkan()

    def append_thinking(self, piece: str, *, reveal: bool = True) -> None:
        """Tambah sepotong teks penalaran (dari thread utama)."""
        if not piece:
            return
        self._text += piece
        self._mengalir = True
        self._waktu_token = time.monotonic()
        if reveal:
            tampilkan_halus(self)
            self._jadwalkan()

    def hide(self) -> None:
        """Sembunyikan blok TANPA menghapus isinya."""
        self._batalkan_timer()
        self.display = False

    def clear(self) -> None:
        """Sembunyikan blok DAN hapus isinya."""
        self._batalkan_timer()
        self._text = ""
        self.collapsed = True
        self.display = False
        if self._content:
            self._content.update("")
        if self._body:
            self._body.clear()
            self._body.display = False

    # --- Render --------------------------------------------------------

    def _batalkan_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._timer = None
        self._kotor = False
        self._mengalir = False

    def _jadwalkan(self) -> None:
        """Tunda render supaya arus token tidak membanjiri UI."""
        if self._timer is not None:
            self._kotor = True
            return
        self._gambar()
        try:
            self._timer = self.set_timer(_JEDA_RENDER, self._selesai_jeda)
        except Exception:  # noqa: BLE001 — belum ter-mount
            self._timer = None

    def _selesai_jeda(self) -> None:
        self._timer = None
        if self._kotor:
            self._kotor = False
            self._jadwalkan()
            return
        if not self._mengalir:
            return
        # Satu jeda render tanpa token baru BUKAN berarti penalaran berhenti.
        # Timer hanya dijadwalkan ulang saat ada token, jadi diam panjang
        # harus diperiksa dengan jam, lalu ditunggu sisa waktunya.
        sisa = _JEDA_DIAM - (time.monotonic() - self._waktu_token)
        if sisa > 0:
            try:
                self._timer = self.set_timer(sisa, self._selesai_jeda)
            except Exception:  # noqa: BLE001 — belum ter-mount
                self._timer = None
            return
        # Akhiri pembaruan berkala setelah aliran tenang.
        self._mengalir = False
        self._gambar()

    def _baris_maks(self) -> int:
        """Berapa baris isi boleh tampil, dibatasi tinggi terminal."""
        try:
            tinggi = self.app.size.height
        except Exception:  # noqa: BLE001 — di luar konteks app
            tinggi = 24
        return max(1, min(self._max_lines, tinggi // 8))

    def _gambar(self) -> None:
        """Gambar blok sesuai keadaan buka/tutup."""
        if not self._content:
            return
        jumlah = len(self._text)
        if jumlah == 0:
            self.display = False
            return

        header = Text(no_wrap=True, overflow="ellipsis")
        header.append(f"{'▸' if self.collapsed else '▾'} ", style=tema.p('aksen'))
        header.append("💭 ", style=tema.p("redup"))
        header.append("Penalaran", style=tema.p("teks"))
        self._content.update(header)
        if self._body is None:
            return
        self._body.display = not self.collapsed

        if self.collapsed:
            return
        body = self._body
        follow = not body.lines or body.is_vertical_scroll_end
        position = body.scroll_y
        width = max(1, (body.content_region.width or self.content_size.width - 3) - 1)
        text = Text(" ".join(self._text.split()), style=tema.p("redup"))
        lines = text.wrap(self.app.console, max(1, width), overflow="fold")
        body.styles.height = min(self._baris_maks(), max(1, len(lines)))
        body.clear()
        body.write(text, width=width)
        if follow:
            body.call_after_refresh(lambda: body.scroll_end(animate=False))
        else:
            body.call_after_refresh(lambda: body.scroll_to(y=position, animate=False, force=True))

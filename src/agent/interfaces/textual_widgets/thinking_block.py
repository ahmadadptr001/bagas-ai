"""ThinkingBlock widget — blok penalaran yang bisa dibuka/tutup.

Menampilkan pikiran model (reasoning tokens):

- Tertutup: ``▸ 💭 pikiran N huruf``
- Terbuka:  ``▾ 💭 pikiran N huruf`` + beberapa baris terakhir

Klik atau Tab untuk buka/tutup.

CATATAN BUG YANG SUDAH DIPERBAIKI (jangan diulang):

1. ``hide()`` dulu MENGHAPUS ``self._text``. Karena Tab (``toggle``) pada
   keadaan tertentu memanggilnya, isi pikiran hilang dan blok tak bisa
   dibuka lagi. Sekarang ``hide()`` hanya menyembunyikan; hanya ``clear()``
   yang menghapus isi.
2. Tak ada metode ``toggle()`` padahal aplikasi memanggilnya (Tab) — dulu
   itu ``AttributeError`` yang tertelan.
3. ``_gambar()`` (dulu bernama ``_render()`` — JANGAN pakai nama itu lagi:
   ia menimpa API internal Textual ``Widget._render()`` yang wajib
   mengembalikan Visual, sehingga Textual crash dengan
   "'NoneType' object has no attribute 'render_strips'"). Dijalankan pada
   SETIAP token penalaran dan membungkus teks pada lebar tetap 72.
   Sekarang render dijadwalkan berkala (throttle) dan lebarnya mengikuti
   lebar widget.
"""
from __future__ import annotations

import textwrap

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ...ui import tema

# Jarak minimal antar render saat token penalaran mengalir (detik).
_JEDA_RENDER = 0.12


class ThinkingBlock(Widget):
    """Blok penalaran yang bisa dibuka/tutup, di atas ChatBox."""

    DEFAULT_CSS = """
    ThinkingBlock {
        height: auto;
        max-height: 8;
        padding: 0 1;
        display: none;
    }
    """

    collapsed: reactive[bool] = reactive(True)

    def __init__(self, max_lines: int = 5, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._text = ""
        self._max_lines = max_lines
        self._timer = None
        self._kotor = False

    def compose(self):
        yield Static("", id="thinking-content")

    def on_mount(self):
        self._content = self.query_one("#thinking-content", Static)

    def on_click(self, event) -> None:
        """Buka/tutup saat diklik."""
        event.stop()
        self.toggle()

    def on_resize(self, event) -> None:
        """Bungkus ulang teks pada lebar baru."""
        if self.display and not self.collapsed:
            self._gambar()

    # --- API publik ----------------------------------------------------

    def toggle(self) -> None:
        """Buka/tutup blok. Aman dipanggil walau blok sedang tersembunyi."""
        if not self._text:
            return
        self.collapsed = not self.collapsed
        self.display = True
        self._gambar()

    def update_thinking(self, text: str) -> None:
        """Ganti seluruh teks penalaran."""
        if not text:
            self.hide()
            return
        self._text = text
        self.display = True
        self._jadwalkan()

    def append_thinking(self, piece: str) -> None:
        """Tambah sepotong teks penalaran (dari thread utama)."""
        if not piece:
            return
        self._text += piece
        self.display = True
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

    # --- Render --------------------------------------------------------

    def _batalkan_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._timer = None
        self._kotor = False

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

    def _gambar(self) -> None:
        """Gambar blok sesuai keadaan buka/tutup."""
        if not self._content:
            return
        jumlah = len(self._text)
        if jumlah == 0:
            self.display = False
            return

        header = Text(no_wrap=True, overflow="ellipsis")
        header.append(f"  {'▸' if self.collapsed else '▾'} ",
                      style=f"bold {tema.p('aksen_terang')}")
        header.append(f"💭 pikiran {jumlah} huruf", style=tema.p("aksen"))
        header.append(" · tab buka/tutup", style=f"dim {tema.p('redup')}")

        if self.collapsed:
            self._content.update(header)
            return

        lebar = max(20, (self.size.width or 80) - 8)
        baris = textwrap.wrap(" ".join(self._text.split()), lebar)
        ekor = baris[-self._max_lines:]

        hasil = Text()
        hasil.append_text(header)
        hasil.append("\n")
        if len(baris) > self._max_lines:
            hasil.append(f"    ⋮ ({len(baris) - self._max_lines} baris sebelumnya)\n",
                         style=f"dim {tema.p('redup')}")
        for b in ekor:
            hasil.append(f"    {b}\n", style=f"dim italic {tema.p('redup')}")
        self._content.update(hasil)

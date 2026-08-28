"""StreamingPreview widget — teks berjalan saat AI sedang menulis.

Menampilkan potongan terakhir token yang sudah terkumpul. Disembunyikan
saat giliran selesai dan markdown final digambar di MessageList.

CATATAN BUG YANG SUDAH DIPERBAIKI (jangan diulang):

1. ``Text(f"  {preview_text}")`` hanya memberi indentasi pada baris PERTAMA,
   sehingga blok pratinjau tampak patah/miring. Sekarang setiap baris
   diberi indentasi.
2. Ekor diambil per baris SUMBER, bukan per baris TAMPIL. Satu baris panjang
   akan dibungkus jadi banyak baris dan menembus ``max-height`` (isi terpotong
   di tengah). Sekarang teks dibungkus dulu pada lebar widget, baru ekornya
   diambil.
3. Metode gambar ulang kini bernama ``_gambar()`` — JANGAN kembali menamainya
   ``_render()``: nama itu milik API internal Textual (``Widget._render``,
   wajib mengembalikan Visual). Menimpanya membuat Textual crash dengan
   "'NoneType' object has no attribute 'render_strips'".
"""
from __future__ import annotations

import textwrap

from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static

from ...ui import tema


class StreamingPreview(Widget):
    """Pratinjau teks mengalir, tepat di atas ChatBox."""

    DEFAULT_CSS = """
    StreamingPreview {
        height: auto;
        max-height: 8;
        padding: 0 1;
        display: none;
    }
    """

    def __init__(self, max_lines: int = 6, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._text = ""
        self._max_lines = max_lines

    def compose(self):
        yield Static("", id="streaming-content")

    def on_mount(self):
        self._content = self.query_one("#streaming-content", Static)

    def on_resize(self, event) -> None:
        """Bungkus ulang pada lebar baru."""
        if self.display and self._text:
            self._gambar()

    def update_preview(self, text: str) -> None:
        """Perbarui pratinjau dengan teks yang sudah terkumpul."""
        if not text:
            self.hide()
            return
        self._text = text
        self.display = True
        self._gambar()

    def _gambar(self) -> None:
        if not self._content:
            return
        lebar = max(20, (self.size.width or 80) - 6)

        # Bungkus per baris sumber supaya baris kosong/paragraf tetap terjaga,
        # lalu ambil ekor dari hasil BUNGKUSAN (bukan dari baris sumber).
        baris: list[str] = []
        for potong in self._text.split("\n"):
            if potong.strip():
                baris.extend(textwrap.wrap(potong, lebar) or [""])
            else:
                baris.append("")
        ekor = baris[-self._max_lines:]

        t = Text(style=f"dim {tema.p('redup')}")
        if len(baris) > self._max_lines:
            t.append(f"  ⋮ ({len(baris) - self._max_lines} baris sebelumnya)\n",
                     style=f"dim {tema.p('redup')}")
        t.append("\n".join(f"  {b}" for b in ekor))
        self._content.update(t)

    def hide(self) -> None:
        """Sembunyikan pratinjau."""
        self.display = False
        self._text = ""

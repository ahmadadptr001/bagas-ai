"""ImagePreview widget — Minecraft-style pixel block image display.

Mengkonversi gambar menjadi blok warna ASCII untuk ditampilkan di terminal.
Menggunakan ``ui.ascii_art.image_to_blocks_pixels()`` yang sudah ada.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static
from textual.reactive import reactive
from textual.app import RenderResult
from rich.text import Text

from ...ui import tema


class ImagePreview(Widget):
    """Image preview with pixel blocks — docked above chat box."""

    DEFAULT_CSS = """
    ImagePreview {
        height: auto;
        max-height: 10;
        padding: 0 1;
    }
    """

    visible: reactive[bool] = reactive(False)
    title: reactive[str] = reactive("Preview")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._pixel_data: list[list[tuple[int, int, int]]] = []

    def compose(self):
        yield Static("", id="image-content")

    def on_mount(self):
        self._content = self.query_one("#image-content", Static)
        self.display = False

    def show_image(self, pixel_data: list[list[tuple[int, int, int]]],
                   title: str = "Preview"):
        """Display pixel data as Minecraft-style blocks."""
        if not pixel_data:
            self.hide()
            return
        self._pixel_data = pixel_data
        self.title = title
        self.visible = True
        self.display = True
        if self._content:
            self._content.update(self._render_pixels(pixel_data, title))

    def _render_pixels(self, pixel_data: list, title: str) -> RenderResult:
        """Gambar blok piksel dalam kotak yang lebarnya benar.

        Versi lama salah hitung: garis atas kependekan satu karakter dan
        garis bawah kepanjangan satu, jadi kotaknya tak pernah rapat. Lebar
        kolom juga tak pernah dibatasi lebar widget sehingga terpotong.
        """
        garis = tema.p("tepi")
        # Setiap piksel = 2 sel. Sisakan 2 sel untuk dinding kiri+kanan.
        muat = max(1, ((self.size.width or 80) - 2) // 2)
        w = min(max(len(r) for r in pixel_data), muat)

        judul = title[:max(0, w * 2 - 4)]
        isi = w * 2  # lebar isi di antara dua dinding

        lines = Text()
        kepala = f"╭─ {judul} " if judul else "╭─"
        lines.append(kepala, style=garis)
        lines.append("─" * max(0, isi + 2 - len(kepala) - 1), style=garis)
        lines.append("╮\n", style=garis)

        for row in pixel_data:
            lines.append("│", style=garis)
            for r, g, b in row[:w]:
                lines.append("  ", style=f"on rgb({r},{g},{b})")
            if len(row) < w:
                lines.append("  " * (w - len(row)))
            lines.append("│\n", style=garis)

        lines.append("╰", style=garis)
        lines.append("─" * isi, style=garis)
        lines.append("╯", style=garis)

        return lines

    def on_resize(self, event) -> None:
        """Gambar ulang pada lebar baru."""
        if self.display and self._pixel_data and self._content:
            self._content.update(
                self._render_pixels(self._pixel_data, str(self.title)))

    def hide(self):
        """Hide the preview."""
        self.visible = False
        self.display = False
        self._pixel_data = []

    def clear(self):
        """Clear preview data."""
        self.hide()

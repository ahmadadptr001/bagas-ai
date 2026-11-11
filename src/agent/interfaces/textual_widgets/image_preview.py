"""Lampiran ringkas dengan pratinjau opsional dan tombol hapus."""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static, Button
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.app import RenderResult
from rich.text import Text

from ...ui import tema


class ImagePreview(Widget):
    """Baris lampiran dengan thumbnail ringkas di atas kolom pesan."""

    DEFAULT_CSS = """
    ImagePreview {
        height: auto;
        max-height: 6;
        padding: 0 2;
    }
    ImagePreview #image-actions {
        height: 1; padding: 0 1; background: $t-menu_bg;
    }
    ImagePreview #image-actions Button {
        height: 1; min-height: 1; border: none; margin: 0; padding: 0;
        background: transparent; color: $t-teks; text-style: none;
    }
    ImagePreview #image-toggle {
        width: 1fr; min-width: 1; text-align: left; content-align: left middle;
    }
    ImagePreview #image-meta {
        width: auto; max-width: 22; height: 1; margin: 0 1;
        color: $t-redup; text-align: right;
    }
    ImagePreview #image-remove {
        width: 3; min-width: 3; color: $t-teks; background: $t-menu_hover;
    }
    ImagePreview #image-actions Button:hover,
    ImagePreview #image-actions Button:focus {
        background: $t-menu_hover; text-style: none;
    }
    ImagePreview #image-content {
        height: auto; max-height: 4; margin-top: 1; padding: 0 1;
        border-left: solid $t-tepi_redup; background: transparent;
    }
    """

    class Removed(Message, namespace="image_preview"):
        pass

    visible: reactive[bool] = reactive(False)
    title: reactive[str] = reactive("Preview")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._pixel_data: list[list[tuple[int, int, int]]] = []
        self._expanded = False
        self._meta = ""
        self._count = 1
        self._attachment_key = ""

    def compose(self):
        with Horizontal(id="image-actions"):
            yield Button("", id="image-toggle", compact=True, tooltip="Buka pratinjau")
            yield Static("", id="image-meta")
            yield Button("×", id="image-remove", compact=True, tooltip="Hapus lampiran")
        yield Static("", id="image-content")

    def on_mount(self):
        self._content = self.query_one("#image-content", Static)
        self._content.display = False
        self.display = False

    def show_image(self, pixel_data: list[list[tuple[int, int, int]]],
                   title: str = "Preview"):
        """Tampilkan gambar sebagai pratinjau warna terminal."""
        self.show_attachment(title, pixel_data)

    def show_attachment(self, title: str, pixel_data=None, *, meta: str = "",
                        count: int = 1, key: str = ""):
        same = bool(key and key == self._attachment_key)
        self._pixel_data = pixel_data or []
        if not same:
            self._expanded = False
        self._attachment_key = key
        self._meta = meta
        self._count = count
        self.title = title
        self.visible = True
        self.display = True
        self._refresh_preview()
        if self.is_mounted:
            self.call_after_refresh(self._refresh_preview)

    def _refresh_preview(self):
        if self._content is None:
            return
        label = Text("▾ " if self._expanded else "▸ ", style=tema.p("redup"))
        label.append("▧ ", style=tema.p("aksen"))
        label.append(str(self.title), style=tema.p("teks"))
        label.no_wrap = True
        label.overflow = "ellipsis"
        toggle = self.query_one("#image-toggle", Button)
        if toggle.content_size.width:
            label.truncate(max(1, toggle.content_size.width - 2), overflow="ellipsis")
        toggle.label = label
        toggle.tooltip = (
            ("Tutup" if self._expanded else "Buka") + " pratinjau · " + self.title)
        meta = self._meta if self.content_size.width >= 58 else self._meta.split(" · ")[0]
        if self._count > 1:
            meta = f"+{self._count - 1}" + (" · " + meta if meta else "")
        metadata = self.query_one("#image-meta", Static)
        metadata.display = bool(meta)
        metadata.update(Text(meta, no_wrap=True, overflow="ellipsis"))
        self._content.display = self._expanded and bool(self._pixel_data)
        if self._content.display:
            self._content.update(self._render_pixels(self._pixel_data, ""))

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "image-remove":
            self.post_message(self.Removed())
        elif event.button.id == "image-toggle" and self._pixel_data:
            self._expanded = not self._expanded
            self._refresh_preview()

    def _render_pixels(self, pixel_data: list, title: str) -> RenderResult:
        """Thumbnail tanpa bingkai ganda; kedua sumbu diperkecil, bukan dipotong."""
        muat = max(1, ((self._content.content_size.width or self.content_size.width or 36) - 1) // 2)
        w = min(max(len(r) for r in pixel_data), muat)
        h = min(4, len(pixel_data))
        lines = Text(no_wrap=True)
        for y in range(h):
            if y:
                lines.append("\n")
            row = pixel_data[min(len(pixel_data) - 1, int(y * len(pixel_data) / h))]
            for x in range(w):
                r, g, b = row[min(len(row) - 1, int(x * len(row) / w))]
                lines.append("  ", style=f"on rgb({r},{g},{b})")
        return lines

    def on_resize(self, event) -> None:
        """Gambar ulang pada lebar baru."""
        if self.display and self._pixel_data and self._content:
            self.call_after_refresh(self._refresh_preview)

    def hide(self):
        """Hide the preview."""
        self.visible = False
        self.display = False
        self._pixel_data = []
        self._expanded = False
        self._attachment_key = ""

    def clear(self):
        """Clear preview data."""
        self.hide()

"""Title bar untuk ruang kerja terminal."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from rich.text import Text
from rich.align import Align
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Static
from ...ui import tema


class BrandMark(Static):
    """Satu emblem grafis tetap, dirender sebagai piksel warna terminal."""

    def render(self):
        from ...file_viewer import image_cells
        from ...ui.brand import mark_image

        cells = image_cells(mark_image(tema.p("aksen")),
                            min(16, self.content_size.width or 16),
                            max(1, self.content_size.height or 8), tema.p("gema_bg"))
        return Align.center(cells)


class LogoWidget(Widget):
    """Identitas aplikasi, tab aktif, dan akses ke menu yang sudah ada."""

    class Command(Message, namespace="titlebar"):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        try:
            self._version = "v" + version("bagasai")
        except PackageNotFoundError:
            self._version = "dev"

    def compose(self):
        with Horizontal(id="titlebar-brand"):
            yield Static("", id="titlebar-lights")
            yield Static("", id="logo-content")
        yield Static(" ▤  Chat ", id="titlebar-chat")
        with Horizontal(id="titlebar-actions"):
            yield Button("☼", id="titlebar-theme", tooltip="Tema")
            yield Button("⚙", id="titlebar-help", tooltip="Bantuan")

    def on_mount(self):
        self._content = self.query_one("#logo-content", Static)
        self._render_logo()

    def _render_logo(self):
        if self._content is None:
            return
        lights = Text()
        for color in ("#ff5f57", "#febc2e", "#28c840"):
            lights.append("● ", style=color)
        self.query_one("#titlebar-lights", Static).update(lights)
        rendered = Text()
        rendered.append("✦  ", style=f"bold {tema.p('aksen')}")
        rendered.append("AI Agent", style=f"bold {tema.p('teks')}")
        self._content.update(rendered)
        self._content.tooltip = self._version

    def on_button_pressed(self, event: Button.Pressed):
        command = {"titlebar-theme": "/theme", "titlebar-help": "/help"}.get(event.button.id)
        if command:
            event.stop()
            self.post_message(self.Command(command))

    def set_version(self, version: str):
        """Set version string for tagline."""
        self._version = version
        self._render_logo()

    def refresh_theme(self):
        """Re-render with current theme."""
        self._render_logo()

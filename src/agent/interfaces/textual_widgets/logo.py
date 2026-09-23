"""Header ringkas dengan warna tema untuk layar percakapan."""
from __future__ import annotations

from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static
from ...ui import tema


class LogoWidget(Widget):
    """ASCII art logo with gradient — shown at startup."""

    DEFAULT_CSS = """
    LogoWidget {
        height: auto;
        max-height: 4;
        content-align: center middle;
        width: 100%;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._version: str = ""

    def compose(self):
        yield Static("", id="logo-content")

    def on_mount(self):
        self._content = self.query_one("#logo-content", Static)
        self._render_logo()

    def _render_logo(self):
        if not self._content:
            return

        rendered = Text("bagas-ai", style=f"bold {tema.p('aksen')}")
        if self._version:
            rendered.append(f"  {self._version}", style=tema.p("redup"))
        rendered.append("\nTulis tugasmu untuk mulai.", style=tema.p("redup"))
        self._content.update(rendered)

    def set_version(self, version: str):
        """Set version string for tagline."""
        self._version = version
        self._render_logo()

    def refresh_theme(self):
        """Re-render with current theme."""
        self._render_logo()

"""LogoWidget — ASCII art banner dengan gradient warna tema.

Menampilkan logo bagas-ai menggunakan pyfiglet dengan gradient
dari tema aktif. Cascading font sizes untuk responsivitas.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static
from textual.reactive import reactive
from textual.app import RenderResult
from rich.text import Text

from ...ui import tema

try:
    from pyfiglet import Figlet
except Exception:
    Figlet = None


# Font cascade — coba dari yang besar ke kecil
_FONTS = ["ansi_shadow", "slant", "small", "digital"]


def _gradient_text(text: str, colors: list[str]) -> Text:
    """Apply gradient colors to text, line by line (vertical gradient)."""
    if not text:
        return Text("")
    if not colors:
        return Text(text)

    t = Text()
    lines = text.split("\n")
    total_height = len(lines)
    total_colors = len(colors)

    if total_height == 0:
        return Text("")

    for y, line in enumerate(lines):
        if total_height == 1:
            color_idx = 0
        else:
            color_idx = int(y * (total_colors - 1) / (total_height - 1))
        color = colors[min(color_idx, total_colors - 1)]
        t.append(line, style=f"bold {color}")
        if y < total_height - 1:
            t.append("\n")

    return t


class LogoWidget(Widget):
    """ASCII art logo with gradient — shown at startup."""

    DEFAULT_CSS = """
    LogoWidget {
        height: auto;
        max-height: 14;
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

        # Get gradient colors from theme
        grad = tema.p("grad")
        if not isinstance(grad, list) or len(grad) < 2:
            grad = ["#58a6ff", "#3b82f6", "#2563eb", "#1d4ed8"]

        # Try figlet fonts
        logo_text = ""
        if Figlet:
            for font in _FONTS:
                try:
                    f = Figlet(font=font)
                    logo_text = f.renderText("bagas-ai")
                    # Check if it fits (max 60 chars wide)
                    max_line = max(len(l) for l in logo_text.split("\n"))
                    if max_line <= 60:
                        break
                except Exception:
                    continue

        if not logo_text:
            logo_text = "  ╔╦╗╔═╗╔═╗╔╦╗  ╔═╗╔═╗╦═╗\n" \
                        "   ║ ║╣ ╚═╗ ║   ╠═╝║ ║╠╦╝\n" \
                        "   ╩ ╚═╝╚═╝ ╩   ╩  ╚═╝╩╚═"

        # Render with gradient
        rendered = _gradient_text(logo_text.rstrip(), grad)
        self._content.update(rendered)

    def set_version(self, version: str):
        """Set version string for tagline."""
        self._version = version

    def refresh_theme(self):
        """Re-render with current theme."""
        self._render_logo()

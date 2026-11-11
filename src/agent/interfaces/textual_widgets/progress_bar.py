"""Indikator aktivitas satu baris, tanpa perkiraan persentase kemajuan."""
from __future__ import annotations

import time

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ...ui import tema
from .animasi import tampilkan_halus

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_INTERVAL = 0.15


class TurnProgressBar(Widget):
    """Status ringkas yang bergerak hanya selama indikator terlihat."""

    DEFAULT_CSS = """
    TurnProgressBar {
        display: none;
        height: 1;
        max-height: 1;
    }
    TurnProgressBar #progress-content { width: 100%; height: 1; }
    """

    visible: reactive[bool] = reactive(False)
    fraction: reactive[float] = reactive(0.0)
    label: reactive[str] = reactive("")
    phase: reactive[int] = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._timer = None
        self._started_at: float | None = None

    def compose(self):
        yield Static("", id="progress-content")

    def on_mount(self):
        self._content = self.query_one("#progress-content", Static)
        # Milik widget: pemindaian/memori/browser juga tetap bergerak ketika
        # tidak ada giliran chat. Pause saat tersembunyi agar tidak merender.
        self._timer = self.set_interval(_INTERVAL, self.tick, pause=True)
        if self.visible:
            self._timer.resume()
            self._gambar()

    def on_unmount(self):
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_resize(self, event) -> None:
        if self.display:
            self._gambar()

    @staticmethod
    def _label(text: str) -> str:
        label = " ".join(text.split()).rstrip(". …") or "Menunggu respons"
        return label[0].upper() + label[1:]

    def show(self, fraction: float = 0.0, label: str = ""):
        """Mulai/tampilkan status; token berulang tidak mereset animasi."""
        clean = self._label(label)
        changed = clean != self.label
        starting = not self.visible
        self.fraction = min(1.0, max(0.0, fraction))
        self.label = clean
        if starting:
            self.phase = 0
            self._started_at = time.monotonic()
            self.visible = True
            if self._timer is not None:
                self._timer.resume()
            tampilkan_halus(self)
        if starting or changed:
            self._gambar()

    def update_progress(self, fraction: float, label: str = ""):
        """Pertahankan API lama; fraksi tidak dipakai sebagai waktu tunggu."""
        self.fraction = min(1.0, max(0.0, fraction))
        if label:
            clean = self._label(label)
            if clean != self.label:
                self.label = clean
                if self.visible:
                    self._gambar()

    def _gambar(self) -> None:
        if self._content is None or not self.visible:
            return
        width = max(1, self.content_size.width or self._content.size.width or 40)
        elapsed = max(0, int(time.monotonic() - self._started_at)) \
            if self._started_at is not None else 0
        # Jam di kanan hanya pada area yang cukup lebar dan setelah jeda awal.
        # Lebar label memakai sel terminal, termasuk CJK/emoji dan nama file.
        clock = f"{elapsed // 60}:{elapsed % 60:02d}" if elapsed >= 3 and width >= 48 else ""
        label_width = max(0, width - 3 - (len(clock) + 2 if clock else 0))
        label = Text(self.label or "Menunggu respons", style=tema.p("redup"))
        label.truncate(label_width, overflow="ellipsis")
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(_FRAMES[self.phase % len(_FRAMES)], style=tema.p("aksen"))
        line.append("  ")
        line.append_text(label)
        if clock:
            line.append(" " * max(2, width - line.cell_len - len(clock)))
            line.append(clock, style=tema.p("redup"))
        line.truncate(width, overflow="ellipsis")
        self._content.update(line)
        self.tooltip = self.label

    def hide(self):
        self.visible = False
        self.display = False
        self._started_at = None
        if self._timer is not None:
            self._timer.pause()

    def tick(self):
        if not self.visible or not self.display:
            return
        self.phase = (self.phase + 1) % len(_FRAMES)
        self._gambar()

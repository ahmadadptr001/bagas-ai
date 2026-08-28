"""PlanPanel widget — collapsible step-by-step plan display.

Menampilkan rencana eksekusi AI dengan status per step:
✓ done, ▸ active, · pending.
Runtuh jadi satu baris saat terminal terlalu kecil.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static
from textual.reactive import reactive
from textual.app import RenderResult
from rich.text import Text

from ...ui import tema


class PlanPanel(Widget):
    """Collapsible plan display — docked above chat box."""

    DEFAULT_CSS = """
    PlanPanel {
        height: auto;
        max-height: 12;
        padding: 0 1;
    }
    """

    visible: reactive[bool] = reactive(False)
    steps: reactive[list] = reactive(list)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None

    def compose(self):
        yield Static("", id="plan-content")

    def on_mount(self):
        self._content = self.query_one("#plan-content", Static)
        self.display = False

    def update_plan(self, steps: list[dict]):
        """Update the plan steps.

        Each step: {"text": str, "status": "done"|"active"|"pending"}
        """
        self.steps = steps
        self.visible = bool(steps)
        self.display = self.visible
        if self._content and steps:
            self._content.update(self._render_plan(steps))

    def _render_plan(self, steps: list[dict]) -> RenderResult:
        """Gambar rencana sebagai rel kiri (tanpa kotak penuh).

        Kotak penuh butuh perhitungan lebar kanan; versi lama memakai lebar
        tetap (10 dan 20 tanda hubung) sehingga kotaknya selalu patah pada
        lebar terminal apa pun. Rel kiri selalu rapi di semua lebar.
        """
        garis = tema.p("tepi_redup")
        selesai = sum(1 for s in steps if s.get("status") == "done")
        lebar = max(20, (self.size.width or 80) - 6)

        lines = Text()
        lines.append("  ◈ ", style=f"bold {tema.p('aksen')}")
        lines.append(f"Rencana {selesai}/{len(steps)}\n",
                     style=f"bold {tema.p('aksen_terang')}")

        for step in steps:
            status = step.get("status", "pending")
            teks = str(step.get("text", ""))
            if len(teks) > lebar:
                teks = teks[:lebar - 1] + "…"
            if status == "done":
                ikon, style = "✓", tema.p("aksen")
            elif status == "active":
                ikon, style = "▸", f"bold {tema.p('aksen_terang')}"
            else:
                ikon, style = "·", tema.p("redup")
            lines.append("  │ ", style=garis)
            lines.append(f"{ikon} ", style=style)
            lines.append(teks, style=style)
            lines.append("\n")

        return lines

    def collapse(self):
        """Runtuh jadi satu baris."""
        if self._content and self.steps:
            done = sum(1 for s in self.steps if s.get("status") == "done")
            t = Text()
            t.append(f"  ◈ rencana · {done}/{len(self.steps)} selesai",
                     style=f"bold {tema.p('aksen')}")
            self._content.update(t)

    def on_resize(self, event) -> None:
        """Gambar ulang pada lebar baru.

        ``event.size`` di sini adalah ukuran WIDGET (tingginya otomatis),
        jadi tinggi terminal harus diambil dari app — bukan dari event.
        """
        if not self.display or not self.steps:
            return
        try:
            tinggi = self.app.size.height
        except Exception:  # noqa: BLE001 — di luar konteks app
            tinggi = 24
        self.check_collapse(tinggi)

    def check_collapse(self, terminal_height: int):
        """Auto-collapse if terminal is too short."""
        if not self.steps or not self._content:
            return
        if terminal_height < len(self.steps) + 6:
            self.collapse()
        else:
            self._content.update(self._render_plan(self.steps))

    def clear(self):
        """Clear the plan."""
        self.steps = []
        self.visible = False
        self.display = False
        if self._content:
            self._content.update("")

"""PlanSidebar — panel rencana versi DOCK KANAN (layar lebar/desktop).

Isi sama dengan PlanPanel (footer), tapi doknya di kanan: rencana panjang
tak lagi memakan tinggi footer (yang mengecilkan area chat). "Aktif" di
layar lebar; otomatis disembunyikan saat terminal menyempit — yang dipakai
cukup PlanPanel inline di footer (lihat BagasAIApp._perbarui_layout_plan).

Sumber datanya plan_tool (tool `plan()`/`plan_step()` milik model), di-poll
ringan dari app tiap ~300 ms — mekanisme yang sama dengan cli.py._panel_plan.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static
from textual.reactive import reactive
from rich.text import Text

from ...ui import tema


class PlanSidebar(Widget):
    """Panel rencana docked kanan — aktif hanya di layar lebar."""

    DEFAULT_CSS = """
    PlanSidebar {
        dock: right;
        width: 32;
        height: 100%;
        display: none;
        background: $t-gema_bg;
        border-left: tall $t-tepi_redup;
        padding: 0 1;
    }
    """

    steps: reactive[list] = reactive(list)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None

    def compose(self):
        yield Static("", id="plan-side-content")

    def on_mount(self):
        self._content = self.query_one("#plan-side-content", Static)
        self.display = False

    def update_plan(self, steps: list[dict]):
        """Sinkronkan isi sidebar (dipanggil app bersama PlanPanel)."""
        self.steps = steps
        if self._content and steps:
            teks = self._render_langkah(steps)
            # Versi plain disimpan agar harness/pengetesan bisa membaca isi
            # tanpa bergantung pada API internal Static Textual.
            self.terakhir = teks.plain
            self._content.update(teks)

    def _render_langkah(self, steps: list[dict]) -> Text:
        """Versi sidebar dari panel rencana: tanpa truncasi per-lebar
        (sidebar punya lebar sendiri yang stabil), teks panjang dibungkus
        OptionList tidak dipakai di sini — Static memotongnya rapi.

        Nama sengaja BUKAN ``_render`` — itu metode internal Widget milik
        Textual dan tertimpa kita bikin render seluruh app mogok.
        """
        garis = tema.p("tepi_redup")
        selesai = sum(1 for s in steps if s.get("status") == "done")

        t = Text()
        t.append("◈ Rencana ", style=f"bold {tema.p('aksen')}")
        t.append(f"{selesai}/{len(steps)}\n", style=f"bold {tema.p('aksen_terang')}")
        t.append("─" * 28 + "\n", style=garis)
        for step in steps:
            status = step.get("status", "pending")
            teks = str(step.get("text", ""))
            if status == "done":
                ikon, style = "✓", tema.p("aksen")
            elif status == "active":
                ikon, style = "▸", f"bold {tema.p('aksen_terang')}"
            else:
                ikon, style = "·", tema.p("redup")
            t.append(f"{ikon} ", style=style)
            t.append(teks + "\n", style=style)
        return t

    def clear(self):
        self.steps = []
        if self._content:
            self._content.update("")

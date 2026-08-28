"""InfoSidebar — sidebar kanan untuk terminal selebar "dashboard".

Selalu tampil begitu terminal >= BagasAIApp._LEBAR_MIN kolom. Berisi dua
seksi:

- PlanSidebar ("◈ Rencana") — muncul hanya saat model memakai plan()/
  plan_step(); rencana yang tuntas tampil sebentar lalu hilang sendiri.
- SystemPanel ("◈ Sistem") — kesehatan mesin real time: CPU, RAM, disk,
  GPU bila terbaca.

Saat terminal menyempit, seluruh sidebar disembunyikan dan rencana
tampil inline sebagai PlanPanel di footer (perilaku lama — lihat
BagasAIApp._perbarui_layout_plan).
"""
from __future__ import annotations

from textual.widget import Widget

from .plan_sidebar import PlanSidebar
from .system_panel import SystemPanel


class InfoSidebar(Widget):
    """Wadah dock-kanan: seksi sistem (selalu) + rencana (saat ada)."""

    DEFAULT_CSS = """
    InfoSidebar {
        dock: right;
        width: 34;
        height: 100%;
        display: none;
        background: $t-gema_bg;
        border-left: tall $t-tepi_redup;
        padding: 0 1;
        overflow: hidden;
    }
    """

    def compose(self):
        yield PlanSidebar(id="plan-side")
        yield SystemPanel(id="system-panel")

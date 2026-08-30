"""PlanSidebar — seksi rencana di dalam InfoSidebar (dock kanan, layar lebar).

SEKSI, bukan lagi widget dock sendirian: sejak sidebar kanan menjadi panel
informasi umum (kesehatan sistem + rencana, lihat info_sidebar.py), widget
ini tinggal bagian "◈ Rencana" yang selalu hadir selama sidebar desktop
terlihat. Sebelum model memanggil plan()/plan_step(), bagian ini menampilkan
empty-state agar fitur planning tetap mudah ditemukan pengguna.

Sumber datanya plan_tool (tool ``plan()``/``plan_step()`` milik model),
di-poll ringan dari app tiap ~300 ms — mekanisme yang sama dengan
cli.py._panel_plan. Rencana yang TUNTAS tampil sebentar (centang penuh)
lalu langkahnya diganti empty-state oleh app — state plan_tool tetap utuh
sampai giliran baru (lihat BagasAIApp._poll_plan).
"""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static
from textual.reactive import reactive
from rich.text import Text

from ...ui import tema


class PlanSidebar(Widget):
    """Seksi rencana di sidebar kanan, termasuk empty-state planning."""

    DEFAULT_CSS = """
    PlanSidebar {
        height: auto;
        display: none;
        padding: 0;
    }
    """

    steps: reactive[list] = reactive(list)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        # Teks render terakhir (plain) — dipakai harness pengujian.
        self.terakhir: str = ""

    def compose(self):
        yield Static("", id="plan-side-content")

    def on_mount(self):
        self._content = self.query_one("#plan-side-content", Static)
        self.clear(tampil=False)

    def update_plan(self, steps: list[dict]):
        """Sinkronkan isi seksi rencana (dipanggil app bersama PlanPanel)."""
        self.steps = steps
        if steps:
            teks = self._render_langkah(steps)
            # Versi plain disimpan agar harness/pengetesan bisa membaca isi
            # tanpa bergantung pada API internal Static Textual.
            self.terakhir = teks.plain
            self._content.update(teks)
        self.display = bool(steps)

    def _render_kosong(self) -> Text:
        """Empty-state yang membuat fitur planning tetap terlihat."""
        t = Text()
        t.append("◈ Rencana\n", style=f"bold {tema.p('aksen')}")
        t.append("─" * self._lebar_garis() + "\n",
                 style=tema.p("tepi_redup"))
        t.append("· Belum ada rencana aktif\n", style=tema.p("redup"))
        return t

    def _lebar_garis(self) -> int:
        """Lebar divider mengikuti area nyata, termasuk sidebar minimum."""
        try:
            return max(10, int(self.size.width or 28))
        except Exception:  # noqa: BLE001 — widget belum masuk layout
            return 28

    def _render_langkah(self, steps: list[dict]) -> Text:
        """Rencana versi sidebar: tanpa truncasi per-lebar (sidebar punya
        lebar sendiri yang stabil).

        Nama sengaja BUKAN ``_render`` — itu metode internal Widget milik
        Textual dan tertimpa kita bikin render seluruh app mogok.
        """
        garis = tema.p("tepi_redup")
        selesai = sum(1 for s in steps if s.get("status") == "done")

        t = Text()
        t.append("◈ Rencana ", style=f"bold {tema.p('aksen')}")
        t.append(f"{selesai}/{len(steps)}\n", style=f"bold {tema.p('aksen_terang')}")
        t.append("─" * self._lebar_garis() + "\n", style=garis)
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

    def on_resize(self, _event) -> None:
        """Gambar ulang divider saat lebar sidebar ditarik pengguna."""
        if not self.display or self._content is None:
            return
        teks = (self._render_langkah(self.steps) if self.steps
                else self._render_kosong())
        self.terakhir = teks.plain
        self._content.update(teks)

    def clear(self, *, tampil: bool = False):
        """Kosongkan langkah; opsional tampilkan empty-state di sidebar."""
        self.steps = []
        if self._content:
            if tampil:
                teks = self._render_kosong()
                self.terakhir = teks.plain
                self._content.update(teks)
            else:
                self.terakhir = ""
                self._content.update("")
        self.display = tampil

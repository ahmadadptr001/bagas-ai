"""SystemPanel — seksi kesehatan sistem di sidebar (layar lebar).

Selalu tampil di dalam InfoSidebar begitu terminal cukup lebar: CPU, RAM,
disk, dan GPU bila terbaca. Datanya di-poll app (psutil, cepat, di thread
UI) tiap ~2 dtk; GPU datang dari thread khusus (nvidia-smi adalah proses
eksternal — JANGAN dipanggil di thread UI, ia membekukan render ±100-300 ms).

Bar progres memakai blok █/░ — skala kecil 10 sel, cukup untuk melihat
tren tanpa memakan lebar sidebar.
"""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static
from rich.text import Text

from ... import config
from ...ui import tema


def _bar(persen: float, lebar: int = 10) -> str:
    """Bar blok 10 sel untuk 0-100 persen."""
    persen = max(0.0, min(100.0, float(persen)))
    isi = round(lebar * persen / 100)
    return "█" * isi + "░" * (lebar - isi)


class SystemPanel(Widget):
    """Seksi "◈ Sistem" — metrik kesehatan mesin real time."""

    DEFAULT_CSS = """
    SystemPanel {
        height: auto;
        padding: 0;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        # Teks render terakhir (plain) — dipakai harness pengujian.
        self.terakhir: str = ""

    def compose(self):
        yield Static("", id="system-content")

    def on_mount(self):
        self._content = self.query_one("#system-content", Static)
        self.terapkan(cpu=0.0, ram_persen=0.0, ram_teks="",
                      disk_persen=0.0, disk_teks="",
                      gpu_nama="…", gpu_metrik="")

    def terapkan(self, cpu: float, ram_persen: float, ram_teks: str,
                 disk_persen: float, disk_teks: str,
                 gpu_nama: str, gpu_metrik: str) -> None:
        """Gambar ulang panel dari data terkini (dipanggil app tiap poll)."""
        t = Text()
        t.append("◈ Sistem", style=f"bold {tema.p('aksen')}")
        t.append("\n" + "─" * 28 + "\n", style=tema.p("tepi_redup"))

        def baris(label, persen, teks):
            t.append(f"{label:<5}", style=tema.p("redup"))
            t.append("▕", style=tema.p("tepi_redup"))
            warna = (tema.p("aksen") if persen < 80
                     else tema.p("exit_footer"))
            t.append(_bar(persen), style=warna)
            t.append("▏ ", style=tema.p("tepi_redup"))
            t.append(f"{persen:3.0f}%", style=f"bold {warna}")
            if teks:
                t.append(f" {teks}", style=tema.p("redup"))
            t.append("\n")

        baris("CPU", cpu, "")
        baris("RAM", ram_persen, ram_teks)
        baris("Disk", disk_persen, disk_teks)
        # GPU: nama selalu; metrik (util/mem) hanya bila terbaca — GPU
        # non-NVIDIA di Windows tak punya cara baca ringan lintas vendor.
        t.append("GPU  ", style=tema.p("redup"))
        t.append((gpu_nama or "—")[:24], style=tema.p("teks"))
        t.append("\n")
        if gpu_metrik:
            t.append("     ", style=tema.p("redup"))
            t.append(gpu_metrik, style=tema.p("redup"))
            t.append("\n")

        # Footer panel: folder project aktif — sama dengan yang dipakai
        # tools file/shell (config.PROJECT_ROOT), jadi tak pernah bohong
        # tentang di mana bagas-ai sedang bekerja.
        t.append("─" * 28 + "\n", style=tema.p("tepi_redup"))
        try:
            root = str(config.PROJECT_ROOT)
        except Exception:  # noqa: BLE001 — config tak terbaca
            root = ""
        if root:
            # Potong KEPALA path (bukan ekor): nama folder & induknya yang
            # penting, drive/root lama boleh jadi "…".
            label = "📁 "
            muat = 28 - len(label)
            tampil = root.replace("\\", "/")
            if len(tampil) > muat:
                tampil = "…" + tampil[-(muat - 1):]
                # Beri potongan di batas folder, bukan di tengah nama —
                # "…JECTS/ai-agent" lebih buram daripada "…/ai-agent".
                spasi = tampil.find("/")
                if 0 < spasi < len(tampil) - 1:
                    tampil = "…" + tampil[spasi + 1:]
            t.append(label, style=tema.p("redup"))
            t.append(tampil, style=tema.p("redup"))
            t.append("\n")

        if self._content:
            self.terakhir = t.plain
            self._content.update(t)

    def clear(self):
        self.terapkan(cpu=0.0, ram_persen=0.0, ram_teks="",
                      disk_persen=0.0, disk_teks="",
                      gpu_nama="…", gpu_metrik="")

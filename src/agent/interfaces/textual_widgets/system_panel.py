"""SystemPanel — seksi kesehatan sistem di sidebar (layar lebar).

Selalu tampil di dalam InfoSidebar begitu terminal cukup lebar: CPU, RAM,
disk, dan GPU bila terbaca. Datanya di-poll app (psutil, cepat, di thread
UI) tiap ~2 dtk; GPU datang dari thread khusus (nvidia-smi adalah proses
eksternal — JANGAN dipanggil di thread UI, ia membekukan render ±100-300 ms).

Grafik mini menampilkan sampel metrik asli, tanpa data dekoratif.
"""
from __future__ import annotations

from collections import deque

from textual.widget import Widget
from textual.widgets import Static
from rich.text import Text

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
        self._riwayat = {label: deque(maxlen=10) for label in ("CPU", "RAM", "Disk")}

    def _garis(self) -> int:
        """Lebar garis pemisah — mengikuti lebar sidebar (bisa diubah
        pengguna lewat handle tarik di InfoSidebar)."""
        try:
            w = self.content_size.width or 27
        except Exception:  # noqa: BLE001 — belum ter-mount
            w = 31
        return max(10, w)

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
        t.append("▥ ", style=tema.p("aksen"))
        t.append("Sistem\n\n", style=tema.p("teks"))

        def baris(label, persen, teks):
            warna = tema.p({"CPU": "aksen", "RAM": "aksen2", "Disk": "ubah_footer"}[label])
            riwayat = self._riwayat[label]
            riwayat.append(max(0.0, min(100.0, float(persen))))
            grafik = "".join("▁▂▃▄▅▆▇█"[min(7, int(v * 8 / 100))] for v in riwayat)
            t.append(f"{label:<5}", style=tema.p("redup"))
            t.append(f"{persen:3.0f}%", style=warna)
            # Posisi grafik mengikuti lebar panel; persentase tetap sejajar.
            espace = max(1, self._garis() - 19)
            t.append(" " * espace)
            t.append(grafik.rjust(10), style=warna)
            t.append("\n")

        baris("CPU", cpu, "")
        baris("RAM", ram_persen, ram_teks)
        baris("Disk", disk_persen, disk_teks)
        # GPU: nama selalu; metrik (util/mem) hanya bila terbaca — GPU
        # non-NVIDIA di Windows tak punya cara baca ringan lintas vendor.
        nama = gpu_nama or "—"
        sisa = max(8, self._garis() - 5)
        if len(nama) > sisa:
            nama = nama[:max(1, sisa - 1)] + "…"
        t.append("GPU  ", style=tema.p("redup"))
        t.append(nama, style=tema.p("teks"))
        if gpu_metrik:
            t.append("\n     ", style=tema.p("redup"))
            t.append(gpu_metrik, style=tema.p("redup"))

        # Nilai rinci tetap tersedia tanpa membuat kartu bertambah tinggi.
        self.tooltip = "\n".join(filter(None, (
            f"RAM: {ram_teks}" if ram_teks else "",
            f"Disk: {disk_teks}" if disk_teks else "",
            f"GPU: {gpu_nama}" if gpu_nama else "",
            gpu_metrik,
        ))) or None

        if self._content:
            self.terakhir = t.plain
            self._content.update(t)

    def clear(self):
        self.terapkan(cpu=0.0, ram_persen=0.0, ram_teks="",
                      disk_persen=0.0, disk_teks="",
                      gpu_nama="…", gpu_metrik="")

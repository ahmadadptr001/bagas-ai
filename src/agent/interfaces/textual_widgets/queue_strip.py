"""QueueStrip widget — daftar prompt yang mengantre, tampak "disabled".

Prompt yang dikirim saat giliran sedang berjalan TIDAK di-echo ke riwayat
percakapan. Ia tampil di strip ini — nempel di bawah area jawaban —
dengan gaya redup penuh sebagai penanda bahwa prompt itu BARU menunggu
giliran. Begitu antrean benar-benar dijalankan, aplikasi mengosongkan
strip ini dan meng-echo promptnya sebagai pesan pengguna normal di
riwayat: posisinya tak lagi menempel, warnanya tak lagi disabled.

Bentuknya mengikuti gema prompt di riwayat: panah + teks, tanpa latar.
Tanpa label tambahan ("diantrekan", "menunggu", dst.) — isi prompt itu
sendiri cukup jelas sebagai konteks.
"""
from __future__ import annotations

from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static

from ...ui import tema
from .animasi import tampilkan_halus


class QueueStrip(Widget):
    """Baris-baris prompt mengantre, diredupkan penuh."""

    DEFAULT_CSS = """
    QueueStrip {
        height: auto;
        max-height: 3;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._items: list[str] = []

    def compose(self):
        yield Static("", id="queue-content")

    def on_mount(self):
        self._content = self.query_one("#queue-content", Static)
        self.display = False

    def on_resize(self, event) -> None:
        """Potong ulang teks pada lebar baru."""
        if self.display and self._items:
            self.set_items(self._items)

    def set_items(self, items: list[str]) -> None:
        """Tampilkan daftar prompt mengantre; kosongkan bila tak ada."""
        self._items = list(items)
        if not self._content:
            return
        if not self._items:
            self.display = False
            return

        lebar = self.size.width or 0
        if not lebar:
            try:
                lebar = self.app.size.width
            except Exception:  # noqa: BLE001 — di luar konteks app
                lebar = 80

        # Gaya sama dengan gema prompt pengguna, tapi diredupkan penuh
        # ("disabled"): panah dan isi sama-sama kusam.
        panah = f"dim bold {tema.p('redup')}"
        isi = f"dim italic {tema.p('redup')}"

        t = Text()
        for i, teks in enumerate(self._items[:2]):
            if i:
                t.append("\n")
            # Baris panjang dipotong — strip cuma pengingat, bukan editor.
            sisa = max(4, lebar - 2)
            baris = teks if len(teks) <= sisa else teks[:max(1, sisa - 1)] + "…"
            t.append("❯ ", style=panah)
            t.append(baris, style=isi)
        if len(self._items) > 2:
            t.append(f"\n  +{len(self._items) - 2} pesan", style=isi)
        self._content.update(t)
        tampilkan_halus(self)

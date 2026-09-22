"""ThinkingBlock widget — blok penalaran yang bisa dibuka/tutup.

Menampilkan pikiran model (reasoning tokens):

- Tertutup: ``▸ 💭 berpikir... 382 karakter``
- Terbuka:  ``▾ 💭 berpikir... 382 karakter`` + beberapa baris terakhir

Buka/tutup dengan KLIK pada barisnya (tanda ▸/▾ penanda keadaan).

CATATAN BUG YANG SUDAH DIPERBAIKI (jangan diulang):

1. ``hide()`` dulu MENGHAPUS ``self._text``. Karena Tab (``toggle``) pada
   keadaan tertentu memanggilnya, isi pikiran hilang dan blok tak bisa
   dibuka lagi. Sekarang ``hide()`` hanya menyembunyikan; hanya ``clear()``
   yang menghapus isi.
2. Tak ada metode ``toggle()`` padahal aplikasi memanggilnya (Tab) — dulu
   itu ``AttributeError`` yang tertelan.
3. ``_gambar()`` (dulu bernama ``_render()`` — JANGAN pakai nama itu lagi:
   ia menimpa API internal Textual ``Widget._render()`` yang wajib
   mengembalikan Visual, sehingga Textual crash dengan
   "'NoneType' object has no attribute 'render_strips'"). Dijalankan pada
   SETIAP token penalaran dan membungkus teks pada lebar tetap 72.
   Sekarang render dijadwalkan berkala (throttle) dan lebarnya mengikuti
   lebar widget.
4. Header dulu berbunyi "💭 pikiran 382 huruf · tab buka/tutup" (2026-09-22).
   Dua-tiganya keliru: **Tab TIDAK PERNAH terikat** di aplikasi TUI (Tab
   milik ChatBox untuk melengkapi otomatis — lihat BINDINGS textual_app),
   jadi kalimat itu janji palsu yang membuat pengguna menekan tombol yang
   salah; "huruf" bukan istilah yang dipakai di tempat lain (cli.py memakai
   "karakter" + pemisah ribuan titik); dan kata "pikiran" tetap terpampang
   setelah penalaran selesai. Sekarang: tanpa hint tombol sama sekali,
   hitungan berformat ``1.234 karakter``, dan kata "berpikir..." hanya
   muncul selagi token penalaran benar-benar mengalir.
   Ambang "berhenti mengalir" SENGAJA lama (``_JEDA_DIAM``), bukan jeda
   render: model sering tersendat ratusan milidetik di tengah penalaran,
   dan ambang sependek jeda render membuat header berkedip
   "berpikir.../pikiran" puluhan kali per giliran.
5. Tinggi blok dulu dipagari ``max-height`` di CSS (8/4/3 baris menurut
   lebar & tinggi layar). Batas itu memotong dari BAWAH — justru baris
   TERBARU yang paling ingin dibaca yang hilang, dan angkanya tak tahu
   berapa baris isi sebenarnya. Sekarang jumlah baris isi dihitung di
   Python (``_baris_maks``), jadi hanya ada satu sumber kebenaran.
"""
from __future__ import annotations

import textwrap
import time

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ...ui import tema

# Jarak minimal antar render saat token penalaran mengalir (detik).
_JEDA_RENDER = 0.12
# Lama tanpa token sebelum header berhenti mengaku "berpikir..." (detik).
# Jauh lebih panjang dari _JEDA_RENDER: ini soal MAKNA, bukan soal beban
# render — model lazim tersendat sepersekian detik di tengah penalaran.
_JEDA_DIAM = 1.2
# Baris isi maksimum saat blok dibuka (batas atas, sebelum disesuaikan
# dengan tinggi terminal). Sejalan dengan _PIKIR_BARIS di cli.py.
_PIKIR_BARIS = 8
# Perkiraan tinggi bagian lain layar (riwayat + kotak input + status bar).
# Blok berpikir adalah pelengkap: di terminal pendek ia harus mengalah.
_SISA_LAYAR = 15


def _fmt(n: int) -> str:
    """1234 -> "1.234" — pemisah ribuan gaya Indonesia (sama dengan cli.py)."""
    return f"{n:,}".replace(",", ".")


class ThinkingBlock(Widget):
    """Blok penalaran yang bisa dibuka/tutup, di atas ChatBox."""

    DEFAULT_CSS = """
    ThinkingBlock {
        height: auto;
        padding: 0 1;
        display: none;
    }
    """

    collapsed: reactive[bool] = reactive(True)

    def __init__(self, max_lines: int = _PIKIR_BARIS, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        self._text = ""
        self._max_lines = max_lines
        self._timer = None
        self._kotor = False
        self._mengalir = False  # token penalaran sedang masuk?
        self._waktu_token = 0.0  # monotonic() token penalaran terakhir

    def compose(self):
        yield Static("", id="thinking-content")

    def on_mount(self):
        self._content = self.query_one("#thinking-content", Static)

    def on_click(self, event) -> None:
        """Buka/tutup saat diklik."""
        event.stop()
        self.toggle()

    def on_resize(self, event) -> None:
        """Bungkus ulang teks pada lebar baru."""
        if self.display and not self.collapsed:
            self._gambar()

    # --- API publik ----------------------------------------------------

    def toggle(self) -> None:
        """Buka/tutup blok. Aman dipanggil walau blok sedang tersembunyi."""
        if not self._text:
            return
        self.collapsed = not self.collapsed
        self.display = True
        self._gambar()

    def update_thinking(self, text: str) -> None:
        """Ganti seluruh teks penalaran."""
        if not text:
            self.hide()
            return
        self._text = text
        self._mengalir = True
        self._waktu_token = time.monotonic()
        self.display = True
        self._jadwalkan()

    def append_thinking(self, piece: str) -> None:
        """Tambah sepotong teks penalaran (dari thread utama)."""
        if not piece:
            return
        self._text += piece
        self._mengalir = True
        self._waktu_token = time.monotonic()
        self.display = True
        self._jadwalkan()

    def hide(self) -> None:
        """Sembunyikan blok TANPA menghapus isinya."""
        self._batalkan_timer()
        self.display = False

    def clear(self) -> None:
        """Sembunyikan blok DAN hapus isinya."""
        self._batalkan_timer()
        self._text = ""
        self.collapsed = True
        self.display = False
        if self._content:
            self._content.update("")

    # --- Render --------------------------------------------------------

    def _batalkan_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._timer = None
        self._kotor = False
        self._mengalir = False

    def _jadwalkan(self) -> None:
        """Tunda render supaya arus token tidak membanjiri UI."""
        if self._timer is not None:
            self._kotor = True
            return
        self._gambar()
        try:
            self._timer = self.set_timer(_JEDA_RENDER, self._selesai_jeda)
        except Exception:  # noqa: BLE001 — belum ter-mount
            self._timer = None

    def _selesai_jeda(self) -> None:
        self._timer = None
        if self._kotor:
            self._kotor = False
            self._jadwalkan()
            return
        if not self._mengalir:
            return
        # Satu jeda render tanpa token baru BUKAN berarti penalaran berhenti.
        # Timer hanya dijadwalkan ulang saat ada token, jadi diam panjang
        # harus diperiksa dengan jam, lalu ditunggu sisa waktunya.
        sisa = _JEDA_DIAM - (time.monotonic() - self._waktu_token)
        if sisa > 0:
            try:
                self._timer = self.set_timer(sisa, self._selesai_jeda)
            except Exception:  # noqa: BLE001 — belum ter-mount
                self._timer = None
            return
        # Benar-benar diam: header tak boleh terus mengaku "berpikir...".
        self._mengalir = False
        self._gambar()

    def _baris_maks(self) -> int:
        """Berapa baris isi boleh tampil, dibatasi tinggi terminal."""
        try:
            tinggi = self.app.size.height
        except Exception:  # noqa: BLE001 — di luar konteks app
            tinggi = 24
        return max(3, min(self._max_lines, tinggi - _SISA_LAYAR))

    def _gambar(self) -> None:
        """Gambar blok sesuai keadaan buka/tutup."""
        if not self._content:
            return
        jumlah = len(self._text)
        if jumlah == 0:
            self.display = False
            return

        header = Text(no_wrap=True, overflow="ellipsis")
        header.append(f"  {'▸' if self.collapsed else '▾'} ",
                      style=f"bold {tema.p('aksen_terang')}")
        # "berpikir…" hanya selama token benar-benar mengalir; sesudahnya
        # "pikiran" — supaya header tidak menjanjikan sesuatu yang berhenti.
        kata = "berpikir..." if self._mengalir else "pikiran"
        header.append(f"💭 {kata} {_fmt(jumlah)} karakter",
                      style=tema.p("aksen"))

        if self.collapsed:
            self._content.update(header)
            return

        batas = self._baris_maks()
        lebar = max(20, (self.size.width or 80) - 8)
        baris = textwrap.wrap(" ".join(self._text.split()), lebar)
        ekor = baris[-batas:]

        hasil = Text()
        hasil.append_text(header)
        hasil.append("\n")
        if len(baris) > batas:
            hasil.append(f"    ⋮ ({len(baris) - batas} baris sebelumnya)\n",
                         style=f"dim {tema.p('redup')}")
        for b in ekor:
            hasil.append(f"    {b}\n", style=f"dim italic {tema.p('redup')}")
        self._content.update(hasil)

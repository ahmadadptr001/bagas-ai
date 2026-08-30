"""TurnProgressBar widget — animasi menunggu berbentuk logo bagas-ai.

Menggantikan bar pil lama ``▐▰▰╌╌╌▌``: kini yang berputar adalah LOGO
BUNGA BENGAWAN bagas-ai (bunga heksagonal 6 kelopak, turunan pixel-art
logo resmi) dengan animasi BONGKAR-PASANG — mengacu animasi logo
ChatGPT/Qwen saat menunggu jawaban:

  pasang  : sapuan sudut menyala dari atas searah jarum jam, kelopak
            demi kelopak menyusun logo utuh;
  tahan   : logo utuh + pita terang berputar menyapu kelopak (shimmer);
  bongkar : sapuan lanjut mematikan kelopak dari belakang, logo
            terurai;
  ... lalu diulang terus (loop) selama giliran berjalan.

Logo digambar dengan half-block (▀ ▄ █) — satu sel teks menampung dua
piksel vertikal. Warna mengikuti gradasi aksen tema aktif (terang di
pusat, dalam di ujung kelopak), jadi tiap tema punya logo warnanya
sendiri.

CATATAN: JANGAN menamai metode privat dengan nama API internal Textual
(contoh kasus lama: ``_render()`` yang menimpa ``Widget._render()`` dan
membuat crash "'NoneType' object has no attribute 'render_strips'").
Nama aman di sini: ``_gambar()``.
"""
from __future__ import annotations

import math

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ...ui import tema

# ── Logo: bunga heksagonal 6 kelopak, 9×8 piksel ────────────────────
# Siluet heksagon (tanda merek bagas-ai) dengan lubang antar-kelopak —
# versi kecil dari logo resmi; ditampilkan 4 baris teks via half-block.
_LOGO = (
    " # ",
    "###",
    "# # ",
)

# Durasi tiap tahap animasi (dalam tick; 1 tick ≈ 0.15 dtk dari app).
_TAHAP_PASANG = 14
_TAHAP_TAHAN = 8
_TAHAP_BONGKAR = 14
_SIKLUS = _TAHAP_PASANG + _TAHAP_TAHAN + _TAHAP_BONGKAR

# Lebar pita terang di tepi sapuan / shimmer (derajat).
_LEBAR_PITA = 45.0


class _Piksel:
    """Satu piksel logo: posisi grid, sudut (°), dan jari-jari (0..1)."""

    __slots__ = ("x", "y", "sudut", "r")

    def __init__(self, x: int, y: int, cx: float, cy: float, r_maks: float):
        self.x, self.y = x, y
        dx, dy = x - cx, y - cy
        self.sudut = math.degrees(math.atan2(dy, dx)) % 360.0
        self.r = min(1.0, math.hypot(dx, dy) / r_maks)


class TurnProgressBar(Widget):
    """Animasi logo bagas-ai — ditampilkan selama giliran AI berjalan."""

    DEFAULT_CSS = """
    TurnProgressBar {
        height: auto;
        max-height: 3;
    }
    """

    visible: reactive[bool] = reactive(False)
    fraction: reactive[float] = reactive(0.0)
    label: reactive[str] = reactive("")
    phase: reactive[int] = reactive(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._content: Static | None = None
        # Pratinjau piksel logo: sudut & jari-jari, dihitung sekali.
        cx, cy = (len(_LOGO[0]) - 1) / 2, (len(_LOGO) - 1) / 2
        r_maks = math.hypot(cx, cy)
        self._piksel = [
            _Piksel(x, y, cx, cy, r_maks)
            for y, baris in enumerate(_LOGO)
            for x, c in enumerate(baris)
            if c == "#"
        ]

    def compose(self):
        yield Static("", id="progress-content")

    def on_mount(self):
        self._content = self.query_one("#progress-content", Static)
        self.display = False

    def on_resize(self, event) -> None:
        """Gambar ulang dengan lebar baru."""
        if self.display:
            self._gambar()

    def show(self, fraction: float = 0.0, label: str = ""):
        """Tampilkan animasi (fraction dipertahankan demi API lama)."""
        self.fraction = fraction
        self.label = label
        self.visible = True
        self.display = True
        self._gambar()

    def update_progress(self, fraction: float, label: str = ""):
        """Perbarui label status (fraksi tak dipakai animasi logo)."""
        self.fraction = min(1.0, max(0.0, fraction))
        if label:
            self.label = label
        self._gambar()

    # ── Mesin animasi ────────────────────────────────────────────────

    def _sudut_pita(self) -> tuple[float, float]:
        """(sudut_akhir_sapuan, sudut_shimmer) untuk tick saat ini.

        Sudut sapuan = seberapa jauh logo "tersusun" (360° = utuh).
        Shimmer hanya relevan saat tahan.
        """
        p = self.phase
        if p < _TAHAP_PASANG:
            return p / _TAHAP_PASANG * 360.0, -1.0
        p -= _TAHAP_PASANG
        if p < _TAHAP_TAHAN:
            # Tahan: logo utuh; pita terang berputar menyapu.
            return 360.0, (p * 45.0) % 360.0
        p -= _TAHAP_TAHAN
        sisip = max(0.0, 1.0 - p / _TAHAP_BONGKAR)
        return sisip * 360.0, -1.0

    def _warna_piksel(self, pik: _Piksel, sapuan: float, shimmer: float,
                      warna: dict[str, str]) -> tuple[str, bool] | None:
        """(warna, bold) piksel untuk frame ini; None = padam (tak tampak).

        Piksel menyala bila sudutnya sudah "terlewati" sapuan; yang baru
        saja menyala (dekat tepi sapuan) atau tersapu shimmer diberi
        warna paling terang + bold. Warna dan bold dipisah supaya aman
        dirangkai sebagai `fg on bg` saat dua piksel dalam satu sel
        berbeda keadaan.
        """
        # 270° = arah atas pada koordinat layar (y ke bawah): sapuan
        # mulai dari kelopak atas lalu berputar searah jarum jam.
        rho = (pik.sudut - 270.0) % 360.0
        if rho >= sapuan:
            return None
        # Gradasi radial: pusat terang, ujung kelopak dalam.
        if pik.r < 0.35:
            dasar = warna["terang"]
        elif pik.r < 0.75:
            dasar = warna["aksen"]
        else:
            dasar = warna["dalam"]
        tepi = sapuan - rho  # jarak di belakang tepi sapuan
        if 0 <= tepi < _LEBAR_PITA:
            return warna["terang"], True
        if shimmer >= 0:
            d = abs(rho - shimmer)
            d = min(d, 360.0 - d)
            if d < _LEBAR_PITA:
                return warna["terang"], True
        return dasar, False

    def _gambar(self) -> None:
        """Susun satu frame logo + label lalu tulis ke Static."""
        if not self._content:
            return
        lebar_w = self.size.width or 0
        if not lebar_w:
            try:
                lebar_w = self.app.size.width
            except Exception:  # noqa: BLE001 — di luar konteks app
                lebar_w = 80

        warna = {
            "terang": tema.p("aksen_terang"),
            "aksen": tema.p("aksen"),
            "dalam": tema.p("aksen2"),
        }
        sapuan, shimmer = self._sudut_pita()

        # Warna tiap piksel untuk frame ini, dikelompokkan per baris.
        tinggi = len(_LOGO)
        grid: list[dict[int, tuple[str, bool]]] = [dict() for _ in range(tinggi)]
        for pik in self._piksel:
            w = self._warna_piksel(pik, sapuan, shimmer, warna)
            if w is not None:
                grid[pik.y][pik.x] = w

        # Label status: di tengah vertikal, di kanan logo.
        label_txt = ""
        if self.label:
            sisa = max(0, lebar_w - len(_LOGO[0]) - 4)
            label_txt = self.label if len(self.label) <= sisa else \
                self.label[:max(1, sisa - 1)] + "…"
        baris_label = tinggi // 2

        # Rasterisasi half-block: 2 piksel vertikal per sel teks.
        t = Text(no_wrap=True)
        for y in range(0, tinggi, 2):
            if y:
                t.append("\n")
            for x in range(len(_LOGO[0])):
                atas = grid[y].get(x)
                bawah = grid[y + 1].get(x) if y + 1 < tinggi else None
                if atas and bawah:
                    wa, ba = atas
                    wb, bb = bawah
                    if wa == wb and ba == bb:
                        t.append("█",
                                 style=("bold " if (ba or bb) else "") + wa)
                    else:
                        # ▀ = separuh ATAS sel: fg piksel atas, bg piksel
                        # bawah. Bold hanya milik fg — bg murni warna.
                        t.append("▀",
                                 style=("bold " if ba else "")
                                 + f"{wa} on {wb}")
                elif atas:
                    wa, ba = atas
                    t.append("▀", style=("bold " if ba else "") + wa)
                elif bawah:
                    wb, bb = bawah
                    t.append("▄", style=("bold " if bb else "") + wb)
                else:
                    t.append(" ")
            if y == baris_label - (baris_label % 2) and label_txt:
                t.append(f"  {label_txt}", style=f"bold {warna['terang']}")

        self._content.update(t)

    def hide(self):
        """Sembunyikan animasi."""
        self.visible = False
        self.display = False

    def tick(self):
        """Majukan satu frame animasi."""
        self.phase = (self.phase + 1) % _SIKLUS
        if self.visible:
            self._gambar()

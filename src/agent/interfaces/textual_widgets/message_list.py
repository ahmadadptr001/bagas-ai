"""MessageList widget — area gulir untuk seluruh isi percakapan.

Menampilkan gema pengguna, jawaban AI (markdown), langkah tool,
pemberitahuan, diff, dan gambar. Gulir otomatis ke bawah.

CATATAN BUG YANG SUDAH DIPERBAIKI (jangan diulang):

1. Dulu ini ``Widget`` yang membungkus ``RichLog`` anak dan menyimpannya di
   ``self._log`` pada ``on_mount``. Setiap metode diawali ``if not self._log:
   return`` sehingga SEMUA tulisan sebelum mount HILANG TANPA JEJAK. Sekarang
   kelas ini TURUNAN ``RichLog`` langsung — RichLog sendiri sudah menunda
   tulisan sampai ukurannya diketahui (``_deferred_renders``).
2. ``RichLog.min_width`` bawaannya 78. Semua tulisan dirender minimal 78 kolom
   lalu dipotong horizontal di terminal sempit. Sekarang ``min_width=1`` dan
   setiap tulisan diberi ``width=`` selebar area isi.
3. ``RichLog`` bawaannya ``can_focus=True``: klik di riwayat merebut fokus dari
   kotak input dan ketikan berikutnya hilang. Sekarang ``can_focus=False``.
4. RichLog menyimpan hasil render sebagai ``Strip`` dan TIDAK pernah
   membungkus ulang saat lebar berubah. Semua tulisan sekarang lewat
   ``_emit()`` yang juga menyimpan renderable-nya, jadi ``on_resize`` bisa
   menggambar ulang (dengan jeda) agar teks ikut lebar baru.
5. ``append_notice`` dulu hanya memberi spasi di baris PERTAMA
   (``f"  {text}"``), sehingga seluruh keluaran berkotak (mis. ``/help``)
   miring dan patah. Sekarang setiap baris diberi indentasi.
6. Jawaban yang sedang mengalir dulu tampil di DUA tempat: panel
   ``StreamingPreview`` terpisah DI BAWAH blok berpikir — tanpa label dan
   diredupkan, jadi mudah disangka blok "berpikir" kedua — lalu muncul lagi
   sebagai jawaban final saat giliran selesai. Panel itu dibuang; jawaban
   kini tumbuh LANGSUNG di area percakapan sebagai markdown
   (``flush_stream``), dan ``end_stream`` memastikan tak ada tulisan ganda
   di akhir giliran.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

from rich import box
from rich.console import RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.text import Text
from textual.events import Resize
from rich.style import Style as RichStyle
from textual.strip import Strip
from textual.widgets import RichLog

from ...ui import tema
from ...ui.clipboard import copy_text
from ...ui.diff_view import DiffView

if TYPE_CHECKING:
    from ...core import Agent

# Renderable yang disimpan untuk digambar ulang saat lebar layar berubah.
_MAKS_RIWAYAT = 1500
# Batas baris di dalam RichLog (menahan pemakaian memori sesi panjang).
_MAKS_BARIS_LOG = 12000
# Jeda gambar-ulang setelah resize berhenti (detik).
_JEDA_RESIZE = 0.2
# Jeda minimum antar gambar-ulang jawaban yang sedang mengalir (detik).
# Token datang puluhan per detik; merender markdown seluruh jawaban sesering
# itu murni boros CPU tanpa bedanya bagi mata.
_JEDA_ALIRAN = 0.12
# Kursor hidup di ujung jawaban yang sedang mengalir. Kedipnya lambat
# (0.55 dtk) supaya tidak terasa seperti kedipan error; glyph selebar 1 sel.
_JEDA_KURSOR = 0.55
_KURSOR = "▍"


def _bersih_kendali(text: str) -> str:
    """Buang escape ANSI supaya tidak bocor sebagai teks mentah."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _indent(text: str, pad: str = "  ") -> str:
    """Beri indentasi pada SETIAP baris (bukan cuma baris pertama)."""
    return "\n".join(pad + b for b in text.split("\n"))


_SAMBUNG_POHON = ("├", "└", "┣", "┗")
_GARIS_POHON = _SAMBUNG_POHON + ("│", "─", "┃", "━")
_PAGAR = re.compile(r"^\s*(```|~~~)")


def _pagari_pohon(text: str) -> str:
    """Bungkus gambar pohon direktori dengan pagar kode sebelum di-Markdown-kan.

    Port dari ``cli._pagari_pohon`` — versi Textual lama terlalu ketat (hanya
    ``[│├└…]`` di awal baris) sehingga pohon dengan indentasi/spasi awal lolos
    dan Markdown meremukkannya jadi satu paragraf. Kenapa perlu: Markdown
    menganggap baris-baris berurutan sebagai SATU paragraf lalu membungkusnya
    ulang mengikuti lebar terminal — gambar pohon ("├── agent/") jadi hancur.
    """
    if not any(c in text for c in _SAMBUNG_POHON):
        return text
    keluar: list[str] = []
    blok: list[str] = []
    dalam_pagar = False

    def _tutup() -> None:
        if not blok:
            return
        layak = len(blok) >= 2 and any(
            any(c in b for c in _SAMBUNG_POHON) for b in blok)
        keluar.extend(["```text", *blok, "```"] if layak else blok)
        blok.clear()

    for baris in text.splitlines():
        if _PAGAR.match(baris):
            _tutup()
            dalam_pagar = not dalam_pagar
            keluar.append(baris)
            continue
        if dalam_pagar:
            keluar.append(baris)
            continue
        if any(c in baris for c in _GARIS_POHON):
            blok.append(baris)
            continue
        if not blok and baris.strip().endswith(("/", "\\")) and \
                not baris.lstrip().startswith(("#", ">", "-", "*", "+")):
            blok.append(baris)
            continue
        _tutup()
        keluar.append(baris)
    _tutup()
    return "\n".join(keluar)


# Pygments theme untuk blok kode ```lang``` — gelap: gruvbox-dark (paritas
# CLI, palet hangat); terang: gruvbox-light (pasangan resmi — nama "github"
# TIDAK ada di pygments, hanya "github-dark"). Dipilih SAAT RENDER (bukan
# konstanta modul) supaya /theme vslight ikut berganti tanpa muat ulang.
try:  # pragma: no cover — bergantung versi pygments
    from pygments.styles import get_style_by_name as _gsbn
    _gsbn("gruvbox-dark")
    _gsbn("gruvbox-light")
    _MD_GELAP = "gruvbox-dark"
    _MD_TERANG = "gruvbox-light"
except Exception:  # pragma: no cover
    _MD_GELAP = "monokai"
    _MD_TERANG = "default"


def _tema_kode_md() -> str:
    """Tema pygments blok kode mengikuti sisi terang/gelap tema aktif."""
    from ...ui.textual_theme import latar_terang
    return _MD_TERANG if latar_terang() else _MD_GELAP


def _gaya_diff() -> tuple[str, str, str, str, str]:
    """(add, del, ctx, gut_add, gut_del) mengikuti sisi terang/gelap tema.

    Latar gelap (#0d2312 dsb.) di atas kanvas putih memberi kontras ~1.3:1
    dan teks hijau/merah pucat nyaris hilang — tema terang memakai pasangan
    GitHub-light (teks pekat di atas blush muda)."""
    from ...ui.textual_theme import latar_terang
    if latar_terang():
        return ("#1a7f37 on #e6ffec", "#cf222e on #ffebe9", "#6e7781",
                "#1a7f37 on #ccf2d4", "#cf222e on #ffd6d6")
    return ("#a3ccad on #0d2312", "#cca3a3 on #230d0d", "grey50",
            "#4f8f5c on #08170b", "#96565a on #170808")


# Batas baris kode saat blok write() DIBUKA — di atas itu tetap dipangkas
# (dengan keterangan) supaya render ribuan baris tak membekukan UI.
_MAKS_BLOK_BUKA = 400


class _BlokTulis:
    """Renderable "write(nama_file)" yang bisa diciutkan.

    Ringkas dulu (maks. ``muat`` baris isi, bawaan 7); klik di MANAPUN
    area blok (judul maupun isi) untuk membuka/menutup penuh — lihat
    on_mouse_up di MessageList. RichLog tak bisa menampung widget
    interaktif, jadi toggle-nya lewat klik baris + gambar ulang. Nama
    sengaja BUKAN berawalan underscore kembar ``_render``: ini protokol
    __rich__ Rich, bukan internal Textual.
    """

    def __init__(self, path: str, kode: str, is_new: bool = False,
                 muat: int = 4, sintaks=None):
        self.path = path
        self.kode = (kode or "").rstrip("\n")
        self.is_new = is_new
        self.muat = muat
        self.terbuka = False
        self.baris_judul: int | None = None  # diisi _catat_blok
        self._sintaks = sintaks  # list[Text] per baris (opsional)

    def __rich__(self, console=None, options=None) -> Text:
        baris = self.kode.split("\n") if self.kode else []
        t = Text()
        t.append(f"\n  {'▾' if self.terbuka else '▸'} ",
                 style=f"bold {tema.p('aksen')}")
        t.append("▤ " + self.path, style=tema.p('teks'))
        if self.is_new:
            t.append(" · baru", style=tema.p("redup"))
        t.append("\n")

        tampil = baris if self.terbuka else baris[:self.muat]
        dipangkas = False
        if self.terbuka and len(tampil) > _MAKS_BLOK_BUKA:
            tampil = tampil[:_MAKS_BLOK_BUKA]
            dipangkas = True
        for i, b in enumerate(tampil):
            t.append("  │ ", style=tema.p("tepi_redup"))
            if self._sintaks and i < len(self._sintaks):
                # Use the normal UI foreground as the base, then let lexer
                # token spans override it. Otherwise non-token text becomes
                # the terminal default (usually white).
                body = self._sintaks[i].copy()
                body.style = tema.p("teks")
                t.append_text(body)
            else:
                t.append(b, style=tema.p("teks"))
            t.append("\n")

        if not self.terbuka and len(baris) > self.muat:
            t.append(f"  … +{len(baris) - self.muat} baris\n",
                     style=tema.p("redup"))
        elif self.terbuka and dipangkas:
            t.append(f"  … (dipangkas di {_MAKS_BLOK_BUKA} baris)\n",
                     style=tema.p("redup"))
        return t


class _BlokDetail:
    """Satu baris ringkasan; klik untuk membaca detail yang tersimpan."""

    def __init__(self, title: Text, body: Text, *, preview: int = 0,
                 max_expanded_lines: int | None = None):
        self.title = title
        self.body = body
        self.preview = preview
        self.max_expanded_lines = max_expanded_lines
        self.line_offset = 0
        self._line_count = 0
        self.terbuka = False
        self.baris_judul: int | None = None

    @property
    def plain(self) -> str:
        return self.title.plain + "\n" + self.body.plain

    def __rich_console__(self, console, options):
        header = Text("  " + ("▾" if self.terbuka else "▸") + " ", style=tema.p("redup"))
        header.append_text(self.title)
        if not self.terbuka and not self.preview:
            header.truncate(options.max_width, overflow="ellipsis")
            yield header
            return
        body = (Text(" ".join(self.body.plain.split()), style=self.body.style)
                if self.max_expanded_lines is not None else self.body)
        lines = body.wrap(console, max(1, options.max_width - 4), overflow="fold", no_wrap=False)
        self._line_count = len(lines)
        if self.terbuka and self.max_expanded_lines is not None:
            self.line_offset = min(self.line_offset, max(0, len(lines) - self.max_expanded_lines))
            shown = lines[self.line_offset:self.line_offset + self.max_expanded_lines]
            if len(lines) > self.max_expanded_lines:
                header.append(" ↕", style=tema.p("redup"))
        else:
            shown = lines if self.terbuka else lines[:self.preview]
        header.truncate(options.max_width, overflow="ellipsis")
        yield header
        for line in shown:
            text = Text("    ")
            text.append_text(line)
            yield text

    def scroll_lines(self, delta: int) -> bool:
        if not self.terbuka or self.max_expanded_lines is None:
            return False
        offset = max(0, min(self.line_offset + delta,
                            self._line_count - self.max_expanded_lines))
        if offset == self.line_offset:
            return False
        self.line_offset = offset
        return True


class _PesanPengguna:
    """Gema prompt pengguna sebagai kartu membulat ala aplikasi chat.

    Renderable, bukan widget: RichLog hanya menerima renderable. ``expand``
    sengaja False supaya pesan pendek menjadi gelembung ringkas, sementara
    pesan panjang membungkus di lebar area percakapan. ``plain`` disediakan
    untuk harness/replay yang membaca isi ``MessageList._items``.
    """

    def __init__(self, teks: str, prefix: str = "❯ ", spasi: bool = True):
        self.teks = teks
        self.prefix = prefix
        self.spasi = spasi

    @property
    def plain(self) -> str:
        return self.teks

    def __rich_console__(self, console, options):
        if self.spasi:
            yield Text("\n")
        isi = Text()
        for i, baris in enumerate(self.teks.split("\n")):
            if i:
                isi.append("\n")
            if i == 0:
                isi.append(self.prefix, style=f"bold {tema.p('aksen')}")
            else:
                isi.append("  ", style=tema.p("redup"))
            isi.append(baris, style=f"bold {tema.p('teks')}")
        yield Panel(
            isi,
            box=box.ROUNDED,
            border_style=tema.p("tepi_redup"),
            style=f"on {tema.p('menu_bg')}",
            padding=(0, 1),
            expand=False,
        )


class MessageList(RichLog, can_focus=False):
    """Riwayat percakapan yang bisa digulir; mengisi ruang sisa layar."""

    def __init__(self, agent: "Agent | None" = None, **kwargs):
        super().__init__(
            markup=False,      # isi sudah berupa Text/Markdown ber-style;
                               # markup=True bikin "[foto]" ditelan sebagai tag
            highlight=False,    # jangan pakai ReprHighlighter
            wrap=True,
            auto_scroll=True,
            min_width=1,        # jangan paksa 78 kolom
            max_lines=_MAKS_BARIS_LOG,
            **kwargs,
        )
        self._agent = agent
        self._items: deque[RenderableType] = deque(maxlen=_MAKS_RIWAYAT)
        self._lebar_terakhir = 0
        self._tinggi_terminal_terakhir = 0
        self._timer_resize = None
        self._menggambar_ulang = False
        # Keadaan aliran token (lihat "Aliran jawaban" di bawah).
        # ``_stream_buf`` ditulis dari thread pekerja, sisanya hanya dari
        # thread UI.
        self._stream_buf = ""
        self._stream_lock = threading.Lock()
        self._aliran_awal: int | None = None   # baris pertama item hidup
        self._aliran_teks = ""                 # isi yang SEDANG tampil
        self._aliran_item: RenderableType | None = None  # renderable-nya
        self._aliran_waktu = 0.0               # waktu gambar-ulang terakhir
        # Kursor "▍" yang berkedip di ujung jawaban hidup. Glyph-nya HANYA
        # ditambahkan saat menggambar, tidak pernah masuk _aliran_teks /
        # _items, jadi jawaban final & riwayat tetap bersih.
        self._kursor_timer = None
        self._kursor_tampil = False
        self._kursor_tergambar = False
        # Seleksi salin-otomatis (lihat "Salin otomatis" di bawah).
        # Seleksi adalah rentang karakter: jangkar & ujung berupa
        # (baris_konten, indeks_karakter). _salin_mode menentukan
        # granularitas: "huruf" (seret), "kata" (klik ganda), "baris"
        # (klik 3x). _salin_lo/_salin_hi: rentang tersorot — TETAP hidup
        # setelah tombol dilepas supaya sorotannya terlihat, lenyap saat
        # klik berikutnya.
        self._salin_mode: str = "huruf"
        self._salin_jangkar: tuple[int, int] | None = None
        self._salin_lo: tuple[int, int] | None = None
        self._salin_hi: tuple[int, int] | None = None
        # Deteksi klik ganda/tiga kali: waktu & posisi klik kiri terakhir.
        self._klik_terakhir: tuple[float, int, int] | None = None
        # Baris area blok write() (judul + isi) -> toggle buka/tutup.
        # Dibangun ulang tiap gambar ulang (lihat _catat_blok).
        self._blok_klik: dict[int, _BlokTulis | _BlokDetail | DiffView] = {}
        # Klik POLOS di ISI blok ditunda selama jendela klik-ganda. Bila
        # klik kedua datang, timer dibatalkan dan klik itu menjadi seleksi
        # kata/baris — jadi double/triple-click tak sempat menutup blok.
        self._timer_blok = None
        self._blok_tunda: _BlokTulis | _BlokDetail | DiffView | None = None
        # Header "🤖 bagas-ai" ditulis sekali di awal giliran aliran.
        self._tulis_header_ai = True

    # --- Inti penulisan ------------------------------------------------

    def _lebar(self) -> int | None:
        """Lebar render = lebar area isi. None bila ukuran belum diketahui."""
        try:
            w = self.scrollable_content_region.width
        except Exception:  # noqa: BLE001 — belum ter-mount
            return None
        return max(20, w) if w else None

    def _emit(self, renderable: RenderableType, simpan: bool = True) -> None:
        """Satu-satunya jalan tulis ke log.

        ``simpan=False`` untuk isi sementara yang tak perlu digambar ulang.

        Tulisan lain MENUTUP aliran jawaban: teks yang menyusul setelah
        langkah tool/pemberitahuan adalah paragraf BARU, bukan lanjutan item
        yang sedang tumbuh. Tanpa penutupan ini, flush berikutnya akan
        menggambar ulang di baris item hidup — menimpa langkah tool yang
        baru saja dicetak di bawahnya.

        ``_tutup_aliran`` juga MENGOSONGKAN buffer token. Ini bagian yang
        dulu terlewat: tanpa itu, teks yang sudah tampil dan tersegel ikut
        terbawa buffer, lalu digambar lagi sebagai segmen baru — jawaban
        tampil dua kali begitu ada langkah tool di tengahnya.
        """
        self._tutup_aliran()
        if simpan:
            self._items.append(renderable)
        try:
            awal = len(self.lines)
            self.write(renderable, width=self._lebar())
            self._catat_blok(renderable, awal)
        except Exception:  # noqa: BLE001 — jangan sampai UI mati karena render
            pass

    def _catat_blok(self, item: RenderableType, awal_baris: int) -> None:
        """Catat SEMUA baris blok write() yang barusan ditulis.

        Judul + seluruh isi ikut dipetakan supaya klik di manapun di area
        blok membuka/menutupnya (tanpa perlu tepat di judul). Tulisan yang
        ditunda RichLog (ukuran belum diketahui) tak menambah
        ``self.lines`` — pemetaannya nanti dibangun ulang oleh _gambar_ulang.
        """
        if isinstance(item, (_BlokTulis, _BlokDetail, DiffView)) and awal_baris < len(self.lines):
            item.baris_judul = awal_baris
            for baris in range(awal_baris, len(self.lines)):
                self._blok_klik[baris] = item

    # --- Gambar ulang saat lebar berubah -------------------------------

    def on_resize(self, event: Resize) -> None:
        """Jadwalkan gambar ulang saat lebar berubah.

        JANGAN panggil ``super().on_resize()``: Textual mengirim event ke
        setiap handler di MRO, jadi ``RichLog.on_resize`` (yang melepas
        tulisan tertunda) tetap ikut jalan. Memanggilnya lagi akan
        menggandakan isi.
        """
        lebar = event.size.width
        if lebar and lebar != self._lebar_terakhir:
            if self._lebar_terakhir:
                self._jadwalkan_gambar_ulang()
            self._lebar_terakhir = lebar
        tinggi = self.app.size.height
        if tinggi != self._tinggi_terminal_terakhir:
            self._tinggi_terminal_terakhir = tinggi
            if any(isinstance(item, _BlokDetail) and item.max_expanded_lines is not None
                   for item in self._items):
                self._jadwalkan_gambar_ulang()

    def _jadwalkan_gambar_ulang(self) -> None:
        """Tunda gambar ulang sampai resize berhenti (hemat saat drag)."""
        if self._timer_resize is not None:
            try:
                self._timer_resize.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._timer_resize = self.set_timer(_JEDA_RESIZE,
                                                self._gambar_ulang)
        except Exception:  # noqa: BLE001 — belum ter-mount
            self._timer_resize = None

    def _gambar_ulang(self) -> None:
        """Bersihkan lalu tulis ulang riwayat pada lebar baru."""
        self._timer_resize = None
        self._batalkan_toggle_blok()
        # Nomor baris berubah setelah dibungkus ulang di lebar baru — sorotan
        # lama bisa menunjuk baris yang keliru, lebih baik lenyap.
        self._salin_jangkar = None
        self._salin_lo = self._salin_hi = None
        if self._menggambar_ulang or not (self._items or self._aliran_item):
            return
        self._menggambar_ulang = True
        try:
            items = list(self._items)
            item_aliran = self._aliran_item
            teks_aliran = self._aliran_teks
            self.clear()
            self._blok_klik.clear()
            self._aliran_awal = None
            self._aliran_teks = ""
            self._aliran_item = None
            lebar = self._lebar()
            for it in items:
                try:
                    if isinstance(it, _BlokDetail) and it.max_expanded_lines is not None:
                        it.max_expanded_lines = max(1, min(3, self.app.size.height // 8))
                    awal = len(self.lines)
                    self.write(it, width=lebar)
                    self._catat_blok(it, awal)
                except Exception:  # noqa: BLE001
                    pass
            self._items.clear()
            self._items.extend(items)
            # Item hidup tidak ada di ``_items``, jadi ia harus ditulis
            # sendiri di sini — kalau tidak, jawaban yang sedang mengalir
            # LENYAP begitu jendela diubah lebarnya. Renderable-nya dipakai
            # ulang: Rich merendernya pada lebar konsol baru, jadi isinya ikut
            # menyesuaikan tanpa perlu mengurai markdown sekali lagi.
            if item_aliran is not None:
                posisi = len(self.lines)
                try:
                    self.write(item_aliran, width=lebar)
                except Exception:  # noqa: BLE001
                    pass
                if len(self.lines) > posisi:
                    self._aliran_awal = posisi
                    self._aliran_teks = teks_aliran
                    self._aliran_item = item_aliran
            self.scroll_end(animate=False)
        finally:
            self._menggambar_ulang = False

    # --- Salin otomatis saat teks diblok ---------------------------------
    #
    # RichLog (Textual 8.2.8) tidak punya seleksi teks bawaan, dan terminal
    # sendiri tak bisa menyalin teks milik aplikasi TUI penuh-layar (mouse
    # kita yang menangkapnya). Karena itu seleksi dibuat sendiri, bergaya
    # seleksi terminal biasa: seret = per karakter, klik ganda = per kata,
    # klik tiga kali = per baris. Lepaskan tombol dan isinya LANGSUNG
    # tersalin ke clipboard.
    #
    # Kolom mouse adalah KOLOM SEL (emoji/CJK lebar 2), sementara teks
    # tersimpan sebagai karakter — pemetaannya memakai rich.cells.cell_len
    # per karakter (lihat _posisi_sel). Batas seleksi pada baris pertama /
    # terakhir dipotong per karakter; baris di antaranya otomatis penuh.

    _JEDA_KLIK_GANDA = 0.5  # dtk — batas klik dianggap ganda/tiga kali

    def _baris_konten(self, y: float) -> int:
        """Ubah y mouse (relatif widget) menjadi nomor baris konten."""
        try:
            geser = int(self.scroll_offset.y)
        except Exception:  # noqa: BLE001 — belum ter-mount
            geser = 0
        n = max(0, int(y) - self.gutter.top + geser)
        return min(n, max(0, len(self.lines) - 1))

    def _strip(self, baris: int) -> Strip | None:
        """Strip baris konten ke-n, atau None bila di luar jangkauan."""
        try:
            s = self.lines[baris]
        except Exception:  # noqa: BLE001 — indeks basi (log terpangkas)
            return None
        return s if isinstance(s, Strip) else None

    @staticmethod
    def _posisi_sel(teks: str, kolom: int) -> int:
        """Kolom sel -> indeks karakter: lompati karakter lebar-2 penuh.

        ``kolom`` di-CLAMP ke panjang teks. Kolom yang jatuh DI TENGAH
        karakter lebar-2 (CJK/emoji) dibulatkan ke ujung terdekatnya —
        tebakan terbaik yang bisa dilakukan tanpa data layout terminal."""
        from rich.cells import cell_len

        posisi = 0  # kolom sel yang sudah dilalui
        for i, ch in enumerate(teks):
            lebar = cell_len(ch)
            if posisi + lebar > kolom:
                # Kolom jatuh di tengah karakter lebar-2: bulatkan ke
                # ujung terdekat.
                return i if (kolom - posisi) <= (lebar // 2) else i + 1
            posisi += lebar
        return len(teks)

    @staticmethod
    def _kolom_sel(teks: str, indeks: int) -> int:
        """Indeks karakter -> kolom sel (kebalikan _posisi_sel)."""
        from rich.cells import cell_len

        return cell_len(teks[:indeks])

    def _titik_mouse(self, event) -> tuple[int, int]:
        """(baris, indeks_karakter) dari event mouse."""
        baris = self._baris_konten(event.y)
        s = self._strip(baris)
        if s is None:
            return baris, 0
        # Kolom widget + scroll horizontal — RichLog bisa digeser ke samping.
        try:
            kolom = max(0, int(event.x) - self.gutter.left) + int(self.scroll_offset.x)
        except Exception:  # noqa: BLE001
            kolom = int(event.x)
        return baris, self._posisi_sel(s.text, kolom)

    def _batas_kata(self, teks: str, pos: int) -> tuple[int, int]:
        """Rentang kata yang memuat ``pos``: [awal, akhir) gaya terminal.

        Kata = rangkaian karakter sejenis (alfanumerik vs tanda baca vs
        spasi) — pola yang sama dengan klik-ganda di terminal desktop."""
        n = len(teks)
        if n == 0:
            return 0, 0
        pos = min(max(pos, 0), n - 1)

        def _kelas(c: str) -> int:
            if c.isspace():
                return -1
            return 1 if (c.isalnum() or c == "_") else 0

        k = _kelas(teks[pos])
        awal = pos
        while awal > 0 and _kelas(teks[awal - 1]) == k:
            awal -= 1
        akhir = pos + 1
        while akhir < n and _kelas(teks[akhir]) == k:
            akhir += 1
        return awal, akhir

    def _batalkan_toggle_blok(self) -> None:
        """Batalkan toggle blok yang menunggu jendela klik-ganda."""
        if self._timer_blok is not None:
            try:
                self._timer_blok.stop()
            except Exception:  # noqa: BLE001 — timer sudah mati
                pass
            self._timer_blok = None
        self._blok_tunda = None

    def _toggle_blok(self, blok: _BlokTulis | _BlokDetail | DiffView) -> None:
        blok.terbuka = not blok.terbuka
        self._redraw_block(blok)

    def _redraw_block(self, blok) -> None:
        offset = (blok.baris_judul or 0) - self.scroll_y
        self._gambar_ulang()
        self.call_after_refresh(lambda: self.scroll_to(
            y=max(0, (blok.baris_judul or 0) - offset), animate=False, force=True))

    def _scroll_reasoning(self, event, delta: int) -> None:
        blok = self._blok_klik.get(self._baris_konten(event.y))
        if isinstance(blok, _BlokDetail) and blok.scroll_lines(delta):
            self._batalkan_toggle_blok()
            event.stop()
            event.prevent_default()
            self._redraw_block(blok)

    def on_mouse_scroll_up(self, event) -> None:
        self._scroll_reasoning(event, -1)

    def on_mouse_scroll_down(self, event) -> None:
        self._scroll_reasoning(event, 1)

    def _toggle_blok_tunda(self) -> None:
        """Timer habis: klik polos di isi blok baru boleh menutup/membuka."""
        self._timer_blok = None
        blok, self._blok_tunda = self._blok_tunda, None
        if blok is None or not any(b is blok for b in self._blok_klik.values()):
            return
        self._toggle_blok(blok)

    def on_mouse_down(self, event) -> None:
        if getattr(event, "button", -1) != 1:
            return
        import time as _waktu

        # Klik baru membatalkan toggle blok yang masih menunggu. Kalau klik
        # ini ternyata klik kedua/ketiga, blok TIDAK boleh sempat tertutup.
        self._batalkan_toggle_blok()
        titik = self._titik_mouse(event)
        sekarang = _waktu.monotonic()
        ganda = False
        # Klik ganda/tiga di area blok write() tetap menyeleksi kata/baris.
        # Klik POLOS (bukan ganda) baru membuka/menutup blok di mouse_up,
        # jadi isi tool pun bisa diblok dan disalin seperti teks lain.
        if self._klik_terakhir is not None:
            t_lama, x_lama, y_lama = self._klik_terakhir
            # Posisi dibandingkan longgar (±1 sel): klik ganda manusia tak
            # pernah jatuh di piksel yang persis sama.
            if (sekarang - t_lama <= self._JEDA_KLIK_GANDA
                    and abs(event.x - x_lama) <= 1
                    and abs(event.y - y_lama) <= 1):
                ganda = True
        self._klik_terakhir = (sekarang, event.x, event.y)
        blok = self._blok_klik.get(titik[0])
        if (blok is not None and titik[0] == blok.baris_judul
                and 0 <= event.x - self.gutter.left <= 4):
            # Chevron berfungsi sebagai kontrol, termasuk dua klik cepat.
            ganda = False
            self._klik_terakhir = None

        if ganda:
            baris, pos = titik
            s = self._strip(baris)
            teks = s.text if s is not None else ""
            # Ganda kedua berturut-turut pada baris sama = tiga kali ->
            # seluruh baris (mode "baris").
            if (self._salin_mode == "kata"
                    and self._salin_lo is not None
                    and self._salin_lo[0] == baris
                    and self._salin_hi is not None
                    and self._salin_hi[0] == baris):
                self._salin_mode = "baris"
                self._salin_jangkar = (baris, 0)
                self._salin_lo = (baris, 0)
                self._salin_hi = (baris, len(teks))
            else:
                awal, akhir = self._batas_kata(teks, pos)
                self._salin_mode = "kata"
                self._salin_jangkar = (baris, awal)
                self._salin_lo = (baris, awal)
                self._salin_hi = (baris, akhir)
            self.refresh()
            return

        # Klik baru selalu memulai seleksi segar — sorotan lama (bila ada)
        # ikut lenyap.
        self._salin_mode = "huruf"
        self._salin_jangkar = titik
        self._salin_lo = self._salin_hi = titik
        self.capture_mouse()
        self.refresh()

    def on_mouse_move(self, event) -> None:
        blok = self._blok_klik.get(self._baris_konten(event.y))
        self.tooltip = ("Gulir untuk membaca penalaran" if isinstance(blok, _BlokDetail)
                        and blok.terbuka and blok.max_expanded_lines is not None else None)
        if self._salin_jangkar is None:
            return
        titik = self._titik_mouse(event)
        mode = self._salin_mode
        if mode == "huruf" and titik != self._salin_jangkar:
            self._klik_terakhir = None
        if mode == "huruf":
            ujung = titik
        elif mode == "kata":
            baris, pos = titik
            s = self._strip(baris)
            teks = s.text if s is not None else ""
            awal, akhir = self._batas_kata(teks, pos)
            jangkar_baris, jangkar_pos = self._salin_jangkar
            if baris == jangkar_baris:
                ujung = (baris, akhir if pos >= jangkar_pos else awal)
            else:
                ujung = (baris, akhir if baris > jangkar_baris else awal)
        else:  # "baris"
            baris = titik[0]
            s = self._strip(baris)
            n = len(s.text) if s is not None else 0
            ujung = (baris, n if baris >= self._salin_jangkar[0] else 0)
        lo, hi = self._urut(self._salin_jangkar, ujung)
        if (lo, hi) != (self._salin_lo, self._salin_hi):
            self._salin_lo, self._salin_hi = lo, hi
            self.refresh()

    def on_mouse_up(self, event) -> None:
        if getattr(event, "button", -1) != 1 or self._salin_jangkar is None:
            return
        jangkar, self._salin_jangkar = self._salin_jangkar, None
        self.release_mouse()
        if self._salin_mode in ("kata", "baris"):
            # Klik ganda/tiga kali: seleksi sudah terbentuk di mouse_down;
            # langsung salin.
            teks = self._teks_salin(self._salin_lo, self._salin_hi)
            if teks:
                self._salin_ke_clipboard(
                    teks, self._salin_hi[0] - self._salin_lo[0] + 1)
            return
        titik = self._titik_mouse(event)
        lo, hi = self._urut(jangkar, titik)
        if lo == hi:
            # Klik polos: kalau jatuh di AREA blok write() (judul atau
            # isi), buka/tutup isinya (RichLog tak bisa menampung widget
            # interaktif, jadi toggle-nya gambar ulang seluruh riwayat).
            # Klik di JUDUL langsung; klik di ISI ditunda selama jendela
            # klik-ganda supaya klik kedua/ketiga bisa menyeleksi kata/baris
            # tanpa blok sempat tertutup.
            blok = self._blok_klik.get(lo[0])
            if blok is not None:
                self._salin_lo = self._salin_hi = None
                self.refresh()
                if lo[0] == blok.baris_judul:
                    self._toggle_blok(blok)
                elif isinstance(blok, _BlokDetail) and blok.max_expanded_lines is not None:
                    return
                else:
                    self._blok_tunda = blok
                    self._timer_blok = self.set_timer(
                        self._JEDA_KLIK_GANDA + 0.02,
                        self._toggle_blok_tunda)
                return
            # Selain itu: tak ada yang disalin; sorotan (yang memang cuma
            # satu titik tadi) dibersihkan.
            self._salin_lo = self._salin_hi = None
            self.refresh()
            return
        self._salin_lo, self._salin_hi = lo, hi
        teks = self._teks_salin(lo, hi)
        if teks:
            self._salin_ke_clipboard(teks, hi[0] - lo[0] + 1)
            # Sorotan DIBIARKAN tampil: satu-satunya umpan balik visual apa
            # yang barusan tersalin.

    @staticmethod
    def _urut(a: tuple[int, int], b: tuple[int, int]
              ) -> tuple[tuple[int, int], tuple[int, int]]:
        """(lo, hi) dari dua titik (baris, indeks)."""
        return (a, b) if a <= b else (b, a)

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        lo, hi = self._salin_lo, self._salin_hi
        if lo is None or hi is None or lo == hi:
            return strip
        try:
            baris = int(y + self.scroll_offset.y)
        except Exception:  # noqa: BLE001 — sorotan tak boleh mematikan render
            return strip
        if not (lo[0] <= baris <= hi[0]):
            return strip
        teks = strip.text
        # ``strip`` sudah dipadding selebar widget; batas baris memakai
        # isi asli (``self.lines``) supaya seleksi tak menyeret spasi
        # perapihan di ujung kanan.
        isi = self._strip(baris)
        panjang = len(isi.text) if isi is not None else len(teks.rstrip())
        if baris == lo[0] and baris == hi[0]:
            awal, akhir = lo[1], min(hi[1], panjang)
        elif baris == lo[0]:
            awal, akhir = lo[1], panjang
        elif baris == hi[0]:
            awal, akhir = 0, min(hi[1], panjang)
        else:
            awal, akhir = 0, panjang
        try:
            return self._sorot_strip(strip, teks, awal, akhir)
        except Exception:  # noqa: BLE001 — sorotan tak boleh mematikan render
            return strip

    def _sorot_strip(self, strip: Strip, teks: str,
                     awal: int, akhir: int) -> Strip:
        """Sorot HANYA rentang teks[awal:akhir] dengan warna tema.

        Jangan memakai ``reverse=True``: inverse menukar warna teks/latar
        sehingga blok seleksi keluar dari palet tema (dan sel kosong di
        kiri ikut terwarnai). Bagian tengah strip diberi latar
        ``menu_aktif_bg`` + teks ``menu_aktif_teks``; bagian kiri/kanan
        dibiarkan utuh supaya seleksi satu baris tak melebar ke seluruh
        lebar terminal.
        """
        if awal >= akhir or awal >= len(teks):
            return strip
        akhir = min(akhir, len(teks))
        awal_sel = self._kolom_sel(teks, awal)
        akhir_sel = self._kolom_sel(teks, akhir)
        if akhir_sel <= awal_sel:
            return strip
        gaya = RichStyle(
            bgcolor=tema.p("menu_aktif_bg"),
            color=tema.p("menu_aktif_teks"),
        )
        kiri = strip.crop(0, awal_sel)
        tengah = strip.crop(awal_sel, akhir_sel)
        kanan = strip.crop(akhir_sel)
        # post_style: warna seleksi HARUS menang atas warna teks asli
        # (apply_style biasa menaruh style lama di atas, jadi warnanya
        # tetap warna teks dan latar seleksi tak pernah terlihat).
        tengah = Strip(
            list(Segment.apply_style(tengah._segments, post_style=gaya)),
            tengah.cell_length,
        )
        return Strip.join([kiri, tengah, kanan])

    def _teks_salin(self, lo: tuple[int, int], hi: tuple[int, int]) -> str:
        """Teks polos rentang lo..hi; baris penuh dirapikan (rstrip).

        Baris pertama/terakhir dipotong per karakter sesuai batas seleksi;
        baris kosong pengapit dibuang supaya tempelan tak berongga."""
        if lo[0] == hi[0]:
            s = self._strip(lo[0])
            if s is None:
                return ""
            return s.text[lo[1]:hi[1]].rstrip()
        baris: list[str] = []
        pertama = self._strip(lo[0])
        if pertama is not None:
            baris.append(pertama.text[lo[1]:].rstrip())
        for n in range(lo[0] + 1, hi[0]):
            s = self._strip(n)
            if s is not None:
                baris.append(s.text.rstrip())
        terakhir = self._strip(hi[0])
        if terakhir is not None:
            baris.append(terakhir.text[:hi[1]].rstrip())
        while baris and not baris[0].strip():
            baris.pop(0)
        while baris and not baris[-1].strip():
            baris.pop()
        return "\n".join(baris)

    def _salin_ke_clipboard(self, teks: str, jumlah: int) -> None:
        """Salin lewat OSC 52 DAN clip.exe — dua jalur, bukan salah satu.

        OSC 52 hanya dijawab terminal yang mendukungnya; clip.exe pasti ada
        di Windows tapi tak berguna di terminal jarak jauh. Keduanya dijalani
        berurutan: yang mana pun berhasil, clipboard pengguna terisi."""
        if not copy_text(self.app, teks):
            return
        try:
            self.app.notify(
                "Disalin", timeout=1.5)
        except Exception:  # noqa: BLE001 — notify opsional
            pass

    # --- Pesan ---------------------------------------------------------

    def append_user_message(self, text: str, prefix: str = "❯ ") -> None:
        """Gema pesan pengguna sebagai gelembung membulat.

        Prompt = manusia, jadi ia mendapat permukaan sendiri (``menu_bg``)
        dengan sudut membulat dan panah aksen. Blok tool tetap tanpa kartu —
        hanya mesin yang boleh terlihat seperti keluaran terminal. Prompt
        yang sedang mengantre TIDAK lewat sini; ia tampil di QueueStrip.
        """
        # Jarak kecil dari jawaban sebelumnya (bukan pesan pertama).
        self._emit(_PesanPengguna(text, prefix, spasi=bool(self._items)))

    def append_ai_message(self, text: str, paksa_header: bool = False) -> None:
        """Jawaban AI, dirender sebagai markdown + header chip.

        ``paksa_header=True`` untuk riwayat/compact (tiap pesan punya chip);
        jalur stream cukup sekali per giliran lewat flag ``_tulis_header_ai``.
        """
        self._chip_header_ai(paksa=paksa_header)
        self._emit(self._markdown(text))

    def _teks_header_ai(self) -> Text:
        """Chip ``✦ bagas-ai`` — satu sumber untuk jalur biasa & aliran."""
        t = Text()
        if self._items:
            t.append("\n")
        bg = tema.p("menu_bg")
        # Spasi kiri/kanan ikut berlatarbelakang: hasilnya pill tipis,
        # bukan teks yang mengambang seperti header CLI.
        t.append(" ", style=f"on {bg}")
        t.append("✦", style=f"bold {tema.p('aksen')} on {bg}")
        t.append(" bagas-ai ", style=f"bold {tema.p('aksen2')} on {bg}")
        t.append("\n")
        return t

    def _chip_header_ai(self, paksa: bool = False) -> None:
        """Chip ``✦ bagas-ai`` di atas jawaban — paritas dengan CLI.

        Ditulis lewat ``_emit`` supaya ikut ``_gambar_ulang`` saat lebar
        berubah. Jalur aliran memakai salinan sebaris di ``_gambar_aliran``
        (write langsung) karena ``_emit`` di sana akan menutup aliran.
        Tanpa ``paksa``, chip hanya ditulis sekali per giliran
        (flag di-reset di ``begin_stream``) — mencegah double header
        bila stream sempat menggambar header lalu jatuh ke jalur ini.
        """
        if not paksa and not self._tulis_header_ai:
            return
        self._tulis_header_ai = False
        self._emit(self._teks_header_ai(), simpan=True)

    @staticmethod
    def _markdown(teks: str) -> RenderableType:
        """Markdown jawaban; jatuh ke teks polos bila markdown cacat.

        Dipakai bersama oleh jalur aliran (digambar ulang berkali-kali) dan
        jalur biasa, supaya keduanya menghasilkan tampilan yang SAMA —
        jawaban yang mengalir tak berubah rupa saat giliran selesai.
        ``code_theme`` = paritas CLI (blok kode ```lang``` ber-gruvbox).
        """
        bersih = _pagari_pohon(_bersih_kendali(teks))
        try:
            return Markdown(bersih, code_theme=_tema_kode_md())
        except Exception:  # noqa: BLE001 — markdown cacat
            return Text(bersih)

    def append_welcome(self, model: str, is_web: bool) -> None:
        """Kartu sambutan; tetap tersimpan di riwayat render saat resize."""
        text = Text()
        text.append("✦  ", style=f"bold {tema.p('aksen')}")
        text.append("Selamat datang di Bagas-AI! Ketik pesan atau ",
                    style=tema.p("teks"))
        text.append("/help", style=f"bold {tema.p('aksen')}")
        text.append(" untuk bantuan.\n", style=tema.p("teks"))
        text.append(f"   Model: {model}  ·  {'🌐 web' if is_web else '🤖 api'}",
                    style=tema.p("redup"))
        self._emit(Panel(text, box=box.ROUNDED, padding=(0, 1), expand=False,
                         style=f"on {tema.p('menu_bg')}",
                         border_style=tema.p("tepi_redup")))

    def append_help(self, *, full: bool = False) -> None:
        from ...ui.help import HelpContent
        start = len(self.lines)
        self._emit(HelpContent(full=full))
        # Keluaran panjang dibuka dari judul, tanpa menghapus percakapan lama.
        self.call_after_refresh(lambda: self.scroll_to(y=start, animate=False, force=True))

    def append_notice(self, text: str, style: str | None = None) -> None:
        """Pemberitahuan sistem (info, peringatan, keluaran /help, dll)."""
        s = style or f"italic {tema.p('aksen_terang')}"
        clean = _bersih_kendali(str(text)).strip()
        if len(clean) > 280 or len(clean.splitlines()) > 4:
            title = Text(clean.splitlines()[0], style=s)
            title.truncate(90, overflow="ellipsis")
            self._emit(_BlokDetail(title, Text(clean, style=s)))
        else:
            self._emit(Text(_indent(clean), style=s, no_wrap=False))

    def append_prompt(self, title: str, lines: list[str] | None = None,
                      hint: str = "", echo: str | None = None) -> None:
        """Blok tanya-jawab terminal-style di riwayat (tanpa modal).

        Dipakai ask_user, menu /model, konfirmasi — mengikuti gaya
        prompt klasik: ``? judul`` + baris bernomor + petunjuk, opsional
        ``> jawaban`` setelah pengguna mengetik.
        """
        t = Text()
        t.append(f"? {title}", style=f"bold {tema.p('aksen')}")
        for baris in lines or []:
            t.append(f"\n  {baris}", style=tema.p("teks"))
        if hint:
            t.append(f"\n  {hint}", style=f"dim {tema.p('redup')}")
        if echo is not None:
            # Selaras dgn opsi/hint (kolom 2); dulu ">" di kolom 0 bikin
            # blok prompt tampak meleset.
            t.append(f"\n  > {echo}", style=f"dim {tema.p('redup')}")
        self._emit(t)

    def append_tool_step(self, name: str, args: dict, result: str | None = None,
                         duration: float | None = None,
                         success: bool = True) -> None:
        """Ringkasan satu baris; keluaran lengkap dibuka dengan klik."""
        from ...ui.activity import activity_label
        icon = "✓" if success else "✗"
        title = Text(icon + " ", style=tema.p("redup") if success else tema.p("exit_footer"))
        title.append(activity_label(name, args), style=tema.p("teks"))
        if result:
            body = Text(_bersih_kendali(str(result)), style=tema.p("redup"))
            self._emit(_BlokDetail(title, body, preview=2))
        else:
            self._emit(title)

    def append_reasoning(self, text: str) -> _BlokDetail:
        if self._items:
            self._emit(Text(""))
        item = _BlokDetail(Text("💭 Penalaran", style=tema.p("redup")),
                           Text(_bersih_kendali(text), style=tema.p("redup")),
                           max_expanded_lines=max(1, min(3, self.app.size.height // 8)))
        self._emit(item)
        return item

    def update_reasoning(self, item: _BlokDetail, text: str) -> None:
        clean = _bersih_kendali(text)
        if item.body.plain != clean:
            item.body = Text(clean, style=tema.p("redup"))
            if item.terbuka:
                self._redraw_block(item)

    # --- Diff ----------------------------------------------------------

    def append_diff(self, path: str, old: str, new: str, is_new: bool = False,
                    limit: int = 200) -> None:
        """Diff gaya GitHub dengan pewarnaan sintaks pada baris tambahan."""
        import difflib

        diff = list(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        ))
        if not diff:
            return

        self._emit(DiffView(path, "\n".join(line.rstrip("\n") for line in diff),
                            is_new=is_new,
                            syntax=self._syntax_highlight(path, new),
                            max_lines=max(1500, limit)))

    def append_delete(self, path: str, content: str, limit: int = 80) -> None:
        """Pratinjau penghapusan tetap terlihat sebagai diff merah."""
        import difflib
        diff = "\n".join(difflib.unified_diff(content.splitlines(), [],
                          fromfile=f"a/{path}", tofile="/dev/null", lineterm=""))
        self._emit(DiffView(path, diff, deleted=True, max_lines=max(1500, limit)))

    def append_write_block(self, path: str, kode: str, is_new: bool = False,
                           muat: int = 4) -> "_BlokTulis":
        """Blok "write(nama_file)" — isi kode maks. ``muat`` baris dulu,
        klik di manapun area blok untuk membuka/menutup penuh.

        write_file menulis ulang SELURUH isi berkas, jadi diff unified-nya
        nyaris tak bermakna (semua baris "berubah"); blok ringkas + sintaks
        berwarna jauh lebih terbaca. edit_file/edit_files tetap lewat
        append_diff.
        """
        blok = _BlokTulis(path, kode, is_new=is_new, muat=muat,
                          sintaks=self._syntax_highlight(path, kode))
        self._emit(blok)
        return blok

    def append_diff_replay(self, rec: dict) -> None:
        """Kartu yang sama untuk diff OpenCode dan sesi yang dilanjutkan."""
        path = str(rec.get("path") or "?")
        self._emit(DiffView(path, str(rec.get("diff") or ""),
                            is_new=bool(rec.get("is_new")),
                            deleted=bool(rec.get("deleted"))))

    def _syntax_highlight(self, path: str, code: str):
        """Kembalikan daftar ``Text`` per baris berwarna, atau None."""
        if not code or len(code) > 400_000:
            return None
        try:
            from rich.syntax import Syntax
            from pygments.lexers import guess_lexer_for_filename

            # Lexer detection only needs the filename suffix. A full Windows
            # path can fail on some Pygments versions.
            nama = path.replace("\\", "/").rsplit("/", 1)[-1]
            lexer = guess_lexer_for_filename(nama, code)
            if not lexer or lexer.name in ("Text only", "text"):
                return None
            syn = Syntax(code, lexer.name, theme=_tema_kode_md(),
                         line_numbers=False, word_wrap=False)
            hasil = []
            for line_text in syn.highlight(code).split("\n"):
                clean = Text(line_text.plain, no_wrap=True)
                for span in line_text.spans:
                    st = span.style
                    # Rich exposes span styles as Style or (on older
                    # versions) serialised strings.
                    if not isinstance(st, RichStyle):
                        try:
                            st = RichStyle.parse(st)
                        except Exception:  # noqa: BLE001
                            continue
                    if not st.color:
                        continue
                    # Do not copy the syntax theme background into the panel.
                    clean.stylize(
                        RichStyle(color=st.color, bold=st.bold,
                                  italic=st.italic, underline=st.underline,
                                  dim=st.dim),
                        span.start, span.end,
                    )
                hasil.append(clean)
            return hasil
        except Exception:  # noqa: BLE001 — pygments opsional
            return None

    # --- Gambar & rencana ---------------------------------------------

    def append_image_preview(self, pixel_data: list,
                             title: str = "Pratinjau") -> None:
        """Pratinjau gambar sebagai blok warna."""
        if not pixel_data:
            return
        garis = tema.p("tepi")
        max_w = max(len(r) for r in pixel_data)

        atas = Text(f"  ╭─ {title} ", style=garis)
        atas.append("─" * max(0, max_w * 2 - len(title) - 4), style=garis)
        atas.append("╮", style=garis)
        self._emit(atas)

        for row in pixel_data:
            t = Text("  │", style=garis)
            for r, g, b in row:
                t.append("  ", style=f"on rgb({r},{g},{b})")
            if max_w - len(row) > 0:
                t.append("  " * (max_w - len(row)))
            t.append("│", style=garis)
            self._emit(t)

        bawah = Text("  ╰", style=garis)
        bawah.append("─" * (max_w * 2 + 1), style=garis)
        bawah.append("╯", style=garis)
        self._emit(bawah)

    def append_plan(self, steps: list[dict], title: str = "Rencana") -> None:
        """Daftar langkah rencana."""
        if not steps:
            return
        garis = tema.p("tepi")
        self._emit(Text(f"  ╭─── {title} ───╮", style=garis))
        for step in steps:
            status = step.get("status", "pending")
            if status == "done":
                ikon, style = "✓", f"bold {tema.p('aksen')}"
            elif status == "active":
                ikon, style = "▸", f"bold {tema.p('aksen_terang')}"
            else:
                ikon, style = "·", tema.p("redup")
            self._emit(Text(f"  │ {ikon} {step.get('text', '')}", style=style))
        self._emit(Text(f"  ╰{'─' * 20}╯", style=garis))

    # --- Bersihkan -----------------------------------------------------

    def clear_messages(self) -> None:
        """Kosongkan seluruh riwayat."""
        # simpan=False: riwayatnya memang sedang dibuang.
        self._tutup_aliran(simpan=False)
        self._items.clear()
        try:
            self.clear()
        except Exception:  # noqa: BLE001
            pass

    # --- Aliran jawaban -------------------------------------------------
    #
    # Jawaban yang sedang mengalir tumbuh LANGSUNG di area percakapan (dulu
    # ada panel pratinjau terpisah di bawah blok berpikir; panel itu dibuang
    # karena isinya tak berlabel dan mudah disangka blok "berpikir" kedua).
    #
    # RichLog menyimpan hasil render sebagai ``Strip`` dan tak pernah
    # membungkus ulang, jadi item yang sedang tumbuh digambar ulang DI
    # TEMPATNYA: barisnya dipotong dari ``self.lines`` lalu ditulis lagi
    # dengan isi terbaru. Item hidup itu TIDAK pernah masuk ``self._items``
    # (deque riwayat yang dipakai _gambar_ulang) — kalau masuk, ia tergambar
    # dua kali setiap kali lebar jendela berubah. Penandanya ``_aliran_awal``:
    # indeks baris pertama item hidup di dalam ``self.lines``.

    def _mulai_kursor(self) -> None:
        """Nyalakan timer kedip kursor (idempoten)."""
        if self._kursor_timer is not None:
            return
        self._kursor_tampil = True
        try:
            self._kursor_timer = self.set_interval(_JEDA_KURSOR,
                                                   self._kedip_kursor)
        except Exception:  # noqa: BLE001 — belum ter-mount
            self._kursor_timer = None

    def _hentikan_kursor(self) -> None:
        """Matikan timer kedip; dipanggil saat aliran ditutup/dibersihkan."""
        if self._kursor_timer is not None:
            try:
                self._kursor_timer.stop()
            except Exception:  # noqa: BLE001 — timer sudah mati
                pass
            self._kursor_timer = None
        self._kursor_tampil = False

    def _kedip_kursor(self) -> None:
        """Balik keadaan kursor lalu gambar ulang jawaban hidup."""
        if self._aliran_awal is None:
            self._hentikan_kursor()
            return
        self._kursor_tampil = not self._kursor_tampil
        if not self._aliran_teks:
            return
        try:
            self._gambar_aliran(self._aliran_teks,
                                kursor=self._kursor_tampil)
        except Exception:  # noqa: BLE001 — kedip tak boleh mematikan UI
            pass

    def begin_stream(self) -> None:
        """Mulai sesi aliran untuk satu jawaban AI (thread UI)."""
        # Jawaban sebelumnya (kalau ada) disegel ke riwayat dulu; buffer
        # token ikut dikosongkan oleh _tutup_aliran.
        self._tutup_aliran()
        self._aliran_waktu = 0.0
        self._tulis_header_ai = True
        self._mulai_kursor()

    def append_token(self, piece: str) -> None:
        """Kumpulkan satu token. Dipanggil dari thread pekerja.

        Sengaja hanya menumpuk teks: thread pekerja tak boleh menyentuh
        widget. Penggambarannya diurus ``flush_stream`` di thread UI.
        """
        with self._stream_lock:
            self._stream_buf += piece

    def teks_aliran(self) -> str:
        """Salinan isi buffer aliran (aman lintas thread)."""
        with self._stream_lock:
            return self._stream_buf

    def flush_stream(self, paksa: bool = False) -> None:
        """Gambar ulang jawaban yang sedang tumbuh (thread UI).

        Dipanggil untuk SETIAP token, jadi jeda ``_JEDA_ALIRAN`` diatur di
        sini: merender markdown seluruh jawaban puluhan kali per detik itu
        boros tanpa bedanya bagi mata. ``paksa=True`` melewati jeda — dipakai
        saat giliran berakhir supaya kata terakhir pasti tampil.
        """
        if not self._size_known:
            # Ukuran belum diketahui: tulisan akan ditunda RichLog dan
            # penanda baris jadi tak bisa dipercaya. Tunggu tata letak.
            return
        # Spasi/baris kosong di UJUNG tidak digambar: baris kosong terakhir
        # tak terlihat, tapi kalau ikut disimpan ia membuat teks yang tampil
        # selalu "berbeda" dari teks final di end_stream hanya karena beda
        # baris baru — dan itu memicu gambar ulang penuh yang terlihat
        # seperti kedipan di akhir giliran.
        teks = self.teks_aliran().rstrip()
        if not teks or teks == self._aliran_teks:
            return
        sekarang = time.monotonic()
        if (not paksa and self._aliran_awal is not None
                and sekarang - self._aliran_waktu < _JEDA_ALIRAN):
            return
        self._aliran_waktu = sekarang
        self._mulai_kursor()
        self._gambar_aliran(teks)

    def _gambar_aliran(self, teks: str, kursor: bool | None = None) -> None:
        """Tulis — atau gambar ulang di tempatnya — item jawaban hidup.

        ``kursor`` memaksa glyph kursor tampil/sembunyi; ``None`` mengikuti
        keadaan kedip saat ini. Glyph hanya masuk ke renderable layar —
        ``_aliran_teks`` tetap teks bersih supaya perbandingan di
        ``end_stream`` dan riwayat ``_items`` tak pernah tercemar.
        """
        if not self._size_known:
            return
        try:
            # Chip header sekali di awal giliran — sebelum item hidup, jadi
            # ia masuk riwayat dan TIDAK ikut terhapus saat stream digambar
            # ulang (panggilan _emit akan menutup aliran; write langsung aman).
            if self._tulis_header_ai:
                self._tulis_header_ai = False
                hdr = self._teks_header_ai()
                self._items.append(hdr)
                try:
                    self.write(hdr, width=self._lebar())
                except Exception:  # noqa: BLE001 — render header tak kritis
                    pass
            awal = self._aliran_awal
            if awal is not None and 0 <= awal <= len(self.lines):
                # Buang baris item lama. Cache render RichLog berkunci
                # (baris, lebar, ...) — tanpa dibersihkan, baris LAMA bisa
                # muncul kembali dari cache meski ``lines`` sudah dipotong.
                del self.lines[awal:]
                self._line_cache.clear()
            posisi = len(self.lines)
            tampil_kursor = (self._kursor_tampil if kursor is None
                             else bool(kursor))
            isi = teks + (" " + _KURSOR if tampil_kursor else "")
            item = self._markdown(isi)
            self.write(item, width=self._lebar())
            if len(self.lines) > posisi:
                self._aliran_awal = posisi
                self._aliran_teks = teks
                self._aliran_item = item
                self._kursor_tergambar = tampil_kursor
        except Exception:  # noqa: BLE001 — render tak boleh mematikan UI
            self._tutup_aliran()

    def _tutup_aliran(self, simpan: bool = True) -> None:
        """Akhiri item hidup; aliran berikutnya mulai sebagai paragraf baru.

        ``simpan`` memindahkan item yang sudah tampil ke ``_items`` (riwayat
        yang dipakai gambar ulang). Tanpa itu, jawaban yang sudah tersegel
        hanya hidup di ``self.lines`` — dan LENYAP begitu jendela diubah
        lebarnya, karena ``_gambar_ulang`` membersihkan seluruh log lalu
        menggambar ulang dari ``_items`` saja.

        Buffer token ikut dikosongkan: teks yang sudah tampil tak boleh
        dikirim ulang sebagai segmen berikutnya.
        """
        self._hentikan_kursor()
        # Kursor hanya glyph LAYAR. Sebelum item disegel ke _items (sumber
        # gambar ulang), gambar ulang tanpa kursor supaya riwayat bersih —
        # kalau tidak, "▍" ikut tersimpan dan muncul lagi saat resize.
        if self._kursor_tergambar:
            teks = self._aliran_teks
            self._kursor_tergambar = False
            if simpan and teks:
                try:
                    self._gambar_aliran(teks, kursor=False)
                except Exception:  # noqa: BLE001 — jangan kehilangan jawaban
                    pass
        if simpan and self._aliran_item is not None:
            self._items.append(self._aliran_item)
        self._aliran_awal = None
        self._aliran_teks = ""
        self._aliran_item = None
        self._kursor_tergambar = False
        with self._stream_lock:
            self._stream_buf = ""

    def end_stream(self, teks_akhir: str | None = None) -> str:
        """Akhiri aliran; kembalikan teks yang BENAR-BENAR tampil.

        ``teks_akhir`` adalah teks final dari agen — bisa BERBEDA dari
        buffer (jalur non-streaming, atau jawaban yang diganti setelah
        pembacaan ulang gambar). Bila isinya sama dengan yang sudah di
        layar, layar TIDAK digambar ulang: dulu jawaban dicetak dua kali di
        akhir giliran (sekali redup sebagai pratinjau, sekali terang sebagai
        jawaban final) dan itu terlihat seperti kedipan.
        """
        with self._stream_lock:
            buffer = self._stream_buf
            self._stream_buf = ""
        final = teks_akhir or buffer
        hidup = self._aliran_awal is not None
        # Jawaban berisi spasi/baris kosong saja = tidak ada jawaban; biar
        # pemanggil yang memutuskan apa yang ditampilkan (mis. "(no response)").
        bersih = final.rstrip()
        if not bersih:
            self._tutup_aliran()
            return ""
        if not hidup:
            # Tak ada item hidup (jalur non-streaming): tulis sebagai pesan
            # biasa agar ikut digambar ulang saat lebar berubah. Lewat
            # ``append_ai_message`` — bukan ``_emit`` langsung — supaya ini
            # benar-benar jalan masuk yang SAMA dengan jawaban lain: apa pun
            # yang membungkus/memantau metode publik itu (mis. uji) tetap
            # melihat jawaban akhir, dan tak ada dua jalur yang bisa berbeda
            # perlakuan.
            self.append_ai_message(bersih)
            return final
        if self._aliran_teks != bersih:
            # Isi final berbeda: gambar ulang di posisi yang sama supaya
            # tidak ada dua versi jawaban di layar. Dibandingkan dalam bentuk
            # yang sudah dirapikan — sama seperti yang digambar flush_stream.
            # kursor=False: ini teks final, glyph hidup tak boleh ikut.
            self._gambar_aliran(bersih, kursor=False)
        # Item hidup kini selesai. Pindahkan kepemilikannya ke riwayat —
        # tanpa menggambar ulang, jadi tampilannya tak berubah sedetik pun.
        self._tutup_aliran()
        return final

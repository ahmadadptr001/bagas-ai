"""InfoSidebar — sidebar kanan untuk terminal selebar "dashboard".

Selalu tampil begitu terminal >= BagasAIApp._LEBAR_MIN kolom. Berisi:

- PlanSidebar ("◈ Rencana") — selalu muncul di sidebar; menampilkan
  empty-state sampai model memakai plan()/plan_step(). Rencana yang tuntas
  tampil sebentar lalu kembali ke empty-state.
- SystemPanel ("◈ Sistem") — kesehatan mesin real time: CPU, RAM, disk,
  GPU bila terbaca.
- FOOTER PATH: path LENGKAP folder project (di-wrap per batas folder,
  tak dipotong "…"). Diklik -> membuka ProjectTree — tree isi project
  yang tiap foldernya bisa di-expand lagi (lazy: dibaca saat di-expand).
- HANDLE DI TEPI KIRI: seluruh sidebar bisa ditarik untuk mengatur lebar
  28..60 kolom; hasilnya tersimpan di prefs.json untuk sesi berikutnya.

Saat terminal menyempit, seluruh sidebar disembunyikan dan rencana
tampil inline sebagai PlanPanel di footer (perilaku lama — lihat
BagasAIApp._perbarui_layout_plan).
"""
from __future__ import annotations

from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Button
from rich.text import Text

from ... import config
from ...ui import tema
from .plan_sidebar import PlanSidebar
from .project_tree import ProjectTree
from .system_panel import SystemPanel

# Batas lebar sidebar (kolom) yang bisa dituju handle tarik.
LEBAR_MIN, LEBAR_DEFAULT, LEBAR_MAKS = 28, 34, 60
# Nama key prefs.json untuk lebar sidebar pilihan pengguna.
_KEY_LEBAR = "sidebar_lebar"


class AksiSidebar(Message, namespace="info_sidebar"):
    """Kontrol buka/tutup tree path yang diklik pengguna."""

    def __init__(self, aksi: str) -> None:
        super().__init__()
        self.aksi = aksi


class _Klik(Static):
    """Static yang bisa diklik — mem-forward klik sebagai AksiSidebar."""

    def __init__(self, aksi: str, teks: str = "", **kwargs):
        super().__init__(teks, **kwargs)
        self._aksi = aksi

    def on_click(self, event) -> None:
        self.post_message(AksiSidebar(self._aksi))
        event.stop()


class _ResizeHandle(Static):
    """Handle tarik di batas kiri yang me-resize seluruh InfoSidebar."""

    def __init__(self, **kwargs):
        super().__init__("│", **kwargs)
        self._menarik = False
        self._awal_x = 0.0
        self._lebar_awal = LEBAR_DEFAULT

    def on_mouse_down(self, event) -> None:
        if getattr(event, "button", -1) != 1:
            return
        sidebar = self.parent
        if not isinstance(sidebar, InfoSidebar):
            return
        self._menarik = True
        self._awal_x = float(getattr(event, "screen_x", 0.0) or 0.0)
        self._lebar_awal = sidebar._lebar
        self.capture_mouse()
        self.add_class("-dragging")
        event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._menarik:
            return
        sidebar = self.parent
        if not isinstance(sidebar, InfoSidebar):
            return
        kini_x = float(getattr(event, "screen_x", self._awal_x)
                       or self._awal_x)
        # Sidebar dock kanan: tepi ditarik ke kiri berarti makin lebar.
        perubahan = round(self._awal_x - kini_x)
        sidebar.terapkan_lebar(self._lebar_awal + perubahan, simpan=False)
        event.stop()

    def on_mouse_up(self, event) -> None:
        if getattr(event, "button", -1) != 1 or not self._menarik:
            return
        self._menarik = False
        self.release_mouse()
        self.remove_class("-dragging")
        sidebar = self.parent
        if isinstance(sidebar, InfoSidebar):
            sidebar._simpan_lebar(sidebar._lebar)
        event.stop()


class InfoSidebar(Widget):
    """Wadah dock-kanan: seksi sistem (selalu) + rencana (saat ada) +
    footer path/tree project + handle resize seluruh sidebar."""

    DEFAULT_CSS = """
    InfoSidebar {
        dock: right;
        width: 34;
        height: 100%;
        display: none;
        background: $t-gema_bg;
        border-left: tall $t-tepi_redup;
        padding: 0 1 0 0;
        overflow: hidden;
    }
    InfoSidebar #sidebar-resize {
        dock: left;
        width: 1;
        height: 100%;
        padding: 0;
        color: $t-tepi_redup;
        background: transparent;
        text-style: bold;
    }
    InfoSidebar #sidebar-resize:hover,
    InfoSidebar #sidebar-resize.-dragging {
        color: $t-aksen;
        background: $t-menu_sorot;
    }
    InfoSidebar #path-footer {
        height: auto;
        padding: 0;
    }
    """

    # Lebar ISI sidebar (kolom minus border kiri & padding) — widget anak
    # (garis SystemPanel, wrap path/tree) menyesuaikan diri lewat watcher.
    lebar_isi: reactive[int] = reactive(31, layout=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lebar = LEBAR_DEFAULT
        self._tree: ProjectTree | None = None
        self._path: Static | None = None
        # Teks render path terakhir (plain) — dipakai harness pengujian.
        self.terakhir_path: str = ""

    # --- Susun ----------------------------------------------------------

    def compose(self):
        yield _ResizeHandle(id="sidebar-resize")
        yield Button("× Tutup sidebar", id="sidebar-close", variant="default")
        yield PlanSidebar(id="plan-side")
        yield SystemPanel(id="system-panel")
        yield _Klik("toggle", id="path-footer")
        yield ProjectTree()

    def on_mount(self):
        self._tree = self.query_one("#project-tree", ProjectTree)
        self._path = self.query_one("#path-footer", Static)
        self._tree.display = False
        self.terapkan_lebar(self._baca_lebar_tersimpan())
        # Reaktif tak memicu watcher bila nilainya tak berubah dari default
        # — gambar path eksplisit supaya footer pasti terisi.
        self._gambar_path()

    # --- Lebar sidebar --------------------------------------------------

    def _baca_lebar_tersimpan(self) -> int:
        try:
            from ... import prefs
            v = prefs.load().get(_KEY_LEBAR)
            if isinstance(v, (int, float)) and LEBAR_MIN <= v <= LEBAR_MAKS:
                return int(v)
        except Exception:  # noqa: BLE001 — prefs opsional
            pass
        return LEBAR_DEFAULT

    def _simpan_lebar(self, n: int) -> None:
        try:
            from ... import prefs
            prefs.save(**{_KEY_LEBAR: int(n)})
        except Exception:  # noqa: BLE001 — prefs opsional
            pass

    def terapkan_lebar(self, n: int, *, simpan: bool = True) -> None:
        """Set lebar sidebar (kolom) — widget anak menyesuaikan lewat
        watcher ``lebar_isi``."""
        self._lebar = max(LEBAR_MIN, min(LEBAR_MAKS, int(n)))
        self.styles.width = self._lebar
        # border kiri 1 + handle 1 + padding kanan 1 = isi _lebar - 3.
        self.lebar_isi = max(10, self._lebar - 3)
        # Footer adalah sibling dock-bottom. Ikut pendekkan sampai batas kiri
        # sidebar agar box pra-jawaban/chat tak menggambar di atas divider.
        try:
            self.app._sinkron_footer_sidebar()
        except Exception:  # noqa: BLE001 — app/layout belum siap
            pass
        if simpan:
            self._simpan_lebar(self._lebar)

    def watch_lebar_isi(self, isi: int) -> None:
        """Lebar berubah -> segera gambar ulang path & garis sistem
        (SystemPanel sendiri di-poll app tiap 2 dtk; ini supaya garisnya
        instan mengikuti drag, bukan menunggu poll berikut)."""
        if self._tree is not None:
            try:
                self._tree.set_lebar_karakter(isi - 2)
            except Exception:  # noqa: BLE001
                pass
        self._gambar_path()
        try:
            self.app._poll_sistem()
        except Exception:  # noqa: BLE001 — app belum siap / dibongkar
            pass

    # --- Footer path + tree --------------------------------------------

    def _gambar_path(self) -> None:
        """Path LENGKAP project — di-wrap di batas folder (bukan dipotong
        "…"): baris lanjutan menjorok, path panjang pun terbaca utuh."""
        if self._path is None:
            return
        isi = self.lebar_isi
        t = Text()
        t.append("─" * isi + "\n", style=tema.p("tepi_redup"))
        t.append("📁 ", style=tema.p("redup"))
        # Ikon toggle: ▸ tree tertutup, ▾ tree terbuka.
        terbuka = self._tree is not None and self._tree.display
        t.append(("▾" if terbuka else "▸") + " ",
                 style=f"bold {tema.p('aksen')}")
        sisa = isi - 5  # dipakai "📁 " + "▾ "
        path = str(config.PROJECT_ROOT).replace("\\", "/")
        # Wrap di batas "/" — jangan belah nama folder di tengah.
        while len(path) > sisa:
            potong = path.rfind("/", 0, sisa + 1)
            if potong <= 0:
                potong = sisa
            t.append(path[:potong] + "\n   ", style=tema.p("redup"))
            path = path[potong:].lstrip("/")
        t.append(path, style=tema.p("teks"))
        t.append("\n  klik path: tree · tarik tepi kiri: lebar",
                 style=f"dim {tema.p('redup')}")
        self.terakhir_path = t.plain
        self._path.update(t)

    # --- Klik kontrol ---------------------------------------------------

    def on_info_sidebar_aksi_sidebar(self, event: AksiSidebar) -> None:
        if event.aksi == "toggle" and self._tree is not None:
            buka = not self._tree.display
            self._tree.display = buka
            if buka:
                self._tree.buka_root()
            self._gambar_path()
        event.stop()

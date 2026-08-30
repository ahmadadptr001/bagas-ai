"""Antarmuka Textual utama bagas-ai — menggantikan cli.py monolitik.

App Textual ini mengelola seluruh UI: chat, status bar, streaming,
menus, dan slash commands. Berkomunikasi dengan Agent via callback
yang sudah ada (on_token, on_tool, on_message, dll).

Usage::

    from .textual_app import BagasAIApp
    app = BagasAIApp(agent=agent, resume=resume, resume_id=resume_id)
    app.run()
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Vertical, Horizontal
from textual.reactive import reactive
from textual.widgets import Button

from .textual_widgets import (
    StatusBar, ChatBox, MessageList, PlanPanel, PlanSidebar, InfoSidebar,
    SystemPanel, ImagePreview, TurnProgressBar, LogoWidget,
    StreamingPreview, ThinkingBlock, SelectScreen, MultiSelectScreen,
    ConfirmScreen, TextPromptScreen, ThemeScreen, QueueStrip, BtwScreen,
    BukaBerkas, FileEditorScreen,
)
from ..ui.textual_theme import generate_css, variabel as variabel_tema
from ..ui import tema
from .. import interaction
from .. import session as session_mod
from ..session import Session
from .. import config, workspace, longmem, models, prefs

try:
    from pyfiglet import Figlet
except Exception:
    Figlet = None

if TYPE_CHECKING:
    from ..core import Agent
    from ..session import Session

# Entri isian bebas di menu ask_user — kembaran cli._OPSI_TULIS.
_OPSI_TULIS = "✎ Tulis jawaban sendiri…"


def _format_multi(terpilih: list[str]) -> str:
    """Gabungkan jawaban majemuk — dinomori seperti cli._tanya_pilihan
    supaya AI tak salah membaca jawaban majemuk sebagai satu kalimat."""
    if not terpilih:
        return "(tidak memilih apa pun)"
    if len(terpilih) == 1:
        return terpilih[0]
    return "; ".join(f"({i}) {j}" for i, j in enumerate(terpilih, 1))


class BagasAIApp(App):
    """Textual application untuk bagas-ai chat interface.

    Layout:
    ┌─────────────────────────────────┐
    │        LogoWidget (top)         │  ← welcome screen
    │       MessageList (fill)        │  ← scrollable messages
    │       PlanPanel (collapsible)   │  ← step plan (layar sempit)
    │       ImagePreview (optional)   │  ← pixel blocks
    │       TurnProgressBar (during)  │  ← animasi logo bunga
    │       ChatBox (input)           │  ← user input
    │       StatusBar (footer)        │  ← always visible
    └─────────────────────────────────┘
    Layar LEBAR (≥ _LEBAR_MIN kolom, "dashboard"): InfoSidebar di-dock di
    KANAN — kesehatan sistem real time (CPU/RAM/disk/GPU) selalu tampil,
    plus seksi planning yang selalu hadir (empty-state sebelum plan()).
    Rencana tuntas kembali ke empty-state setelah beberapa detik. Lihat
    _perbarui_layout_plan.
    """

    CSS = ""  # Will be set dynamically from theme

    TITLE = "bagas-ai"
    SUB_TITLE = "AI agent serbaguna"

    # Kelas responsif yang dipasang otomatis pada Screen sesuai ukuran
    # terminal. Aturan CSS-nya ada di textual_theme.py (Screen.-sempit dst.).
    # -lebar = ukuran "dashboard"/desktop: rencana pindah ke sidebar kanan
    # (lihat _perbarui_layout_plan). Satu breakpoint per dimensi — kelas
    # yang dipasang adalah breakpoint TERBESAR yang terpenuhi, bukan kumulatif.
    _LEBAR_MIN = 100
    HORIZONTAL_BREAKPOINTS = [(0, "-sempit"), (60, "-normal"),
                              (_LEBAR_MIN, "-lebar")]
    VERTICAL_BREAKPOINTS = [(0, "-pendek"), (24, "-tinggi")]

    BINDINGS = [
        Binding("ctrl+c", "cancel", show=False, priority=True),
        Binding("ctrl+d", "eof", show=False, priority=True),
        Binding("ctrl+l", "clear", show=False),
        Binding("ctrl+w", "delete_word", show=False),
        Binding("f4", "voice_dictation", show=False, priority=True),
        # Tab TIDAK diikat di sini: ChatBox memakainya untuk melengkapi
        # autocomplete. Binding global akan merebut tombol dari input.
    ]

        # State
    is_turn_active: reactive[bool] = reactive(False)
    typing_buf: reactive[str] = reactive("")
    typing_pos: reactive[int] = reactive(0)

    def __init__(self, agent: "Agent", resume: bool = False,
                 resume_id: str = "", **kwargs):
        # WAJIB sebelum super().__init__(): App membuat stylesheet dan
        # memanggil get_css_variables() di sana — bila _tema_pratinjau
        # belum ada, variabel $t-* gagal diinjeksi (AttributeError yang
        # tertelan except) dan seluruh CSS mogok dengan
        # "reference to undefined variable '$t-gema_bg'".
        # _tema_pratinjau: tema yang sedang DIPRATINJAU (None = tema aktif).
        self._tema_pratinjau: str | None = None
        super().__init__(**kwargs)
        self.agent = agent
        self.resume = resume
        self.resume_id = resume_id
        self._cancel_event = threading.Event()
        self._prompt_queue: list[str] = []
        self._antre_lock = threading.Lock()
        # Riwayat teks yang pernah dikirim pengguna (untuk panah-atas;
        # lihat on_chatbox_recall). _hist_idx = posisi jalan-jalan riwayat,
        # None = belum sedang menjelajah (tekan up memulai dari yang terakhir).
        self._riwayat_masukan: list[str] = []
        self._hist_idx: int | None = None
        # /voice selalu mulai MATI dan tidak disimpan ke preferensi: perangkat
        # mikrofon tak boleh menyala sendiri pada sesi berikutnya.
        self._voice_state: dict = {
            "pendengar": None,
            "jangkauan": None,
            "task_active": False,
            "wanted": False,
            "dictation_active": False,
            "dictation_phase": "",
            "dictation_stop": None,
        }
        self._pending_gambar: dict = {}
        self._image_task_active = False
        self._tui_mode = True
        # /live: ambil SATU screenshot terbaru tepat sebelum tiap pertanyaan
        # normal dikirim. Bukan perekaman video kontinu di latar belakang.
        self._live_screen = False
        self._live_starting = False
        self._live_start_token = 0
        self._first_idle = True
        self._worker_thread: threading.Thread | None = None
        self._turn_id = 0
        self._pending_tool_args: dict[str, dict] = {}
        self._progress_timer = None
        # Snapshot rencana terakhir (lihat _poll_plan) — dipakai untuk
        # menggambar ulang panel saat layout berpindah sidebar <-> footer.
        self._plan_cache: list[dict] = []
        # Rencana TUNTAS: waktu (time.monotonic) saat semua langkah
        # selesai — langkah tampil ±8 dtk lalu kembali ke empty-state
        # (lihat _poll_plan). State plan_tool TIDAK disentuh: reset()
        # hanya boleh dipanggil core.run() saat giliran baru, jadi sistem
        # tak pernah "melupakan" rencana yang belum digantikan.
        self._plan_selesai_pada: float | None = None
        # Rencana tuntas yang langkahnya SUDAH disembunyikan. Cache TIDAK
        # dikosongkan (kalau dikosongkan, poll berikutnya melihat
        # steps != cache dan rencana berkedip muncul-hilang tiap 8 dtk);
        # flag inilah yang membuat _perbarui_layout_plan tetap menahan langkah
        # selesai sampai giliran baru (plan_tool.reset via core.run).
        self._plan_disembunyikan: bool = False
        self._sidebar_mobile_open = False
        # Info GPU terakhir dari thread nvidia-smi (lihat _poll_gpu).
        self._gpu_info: dict = {"nama": "…", "metrik": ""}
        # SATU referensi bound-method untuk handler pilihan. Akses ulang
        # ``self._handler_pilihan`` menghasilkan objek bound method BARU
        # (tak pernah `is` sama), jadi pasang/lepas handler memakai
        # referensi yang sama supaya pencocokan di on_unmount akurat.
        self._handler_pilihan_ref = self._handler_pilihan

        # Apply theme CSS
        self.CSS = generate_css()

    def get_css_variables(self) -> dict[str, str]:
        """Suntikkan warna tema bagas sebagai variabel ``$t-*``.

        ``refresh_css()`` memanggil ini lalu me-parse ulang seluruh
        stylesheet — begitulah ganti tema mengubah tampilan SEKETIKA tanpa
        perlu memuat ulang aplikasi. Bila ada tema pratinjau
        (``_tema_pratinjau``), warnanya yang dipakai — itu mekanisme
        pratinjau langsung di menu /theme.
        """
        vars_ = super().get_css_variables()
        try:
            vars_.update(variabel_tema(self._tema_pratinjau))
        except Exception:  # noqa: BLE001 — tema cacat: pakai bawaan saja
            pass
        return vars_

    def pratinjau_tema(self, theme_id: str | None) -> None:
        """Tampilkan ``theme_id`` di seluruh layar TANPA menyimpannya.

        ``None`` mengembalikan tampilan ke tema yang benar-benar aktif.
        Dipanggil ThemeScreen tiap kali sorotan berpindah.
        """
        if self._tema_pratinjau == theme_id:
            return
        self._tema_pratinjau = theme_id
        try:
            self.refresh_css()
        except Exception:  # noqa: BLE001 — CSS cacat jangan matikan app
            pass

    def _safe_call(self, method, *args, **kwargs):
        """Call method on main thread safely.

        If already on the main thread, call directly.
        If on a worker thread, use call_from_thread.
        This avoids the Textual RuntimeError when callbacks
        are invoked from unexpected threads.

        Also catches errors during shutdown when widgets are unmounted.
        """
        try:
            current = threading.current_thread()
            if current is threading.main_thread():
                method(*args, **kwargs)
            else:
                self.call_from_thread(method, *args, **kwargs)
        except Exception:
            # Silently ignore errors during shutdown/unmount
            pass

    def compose(self) -> ComposeResult:
        yield LogoWidget(id="logo")
        yield MessageList(agent=self.agent, id="messages")
        # Sidebar info versi desktop: dock KANAN, aktif saat terminal
        # cukup lebar (kelas -lebar). Isinya kesehatan sistem real time
        # (selalu) + planning (selalu, dengan empty-state). Di bawah
        # itu sidebar disembunyikan dan rencana tampil inline sebagai
        # PlanPanel di footer — perilaku lama (lihat _perbarui_layout_plan).
        yield InfoSidebar(id="sidebar")
        # Strip antrean DI AREA TERMINAL — nempel di bawah jawaban
        # terakhir, bukan di area box footer di atas kotak chat.
        yield QueueStrip(id="queue-strip")
        # SATU wadah yang di-dock (lihat catatan layout di textual_theme.py).
        # Semua panel bawah mengalir vertikal di dalamnya, jadi tingginya
        # menjumlah dan #messages menyusut tepat sebanyak tinggi footer —
        # tidak ada lagi panel yang saling menimpa di baris terakhir.
        with Vertical(id="footer"):
            yield PlanPanel(id="plan")
            yield ImagePreview(id="image-preview")
            yield ThinkingBlock(id="thinking-block")
            yield StreamingPreview(id="streaming-preview")
            yield TurnProgressBar(id="progress")
            yield ChatBox(id="chatbox")
            yield StatusBar(agent=self.agent, id="statusbar")

    def on_mount(self):
        """Initialize app after mount."""
        self._show_welcome()
        self.query_one("#chatbox", ChatBox).focus()

        # Status mikrofon harus cepat berubah dari "dengar" ke "merekam";
        # jeda dua detik membuat indikator privasi terlambat sepanjang satu
        # kalimat pendek. Render ini murah; pembacaan git tetap punya cache.
        self.set_interval(0.4, self._refresh_status)

        # Tanpa ini, ask_user/ask_choice & permintaan izin permissions.py
        # mengembalikan "[tidak interaktif]" di UI Textual — handler default
        # global hanya dipasang cli.py (UI lama). Handler kita aman dipanggil
        # dari thread pekerja: modal didorong lewat call_from_thread lalu
        # giliran MENUNGGU jawaban di threading.Event (lihat _handler_pilihan).
        interaction.set_choice_handler(self._handler_pilihan_ref)

        # Rencana (tool plan/plan_step) dipoll ringan — mekanisme yang sama
        # dengan cli.py._panel_plan. plan_tool.reset() dipanggil core.run()
        # tiap giliran baru, jadi panel otomatis kosong saat giliran berganti.
        self.set_interval(0.3, self._poll_plan)

        # Kesehatan sistem (CPU/RAM/disk) tiap 2 dtk — psutil cepat, aman
        # di thread UI. GPU dibaca thread terpisah (nvidia-smi = proses
        # eksternal yang membekukan render bila dijalankan di thread UI).
        self.set_interval(2.0, self._poll_sistem)
        self._thread_gpu = threading.Thread(
            target=self._poll_gpu, daemon=True)
        self._thread_gpu.start()

        # Peta proyek & fakta OS/hardware: _main() sudah prime() cache disk
        # ke memo (instan), jadi Agent() tak pernah memindai folder saat
        # startup. Kesegarannya diperiksa & dibangun DI THREAD LATAR —
        # kembaran cli._bg_build_map — lalu system prompt disegarkan
        # otomatis begitu peta terbaru siap.
        def _bangun_peta():
            try:
                from .. import projectindex, osinfo
                osinfo.sync_to_memory()
                hw_status = osinfo.sync_hardware_to_memory()
                primed = projectindex.as_prompt_block()
                # config.PROJECT_ROOT dipakai sebagai default di dalam
                # (module ini tak mengimpor config).
                fresh = projectindex.refresh()
                if fresh != primed or hw_status == "added":
                    self._safe_call(self.agent.refresh_system_prompt)
            except Exception:  # noqa: BLE001 — peta opsional
                pass

        try:
            self.run_worker(_bangun_peta, thread=True, group="peta",
                            exclusive=True, exit_on_error=False)
        except Exception:  # noqa: BLE001 — jangan gagalkan startup
            pass

        # _poll_plan early-return saat steps == cache (keduanya kosong di
        # awal), jadi tata layout SEKARANG: begitu terminal cukup lebar,
        # sidebar sistem tampil sejak detik pertama tanpa menunggu rencana
        # pertama atau event resize.
        self._perbarui_layout_plan()

    def on_unmount(self):
        """Lepas handler pilihan saat app ditutup.

        Tanpa ini handler mati tetap terpasang sebagai default global —
        ask_user dari sesi lain (mis. bot Telegram tanpa handler konteks)
        akan menggantung menunggu modal yang tak pernah muncul.
        """
        try:
            if interaction._default_handler is self._handler_pilihan_ref:
                interaction.set_choice_handler(None)
        except Exception:  # noqa: BLE001 — sedang ditutup
            pass
        self._live_screen = False
        try:
            from ..tools.screen import clear_live_capture
            clear_live_capture()
        except Exception:  # noqa: BLE001 — pembersihan best-effort
            pass
        dikte_stop = self._voice_state.get("dictation_stop")
        if dikte_stop is not None:
            dikte_stop.set()
        pendengar = self._voice_state.get("pendengar")
        if pendengar is not None:
            try:
                pendengar.berhenti()
            except Exception:  # noqa: BLE001 — app sedang ditutup
                pass
        try:
            from .. import suara
            suara.tutup()
        except Exception:  # noqa: BLE001 — audio opsional
            pass

    # ─── Rencana (plan / plan_step) ───────────────────────────────────

    def _poll_plan(self):
        """Baca snapshot plan_tool dan tampilkan ke panel yang benar.

        Dijalankan tiap 0.3 dtk di thread UI. Rencana yang TUNTAS
        (current > jumlah langkah) tetap tampil ±8 dtk sebagai daftar
        centang penuh, lalu langkahnya kembali ke empty-state — tapi cache & state
        plan_tool TIDAK di-reset: reset() hanya milik core.run() saat
        giliran baru. Jadi begitu terminal berpindah layout, rencana
        tuntas tadi tetap bisa muncul lagi, dan sistem tak pernah
        "melupakan" rencana yang belum digantikan giliran berikutnya.
        """
        try:
            from ..tools import plan_tool
            snap = plan_tool.get_state()
        except Exception:  # noqa: BLE001 — tools belum siap
            return
        steps = []
        for i, teks in enumerate(snap["steps"]):
            selesai = (i < len(snap["completed"])
                       and bool(snap["completed"][i]))
            aktif = (i + 1) == snap["current"] and not selesai
            steps.append({
                "text": teks,
                "status": "done" if selesai else ("active" if aktif
                                                  else "pending"),
            })
        if steps == self._plan_cache:
            # Tak ada perubahan — tapi jeda tampil rencana tuntas bisa
            # lewat tanpa perubahan langkah, cek penghabisan di sini.
            self._mungkin_sembunyikan_tuntas()
            return
        self._plan_cache = steps
        # Rencana BARU dari model (bukan hantu cache) — tampilkan lagi
        # walau rencana sebelumnya baru saja disembunyikan.
        self._plan_disembunyikan = False
        # Baru tuntas? Catat waktunya. Masih berjalan? buang penandanya.
        if steps and all(s["status"] == "done" for s in steps):
            if self._plan_selesai_pada is None:
                self._plan_selesai_pada = time.monotonic()
        else:
            self._plan_selesai_pada = None
        self._perbarui_layout_plan()
        self._mungkin_sembunyikan_tuntas()

    def _mungkin_sembunyikan_tuntas(self, paksa: bool = False):
        """Sembunyikan panel rencana bila sudah tuntas ±8 dtk.

        Ini PENYEMBUNYIAN TAMPILAN semata — self._plan_cache dan state
        plan_tool dibiarkan apa adanya: cache supaya poll berikutnya tak
        menganggapnya "rencana baru" (kedipan), state plan_tool karena
        reset() hanya milik core.run() saat giliran baru. Beralih layout
        (sempit<->lebar) pun tak membangkitkan hantu rencana tamat.
        """
        if self._plan_selesai_pada is None or self._plan_disembunyikan:
            return
        if not paksa and time.monotonic() - self._plan_selesai_pada < 8.0:
            return
        self._plan_selesai_pada = None
        self._plan_disembunyikan = True
        self._perbarui_layout_plan()

    # ─── Kesehatan sistem (sidebar lebar) ─────────────────────────────

    def _poll_sistem(self):
        """Segarkan seksi Sistem di sidebar (dipanggil tiap 2 dtk)."""
        try:
            panel = self.query_one("#system-panel", SystemPanel)
        except Exception:  # noqa: BLE001 — sidebar belum ada / dibongkar
            return
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=None)  # non-blocking
            ram = psutil.virtual_memory()
            ram_teks = f"{ram.used / (1 << 30):.0f}/{ram.total / (1 << 30):.0f}G"
            disk = psutil.disk_usage(str(Path.home().anchor or "/"))
            disk_teks = (f"{disk.used / (1 << 30):.0f}/"
                         f"{disk.total / (1 << 30):.0f}G")
        except Exception:  # noqa: BLE001 — psutil tak ada: kosongkan metrik
            panel.clear()
            return
        try:
            panel.terapkan(
                cpu=cpu,
                ram_persen=ram.percent, ram_teks=ram_teks,
                disk_persen=disk.percent, disk_teks=disk_teks,
                gpu_nama=self._gpu_info.get("nama", ""),
                gpu_metrik=self._gpu_info.get("metrik", ""),
            )
        except Exception:  # noqa: BLE001 — UI sedang ditutup
            pass

    def _poll_gpu(self):
        """Baca info GPU dari THREAD TERSENDIRI (bukan thread UI).

        nvidia-smi adalah proses eksternal — di thread UI ia membekukan
        render ±100-300 ms tiap kali dipanggil. Thread ini hidup sepanjang
        app, menulis hasil ke self._gpu_info yang dibaca _poll_sistem.
        """
        import subprocess
        while True:
            nama, metrik = "", ""
            try:
                out = subprocess.run(
                    ["nvidia-smi",
                     "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=4,
                ).stdout.strip().splitlines()
                if out:
                    bag = [b.strip() for b in out[0].split(",")]
                    nama = bag[0]
                    if len(bag) >= 4:
                        metrik = f"{bag[1]}% · {bag[2]}/{bag[3]} MiB"
            except Exception:  # noqa: BLE001 — tak ada nvidia-smi
                nama, metrik = "", ""
            if not nama:
                # Fallback nama GPU via WMI (tanpa metrik — tak ada cara
                # baca utilisasi GPU lintas vendor yang ringan di Windows).
                try:
                    out = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "(Get-CimInstance Win32_VideoController |"
                         " Select-Object -First 1).Name"],
                        capture_output=True, text=True, timeout=6,
                    ).stdout.strip()
                    if out:
                        nama = out
                except Exception:  # noqa: BLE001
                    pass
            self._gpu_info = {"nama": nama, "metrik": metrik}
            # nvidia-smi butuh jeda; WMI juga. 5 dtk cukup halus.
            time.sleep(5.0)

    def _sinkron_footer_sidebar(self, lebar_layar: int | None = None) -> None:
        """Batasi footer sampai tepi kiri sidebar saat dashboard aktif.

        ``#footer`` dan ``#sidebar`` sama-sama dock di Screen. Footer dengan
        ``width: 100%`` akan menutupi divider sidebar pada area pra-jawaban,
        chat, dan status bar. Lebar eksplisit ini membuat keduanya bertemu
        tepat di satu batas sehingga garis sidebar utuh sampai bawah.
        """
        try:
            footer = self.query_one("#footer", Vertical)
            sidebar = self.query_one("#sidebar", InfoSidebar)
            layar = int(lebar_layar if lebar_layar is not None
                        else self.size.width)
        except Exception:  # noqa: BLE001 — widget/layout belum siap
            return
        footer.styles.width = (max(1, layar - sidebar._lebar)
                               if sidebar.display else "100%")

    def _perbarui_layout_plan(self, lebar: bool | None = None,
                              lebar_layar: int | None = None):
        """Pindahkan rencana antara sidebar kanan (lebar) dan footer (sempit).

        Sidebar info (#sidebar) adalah WADAH dock-kanan berisi seksi
        Sistem (selalu, hanya di layar lebar) dan seksi Rencana. ``lebar``
        biasanya dihitung dari ukuran app — TAPI saat dipanggil dari
        ``on_resize`` nilainya harus dari ``event.size``: dispatch MRO
        menjalankan handler kita SEBELUM ``App._on_resize`` memperbarui
        ``self._size``, jadi ``self.size`` masih lebar LAMA di situ.
        Kelas Screen tidak dipakai karena hanya melekat pada screen AKTIF —
        saat modal terbuka, screen-nya modal itu dan kelasnya kosong,
        padahal terminal tetap selebar itu.
        """
        try:
            plan = self.query_one("#plan", PlanPanel)
            sidebar = self.query_one("#plan-side", PlanSidebar)
            wadah = self.query_one("#sidebar", InfoSidebar)
        except Exception:  # noqa: BLE001 — widget sedang dibongkar
            return
        if lebar is None:
            try:
                lebar = self.size.width >= self._LEBAR_MIN
            except Exception:  # noqa: BLE001
                lebar = False
        steps = self._plan_cache
        if self._plan_disembunyikan:
            # Rencana tuntas yang sudah "dipensiunkan" — seksi rencana
            # tidak dimunculkan lagi sampai giliran baru menulis rencana
            # baru (flag dilepas di _poll_plan saat steps berubah).
            steps = []
        tampil_sidebar = lebar or self._sidebar_mobile_open
        if not steps:
            plan.clear()
            sidebar.clear(tampil=tampil_sidebar)
            # Sidebar dan seksi Planning TETAP tampil di layar lebar.
            # Tanpa langkah aktif, PlanSidebar menggambar empty-state.
            wadah.display = tampil_sidebar
            self._sinkron_footer_sidebar(lebar_layar)
            return
        if lebar or self._sidebar_mobile_open:
            sidebar.update_plan(steps)
            wadah.display = True
            plan.clear()  # sembunyikan versi footer — jangan dobel
        else:
            wadah.display = False
            sidebar.clear(tampil=False)
            plan.update_plan(steps)
            try:
                plan.check_collapse(self.size.height)
            except Exception:  # noqa: BLE001
                pass
        self._sinkron_footer_sidebar(lebar_layar)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "chat-voice-button":
            event.stop()
            self._toggle_dictation()
            return
        if event.button.id not in ("statusbar-sidebar-toggle", "sidebar-close"):
            return
        self._sidebar_mobile_open = (not self._sidebar_mobile_open
                                     if event.button.id == "statusbar-sidebar-toggle"
                                     else False)
        sidebar = self.query_one("#sidebar", InfoSidebar)
        sidebar.display = self._sidebar_mobile_open
        try:
            self.query_one("#statusbar-sidebar-toggle", Button).label = "▮" if self._sidebar_mobile_open else "▯"
        except Exception:
            pass
        self._sinkron_footer_sidebar()

    # ─── Editor file dari sidebar ─────────────────────────────────────

    def on_project_tree_buka_berkas(self, event: BukaBerkas) -> None:
        """Klik/Enter file pada ProjectTree membuka editor teks aman."""
        event.stop()
        # Satu aktivasi Tree dapat menghasilkan highlight lalu select sangat
        # berdekatan. Jangan menumpuk dua modal editor untuk file yang sama.
        if isinstance(self.screen, FileEditorScreen):
            return
        try:
            from ..editor import load_text_file
            document = load_text_file(event.path)
        except Exception as exc:  # noqa: BLE001 — tampilkan alasan ke pengguna
            self.query_one("#messages", MessageList).append_notice(
                f"File tidak dapat dibuka: {exc}",
                style=f"bold {tema.p('exit_footer')}",
            )
            return

        def save(old: str, new: str) -> tuple[bool, str]:
            return self._save_sidebar_document(document, old, new)

        self.push_screen(FileEditorScreen(document.path, document.text, save))

    def _save_sidebar_document(self, document, old: str,
                               new: str) -> tuple[bool, str]:
        """Simpan hasil editor dan rekam diff ke riwayat UI/sesi."""
        if self.is_turn_active:
            return False, "Tunggu tugas AI selesai sebelum menyimpan file."
        try:
            from ..editor import EditorFileError, save_text_file
            detail = save_text_file(document, new)
        except EditorFileError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 — kegagalan I/O harus terlihat
            return False, f"Gagal menyimpan: {exc}"

        try:
            label = str(document.path.relative_to(config.PROJECT_ROOT.resolve()))
        except ValueError:
            label = str(document.path)
        messages = self.query_one("#messages", MessageList)
        messages.append_diff(label, old, new, is_new=False)
        self._catat_diff_memory(label, old, new, False)
        messages.append_notice(
            f"✓ {label} disimpan · dapat dikembalikan dengan undo_changes",
            style=f"bold {tema.p('aksen')}",
        )
        return True, detail

    # ─── ask_user / ask_choice (dari thread pekerja) ──────────────────

    def _handler_pilihan(self, question: str, options: list[str],
                         multiple: bool) -> str:
        """Handler interaction.ask_choice — dipanggil DI THREAD PEKERJA.

        Dorong modal ke thread UI lewat call_from_thread, lalu BLOKIR thread
        pekerja di threading.Event sampai modal ditutup. core.run() memang
        menunggu jawaban ini sebelum melanjutkan giliran.
        """
        hasil = {"jawab": None}
        selesai = threading.Event()

        def tutup(jawab):
            hasil["jawab"] = jawab
            selesai.set()

        def tampilkan():
            try:
                self._push_pilihan(question, list(options), bool(multiple),
                                   tutup)
            except Exception:  # noqa: BLE001 — UI sedang ditutup
                selesai.set()

        try:
            self.call_from_thread(tampilkan)
        except Exception:  # noqa: BLE001 — app sudah mati
            return "(dibatalkan)"
        # Tunggu sambil siaga app mati — tanpa loop ini worker menggantung
        # selamanya bila app ditutup saat pertanyaan masih terbuka.
        while not selesai.wait(0.25):
            if not self.is_running:
                return "(dibatalkan)"

        jawab = hasil["jawab"]
        if jawab is None:
            jawab = "(dibatalkan)"
        # Tinggalkan ringkasan tanya-jawab di riwayat — kembaran baris
        # "✓ pertanyaan · jawaban" milik cli.py.
        def ringkas():
            try:
                q = question if len(question) <= 46 else question[:45] + "…"
                self.query_one("#messages", MessageList).append_notice(
                    f"✓ {q} · {jawab}", style=tema.p("redup"))
            except Exception:  # noqa: BLE001 — UI ditutup
                pass
        self._safe_call(ringkas)
        return jawab

    def _push_pilihan(self, question, options, multiple, tutup):
        """Alur modal pilihan — JALAN DI THREAD UI.

        Sama persis semangatnya dengan cli._tanya_pilihan: opsi + isian
        bebas di urutan terakhir; isian kosong mengembalikan ke menunya.
        """
        semua = [o for o in options if o] + [_OPSI_TULIS]

        def tulis_sendiri(kembali, tambahan: list[str] | None = None):
            def kirim(teks):
                teks = (teks or "").strip()
                if not teks:
                    kembali()
                    return
                if tambahan:
                    tutup(_format_multi(tambahan + [teks]))
                else:
                    tutup(teks)
            self.push_screen(TextPromptScreen(
                title=question, placeholder="jawabanmu…"), kirim)

        def menu():
            if not multiple:
                def pilih_satu(hasil):
                    if hasil == _OPSI_TULIS:
                        tulis_sendiri(menu)
                    elif hasil is None:
                        tutup(None)
                    else:
                        tutup(hasil)
                self.push_screen(SelectScreen(title=question, options=semua),
                                 pilih_satu)
                return

            def pilih_banyak(terpilih):
                terpilih = list(terpilih or [])
                bebas = _OPSI_TULIS in terpilih
                terpilih = [t for t in terpilih if t != _OPSI_TULIS]
                if bebas:
                    tulis_sendiri(menu, tambahan=terpilih)
                else:
                    tutup(_format_multi(terpilih))

            self.push_screen(MultiSelectScreen(title=question, options=semua),
                             pilih_banyak)

        menu()

    def _show_welcome(self):
        """Display welcome screen with logo and tagline."""
        msg_list = self.query_one("#messages", MessageList)
        # --resume: gema transkrip percakapan sebelumnya SEBELUM sambutan —
        # tanpa ini sesi lanjutan terlihat kosong, seolah baru dimulai
        # (cli.py memang punya replay ini; UI Textual dulunya tidak).
        self._replay_riwayat(msg_list)
        msg_list.append_notice(
            "Selamat datang di bagas-ai! Ketik pesan atau /help untuk bantuan.",
            style=f"italic {tema.p('aksen_terang')}"
        )
        msg_list.append_notice(
            f"Model: {self.agent.model_spec.label} "
            f"({'🌐 web' if self.agent.model_spec.is_web else '🤖 api'})",
            style=tema.p("redup")
        )

    def _replay_riwayat(self, msg_list: MessageList):
        """Gema pesan & diff tersimpan dari sesi yang dilanjutkan (--resume).

        Hanya role user/assistant/diff — pesan system, tool, dan record
        internal lain tak ada gunanya di layar. Diff direplay lewat
        ``append_diff_replay`` supaya potongan kode sesi sebelumnya tetap
        terlihat, bukan lenyap begitu sesi dibuka kembali.
        """
        try:
            riwayat = list(getattr(self.agent, "memory", None).messages)
        except AttributeError:
            return
        replay = [m for m in riwayat
                  if m.get("role") in ("user", "assistant", "diff")]
        if not replay:
            return
        msg_list.append_notice("── percakapan sebelumnya ──",
                               style=tema.p("tepi_redup"))
        for m in replay:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                msg_list.append_user_message(content)
            elif role == "diff":
                msg_list.append_diff_replay(m)
            elif content:
                msg_list.append_ai_message(content)
        msg_list.append_notice("── lanjut di bawah ──",
                               style=tema.p("tepi_redup"))

    def _refresh_status(self):
        """Periodic status bar refresh."""
        bar = self.query_one("#statusbar", StatusBar)
        pendengar = self._voice_state.get("pendengar")
        keadaan = ""
        if self._voice_state.get("dictation_active"):
            keadaan = ("menganalisis"
                       if self._voice_state.get("dictation_phase") == "menganalisis"
                       else "merekam")
        elif pendengar is not None and getattr(pendengar, "aktif", False):
            keadaan = ("merekam" if getattr(pendengar, "merekam", False)
                       else "dengar")
        bar.update_voice_state(keadaan)
        bar.refresh()

    # ─── Message Handling ──────────────────────────────────────────────

    def on_chatbox_submitted(self, event: ChatBox.Submitted):
        """Handle user message submission."""
        text = event.text.strip()
        if not text:
            return
        teks_tampil = text
        lisan = self._voice_state.pop("terucap", None) == text

        # Echo user message
        msg_list = self.query_one("#messages", MessageList)
        # Logo banner hanya untuk layar sambutan; begitu percakapan
        # dimulai ia hanya memakan ruang vertikal berharga.
        try:
            logo = self.query_one("#logo", LogoWidget)
            if logo.display:
                logo.display = False
        except Exception:  # noqa: BLE001 — logo opsional
            pass

        # Handle slash commands
        if text.startswith("/"):
            # /btw memiliki layar dan riwayat sendiri; jangan mencemari chat utama.
            if text.split(maxsplit=1)[0].lower() != "/btw":
                msg_list.append_user_message(text)
            self._handle_command(text)
            return

        # Mention @file menyisipkan isi berkas ke prompt, tanpa mengubah gema
        # riwayat yang dilihat pengguna.
        from ..mentions import expand_mentions
        text, mentioned = expand_mentions(text)
        if mentioned:
            msg_list.append_notice("Konteks disisipkan: " + ", ".join(Path(p).name for p in mentioned),
                                   style=tema.p("redup"))

        # Gambar hasil paste/drop disimpan sebagai path lokal oleh
        # on_chatbox_pasted. Penanda [foto] hanya bentuk ringkas di kotak input;
        # core memerlukan [GAMBAR] <path> agar benar-benar menjadi attachment.
        # Dulu Textual tidak pernah menyimpan path ini sehingga model cuma
        # menerima teks literal "[foto]".
        if "[foto]" in text:
            path_gambar = self._pending_gambar.get("path")
            if not path_gambar:
                msg_list.append_notice(
                    "Gambar tempelan sudah tidak tersedia — tempel ulang file.",
                    style=f"bold {tema.p('exit_footer')}",
                )
                return
            from ..tools.screen import IMAGE_MARK
            text = text.replace("[foto]", f"\n{IMAGE_MARK} {path_gambar}\n")
            self._pending_gambar.clear()
            try:
                self.hide_image_preview()
            except Exception:  # noqa: BLE001 — preview opsional
                pass
        elif self._pending_gambar:
            # Pengguna menghapus marker [foto]; jangan menempelkan gambar lama
            # secara diam-diam ke pesan berikutnya.
            self._pending_gambar.clear()
            try:
                self.hide_image_preview()
            except Exception:  # noqa: BLE001
                pass

        # Catat ke riwayat masukan (panah-atas). Perintah "/" tak masuk —
        # riwayat ini untuk teks yang mau diedit-ulang, bukan menu.
        self._riwayat_masukan.append(teks_tampil)
        if len(self._riwayat_masukan) > 100:
            del self._riwayat_masukan[:-100]
        self._hist_idx = None  # mulai ulang dari paling baru di up berikutnya

        # If turn is active → queue the message. Prompt TIDAK di-echo ke
        # riwayat; ia tampil di QueueStrip (nempel di bawah area jawaban,
        # diredupkan penuh / "disabled") sampai benar-benar dijalankan.
        if self.is_turn_active:
            with self._antre_lock:
                self._prompt_queue.append(text)
            self._perbarui_strip_antre()
            return

        # Start AI turn. Bila masih ada antrean sisa (giliran lama
        # error/dibatalkan), prompt baru TIDAK boleh meloncati mereka:
        # masuk ekor antrean lalu seluruh antrean dijalankan berurutan.
        with self._antre_lock:
            ada_antrean = bool(self._prompt_queue)
            if ada_antrean:
                self._prompt_queue.append(text)
        if ada_antrean:
            self._process_queue()
            return

        msg_list.append_user_message(teks_tampil,
                                     prefix="🎙 " if lisan else "❯ ")
        self._start_turn(text)

    def on_chatbox_cancelled(self, event: ChatBox.Cancelled):
        """Handle Ctrl+C in chatbox."""
        if self.is_turn_active:
            self._cancel_event.set()
            self._stop_turn()
        else:
            self.exit()

    def on_chatbox_recall(self, event: ChatBox.Recall):
        """Panah-atas/-bawah — dua makna (prioritas dari atas):

        1. Panah-atas + ada teks mengantre -> teks antrean TERAKHIR
           ditarik keluar dari antrean dan kembali ke kotak chat untuk
           diedit (QueueStrip ikut disegarkan). Ini jalur "keburu
           kecolongan kirim": salah kirim saat AI masih bekerja bisa
           diambil pulang.
        2. Riwayat masukan: panah-atas menempel teks sebelumnya dan
           berjalan naik tiap tekanan; panah-bawah berjalan kembali ke
           bawah sampai kosong (perilaku shell biasa).
        """
        chatbox = self.query_one("#chatbox", ChatBox)

        # 1) Tarik teks antrean terakhir (hanya untuk panah-atas).
        if not event.maju:
            with self._antre_lock:
                if self._prompt_queue:
                    ditarik = self._prompt_queue.pop()
                else:
                    ditarik = None
            if ditarik is not None:
                chatbox.set_text(ditarik)
                chatbox.focus()
                self._perbarui_strip_antre()
                return

        # 2) Riwayat: naik satu entri per tekanan.
        if not self._riwayat_masukan:
            return
        if event.maju:
            # Turun: di ujung bawah kembali ke kotak kosong & akhir
            # penjelajahan.
            if self._hist_idx is None:
                return
            if self._hist_idx < len(self._riwayat_masukan) - 1:
                self._hist_idx += 1
                chatbox.set_text(self._riwayat_masukan[self._hist_idx])
            else:
                self._hist_idx = None
                chatbox.set_text("")
            chatbox.focus()
            return
        if self._hist_idx is None:
            self._hist_idx = len(self._riwayat_masukan) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        chatbox.set_text(self._riwayat_masukan[self._hist_idx])
        chatbox.focus()

    def on_chatbox_pasted(self, event: ChatBox.Pasted):
        """Handle paste event — show image preview if media detected."""
        if event.is_media:
            self._pending_gambar["path"] = event.media_path
            msg_list = self.query_one("#messages", MessageList)
            msg_list.append_notice(
                f"  🖼 Gambar: {event.media_path}",
                style=f"bold {tema.p('aksen')}"
            )
            try:
                from ..ui.ascii_art import image_to_blocks_pixels
                hasil = image_to_blocks_pixels(event.media_path)
                if hasil:
                    pixel_data, (ow, oh) = hasil
                    self.show_image_preview(
                        pixel_data,
                        f"{Path(event.media_path).name} ({ow}x{oh})",
                    )
            except Exception:  # noqa: BLE001 — dekode gambar gagal
                pass

    # ─── Slash Commands ────────────────────────────────────────────────

    def _handle_command(self, text: str):
        """Process slash commands."""
        cmd = text[1:].strip().lower()
        msg_list = self.query_one("#messages", MessageList)

        # ── Exit & Session ─────────────────────────────────────────────
        if cmd == "exit" or cmd == "quit":
            self.exit()
        elif cmd == "clear":
            msg_list.clear_messages()
        elif cmd == "new":
            self._cmd_new()
        elif cmd == "delete":
            self._cmd_delete()
        elif cmd == "reset":
            self.agent.reset()
            msg_list.append_notice("✓ Sesi direset.",
                                  style=f"bold {tema.p('aksen')}")

        # ── Help ───────────────────────────────────────────────────────
        elif cmd == "help":
            msg_list.append_notice(self._help_text())
        elif cmd == "export" or cmd.startswith("export "):
            self._cmd_export(text)
        elif cmd == "btw" or cmd.startswith("btw "):
            self._cmd_btw(text)

        # ── Model & Effort ─────────────────────────────────────────────
        elif cmd == "model" or cmd.startswith("model "):
            self._cmd_model(text)
        elif cmd == "effort" or cmd.startswith("effort "):
            self._cmd_effort(text)

        # ── Theme ──────────────────────────────────────────────────────
        elif cmd == "theme" or cmd.startswith("theme "):
            self._cmd_theme(text)

        # ── Live screen & Display ──────────────────────────────────────
        elif (cmd in ("live", "video") or cmd.startswith("live ")
              or cmd.startswith("video ")):
            self._cmd_live(text)
        elif cmd == "stream":
            self._tui_mode = not self._tui_mode
            if self._tui_mode:
                msg_list.append_notice("✓ Tampilan mengalir AKTIF",
                                      style=f"bold {tema.p('aksen')}")
            else:
                msg_list.append_notice("○ Tampilan mengalir MATI",
                                      style=tema.p("redup"))

        # ── Audio ──────────────────────────────────────────────────────
        elif cmd == "mic" or cmd.startswith("mic "):
            self._cmd_mic(text)
        elif cmd == "voice" or cmd.startswith("voice "):
            self._cmd_voice(text)

        # ── Local image reader ─────────────────────────────────────────
        elif cmd == "image" or cmd.startswith("image "):
            self._cmd_image(text)

        # ── Memory ─────────────────────────────────────────────────────
        elif cmd == "memory" or cmd.startswith("memory "):
            self._cmd_memory(text)

        # ── Compact ────────────────────────────────────────────────────
        elif cmd == "compact":
            self._cmd_compact()
        elif cmd == "send-compact" or cmd.startswith("send-compact "):
            self._cmd_send_compact(text)

        # ── Directory Context ──────────────────────────────────────────
        elif cmd == "add-dir" or cmd.startswith("add-dir "):
            self._cmd_add_dir(text)
        elif cmd == "dirs":
            self._cmd_dirs()
        elif cmd == "rm-dir" or cmd.startswith("rm-dir "):
            self._cmd_rm_dir(text)

        # ── Project ────────────────────────────────────────────────────
        elif cmd == "scan":
            self._cmd_scan()
        elif cmd == "review":
            self._cmd_review()

        # ── Web & Bot ──────────────────────────────────────────────────
        elif cmd == "web" or cmd.startswith("web "):
            self._cmd_web(text)
        elif cmd == "bot":
            self._cmd_bot()
        elif cmd == "permissions-bot":
            self._cmd_permissions_bot()

        # ── Team ───────────────────────────────────────────────────────
        elif cmd == "tim" or cmd.startswith("tim "):
            msg_list.append_notice(
                "Tim review: aktif (24 specialist)",
                style=tema.p("redup")
            )

        # ── Misc ───────────────────────────────────────────────────────
        elif cmd == "version":
            from ..__main__ import __version__
            msg_list.append_notice(f"bagas-ai v{__version__}",
                                  style=tema.p("aksen"))
        else:
            msg_list.append_notice(
                f"Perintah tidak dikenal: /{cmd}\nKetik /help untuk bantuan.",
                style=f"bold {tema.p('exit_footer')}"
            )

    # ─── Local Image Reader ───────────────────────────────────────────

    def _cmd_export(self, text: str) -> None:
        """Simpan transkrip sesi ke Markdown/JSON."""
        msg_list = self.query_one("#messages", MessageList)
        parts = text.split(maxsplit=1)
        destination = parts[1].strip().strip('"').strip("'") if len(parts) == 2 else ""
        try:
            if self.agent.session is None:
                raise RuntimeError("sesi belum tersedia")
            fmt = destination.lower() if destination.lower() in ("md", "markdown", "json") else "md"
            if fmt != "md":
                destination = ""
            path = session_mod.export_history(self.agent.session, destination or None, fmt=fmt)
            msg_list.append_notice(f"✓ Riwayat diekspor ke {path}", style=tema.p("aksen"))
        except Exception as exc:
            msg_list.append_notice(f"Gagal ekspor: {exc}", style=f"bold {tema.p('exit_footer')}")

    def _cmd_btw(self, text: str) -> None:
        """Obrolan sampingan; tidak masuk antrean atau memory tugas."""
        parts = text.split(maxsplit=1)
        initial = parts[1].strip() if len(parts) == 2 else ""
        sess = getattr(self.agent, "session", None)
        context = ""
        if sess is not None:
            rows = []
            for msg in getattr(sess, "messages", [])[-12:]:
                role, content = msg.get("role", ""), str(msg.get("content", ""))
                if content:
                    rows.append(f"{role}: {content}")
            context = "\n".join(rows)
        def jawab(pertanyaan: str) -> str:
            return self.agent.btw(pertanyaan, context=context)
        self.push_screen(BtwScreen(jawab, initial=initial))

    def _cmd_image(self, text: str) -> None:
        """Baca satu gambar di worker Python lokal, tanpa request provider."""
        msg_list = self.query_one("#messages", MessageList)
        bagian = text.strip().split(maxsplit=1)
        if len(bagian) != 2 or not bagian[1].strip():
            msg_list.append_notice(
                'Pemakaian: /image <path gambar>\nContoh: /image "foto UI.png"',
                style=f"bold {tema.p('exit_footer')}",
            )
            return
        if self._image_task_active:
            msg_list.append_notice(
                "Pembacaan gambar lokal lain masih berjalan.",
                style=f"bold {tema.p('exit_footer')}",
            )
            return
        path = bagian[1].strip().strip('"').strip("'")
        self._image_task_active = True
        msg_list.append_notice(
            f"Membaca {path} dengan Python lokal…",
            style=tema.p("redup"),
        )

        def worker() -> None:
            try:
                from ..tools.image_local import read_image_local
                hasil = read_image_local(path)
            except Exception as exc:  # noqa: BLE001 — laporan ramah UI
                hasil = f"[error] pembacaan gambar lokal gagal: {exc}"
            self._safe_call(self._finish_image_read, hasil)

        threading.Thread(target=worker, daemon=True,
                         name="bagasai-image-local").start()

    def _finish_image_read(self, hasil: str) -> None:
        self._image_task_active = False
        self.query_one("#messages", MessageList).append_notice(
            hasil,
            style=(f"bold {tema.p('exit_footer')}"
                   if hasil.lstrip().startswith("[error]") else tema.p("redup")),
        )

    # ─── Live Screen ──────────────────────────────────────────────────

    def _set_live_screen(self, aktif: bool) -> None:
        """Set state /live, indikator permanen, dan berkas sementaranya."""
        self._live_screen = bool(aktif)
        try:
            self.query_one("#statusbar", StatusBar).update_live_screen(
                self._live_screen)
        except Exception:  # noqa: BLE001 — statusbar opsional/sedang tutup
            pass
        if not aktif:
            try:
                from ..tools.screen import clear_live_capture
                clear_live_capture()
            except Exception:  # noqa: BLE001 — cleanup tak boleh matikan UI
                pass

    def _set_live_vision_state(self, state: str) -> None:
        """Perlihatkan kesiapan alat live secara permanen di footer."""
        try:
            self.query_one("#statusbar", StatusBar).update_live_screen(
                self._live_screen, state,
            )
        except Exception:  # noqa: BLE001 — statusbar opsional/sedang tutup
            pass

    def _cmd_live(self, text: str) -> None:
        """Kelola /live (alias /video): on, off, status, atau toggle."""
        msg_list = self.query_one("#messages", MessageList)
        parts = text.strip().lower().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) == 2 else "toggle"
        hidup = {"on", "aktif", "hidup", "start", "mulai"}
        mati = {"off", "mati", "stop", "berhenti"}

        if arg in {"status", "cek"}:
            if self._live_screen:
                detail = "alat aktif"
            elif self._live_starting:
                detail = "sedang mengaktifkan alat"
            else:
                detail = "alat tidak aktif"
            msg_list.append_notice(
                f"Status live: {detail}.",
                style=(f"bold {tema.p('aksen')}" if self._live_screen
                       else tema.p("redup")),
            )
            return
        if arg not in hidup | mati | {"toggle"}:
            msg_list.append_notice(
                "Pemakaian: /live [on|off|status] (alias: /video)",
                style=f"bold {tema.p('exit_footer')}",
            )
            return

        ingin_aktif = (not self._live_screen if arg == "toggle"
                       else arg in hidup)
        if not ingin_aktif:
            self._live_start_token += 1
            self._live_starting = False
            self._set_live_screen(False)
            msg_list.append_notice("○ alat dinonaktifkan.",
                                   style=tema.p("redup"))
            return

        try:
            from ..tools.screen import (
                clear_live_capture, screen_capture_available,
            )
            tersedia, alasan = screen_capture_available()
        except Exception as exc:  # noqa: BLE001 — dependensi layar bermasalah
            tersedia, alasan = False, str(exc)
        if not tersedia:
            msg_list.append_notice(
                f"✗ alat tidak dapat diaktifkan: {alasan}",
                style=f"bold {tema.p('exit_footer')}",
            )
            return

        if self._live_starting:
            msg_list.append_notice(
                "sedang mengaktifkan alat…",
                style=tema.p("redup"),
            )
            return
        self._live_starting = True
        self._live_start_token += 1
        token = self._live_start_token
        self._set_live_vision_state("checking")
        msg_list.append_notice(
            "mengaktifkan alat…",
            style=tema.p("redup"),
        )

        def worker() -> None:
            try:
                from ..tools.vision_local import ensure_vision_available
                siap, alasan_vision = ensure_vision_available()
            except Exception as exc:  # noqa: BLE001
                siap, alasan_vision = False, str(exc)
            self._safe_call(
                self._finish_live_start, token, siap, alasan_vision,
            )

        threading.Thread(
            target=worker, daemon=True, name="bagasai-live-vision-probe",
        ).start()

    def _finish_live_start(self, token: int, siap: bool, alasan: str) -> None:
        """Aktifkan /live sesudah backend fallback dipastikan tersedia."""
        if token != self._live_start_token:
            return
        self._live_starting = False
        msg_list = self.query_one("#messages", MessageList)
        if not siap:
            self._set_live_screen(False)
            self._set_live_vision_state("error")
            msg_list.append_notice(
                f"✗ alat gagal diaktifkan: {alasan}",
                style=f"bold {tema.p('exit_footer')}",
            )
            return
        from ..tools.screen import clear_live_capture
        clear_live_capture()
        self._set_live_screen(True)
        msg_list.append_notice(
            "✓ alat aktif.",
            style=f"bold {tema.p('aksen')}",
        )

    def _capture_live_attachment(self) -> list[str]:
        """Ambil screenshot just-in-time; live tidak boleh fallback diam-diam."""
        if not self._live_screen:
            return []
        self.agent_on_status("mengambil gambar…")
        try:
            from ..tools.screen import capture_live_screen
            return [str(capture_live_screen())]
        except Exception as exc:  # noqa: BLE001
            self._safe_call(self._set_live_screen, False)
            raise RuntimeError(
                f"alat dihentikan karena pengambilan gambar gagal: {exc}"
            ) from exc

    # ─── Audio: /mic dan /voice ───────────────────────────────────────

    def _audio_notice(self, pesan: str, *, error: bool = False) -> None:
        self.query_one("#messages", MessageList).append_notice(
            pesan,
            style=(f"bold {tema.p('exit_footer')}" if error
                   else tema.p("redup")),
        )

    def _start_audio_task(self, label: str, work) -> None:
        """Jalankan rekam/cek audio yang lambat tanpa membekukan Textual."""
        if self._voice_state.get("task_active"):
            self._audio_notice("Proses audio lain masih berjalan.", error=True)
            return
        self._voice_state["task_active"] = True
        self._audio_notice(label)

        def worker() -> None:
            try:
                ok, pesan = work()
            except Exception as exc:  # noqa: BLE001 — kegagalan audio ramah UI
                ok, pesan = False, f"Audio gagal: {exc}"
            self._safe_call(self._finish_audio_task, ok, pesan)

        threading.Thread(target=worker, daemon=True,
                         name="bagasai-audio-command").start()

    def _finish_audio_task(self, ok: bool, pesan: str) -> None:
        self._voice_state["task_active"] = False
        self._audio_notice(("✓ " if ok else "⚠ ") + pesan,
                           error=not ok)

    def _cmd_mic(self, text: str) -> None:
        """Kelola pembacaan kabar/jawaban melalui pengeras suara."""
        parts = text.strip().lower().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) == 2 else "status"
        if arg in ("on", "hidup", "off", "mati"):
            aktif = arg in ("on", "hidup")
            prefs.save(suara=aktif)
            if not aktif:
                try:
                    from .. import suara
                    suara.diam()
                except Exception:  # noqa: BLE001 — audio opsional
                    pass
            self._audio_notice(
                f"{'✓' if aktif else '○'} Suara kabar "
                f"{'AKTIF' if aktif else 'MATI'}.")
            return

        aktif = bool(prefs.load().get("suara", True))
        if arg in ("tes", "test", "coba"):
            if not aktif:
                self._audio_notice(
                    "Suara sedang mati — nyalakan dengan /mic on.", error=True)
                return

            def tes_mic():
                from .. import suara
                mesin = suara.mesin_tersedia()
                if not mesin:
                    return False, suara.alasan_diam()
                suara.ucap(
                    "Halo, ini suara bagas a i. Kabar dan jawaban model "
                    "akan dibacakan seperti ini.")
                return True, f"Contoh suara sedang diputar ({mesin[0]})."

            self._start_audio_task("♪ Memeriksa mesin suara…", tes_mic)
            return

        if arg not in ("status", "cek"):
            self._audio_notice(
                "Pemakaian: /mic [on|off|tes|status]", error=True)
            return
        self._audio_notice(
            f"♪ Suara kabar {'AKTIF' if aktif else 'MATI'} — "
            "kabar proses dan jawaban akhir dibacakan. Gunakan /mic tes "
            "untuk memeriksa pengeras suara.")

    def _refresh_voice_status(self) -> None:
        try:
            self._refresh_status()
        except Exception:  # noqa: BLE001 — statusbar sedang dibongkar
            pass

    def _toggle_dictation(self) -> None:
        """Tombol/F4: mulai dikte langsung atau akhiri rekaman aktif."""
        stop = self._voice_state.get("dictation_stop")
        if self._voice_state.get("dictation_active") and stop is not None:
            stop.set()
            self._voice_state["dictation_phase"] = "mengakhiri"
            self.query_one("#chatbox", ChatBox).set_voice_recording(
                True, "Mengakhiri rekaman…")
            self._audio_notice("🎙 Mengakhiri rekaman dan menyiapkan transkrip…")
            self._refresh_voice_status()
            return
        if self._voice_state.get("task_active"):
            self._audio_notice("Proses audio lain masih berjalan.", error=True)
            return

        stop = threading.Event()
        sebelumnya = self._voice_state.get("pendengar")
        lanjutkan_listener = bool(
            sebelumnya is not None and getattr(sebelumnya, "aktif", False))
        self._voice_state.update({
            "task_active": True,
            "dictation_active": True,
            "dictation_phase": "menunggu",
            "dictation_stop": stop,
            "pendengar": None,
            "wanted": False,
        })
        self.query_one("#chatbox", ChatBox).set_voice_recording(
            True, "Bicara sekarang; tekan lagi/F4 untuk selesai")
        self._audio_notice(
            "🎙 Dikte aktif — langsung bicara tanpa menyebut ‘bagas ai’. "
            "Tekan tombol/F4 lagi untuk selesai.")
        self._refresh_voice_status()

        def status(fase: str) -> None:
            self._safe_call(self._dictation_phase, fase)

        def worker() -> None:
            try:
                if sebelumnya is not None:
                    sebelumnya.berhenti()
                from .. import dengar
                teks, info = dengar.dengar_dikte(
                    berhenti=stop,
                    on_status=status,
                    jangkauan=self._voice_state.get("jangkauan"),
                )
                galat = ""
            except Exception as exc:  # noqa: BLE001
                teks, info, galat = "", {}, str(exc)
            self._safe_call(self._finish_dictation, teks, info, galat,
                            lanjutkan_listener)

        threading.Thread(target=worker, daemon=True,
                         name="bagasai-direct-dictation").start()

    def _dictation_phase(self, fase: str) -> None:
        if not self._voice_state.get("dictation_active"):
            return
        self._voice_state["dictation_phase"] = fase
        label = {
            "menunggu": "Menunggu suara…",
            "merekam": "Sedang merekam; tekan lagi/F4 untuk selesai",
            "menganalisis": "Menganalisis suara secara lokal…",
        }.get(fase, fase)
        self.query_one("#chatbox", ChatBox).set_voice_recording(True, label)
        if fase == "menganalisis":
            self._audio_notice("🎙 Sedang menganalisis suara secara lokal…")
        self._refresh_voice_status()

    def _finish_dictation(self, teks: str, info: dict,
                          galat: str, lanjutkan_listener: bool) -> None:
        self._voice_state.update({
            "task_active": False,
            "dictation_active": False,
            "dictation_phase": "",
            "dictation_stop": None,
        })
        chatbox = self.query_one("#chatbox", ChatBox)
        chatbox.set_voice_recording(False)
        chatbox.focus()
        self._refresh_voice_status()
        if galat:
            self._audio_notice(f"Dikte gagal: {galat}", error=True)
        elif not teks:
            self._audio_notice(
                "Tidak ada ucapan yang dikenali. Tekan mikrofon lalu coba lagi.",
                error=True)
        else:
            mesin = info.get("engine", "whisper")
            self._audio_notice(f"✓ Dikte lokal selesai ({mesin}): {teks}")
            self._voice_masuk_ui(teks)
        if lanjutkan_listener:
            self._begin_voice()

    def _begin_voice(self, sebelumnya=None) -> None:
        """Mulai/restart pendengar mikrofon di thread terpisah."""
        if self._voice_state.get("task_active"):
            self._audio_notice("Proses audio lain masih berjalan.", error=True)
            return
        self._voice_state["wanted"] = True
        self._voice_state["task_active"] = True
        self._voice_state["pendengar"] = None
        self._refresh_voice_status()
        self._audio_notice("🎙 Menyiapkan dan mengkalibrasi mikrofon…")

        def worker() -> None:
            if sebelumnya is not None:
                try:
                    sebelumnya.berhenti()
                except Exception:  # noqa: BLE001
                    pass
            try:
                from .. import dengar
                pendengar = dengar.Pendengar(
                    self._voice_masuk,
                    self._voice_kabar,
                    jangkauan=self._voice_state.get("jangkauan"),
                )
                alasan = pendengar.mulai()
            except Exception as exc:  # noqa: BLE001
                pendengar, alasan = None, str(exc)
            self._safe_call(self._voice_started, pendengar, alasan)

        threading.Thread(target=worker, daemon=True,
                         name="bagasai-voice-start").start()

    def _voice_started(self, pendengar, alasan: str) -> None:
        self._voice_state["task_active"] = False
        if not self._voice_state.get("wanted"):
            if pendengar is not None:
                threading.Thread(target=pendengar.berhenti,
                                 daemon=True).start()
            return
        if alasan or pendengar is None:
            self._voice_state["wanted"] = False
            self._audio_notice(
                f"Mikrofon tidak dapat dinyalakan: {alasan or 'tidak tersedia'}",
                error=True,
            )
            self._refresh_voice_status()
            return
        self._voice_state["pendengar"] = pendengar
        try:
            from .. import dengar
            threading.Thread(target=dengar.bunyi, args=(True,),
                             daemon=True).start()
            nama = dengar.nama_mikrofon() or "mikrofon bawaan"
            jeda = dengar.JEDA_SELESAI
        except Exception:  # noqa: BLE001
            nama, jeda = "mikrofon bawaan", 2
        self._audio_notice(
            f"● Mikrofon AKTIF — {nama}. Sebut “bagas ai”, ucapkan "
            f"perintah, lalu diam {jeda:.0f} detik. Ucapkan “batalkan” "
            "untuk membuang rekaman.")
        self._refresh_voice_status()

    def _stop_voice(self) -> None:
        self._voice_state["wanted"] = False
        pendengar = self._voice_state.get("pendengar")
        self._voice_state["pendengar"] = None
        self._refresh_voice_status()
        if pendengar is not None:
            def worker() -> None:
                try:
                    pendengar.berhenti()
                    from .. import dengar
                    dengar.bunyi(False)
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=worker, daemon=True,
                             name="bagasai-voice-stop").start()
        self._audio_notice("○ Mikrofon MATI.")

    def _cmd_voice(self, text: str) -> None:
        """Kelola mikrofon sebagai sumber prompt Textual."""
        parts = text.strip().lower().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) == 2 else "status"
        pendengar = self._voice_state.get("pendengar")
        aktif = pendengar is not None and getattr(pendengar, "aktif", False)

        if arg in ("off", "mati"):
            self._stop_voice()
            return
        if arg in ("on", "hidup"):
            if aktif:
                self._audio_notice("Mikrofon sudah aktif.")
            else:
                self._begin_voice()
            return
        if arg in ("tekan", "dikte", "ptt"):
            self._toggle_dictation()
            return
        if arg in ("dekat", "normal", "jauh"):
            self._voice_state["jangkauan"] = arg
            self._audio_notice(f"Jangkauan mikrofon diubah ke {arg.upper()}.")
            if aktif:
                self._begin_voice(sebelumnya=pendengar)
            else:
                self._audio_notice("Setelan berlaku saat /voice on.")
            return

        if arg in ("tes", "test", "coba"):
            if aktif:
                self._audio_notice(
                    "Matikan listener dengan /voice off sebelum tes agar "
                    "perangkat mikrofon tidak dibuka dua kali.", error=True)
                return
            def tes_voice():
                from .. import dengar
                ok, alasan = dengar.siap()
                if not ok:
                    return False, alasan
                teks_dengar, puncak = dengar.dengar_sekali(5.0)
                if not teks_dengar:
                    return False, (f"Tidak ada ucapan yang dikenali "
                                   f"(puncak suara {puncak:.0f}).")
                return True, (f"Terdengar: “{teks_dengar}” "
                              f"(puncak {puncak:.0f}).")
            self._start_audio_task(
                "🎙 Merekam tes selama 5 detik; bicaralah sekarang…",
                tes_voice,
            )
            return

        if arg in ("jangkau", "jarak", "jangkauan"):
            if aktif:
                self._audio_notice(
                    "Matikan listener dengan /voice off sebelum mengukur "
                    "jangkauan mikrofon.", error=True)
                return
            def ukur_voice():
                from .. import dengar
                ok, alasan = dengar.siap()
                if not ok:
                    return False, alasan
                hasil = dengar.ukur(
                    6.0, jangkauan=self._voice_state.get("jangkauan"))
                sampai = hasil["suara_p90"] > hasil["ambang"]
                pesan = (
                    f"Jangkauan {hasil['jangkauan']}: suara "
                    f"{hasil['suara_p90']:.0f}, ambang {hasil['ambang']:.0f}"
                )
                if hasil.get("teks"):
                    pesan += f", terdengar “{hasil['teks']}”"
                if not sampai and hasil.get("saran"):
                    pesan += f". Coba /voice {hasil['saran']}"
                return sampai, pesan
            self._start_audio_task(
                "🎙 Diam sebentar untuk kalibrasi, lalu bicara selama 6 detik…",
                ukur_voice,
            )
            return

        if arg not in ("status", "cek"):
            self._audio_notice(
                "Pemakaian: /voice [tekan|on|off|tes|jangkau|dekat|normal|jauh]",
                error=True,
            )
            return
        jangkauan = (self._voice_state.get("jangkauan") or "bawaan")
        galat = getattr(pendengar, "galat", "") if pendengar else ""
        self._audio_notice(
            f"🎙 Mikrofon {'AKTIF' if aktif else 'MATI'} · jangkauan "
            f"{jangkauan}. Tekan ikon mikrofon/F4 untuk dikte langsung, atau "
            "sebut “bagas ai” pada mode hands-free."
            + (f" Galat terakhir: {galat}" if galat else ""),
            error=bool(galat),
        )

    def _voice_masuk(self, teks: str) -> None:
        """Callback thread Pendengar: teruskan prompt ke thread UI."""
        self._safe_call(self._voice_masuk_ui, teks)

    def _voice_masuk_ui(self, teks: str) -> None:
        teks = (teks or "").strip()
        if not teks:
            return
        if self.is_turn_active:
            self._riwayat_masukan.append(teks)
            with self._antre_lock:
                self._prompt_queue.append(teks)
            self._perbarui_strip_antre()
            self._audio_notice(f"🎙 Perintah suara mengantre: {teks}")
            return
        self._voice_state["terucap"] = teks
        self.on_chatbox_submitted(ChatBox.Submitted(teks))

    def _voice_kabar(self, pesan: str, batal: bool = False) -> None:
        """Callback thread Pendengar untuk pembatalan/kegagalan audio."""
        awalan = "🎙 " if batal else "⚠ Mikrofon: "
        self._safe_call(self._audio_notice, awalan + str(pesan),
                        error=not batal)

    # ─── New Session ───────────────────────────────────────────────────

    def _cmd_new(self):
        """Start a new session."""
        msg_list = self.query_one("#messages", MessageList)
        self.agent.reset()
        msg_list.clear_messages()
        msg_list.append_notice("✓ Sesi baru dimulai.",
                              style=f"bold {tema.p('aksen')}")

    # ─── Delete Session ────────────────────────────────────────────────

    def _cmd_delete(self):
        """Delete current session."""
        async def on_confirm(result: bool):
            if result:
                msg_list = self.query_one("#messages", MessageList)
                self.agent.reset()
                msg_list.clear_messages()
                msg_list.append_notice("✓ Sesi dihapus.",
                                      style=f"bold {tema.p('aksen')}")

        self.push_screen(ConfirmScreen(
            title="Hapus sesi ini? Semua riwayat akan hilang."
        ), on_confirm)

    # ─── Model Command ─────────────────────────────────────────────────

    def _cmd_model(self, text: str):
        """Handle /model command — show model list or set model."""
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            # Direct set: /model gemini-2.5-flash
            self._setelah_ganti_model(parts[1].strip())
        else:
            # Show select menu
            self._show_model_menu()

    def _show_model_menu(self):
        """Menu pilihan model — NAMA MODEL SUNGGUHAN, dikelompokkan per
        kategori dengan pemisah (OpenCode Zen / AI web / API ber-key).

        Tiap layanan web memuai jadi tiap variannya ("GLM-5.2", "K2.6",
        "Qwen3.8-Max") dari web_models connectornya; model API tampil
        apa adanya. Model rekomendasi berlabel "(rekomendasi)" (bold),
        tapi nilai yang dikembalikan tetap alias murninya."""
        from .. import models as models_mod
        from .textual_widgets.modal_screens import _SEP

        options: list = []
        try:
            for kategori, items in models_mod.pilihan_model_grup():
                options.append((_SEP, kategori))
                options.extend(items)
        except Exception:  # noqa: BLE001 — katalog gagal dimuat
            options = []
        if not options:
            options = [self.agent.model_spec.label]
        current = self.agent.model_spec.label
        if getattr(self.agent, "_web_varian", None):
            current += f" · {self.agent._web_varian}"

        async def on_select(result: str | None):
            self._setelah_ganti_model(result)

        self.push_screen(SelectScreen(
            title=f"Model (saat ini: {current})",
            options=options,
        ), on_select)

    def _setelah_ganti_model(self, result: str | None):
        """Pesan + statusbar setelah model berganti (dipakai 2 tempat).

        Model WEB langsung membuka jendela/tab browsernya di sini — lewat
        thread worker, sebab menyambung/login bisa memakan belasan detik dan
        harus tak memblokir UI (lihat Agent.pasang_model_web)."""
        if not result:
            return
        varian = None
        try:
            label = self.agent.set_model(result)
        except ValueError as e:
            msg_list = self.query_one("#messages", MessageList)
            msg_list.append_notice(f"✗ {e}",
                                   style=f"bold {tema.p('exit_footer')}")
            return
        # set_model menyimpan variannya di agent._web_varian; ambil SEBELUM
        # worker jalan (worker bisa jalan duluan & menghabiskannya).
        varian = getattr(self.agent, "_web_varian", None)
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice(f"✓ Model: {label}",
                              style=f"bold {tema.p('aksen')}")
        try:
            spec = self.agent.model_spec
            self.query_one("#statusbar", StatusBar).update_model(
                spec.label, spec.is_web)
        except Exception:  # noqa: BLE001 — statusbar opsional
            pass
        if self.agent.model_spec.is_web:
            self._pasang_model_web(varian)

    def _pasang_model_web(self, varian: str | None):
        """Buka browser untuk model web yang baru dipilih — di thread worker.

        Tiga kemungkinan di dalamnya (lihat Agent.pasang_model_web): jendela
        baru bila browser belum ada, jendela baru setelah menutup yang lama
        bila modelnya berbeda, atau TAB baru + berkas konteks bila modelnya
        sama."""
        msg_list = self.query_one("#messages", MessageList)
        prog = self.query_one("#progress", TurnProgressBar)
        hasil: dict = {}

        def worker():
            try:
                hasil["ok"] = self.agent.pasang_model_web(
                    varian,
                    on_status=lambda m: self._safe_call(prog.show, 0.0, m),
                    on_notice=lambda m: self._safe_call(
                        msg_list.append_notice, f"· {m}", tema.p("redup")))
            except BaseException as exc:  # noqa: BLE001
                hasil["err"] = exc

        wt = threading.Thread(target=worker, daemon=True)

        def selesai() -> None:
            prog.hide()
            if "err" in hasil:
                msg_list.append_notice(f"⚠ {hasil['err']}",
                                       style=f"bold {tema.p('exit_footer')}")
            elif hasil.get("ok"):
                msg_list.append_notice(f"✓ {hasil['ok']}",
                                       style=f"bold {tema.p('aksen')}")

        wt.start()

        def tunggu() -> None:
            wt.join(timeout=240)
            self._safe_call(selesai)

        threading.Thread(target=tunggu, daemon=True).start()

    # ─── Effort Command ────────────────────────────────────────────────

    def _cmd_effort(self, text: str):
        """/effort — kini murni USAHA BERPIKIR.

        Model web: pilih tombol berpikir di situsnya (web_efforts) lalu
        program mengkliknya lewat browser. Model API: pilih tingkat yang
        dikirim sebagai parameter. Varian model sudah TIDAK di sini —
        pindah ke /model."""
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and not self.agent.model_spec.is_web:
            self._effort_api_set(parts[1].strip())
            return
        if self.agent.model_spec.is_web:
            self._show_web_effort_menu()
        else:
            self._show_effort_menu()

    def _effort_api_set(self, nilai: str) -> None:
        msg_list = self.query_one("#messages", MessageList)
        try:
            result = self.agent.set_effort(nilai)
            if result:
                msg_list.append_notice(f"✓ Effort: {result}",
                                      style=f"bold {tema.p('aksen')}")
            else:
                msg_list.append_notice(
                    "Model API ini tak punya tingkat effort.",
                    style=tema.p("redup"))
        except ValueError as e:
            msg_list.append_notice(f"✗ {e}",
                                   style=f"bold {tema.p('exit_footer')}")

    def _show_web_effort_menu(self):
        """Menu tombol USAHA BERPIKIR milik situs (diklik di browser)."""
        spec = self.agent.model_spec
        try:
            from .. import connectors
            conn = connectors.get_connector(spec.connector)
            opts = conn.web_options()
        except Exception as exc:  # noqa: BLE001 — connector/Playwright tak siap
            msg_list = self.query_one("#messages", MessageList)
            msg_list.append_notice(
                f"⚠ connector tak siap: {exc}",
                style=f"bold {tema.p('exit_footer')}")
            return
        msg_list = self.query_one("#messages", MessageList)
        if not opts:
            msg_list.append_notice(
                f"{spec.label} tak punya tombol usaha berpikir yang bisa "
                "diatur dari sini.",
                style=tema.p("redup"))
            return

        async def on_select(result: str | None):
            if not result:
                return
            self._klik_web_option(conn, result)

        self.push_screen(SelectScreen(
            title=f"Usaha berpikir {spec.label} (diklik di situsnya)",
            options=[label for label, _desc in opts],
            hint="; ".join(f"{l} = {d}" for l, d in opts)[:200],
        ), on_select)

    def _klik_web_option(self, conn, label: str) -> None:
        """Klik tombol UI web dari thread pekerja + tampilkan hasilnya."""
        msg_list = self.query_one("#messages", MessageList)
        prog = self.query_one("#progress", TurnProgressBar)
        prog.show(0.0, f"mengklik '{label}'...")
        hasil: dict = {}

        def worker():
            try:
                hasil["ok"] = conn.set_web_option(label)
            except BaseException as exc:  # noqa: BLE001
                hasil["err"] = exc

        wt = threading.Thread(target=worker, daemon=True)

        def selesai() -> None:
            prog.hide()
            if "err" in hasil:
                msg_list.append_notice(f"⚠ {hasil['err']}",
                                       style=f"bold {tema.p('exit_footer')}")
            else:
                msg_list.append_notice(f"✓ {hasil.get('ok', '')}",
                                       style=f"bold {tema.p('aksen')}")

        wt.start()

        def tunggu() -> None:
            wt.join(timeout=180)
            self._safe_call(selesai)

        threading.Thread(target=tunggu, daemon=True).start()

    def _show_effort_menu(self):
        """Show effort selection modal (model API)."""
        spec = self.agent.model_spec
        if not spec.effort_levels:
            msg_list = self.query_one("#messages", MessageList)
            msg_list.append_notice(
                spec.effort_catatan
                or "Model API ini tak punya tingkat effort.",
                style=tema.p("redup"))
            return
        options = list(spec.effort_levels)

        async def on_select(result: str | None):
            if result:
                self._effort_api_set(result)

        self.push_screen(SelectScreen(
            title=f"Effort {spec.label}",
            options=options,
            hint=" · ".join(
                f"{l} = {models.EFFORT_INFO[l][1]}" if l in models.EFFORT_INFO
                else l for l in options),
        ), on_select)

    # ─── Theme Command ─────────────────────────────────────────────────

    def _terapkan_tema(self):
        """Terapkan tema aktif ke SELURUH UI, sekali jalan.

        CSS memakai variabel ``$t-*`` (lihat textual_theme.py). ``refresh_css``
        memanggil ``get_css_variables()`` kita lalu reparse — semua aturan
        langsung bernilai warna baru, tanpa sumber CSS ganda yang saling
        bertarung seperti versi lama.
        """
        try:
            self.refresh_css()
        except Exception:  # noqa: BLE001 — CSS cacat jangan matikan app
            pass

    def _cmd_theme(self, text: str):
        """Handle /theme command."""
        parts = text.split(maxsplit=1)
        msg_list = self.query_one("#messages", MessageList)
        if len(parts) == 2:
            if tema.set_tema(parts[1]):
                self._terapkan_tema()
                self._segar_tema_widget()
                msg_list.append_notice(
                    f"✓ Tema: {tema.label_aktif()}",
                    style=f"bold {tema.p('aksen')}"
                )
            else:
                available = ", ".join(t[0] for t in tema.daftar())
                msg_list.append_notice(
                    f"Tema tidak dikenal. Tersedia: {available}",
                    style=f"bold {tema.p('exit_footer')}"
                )
        else:
            self._show_theme_menu()

    def _segar_tema_widget(self):
        """Widget yang merender warna manual perlu digambar ulang."""
        try:
            self.query_one("#chatbox", ChatBox).refresh_theme()
            self.query_one("#logo", LogoWidget).refresh_theme()
            self.query_one("#statusbar", StatusBar).refresh_theme()
            # Riwayat digambar ulang supaya markdown ikut warna baru.
            # (Teks bergaya manual dari giliran lama tetap pakai warna
            # temanya saat dibuat — biaya kecil demi ganti tema sekali jalan.)
            self.query_one("#messages", MessageList)._gambar_ulang()
        except Exception:  # noqa: BLE001 — widget opsional
            pass

    def _show_theme_menu(self):
        """Menu tema dengan pratinjau langsung saat sorotan berpindah."""
        def on_select(result: str | None):
            # Apa pun hasilnya (pakai/batal), hentikan pratinjau.
            self._tema_pratinjau = None
            if result:
                tema.set_tema(result)
            try:
                self.refresh_css()
            except Exception:  # noqa: BLE001
                pass
            self._segar_tema_widget()
            if result:
                msg_list = self.query_one("#messages", MessageList)
                msg_list.append_notice(
                    f"✓ Tema: {tema.label_aktif()}",
                    style=f"bold {tema.p('aksen')}")

        self.push_screen(ThemeScreen(themes=tema.daftar()), on_select)

    # ─── Memory Command ────────────────────────────────────────────────

    def _cmd_memory(self, text: str):
        """Handle /memory command — list, add, forget."""
        parts = text.split(maxsplit=2)
        msg_list = self.query_one("#messages", MessageList)

        if len(parts) < 2 or parts[1] == "list":
            # Show memory list
            try:
                facts = longmem.all_facts()
                if facts:
                    items = "\n".join(f"  • {f}" for f in facts[:20])
                    msg_list.append_notice(f"Memory:\n{items}",
                                          style=tema.p("aksen"))
                else:
                    msg_list.append_notice("Belum ada memory.",
                                          style=tema.p("redup"))
            except Exception as e:
                msg_list.append_notice(f"✗ Gagal membaca memory: {e}",
                                      style=f"bold {tema.p('exit_footer')}")
        elif parts[1] == "add" and len(parts) == 3:
            # Add memory
            try:
                result = longmem.add(parts[2])
                msg_list.append_notice(f"✓ {result}",
                                      style=f"bold {tema.p('aksen')}")
            except Exception as e:
                msg_list.append_notice(f"✗ Gagal menambah memory: {e}",
                                      style=f"bold {tema.p('exit_footer')}")
        elif parts[1] == "forget" and len(parts) == 3:
            # Forget memory
            try:
                result = longmem.remove(parts[2])
                msg_list.append_notice(f"✓ {result}",
                                      style=f"bold {tema.p('aksen')}")
            except Exception as e:
                msg_list.append_notice(f"✗ Gagal melupakan memory: {e}",
                                      style=f"bold {tema.p('exit_footer')}")
        else:
            msg_list.append_notice(
                "Penggunaan:\n"
                "  /memory list       — lihat semua memory\n"
                "  /memory add <teks> — tambah memory\n"
                "  /memory forget <id> — hapus memory",
                style=tema.p("redup")
            )

    # ─── Compact Command ───────────────────────────────────────────────

    def _cmd_compact(self):
        """Compact context — save conversation to memory file."""
        msg_list = self.query_one("#messages", MessageList)
        progress = self.query_one("#progress", TurnProgressBar)
        progress.show(0.0, "memadatkan ingatan...")

        def worker():
            try:
                result = self.agent.padatkan_sekarang()
                self._safe_call(self._compact_done, result)
            except Exception as exc:
                self._safe_call(self._compact_error, exc)

        import threading
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _compact_done(self, result: str):
        """Handle compact completion."""
        progress = self.query_one("#progress", TurnProgressBar)
        progress.hide()
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice("✓ Riwayat tersimpan.",
                              style=f"bold {tema.p('aksen')}")
        if result:
            msg_list.append_ai_message(result)

    def _compact_error(self, exc: Exception):
        """Handle compact error."""
        progress = self.query_one("#progress", TurnProgressBar)
        progress.hide()
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice(f"✗ Gagal memadatkan: {exc}",
                              style=f"bold {tema.p('exit_footer')}")

    # ─── Send Compact Command ──────────────────────────────────────────

    def _cmd_send_compact(self, text: str):
        """Send compacted memory to current conversation."""
        parts = text.split(maxsplit=1)
        msg_list = self.query_one("#messages", MessageList)
        arg = parts[1].strip() if len(parts) == 2 else ""

        try:
            self.agent.kirim_memory(arg)
            msg_list.append_notice("✓ Memory dikirim ke percakapan.",
                                  style=f"bold {tema.p('aksen')}")
        except Exception as e:
            msg_list.append_notice(f"✗ Gagal mengirim memory: {e}",
                                  style=f"bold {tema.p('exit_footer')}")

    # ─── Add Directory Command ─────────────────────────────────────────

    def _cmd_add_dir(self, text: str):
        """Add directory to context."""
        parts = text.split(maxsplit=1)
        msg_list = self.query_one("#messages", MessageList)

        if len(parts) == 2:
            path = parts[1].strip().strip('"').strip("'")
            try:
                p = workspace.add(path)
                self.agent.refresh_system_prompt()
                msg_list.append_notice(f"✓ Folder konteks ditambahkan: {path}",
                                      style=f"bold {tema.p('aksen')}")
            except ValueError as e:
                msg_list.append_notice(f"✗ {e}",
                                      style=f"bold {tema.p('exit_footer')}")
        else:
            # Show text prompt for path input
            async def on_input(result: str | None):
                if result:
                    path = result.strip().strip('"').strip("'")
                    if path:
                        try:
                            workspace.add(path)
                            self.agent.refresh_system_prompt()
                            ml = self.query_one("#messages", MessageList)
                            ml.append_notice(
                                f"✓ Folder konteks ditambahkan: {path}",
                                style=f"bold {tema.p('aksen')}"
                            )
                        except ValueError as e:
                            ml = self.query_one("#messages", MessageList)
                            ml.append_notice(f"✗ {e}",
                                            style=f"bold {tema.p('exit_footer')}")

            self.push_screen(TextPromptScreen(
                title="Path folder yang mau ditambahkan:",
                placeholder="/path/to/folder",
                hint="Masukkan path folder absolut",
            ), on_input)

    # ─── Dirs Command ──────────────────────────────────────────────────

    def _cmd_dirs(self):
        """List context directories."""
        msg_list = self.query_one("#messages", MessageList)
        try:
            dirs = workspace.list_dirs()
            if dirs:
                items = "\n".join(f"  📂 {d}" for d in dirs)
                msg_list.append_notice(
                    f"Folder konteks:\n{items}\n\n"
                    "Lepas dengan /rm-dir <path>.",
                    style=tema.p("aksen")
                )
            else:
                msg_list.append_notice(
                    "Belum ada folder konteks tambahan.\n"
                    "Ketik /add-dir <path> untuk menambah.",
                    style=tema.p("redup")
                )
        except Exception as e:
            msg_list.append_notice(f"✗ Error: {e}",
                                  style=f"bold {tema.p('exit_footer')}")

    # ─── Remove Directory Command ──────────────────────────────────────

    def _cmd_rm_dir(self, text: str):
        """Remove directory from context."""
        parts = text.split(maxsplit=1)
        msg_list = self.query_one("#messages", MessageList)

        if len(parts) == 2:
            path = parts[1].strip().strip('"').strip("'")
            try:
                if workspace.remove(path):
                    self.agent.refresh_system_prompt()
                    msg_list.append_notice(
                        f"✓ Folder konteks dilepas: {path}",
                        style=f"bold {tema.p('aksen')}"
                    )
                else:
                    msg_list.append_notice(
                        "Folder itu tidak ada di daftar konteks.",
                        style=tema.p("redup")
                    )
            except Exception as e:
                msg_list.append_notice(f"✗ {e}",
                                      style=f"bold {tema.p('exit_footer')}")
        else:
            msg_list.append_notice("Pakai: /rm-dir <path folder>",
                                  style=tema.p("redup"))

    # ─── Scan Command ──────────────────────────────────────────────────

    def _cmd_scan(self):
        """Refresh project map."""
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice("🔍 Menyegarkan peta proyek...",
                              style=tema.p("redup"))
        progress = self.query_one("#progress", TurnProgressBar)
        progress.show(0.0, "memindai proyek...")

        def kerja():
            # Pemindaian BLOKIR (baca banyak berkas) — jangan di thread UI.
            try:
                from .. import projectindex
                hasil = projectindex.ensure(force=True)
                self._safe_call(self._scan_selesai, hasil, None)
            except Exception as e:  # noqa: BLE001
                self._safe_call(self._scan_selesai, None, e)

        try:
            self.run_worker(kerja, thread=True, group="scan",
                            exclusive=True, exit_on_error=False)
        except Exception:  # noqa: BLE001
            progress.hide()

    def _scan_selesai(self, hasil: str | None, err: Exception | None):
        progress = self.query_one("#progress", TurnProgressBar)
        progress.hide()
        msg_list = self.query_one("#messages", MessageList)
        if err is not None:
            msg_list.append_notice(f"✗ Gagal menyegarkan: {err}",
                                  style=f"bold {tema.p('exit_footer')}")
            return
        self.agent.refresh_system_prompt()
        msg_list.append_notice("✓ Peta proyek disegarkan.",
                              style=f"bold {tema.p('aksen')}")

    # ─── Review Command ────────────────────────────────────────────────

    def _cmd_review(self):
        """Review project for bugs."""
        if self.is_turn_active:
            # Tanpa guard ini /review memulai worker KEDUA bersamaan
            # dengan giliran yang sedang berjalan — dua aliran token
            # bertabrakan di UI.
            msg_list = self.query_one("#messages", MessageList)
            msg_list.append_notice(
                "Tunggu giliran saat ini selesai dulu (ctrl+c untuk batalkan).",
                style=tema.p("redup"))
            return
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice("🔎 Mereview proyek untuk bug & kesalahan sistem...",
                              style=tema.p("redup"))
        # Start a turn with the review prompt
        self._start_turn(
            "Lakukan audit bug dan kesalahan sistem menyeluruh pada proyek ini. "
            "Periksa: error handling, race conditions, resource leaks, "
            "security issues, dan potensial bugs lainnya. "
            "Laporkan temuan dalam format yang terstruktur."
        )

    # ─── Web Command ───────────────────────────────────────────────────

    def _cmd_web(self, text: str):
        """Handle /web command — web session management."""
        parts = text.split(maxsplit=1)
        msg_list = self.query_one("#messages", MessageList)

        if self.agent.model_spec.is_web:
            msg_list.append_notice(
                "Web session management:\n"
                "  /web connect  — koneksi ke situs model\n"
                "  /web logout   — logout dari situs model\n"
                "  /web status   — lihat status sesi",
                style=tema.p("redup")
            )
        else:
            msg_list.append_notice(
                "Model saat ini bukan web model.\n"
                "Gunakan /model untuk beralih ke model web.",
                style=tema.p("redup")
            )

    # ─── Bot Command ───────────────────────────────────────────────────

    def _cmd_bot(self):
        """Toggle Telegram bot."""
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice(
            "Telegram bot:\n"
            "  /bot start   — nyalakan bot Telegram\n"
            "  /bot stop    — matikan bot Telegram\n"
            "  /bot status  — lihat status bot",
            style=tema.p("redup")
        )

    # ─── Permissions Bot Command ───────────────────────────────────────

    def _cmd_permissions_bot(self):
        """Manage bot permissions."""
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice(
            "Izin bot Telegram:\n"
            "  /permissions-bot list   — lihat izin\n"
            "  /permissions-bot add    — tambah izin\n"
            "  /permissions-bot remove — hapus izin",
            style=tema.p("redup")
        )

    # ─── Help Text ─────────────────────────────────────────────────────

    def _help_text(self) -> str:
        return """\
╔══════════════════════════════════════════════════╗
║           bagas-ai — Perintah                   ║
╠══════════════════════════════════════════════════╣
║ /model [nama]   Ganti model/varian              ║
║ /effort [level] Usaha berpikir (web & API)      ║
║ /theme [nama]   Ganti/lihat tema                ║
║ /live [on|off]  Screenshot layar tiap pertanyaan║
║ /video          Alias /live                     ║
║ /stream         Toggle tampilan mengalir        ║
║ /mic [on|off]   Bacakan kabar dan jawaban       ║
║ /voice tekan/on  Dikte / mikrofon hands-free     ║
║ /image <path>   Baca gambar lokal via Python     ║
║ /export [path]   Ekspor riwayat chat              ║
║ /btw <pesan>     Ngobrol tanpa ganggu tugas       ║
║ /compact        Compact context                 ║
║ /send-compact   Kirim memory ke percakapan      ║
║ /new            Sesi baru                       ║
║ /delete         Hapus sesi                      ║
║ /reset          Reset sesi                      ║
║ /memory [args]  Kelola memory                   ║
║ /add-dir [path] Tambah folder konteks           ║
║ /dirs           Lihat folder konteks            ║
║ /rm-dir <path>  Lepas folder konteks            ║
║ /scan           Segarkan peta proyek            ║
║ /review         Review proyek untuk bug         ║
║ /web [args]     Kelola sesi web                 ║
║ /bot [args]     Kelola bot Telegram             ║
║ /permissions-bot Kelola izin bot                ║
║ /tim            Tim review                      ║
║ /clear          Bersihkan layar                 ║
║ /version        Lihat versi                     ║
║ /help           Bantuan ini                     ║
║ /exit           Keluar                          ║
╚══════════════════════════════════════════════════╝"""

    # ─── AI Turn ───────────────────────────────────────────────────────

    def _animate_progress(self):
        """Animate progress bar during turn."""
        try:
            progress = self.query_one("#progress", TurnProgressBar)
            # ``phase`` adalah penghitung animasi yang TERUS berputar dan
            # tidak pernah mencapai 0.95 — memeriksa ``phase`` membuat
            # animasi berhenti setelah beberapa detik. Yang dimaksud di sini
            # adalah KEMAJUAN (``fraction``).
            if progress.display and progress.fraction < 0.95:
                progress.tick()
        except Exception:
            pass

    def _start_turn(self, text: str):
        """Start an AI turn in a worker thread."""
        self.is_turn_active = True
        # Event pembatal BARU untuk giliran ini. Kalau event-nya dibagi
        # (satu objek untuk semua giliran), ``clear()`` saat giliran baru
        # dimulai MEMBATALKAN pembatalan giliran lama yang belum selesai —
        # worker lama lanjut bekerja dan hasilnya muncul di giliran baru.
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        self._turn_id += 1
        turn_id = self._turn_id

        self.query_one("#chatbox", ChatBox).set_busy(True)
        progress = self.query_one("#progress", TurnProgressBar)
        progress.show(0.0, "menyiapkan model...")

        # Start progress bar animation
        self._progress_timer = self.set_interval(0.15, self._animate_progress)

        # Counter untuk progress tool steps
        self._tool_count = 0
        self._tool_total = 0

        # Reset thinking block
        thinking = self.query_one("#thinking-block", ThinkingBlock)
        thinking.clear()

        # Begin streaming in message list
        msg_list = self.query_one("#messages", MessageList)
        msg_list.begin_stream()

        def worker():
            attachments: list[str] = []
            try:
                attachments = self._capture_live_attachment()
                pertanyaan = text
                if attachments:
                    # Jalur murah lebih dulu: OCR/CV lokal tanpa inferensi LLM.
                    # Model vision lokal baru dipakai bila jawaban model utama
                    # secara eksplisit menyatakan tidak mampu membaca gambar.
                    from ..tools.image_local import read_image_local
                    self.agent_on_status("membaca teks…")
                    laporan_ocr = read_image_local(
                        attachments[0], ocr=True, vision=False,
                    )
                    pertanyaan += (
                        "\n\n[SISTEM] Data OCR dan pembacaan lokal dari layar "
                        "tersedia di bawah. Jawab dari data ini. Jika datanya "
                        "tidak cukup untuk menjawab, katakan persis 'tidak "
                        "dapat menganalisis gambar'.\n" + laporan_ocr
                    )
                result = self.agent.run(
                    pertanyaan,
                    on_tool=self.agent_on_tool,
                    on_message=self.agent_on_message,
                    on_tool_result=self.agent_on_result,
                    on_status=self.agent_on_status,
                    on_notice=self.agent_on_notice,
                    on_retry=self.agent_on_retry,
                    cancel_event=cancel_event,
                    on_token=self.agent_on_token,
                    on_reasoning=self.agent_on_reasoning,
                    ambil_sisipan=self._ambil_sisipan,
                    on_tim=self.agent_on_tim,
                    on_padat=self.agent_on_padat,
                    # Model utama menerima hasil OCR sebagai teks; gambar mentah
                    # tidak diunggah dan vision lokal belum dijalankan di sini.
                    attachments=[],
                )
                if attachments:
                    from ..tools.vision_local import (
                        VisionLocalError, describe_image,
                        response_needs_vision,
                    )
                    if response_needs_vision(result):
                        self.agent_on_status("sedang menganalisis…")
                        try:
                            result = describe_image(
                                Path(attachments[0]),
                                prompt=(
                                    "Jawab pertanyaan pengguna tentang gambar "
                                    f"ini secara faktual: {text[:1200]}"
                                ),
                                strict=True,
                            )
                        except VisionLocalError as exc:
                            self._safe_call(self._set_live_screen, False)
                            raise RuntimeError(
                                f"analisis lanjutan gagal: {exc}"
                            ) from exc
                        self.agent.replace_last_answer(result)
                        self.agent_on_notice("✓ analisis selesai.")
                self._safe_call(self._turn_complete, result, turn_id)
            except BaseException as exc:
                self._safe_call(self._turn_error, exc, turn_id)
            finally:
                if attachments:
                    try:
                        from ..tools.screen import clear_live_capture
                        clear_live_capture()
                    except Exception:  # noqa: BLE001 — cleanup best-effort
                        pass

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _stop_turn(self):
        """Stop giliran yang sedang berjalan — benar-benar berhenti.

        Dua hal yang dulu tidak dilakukan sehingga "dibatalkan tapi
        dilanjutkan":

        1. ``is_turn_active`` tidak di-reset — UI tetap menganggap giliran
           berjalan, pesan baru masuk antrean, dan antrean itu diproses
           saat worker yang "sudah dibatalkan" itu selesai.
        2. ``_turn_id`` tidak dinaikkan — ``_turn_complete`` dari giliran
           yang dibatalkan tetap diproses (turn_id masih cocok), jadi
           hasilnya dirender seolah-olah giliran tidak pernah dibatalkan.

        Pesan yang mengantre saat pembatalan MAJU sebagai giliran baru:
        pemrosesan antreannya ditunda sampai worker lama benar-benar mati
        (lihat _antre_maju_setelah_batal) — dua agent.run yang jalan
        bersamaan saling menginjak memory/connector.
        """
        self._cancel_event.set()
        try:
            from .. import suara
            suara.diam()
        except Exception:  # noqa: BLE001 — suara opsional
            pass
        # Naikkan turn_id: hasil/error dari worker lama kini "basi" dan
        # diabaikan oleh _turn_complete/_turn_error.
        self._turn_id += 1
        # Referensi worker HARUS dipegang SEBELUM _bersihkan_turn_ui —
        # ia meng-nol-kan _worker_thread, dan antrean hanya boleh maju
        # setelah worker ini benar-benar mati.
        wt = self._worker_thread
        self._bersihkan_turn_ui()
        self._antre_maju_setelah_batal(wt)

    def _antre_maju_setelah_batal(self, wt):
        """Jalankan pesan antrean begitu worker yang dibatalkan mati.

        Dulu antrean TERSANGKUT setelah Ctrl+C: _turn_error giliran lama
        diabaikan (turn_id basi, memang benar), dan _process_queue hanya
        dipanggil dari _turn_complete / pesan baru — jadi pesan yang sudah
        mengantre diam menunggu pengguna mengetik lagi. Sekarang begitu
        worker lama berhenti, antrean maju sebagai giliran baru, persis
        seperti giliran yang selesai normal.

        TAK BOLEH langsung _process_queue() di sini: worker lama mungkin
        masih hidup beberapa detik (menunggu network/tool menghormati
        cancel_event), dan agent.run kedua yang jalan paralel dengannya
        menginjak state agent yang sama. Bergabung lewat thread daemon,
        lalu lanjut di thread UI lewat _safe_call.
        """
        if wt is None or not wt.is_alive():
            # Worker sudah mati / tak pernah ada — antrean boleh maju langsung.
            self._process_queue()
            return

        def tunggu_lalu_maju():
            wt.join(timeout=30.0)
            self._safe_call(self._process_queue)

        threading.Thread(target=tunggu_lalu_maju, daemon=True).start()

    def _stop_progress_timer(self):
        """Hentikan timer animasi progress bila masih hidup.

        Dulu ``_turn_error`` TIDAK menghentikan timer — setiap error
        meninggalkan satu interval 0.15 dtk yang terus berjalan sampai
        aplikasi ditutup (bocor timer).
        """
        if self._progress_timer is not None:
            try:
                self._progress_timer.stop()
            except Exception:  # noqa: BLE001 — timer sudah mati
                pass
            self._progress_timer = None

    def _bersihkan_turn_ui(self):
        """Kembalikan UI ke keadaan idle setelah giliran selesai/gagal."""
        self.is_turn_active = False
        self._worker_thread = None
        self._stop_progress_timer()
        try:
            self.query_one("#progress", TurnProgressBar).hide()
            self.query_one("#streaming-preview", StreamingPreview).hide()
            self.query_one("#thinking-block", ThinkingBlock).hide()
            self.query_one("#chatbox", ChatBox).set_busy(False)
        except Exception:  # noqa: BLE001 — widget sedang dibongkar
            pass

    def _turn_complete(self, result: str, turn_id: int):
        """Called when AI turn completes."""
        # Ignore stale turns (user cancelled or new turn started)
        if turn_id != self._turn_id:
            return

        self._bersihkan_turn_ui()

        msg_list = self.query_one("#messages", MessageList)
        # End streaming — get accumulated text and render final markdown
        stream_text = msg_list.end_stream()

        # Use agent result if available, otherwise use streamed text
        final_text = result or stream_text
        if final_text:
            msg_list.append_ai_message(final_text)
            if prefs.load().get("suara", True):
                try:
                    from .. import suara
                    suara.getar()
                    suara.ucap(final_text, penuh=True)
                except Exception:  # noqa: BLE001 — TTS tak boleh rusak giliran
                    pass
        else:
            msg_list.append_notice("(no response)", style=tema.p("redup"))

        # Process queued commands that were sent during turn
        self._process_queue()

        # Re-focus chatbox
        self.query_one("#chatbox", ChatBox).focus()

    def _perbarui_strip_antre(self) -> None:
        """Selaraskan QueueStrip dengan isi antrean saat ini."""
        with self._antre_lock:
            isi = list(self._prompt_queue)
        try:
            strip = self.query_one("#queue-strip", QueueStrip)
            strip.set_items(isi)
        except Exception:  # noqa: BLE001 — UI belum siap / sedang ditutup
            pass

    def _process_queue(self):
        """Process queued messages after turn completes.

        Batches consecutive non-command messages into a single turn.
        Slash commands are executed immediately.
        Messages were NOT echoed when queued — they live in QueueStrip —
        so echo them here, as normal (non-disabled) user messages.
        """
        with self._antre_lock:
            if not self._prompt_queue:
                return

            # Separate commands from non-commands
            commands = []
            prompts = []
            for item in self._prompt_queue:
                if item.startswith("/"):
                    commands.append(item)
                else:
                    prompts.append(item)
            self._prompt_queue.clear()

        # Execute commands (echoed by _handle_command flow)
        for cmd in commands:
            self._handle_command(cmd)

        # Echo queued prompts as NORMAL user messages — kini benar-benar
        # dijalankan: posisinya masuk riwayat, warnanya tak disabled lagi.
        msg_list = self.query_one("#messages", MessageList)
        for p in prompts:
            msg_list.append_user_message(p)
        self._perbarui_strip_antre()

        # Batch non-command messages into single turn
        if prompts:
            batched = "\n".join(prompts)
            self._start_turn(batched)

    def _turn_error(self, exc: BaseException, turn_id: int):
        """Called when AI turn errors."""
        if turn_id != self._turn_id:
            return

        self._bersihkan_turn_ui()
        try:
            from .. import suara
            suara.diam()
        except Exception:  # noqa: BLE001 — suara opsional
            pass

        msg_list = self.query_one("#messages", MessageList)
        if isinstance(exc, KeyboardInterrupt):
            msg_list.append_notice("⚠ Dibatalkan oleh pengguna.",
                                  style=f"bold {tema.p('exit_footer')}")
        else:
            try:
                from ..llm import ProviderQuotaError
                pesan = (str(exc) if isinstance(exc, ProviderQuotaError)
                         else f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 — formatter tak boleh tutup UI
                pesan = f"{type(exc).__name__}: {exc}"
            msg_list.append_notice(
                f"⚠ {pesan}",
                style=f"bold {tema.p('exit_footer')}"
            )
        self.query_one("#chatbox", ChatBox).focus()

    # ─── Agent Callbacks (called from worker thread) ───────────────────

    def agent_on_token(self, piece: str):
        """Streaming token from API model — forward to MessageList."""
        if self._tui_mode:
            self._safe_call(self._forward_token, piece)

    def _forward_token(self, piece: str):
        """Forward token to message list for streaming display.

        Called from main thread via call_from_thread.
        Appends token then updates the streaming preview widget.
        """
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_token(piece)
        # Read stream tail and length atomically (single lock acquisition)
        with msg_list._stream_lock:
            buf_len = len(msg_list._stream_buf)
            text = msg_list._stream_buf[-600:] if buf_len > 0 else ""
        # Update streaming preview with accumulated text
        preview = self.query_one("#streaming-preview", StreamingPreview)
        if buf_len > 0:
            preview.update_preview(text)
        # Update progress bar
        if self.is_turn_active:
            progress = self.query_one("#progress", TurnProgressBar)
            if buf_len > 0:
                progress.update_progress(
                    min(0.9, buf_len / 500),
                    f"menjawab... ({buf_len} chars)"
                )

    def agent_on_reasoning(self, piece: str):
        """Reasoning token from API model — show in thinking block."""
        self._safe_call(self._forward_reasoning, piece)

    def _forward_reasoning(self, piece: str):
        """Forward reasoning to thinking block widget."""
        if not piece or not piece.strip():
            return
        thinking = self.query_one("#thinking-block", ThinkingBlock)
        thinking.append_thinking(piece)  # Use public method
        # Update progress
        if self.is_turn_active:
            progress = self.query_one("#progress", TurnProgressBar)
            progress.update_progress(0.0, "berpikir...")

    def agent_on_tool(self, name: str, args: dict):
        """Tool about to execute — store args keyed by name."""
        if not hasattr(self, '_pending_tool_args'):
            self._pending_tool_args = {}
        self._pending_tool_args[name] = args
        self._safe_call(self._show_tool_start, name, args)

    def _show_tool_start(self, name: str, args: dict):
        progress = self.query_one("#progress", TurnProgressBar)
        progress.update_progress(0.0, f"⚙ {name}")
        # Pratinjau perubahan file ditampilkan SEBELUM aksi — persis pola
        # cli.py.on_tool: write_file => blok write() ringkas; edit_file/
        # edit_files => diff berwarna; delete_file => isi yang akan hilang.
        # Di sini (thread UI) file pun BELUM tersentuh, jadi isi lamanya
        # masih bisa dibaca untuk di-diff.
        self._pratinjau_file(name, args)

    # Tool tulis/ubah yang pratinjaunya berupa diff/blok (selaras
    # _TOOL_DIFF di cli.py).
    _TOOL_DIFF = ("write_file", "edit_file", "edit_files", "append_file")

    def _pratinjau_file(self, name: str, args: dict) -> None:
        """Diff / blok write() sebelum tool mengubah isi disk.

        Kalau prediksinya "tak akan berubah apa pun" (old == new pada file
        yang ada), jangan tampilkan apa pun — tool-nya akan menolak dan
        pesan galatnya yang tampil sebagai hasil langkah.
        """
        if not isinstance(args, dict):
            return
        try:
            from ..tools.files import _safe_path
        except Exception:  # noqa: BLE001 — modul belum siap
            return
        msg_list = None
        try:
            msg_list = self.query_one("#messages", MessageList)
        except Exception:  # noqa: BLE001 — widget sedang dibongkar
            return

        def proses(path, sub_args, nama_tool):
            try:
                target = _safe_path(path)
                ada = target.is_file()
                lama = (target.read_text(encoding="utf-8",
                                         errors="replace") if ada else "")
            except Exception:  # noqa: BLE001 — baca gagal: tanpa pratinjau
                return
            if nama_tool == "write_file":
                if ada and lama == (sub_args.get("content") or ""):
                    return  # tak akan berubah
                msg_list.append_write_block(
                    path, sub_args.get("content") or "", is_new=not ada)
                self._catat_diff_memory(path, lama,
                                        sub_args.get("content") or "",
                                        not ada)
                return
            # edit_file / append_file / suntingan satuan edit_files.
            baru = self._hitung_sesudah(nama_tool, lama, sub_args)
            if ada and lama == baru:
                return  # akan ditolak/tanpa efek — jangan menyesatkan
            msg_list.append_diff(path, lama, baru, is_new=not ada)
            self._catat_diff_memory(path, lama, baru, not ada)

        try:
            if name == "edit_files":
                for e in (args.get("edits") or []):
                    if isinstance(e, dict) and e.get("path"):
                        proses(e["path"], e, "edit_file")
            elif name in self._TOOL_DIFF and args.get("path"):
                proses(args["path"], args, name)
        except Exception:  # noqa: BLE001 — pratinjau tak boleh mematikan UI
            pass

    @staticmethod
    def _hitung_sesudah(name: str, lama: str, args: dict) -> str:
        """Isi file SETELAH tool diterapkan — simulasi ringkas isi
        cli._isi_sebelum_sesudah (cukup untuk pratinjau diff; kecocokan
        longgar dsb. tetap urusan tool-nya)."""
        if name == "append_file":
            return lama + (args.get("content") or "")
        if name == "edit_file":
            cari = args.get("old_text") or ""
            if not cari or cari not in lama:
                return lama  # akan ditolak — tak ada diff
            jml = args.get("count", 1)
            try:
                jml = int(jml)
            except (TypeError, ValueError):
                jml = 1
            n = lama.count(cari) if jml == -1 else jml
            return lama.replace(cari, args.get("new_text") or "", n)
        return lama

    def _catat_diff_memory(self, path: str, lama: str, baru: str,
                           is_new: bool) -> None:
        """Simpan pratinjau diff ke memory agar --resume tetap memilikinya."""
        try:
            import difflib
            ag = self.agent
            if ag is None or not hasattr(ag, "memory"):
                return
            d = list(difflib.unified_diff(lama.splitlines(), baru.splitlines(),
                                          lineterm="", n=2))
            if len(d) >= 2 and d[0].startswith("---"):
                d = d[2:]
            if len(d) > 400:
                d = d[:400] + ["… (diff tersimpan dipangkas)"]
            ag.memory.add_diff(path, "\n".join(d), is_new=is_new)
        except Exception:  # noqa: BLE001 — memory opsional
            pass

    def agent_on_result(self, name: str, result: str):
        """Tool finished — retrieve stored args by name."""
        if not hasattr(self, '_pending_tool_args'):
            self._pending_tool_args = {}
        args = self._pending_tool_args.pop(name, {})
        self._safe_call(self._show_tool_result, name, result, args)

    def _show_tool_result(self, name: str, result: str, args: dict = None):
        # Pratinjau diff/blok write() sudah ditampilkan di _show_tool_start
        # (sebelum file tersentuh); hasil langkah cukup baris jejak ringkas.
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_tool_step(name, args or {}, result[:200])
        progress = self.query_one("#progress", TurnProgressBar)
        progress.tick()

    def agent_on_message(self, content: str):
        """AI narration before tool call."""
        if prefs.load().get("suara", True):
            try:
                from .. import suara
                suara.ucap(content)
            except Exception:  # noqa: BLE001 — TTS tak boleh rusak callback
                pass
        self._safe_call(self._show_narration, content)

    def _show_narration(self, content: str):
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice(content, style=tema.p("aksen_terang"))

    def agent_on_status(self, msg: str):
        """Phase status update — process through _fase_status."""
        self._safe_call(self._show_status, msg)

    def _show_status(self, msg: str):
        progress = self.query_one("#progress", TurnProgressBar)
        if self.is_turn_active:
            # Simplify verbose status messages
            clean = self._fase_status(msg)
            progress.show(progress.fraction, clean)

    @staticmethod
    def _fase_status(msg: str) -> str:
        """Map verbose status strings to clean phase labels."""
        lower = msg.lower()
        if "menyiapkan" in lower or "prepare" in lower:
            return "menyiapkan model..."
        if "menunggu" in lower or "waiting" in lower:
            return "menunggu model..."
        if "berpikir" in lower or "think" in lower or "pikir" in lower:
            return "berpikir..."
        if "menjawab" in lower or "answer" in lower or "jawab" in lower:
            return "menjawab..."
        if "tool" in lower or "langkah" in lower:
            return "menjalankan tool..."
        if "retry" in lower or "ulang" in lower:
            return f"🔄 retry..."
        if "quota" in lower:
            return "⚠ quota habis..."
        if "busy" in lower or "sibuk" in lower:
            return "model sibuk..."
        return msg[:40] + ("..." if len(msg) > 40 else "")

    def agent_on_notice(self, msg: str):
        """System notice."""
        self._safe_call(self._show_notice, msg)

    def _show_notice(self, msg: str):
        msg_list = self.query_one("#messages", MessageList)
        # append_notice sudah meng-indentasi SETIAP barisnya; menambah
        # "  " di sini membuat baris pertama menjorok dua kali.
        msg_list.append_notice(msg)

    def agent_on_retry(self, attempt: int, wait: float, exc: Exception):
        """Retry notification."""
        self._safe_call(self._show_retry, attempt, wait)

    def _show_retry(self, attempt: int, wait: float):
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_notice(
            f"🔄 Retry {attempt} — menunggu {wait:.0f}s...",
            style=tema.p("redup")
        )

    def _ambil_sisipan(self) -> list[str]:
        """Pull queued messages for insertion into current turn."""
        with self._antre_lock:
            if not self._prompt_queue:
                return []
            diambil = [t for t in self._prompt_queue
                       if not t.startswith("/")]
            if diambil:
                self._prompt_queue[:] = [
                    t for t in self._prompt_queue if t.startswith("/")
                ]
        if diambil:
            # Dipanggil dari thread pekerja — tulis lewat _safe_call.
            # Prompt yang disisipkan kini BENAR-BENAR berjalan: echo
            # sebagai pesan pengguna normal (tak lagi disabled) dan
            # selaraskan strip antrean.
            def jalankan() -> None:
                try:
                    msg_list = self.query_one("#messages", MessageList)
                    for t in diambil:
                        msg_list.append_user_message(t)
                    self._perbarui_strip_antre()
                except Exception:  # noqa: BLE001 — UI sedang ditutup
                    pass
            self._safe_call(jalankan)
        return diambil

    def agent_on_tim(self, names: list[str]):
        """Team review notification."""
        self._safe_call(self._show_tim, names)

    def _show_tim(self, names: list[str]):
        if names:
            msg_list = self.query_one("#messages", MessageList)
            msg_list.append_notice(
                f"Tim: {', '.join(names[:5])}",
                style=tema.p("redup")
            )

    def agent_on_padat(self, progress: float, msg: str):
        """Memory compaction progress."""
        self._safe_call(self._show_padat, progress, msg)

    def _show_padat(self, progress: float, msg: str):
        prog = self.query_one("#progress", TurnProgressBar)
        if self.is_turn_active:
            prog.show(progress, f"📦 {msg}")

    # ─── Key Bindings ──────────────────────────────────────────────────

    def action_cancel(self):
        """Handle Ctrl+C globally."""
        if self.is_turn_active:
            self._cancel_event.set()
            self._stop_turn()
            self.query_one("#messages", MessageList).append_notice(
                "⚠ Dibatalkan.",
                style=f"bold {tema.p('exit_footer')}"
            )
        else:
            # Persist session before exit
            if hasattr(self.agent, '_persist'):
                self.agent._persist()
            self.exit()

    def action_eof(self):
        """Handle Ctrl+D."""
        # Persist session before exit
        if hasattr(self.agent, '_persist'):
            self.agent._persist()
        self.exit()

    def action_clear(self):
        """Handle Ctrl+L — clear messages."""
        self.query_one("#messages", MessageList).clear_messages()

    def action_toggle_thinking(self):
        """Toggle thinking block (dipanggil via App khusus; Tab ditangani
        ChatBox untuk autocomplete, jadi ini biasanya lewat klik)."""
        thinking = self.query_one("#thinking-block", ThinkingBlock)
        thinking.toggle()

    def action_delete_word(self):
        """Handle Ctrl+W — delete previous word at cursor position."""
        chatbox = self.query_one("#chatbox", ChatBox)
        chatbox.delete_word_before_cursor()

    def action_voice_dictation(self):
        """F4 memulai/mengakhiri dikte langsung tanpa wake word."""
        self._toggle_dictation()

    # ─── Resize ────────────────────────────────────────────────────────

    def on_resize(self, event: events.Resize):
        """Ganti kelas responsif saat terminal berubah.

        Kelas -sempit/-pendek/-lebar sudah dipasang otomatis oleh
        HORIZONTAL/VERTICAL_BREAKPOINTS; tugas kita memastikan panel rencana
        ikut runtuh/tegang sesuai tinggi baru, dan memindahkan rencana
        antara sidebar kanan (layar lebar) dan footer (layar sempit).
        """
        try:
            plan = self.query_one("#plan", PlanPanel)
            plan.check_collapse(event.size.height)
        except Exception:  # noqa: BLE001 — panel opsional
            pass
        # event.size, BUKAN self.size: lihat catatan di _perbarui_layout_plan.
        self._perbarui_layout_plan(
            event.size.width >= self._LEBAR_MIN,
            lebar_layar=event.size.width,
        )

    # ─── Image Preview ─────────────────────────────────────────────────

    def show_image_preview(self, pixel_data: list, title: str = "Preview"):
        """Show image preview from paste or tool result."""
        preview = self.query_one("#image-preview", ImagePreview)
        preview.show_image(pixel_data, title)

    def hide_image_preview(self):
        """Hide image preview."""
        self.query_one("#image-preview", ImagePreview).hide()

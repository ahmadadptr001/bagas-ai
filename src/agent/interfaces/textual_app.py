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
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Vertical
from textual.reactive import reactive

from .textual_widgets import (
    StatusBar, ChatBox, MessageList, PlanPanel, PlanSidebar,
    ImagePreview, TurnProgressBar, LogoWidget, StreamingPreview,
    ThinkingBlock, SelectScreen, MultiSelectScreen, ConfirmScreen,
    TextPromptScreen, ThemeScreen, QueueStrip,
)
from ..ui.textual_theme import generate_css, variabel as variabel_tema
from ..ui import tema
from .. import interaction
from .. import session as session_mod
from ..session import Session
from .. import workspace, longmem

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
    Layar LEBAR (≥ _LEBAR_MIN kolom, "dashboard"): PlanPanel diganti
    PlanSidebar yang di-dock di KANAN — rencana tak lagi memakan tinggi
    footer. Lihat _perbarui_layout_plan.
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
        self._voice_state: dict = {}
        self._pending_gambar: dict = {}
        self._tui_mode = True
        self._first_idle = True
        self._worker_thread: threading.Thread | None = None
        self._turn_id = 0
        self._pending_tool_args: dict[str, dict] = {}
        self._progress_timer = None
        # Snapshot rencana terakhir (lihat _poll_plan) — dipakai untuk
        # menggambar ulang panel saat layout berpindah sidebar <-> footer.
        self._plan_cache: list[dict] = []
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
        # Sidebar rencana versi desktop: dock KANAN, aktif saat terminal
        # cukup lebar (kelas -lebar). Di bawah itu ia disembunyikan dan
        # rencana tampil inline sebagai PlanPanel di footer — persis
        # perilaku lama (lihat _perbarui_layout_plan).
        yield PlanSidebar(id="plan-sidebar")
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

        # Start periodic refresh for status bar
        self.set_interval(2.0, self._refresh_status)

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

    # ─── Rencana (plan / plan_step) ───────────────────────────────────

    def _poll_plan(self):
        """Baca snapshot plan_tool dan tampilkan ke panel yang benar.

        Dijalankan tiap 0.3 dtk di thread UI. Snapshot dibandingkan dengan
        cache supaya panel tidak digambar ulang saat tidak ada perubahan.
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
            return
        self._plan_cache = steps
        self._perbarui_layout_plan()

    def _perbarui_layout_plan(self, lebar: bool | None = None):
        """Pindahkan rencana antara sidebar kanan (lebar) dan footer (sempit).

        ``lebar`` biasanya dihitung dari ukuran app — TAPI saat dipanggil
        dari ``on_resize`` nilainya harus dari ``event.size``: dispatch MRO
        menjalankan handler kita SEBELUM ``App._on_resize`` memperbarui
        ``self._size``, jadi ``self.size`` masih lebar LAMA di situ.
        Kelas Screen tidak dipakai karena hanya melekat pada screen AKTIF —
        saat modal terbuka, screen-nya modal itu dan kelasnya kosong,
        padahal terminal tetap selebar itu.
        """
        try:
            plan = self.query_one("#plan", PlanPanel)
            sidebar = self.query_one("#plan-sidebar", PlanSidebar)
        except Exception:  # noqa: BLE001 — widget sedang dibongkar
            return
        if lebar is None:
            try:
                lebar = self.size.width >= self._LEBAR_MIN
            except Exception:  # noqa: BLE001
                lebar = False
        steps = self._plan_cache
        if not steps:
            plan.clear()
            sidebar.clear()
            sidebar.display = False
            return
        if lebar:
            sidebar.update_plan(steps)
            sidebar.display = True
            plan.clear()  # sembunyikan versi footer — jangan dobel
        else:
            sidebar.display = False
            sidebar.clear()
            plan.update_plan(steps)
            try:
                plan.check_collapse(self.size.height)
            except Exception:  # noqa: BLE001
                pass

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
        msg_list.append_notice(
            "Selamat datang di bagas-ai! Ketik pesan atau /help untuk bantuan.",
            style=f"italic {tema.p('aksen_terang')}"
        )
        msg_list.append_notice(
            f"Model: {self.agent.model_spec.label} "
            f"({'🌐 web' if self.agent.model_spec.is_web else '🤖 api'})",
            style=tema.p("redup")
        )

    def _refresh_status(self):
        """Periodic status bar refresh."""
        self.query_one("#statusbar", StatusBar).refresh()

    # ─── Message Handling ──────────────────────────────────────────────

    def on_chatbox_submitted(self, event: ChatBox.Submitted):
        """Handle user message submission."""
        text = event.text.strip()
        if not text:
            return

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
            msg_list.append_user_message(text)
            self._handle_command(text)
            return

        # Catat ke riwayat masukan (panah-atas). Perintah "/" tak masuk —
        # riwayat ini untuk teks yang mau diedit-ulang, bukan menu.
        self._riwayat_masukan.append(text)
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

        msg_list.append_user_message(text)
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

        # ── Model & Effort ─────────────────────────────────────────────
        elif cmd == "model" or cmd.startswith("model "):
            self._cmd_model(text)
        elif cmd == "effort" or cmd.startswith("effort "):
            self._cmd_effort(text)

        # ── Theme ──────────────────────────────────────────────────────
        elif cmd == "theme" or cmd.startswith("theme "):
            self._cmd_theme(text)

        # ── Display ────────────────────────────────────────────────────
        elif cmd == "live":
            self._tui_mode = not self._tui_mode
            if self._tui_mode:
                msg_list.append_notice("✓ Tampilan mengalir AKTIF",
                                      style=f"bold {tema.p('aksen')}")
            else:
                msg_list.append_notice("○ Tampilan mengalir MATI",
                                      style=tema.p("redup"))

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
        """Menu pilihan model — NAMA MODEL SUNGGUHAN, bukan nama umum.

        Tiap layanan web memuai jadi tiap variannya ("GLM-5.2", "K2.6",
        "Qwen3.8-Max") dari web_models connectornya; model API tampil
        apa adanya."""
        from .. import models as models_mod

        try:
            model_list = models_mod.pilihan_model()
        except Exception:  # noqa: BLE001 — katalog gagal dimuat
            model_list = []
        if not model_list:
            model_list = [self.agent.model_spec.label]
        current = self.agent.model_spec.label
        if getattr(self.agent, "_web_varian", None):
            current += f" · {self.agent._web_varian}"

        async def on_select(result: str | None):
            self._setelah_ganti_model(result)

        self.push_screen(SelectScreen(
            title=f"Model (saat ini: {current})",
            options=model_list,
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
║ /live           Toggle tampilan mengalir        ║
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
            try:
                result = self.agent.run(
                    text,
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
                )
                self._safe_call(self._turn_complete, result, turn_id)
            except BaseException as exc:
                self._safe_call(self._turn_error, exc, turn_id)

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
        """
        self._cancel_event.set()
        # Naikkan turn_id: hasil/error dari worker lama kini "basi" dan
        # diabaikan oleh _turn_complete/_turn_error.
        self._turn_id += 1
        self._bersihkan_turn_ui()

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

        msg_list = self.query_one("#messages", MessageList)
        if isinstance(exc, KeyboardInterrupt):
            msg_list.append_notice("⚠ Dibatalkan oleh pengguna.",
                                  style=f"bold {tema.p('exit_footer')}")
        else:
            msg_list.append_notice(
                f"⚠ Error: {type(exc).__name__}: {exc}",
                style=f"bold {tema.p('exit_footer')}"
            )
        self.query_one("#chatbox", ChatBox).focus()

    # ─── Agent Callbacks (called from worker thread) ───────────────────

    def agent_on_token(self, piece: str):
        """Streaming token from API model — forward to MessageList."""
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

    def agent_on_result(self, name: str, result: str):
        """Tool finished — retrieve stored args by name."""
        if not hasattr(self, '_pending_tool_args'):
            self._pending_tool_args = {}
        args = self._pending_tool_args.pop(name, {})
        self._safe_call(self._show_tool_result, name, result, args)

    def _show_tool_result(self, name: str, result: str, args: dict = None):
        msg_list = self.query_one("#messages", MessageList)
        msg_list.append_tool_step(name, args or {}, result[:200])
        progress = self.query_one("#progress", TurnProgressBar)
        progress.tick()

    def agent_on_message(self, content: str):
        """AI narration before tool call."""
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
        self._perbarui_layout_plan(event.size.width >= self._LEBAR_MIN)

    # ─── Image Preview ─────────────────────────────────────────────────

    def show_image_preview(self, pixel_data: list, title: str = "Preview"):
        """Show image preview from paste or tool result."""
        preview = self.query_one("#image-preview", ImagePreview)
        preview.show_image(pixel_data, title)

    def hide_image_preview(self):
        """Hide image preview."""
        self.query_one("#image-preview", ImagePreview).hide()

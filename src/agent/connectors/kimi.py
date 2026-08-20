"""Connector kimi.ai (web) — Kimi (Moonshot AI).

Selektor di bawah DIPETAKAN LANGSUNG ke www.kimi.ai pada sesi nyata yang sudah
login: pesan uji dikirim, DOM-nya dibaca, jumlah elemen & perubahan kelas selama
streaming DIUKUR, lalu hasil bacaan kode produksi dibandingkan dengan isi
halaman. Yang belum terverifikasi ditandai eksplisit di komentarnya — jangan
dianggap sudah terbukti.

Satu artefak kecil yang MASIH ADA: tabel dirender dengan label "Table  Copy" di
atasnya, dan potongan itu ikut terbawa ke jawaban. Kelas pembungkusnya belum
dipetakan, jadi sengaja TIDAK ditebak-tebak di strip_selectors.

Kalau layout situs berubah & jawaban tak lagi terbaca, cukup sesuaikan selektor
DI FILE INI; sisanya (kirim, tunggu, streaming, lampiran) ditangani base.py.
"""
from __future__ import annotations

from typing import Any

from .base import WebConnector
from .browser import BrowserError


class KimiConnector(WebConnector):
    service = "kimi"
    label = "Kimi (web)"
    chat_url = "https://www.kimi.ai/"
    # DIVERIFIKASI live: URL percakapan berbentuk
    #   https://www.kimi.ai/chat/19f846c6-95e2-8d84-8000-09ac082f8780?chat_enter_method=home
    # Pola di bawah membacanya utuh dan berhenti tepat sebelum `?` (tanda tanya
    # tak masuk kelas karakter), jadi lanjut-chat & --resume bekerja.
    chat_url_template = "https://www.kimi.ai/chat/{id}"
    chat_id_pattern = r"/chat/([A-Za-z0-9_-]{8,})"

    # Jendela Kimi berjalan DI LATAR (tersembunyi) seperti connector lain: yang
    # dipakai pengguna cuma hasil akhir yang sudah disaring, bukan proses kerjanya
    # (blok berpikir, langkah pencarian, ketikan bertahap). Dulu sempat dibuat
    # terlihat agar prosesnya bisa diamati; itu tak lagi diperlukan.
    #
    # Ingin melihat prosesnya lagi? Set CONNECTOR_SHOW=true di .env (memaksa semua
    # connector terlihat), atau ubah baris ini jadi True.
    show_window = False

    # --- input ---
    # DIVERIFIKASI: kotak inputnya <div class="chat-input-editor"> berukuran
    # ~734x60, contenteditable — BUKAN <textarea>. Kandidat berikutnya sengaja
    # makin longgar sebagai cadangan bila situs mengganti nama kelasnya.
    input_selector = (
        ".chat-input-editor",
        'div[contenteditable="true"][data-testid*="chat-input"]',
        'div[contenteditable="true"]',
        "textarea",
    )
    input_is_contenteditable = True
    submit_key = "Enter"
    # DIVERIFIKASI dari klik pengguna: tombol kirim = `.send-button-container`
    # di dalam `.chat-editor-action > .right-area`. Enter bisa gagal mengirim
    # saat ada lampiran (persis kasus Qwen), jadi tombol ini dipakai _submit()
    # sebagai cadangan sesudah kotak input terbukti belum kosong.
    send_button_selector = '.send-button-container, [class*="send-button"]'
    # DIVERIFIKASI live: tombol chat baru ada di sidebar. Dipakai agar memulai
    # percakapan baru tak perlu memuat ulang seluruh SPA.
    #
    # WAJIB tuple, jangan disatukan jadi satu string berkoma. TERUKUR di halaman
    # aslinya, ketiganya cocok ke elemen BERBEDA:
    #   .new-chat-btn            -> <a class="new-chat-btn">   "New Chat Ctrl K"
    #   .sidebar-new-chat        -> <div class="sidebar-new-chat"> (pembungkusnya)
    #   [aria-label="New Chat"]  -> <a class="logo">            LOGO SITUS
    # Dalam satu daftar berkoma, `.first` mengambil yang paling awal di DOM —
    # bisa jadi logonya. Sebagai tuple, tombol aslinya yang dicoba lebih dulu.
    new_chat_selector = (
        ".new-chat-btn",
        ".sidebar-new-chat",
        '[aria-label="New Chat"]',
    )

    # --- deteksi belum-login ---
    # TERUKUR: sebagai TAMU, Kimi tetap menampilkan `.chat-input-editor` yang
    # terlihat penuh — persis perangkap yang dulu ditemukan di Qwen. Jadi "kotak
    # input terlihat" BUKAN bukti sudah login; tombol "Log In"-lah penandanya.
    #
    # `:has-text()` Playwright mencocokkan SUBSTRING dan ikut membaca teks
    # keturunan, jadi pola longgar bisa salah tangkap — saat memetakan halaman
    # ini, pencocokan teks naif bahkan tertipu oleh isi tag <style> yang memuat
    # kata "Log In". Karena itu daftar di bawah dijaga tetap spesifik.
    logged_out_selector = (
        'button:has-text("Log In"), button:has-text("Log in"), '
        'button:has-text("Sign in"), button:has-text("登录")'
    )
    # BUKTI POSITIF sudah login — dipanen dengan MEMBANDINGKAN sensus tombol
    # halaman tamu vs halaman sesudah login pada sesi nyata. Yang muncul hanya
    # setelah login: `.user-profile-trigger` (tombol profil, berisi nama akun),
    # `.next-sidebar-section__title` ("Chats"), `.next-sidebar-history-item__more`,
    # `.membership-upgrade` ("Upgrade").
    #
    # Dipilih `.user-profile-trigger` karena tak bergantung keadaan lain: item
    # riwayat hanya ada bila sudah pernah chat, dan tombol Upgrade bisa hilang
    # pada akun berbayar. Ini yang menutup jendela rawan 4,2 detik — lihat
    # logged_in_selector di base.py.
    logged_in_selector = ".user-profile-trigger"

    # --- jawaban ---
    # SELEKTOR KELAS (token utuh), BUKAN `[class*=...]`. Ini bukan selera:
    # DIUKUR pada percakapan nyata berisi 3 jawaban asisten —
    #   [class*="segment-assistant"] -> 9 elemen
    #   .segment-assistant           -> 3 elemen  (tepat satu per jawaban)
    #   [class*="markdown"]          -> 12 elemen
    #   .markdown                    -> 4 elemen
    # Pencocokan SUBSTRING ikut menangkap `segment-assistant-actions-content`
    # (bilah tombol di bawah tiap jawaban), yang letaknya SESUDAH isi jawaban.
    # Pada jawaban yang punya tombol "Reference", bilah itu TIDAK kosong,
    # sehingga aturan "ambil kecocokan terakhir yang ada isinya" pun memilih
    # bilahnya: jawaban 2.020 karakter TERUKUR terbaca sebagai "Reference"
    # saja — 9 karakter. Dengan token kelas, jawaban yang sama terbaca utuh.
    #
    # Kandidat longgar tetap disimpan di urutan belakang sebagai jaring pengaman
    # bila situs mengganti nama kelasnya; selama yang pertama cocok, ia tak
    # pernah terpakai.
    message_selector = (
        ".segment-assistant",
        ".markdown",
        ".chat-content",
        '[class*="segment-assistant"]',
        "[class*='assistant']",
    )
    read_as_markdown = True
    # DIUKUR selama satu jawaban 5.954 karakter (158 sampel berturut-turut):
    # tombol kirim BERUBAH jadi `.send-button-container.disabled.stop` selagi
    # Kimi menulis, dan kelas `stop` hilang begitu selesai.
    #
    # Penanda ini WAJIB ada. Tanpanya `_is_done()` selalu True sehingga penantian
    # hanya bersandar pada "teks berhenti berubah" — dan itu TERUKUR salah: di
    # awal streaming panjang teks sempat melompat 249 -> 9 karakter (DOM dirender
    # ulang), sehingga jawaban dipotong pada 107 karakter, putus di tengah kata.
    stop_selectors = (
        ".send-button-container.stop",
        '[class*="send-button-container"][class*="stop"]',
    )
    # Saat masih berpikir, satu-satunya teks yang muncul adalah indikator ini.
    noise_pattern = r"(?:(?:Thinking|Berpikir|思考)[^\n]*\s*)+"
    # Bagian yang ADA DI DALAM wadah jawaban tapi BUKAN jawaban. DIPETAKAN dari
    # DOM sungguhan, bukan ditebak dari nama:
    #
    #   toolcall-container thinking-container   -> blok BERPIKIR (mode reasoning)
    #   toolcall-container toolcall-web_search  -> blok PENCARIAN WEB
    #       ("Search / <kueri> / 5 results" + daftar rujukan)
    #   upgrade-membership                      -> banner promo yang DISISIPKAN
    #       ke dalam jawaban, mis. "High demand. Switched to K2.6 Instant for
    #       speed. Upgrade to use K2.6 Thinking."
    #   segment-assistant-actions*              -> bilah tombol (Copy, Reference)
    #
    # Keduanya yang pertama sekeluarga `toolcall-container`, jadi SATU pola
    # menutup dua kebocoran sekaligus. TERUKUR pada jawaban 2.517 karakter:
    # sesudah disaring tinggal 2.060 karakter dan ketiga jejak khas blok
    # pencarian ("… logo simbol", "5 results", "Reference") hilang seluruhnya
    # sementara tabel & isi jawabannya utuh. Pada jawaban mode berpikir, 976
    # karakter menyusut jadi 118 — sisanya memang penalaran.
    #
    # Ini lebih dari sekadar kerapian: pada jalur agent, blok [[TOOL]] yang cuma
    # DIRENCANAKAN di dalam proses berpikir bisa ikut terbaca lalu BENAR-BENAR
    # dieksekusi (lihat JS_CODE_BLOCKS di base.py).
    #
    # Semua menargetkan CLASS, bukan teks, agar jawaban biasa tak salah terbuang;
    # base.py juga punya pengaman: penyaringan yang malah mengosongkan jawaban
    # DIBATALKAN.
    strip_selectors = (
        '[class*="toolcall-container"]',
        '[class*="segment-assistant-actions"]',
        ".upgrade-membership",
        # Cadangan bila situs mengganti penamaan wadah tool-nya.
        '[class*="thinking-container"]',
        '[class*="reasoning"]',
    )

    # --- lampiran (mis. screenshot untuk debug visual) ---
    # DIPETAKAN dari DOM kimi.ai (2026-08-20, live):
    #   .toolkit-trigger-btn  -> popover .n-popover berisi 4 item .toolkit-item:
    #                            "Add files & photos" / "Plugins" / "Skills" /
    #                            "Web search". Klik "Add files & photos" baru
    #                            memicu pemilih berkas (file_chooser).
    #   setelah file dipilih  -> kartu .file-card-container.normal.success
    #                            (dulu .image-thumbnail — situs sudah berubah).
    # Input file `input.hidden-input` ADA di DOM sesudah popover dibuka, tapi
    # vis:false — set_input_files padanya BEROBOTAN: Kimi tidak memproses
    # file yang diisi tanpa melalui alur "Add files & photos" → file_chooser.
    # Karena itu _upload di-override di bawah untuk pakai alur yang benar.
    file_input_selector = "input.hidden-input, input[type='file']"
    # Item "Add files & photos" di popover toolkit — satu-satunya jalur upload
    # yang berfungsi di kimi.ai sekarang.
    _ITEM_ADD_FILES = '.toolkit-item:has-text("Add files")'
    # Kartu pratinjau yang SUDAH SELESAI diunggah. `normal success` memastikan
    # unggahan benar-benar tuntas (bukan error/loading).
    attach_item_selector = ".file-card-container.normal.success"

    # Tombol pembuka popover lampiran.
    _BTN_TOOLKIT = ".toolkit-trigger-btn"

    # Teks pemberitahuan limit Kimi belum pernah terlihat; sengaja DIKOSONGKAN
    # agar tak ada pola longgar yang salah menangkap jawaban biasa sebagai
    # "kuota habis" (pelajaran dari connector claude.ai yang kini dihapus: pola
    # seperti "usage limit" ikut cocok dengan jawaban AI yang kebetulan membahas
    # rate limit).
    limit_patterns = ()

    # Pemberitahuan SIBUK Kimi — bentuk aslinya terlihat langsung di terminal:
    #   "System is currently busy. Please try again later."
    #   "Capacity is busy. Please wait or upgrade"
    # Muncul DI TEMPAT balasan, jadi sebelum ini ia tampil sebagai "jawaban".
    # Sengaja dijangkar pada kata "busy" bersama subjeknya (system/capacity/
    # server) supaya kalimat biasa yang memuat "busy" tak ikut tertangkap;
    # penjaga panjang di base (busy_max_chars) menutup sisanya.
    busy_patterns = (
        r"\b(system|capacity|server|service)\s+is\s+(currently\s+)?busy\b",
        r"\bsistem\s+sedang\s+sibuk\b",
        r"\bplease\s+wait\s+or\s+upgrade\b",
    )

    # --- /effort: pemilih model + usaha berpikir ---
    # DIPETAKAN LANGSUNG pada sesi login. Berbeda dari Qwen yang kontrolnya
    # tersebar di dua tempat, Kimi menaruh SEMUANYA di balik satu pembuka
    # `.current-model` (berlabel mis. "K2.6 Standard") di baris komposer:
    #   .model-item   -> "K2.6" (obrolan cepat) | "K3" (andalan) | "K3 Swarm"
    #   .effort-item  -> "Thinking effort" -> submenu .effort-option:
    #                    "Standard" | "High"
    # Tak ada toggle "berpikir" tersendiri — usaha berpikir adalah submenu di
    # dalam pemilih model, jadi bentuknya path-klik dua tingkat.
    #
    # PENTING: item menu Kimi TIDAK memakai role ARIA — TERUKUR
    # [role=menuitem]=0, [role=menuitemradio]=0, [role=option]=0. Karena itu
    # menu_item_selector di bawah WAJIB ditimpa; dengan daftar ARIA bawaan,
    # base menganggap menunya tak pernah terbuka.
    _BTN_MODEL = ".current-model"
    # Urutan penting: `.effort-option` didahulukan supaya "Standard"/"High"
    # mengenai pilihan di submenu, bukan `.effort-item` induknya yang teksnya
    # juga memuat nilai terpilih ("Thinking effort Standard").
    menu_item_selector = (".effort-option", ".effort-item", ".model-item",
                          ".toolkit-item")
    web_model_button = _BTN_MODEL
    web_actions = (
        ("K2.6", ("K2.6",), "obrolan cepat, balasan singkat", _BTN_MODEL),
        ("K3", ("K3",), "chat & agent, model andalan", _BTN_MODEL),
        ("K3 Swarm", ("K3 Swarm",),
         "pencarian masif & pemrosesan borongan", _BTN_MODEL),
        ("Thinking effort: Standard", ("Thinking effort", "Standard"),
         "usaha berpikir standar", _BTN_MODEL),
        ("Thinking effort: High", ("Thinking effort", "High"),
         "usaha berpikir tinggi", _BTN_MODEL),
    )

    def _upload(self, page: Any, paths: list[str]) -> None:
        """Lampirkan file, meniru persis yang dilakukan pengguna.

        Alur di kimi.ai (2026-08-20, live):
          1. Klik `.toolkit-trigger-btn` → popover terbuka (4 item toolkit)
          2. Klik "Add files & photos" → file_chooser muncul
          3. Pilih file → kartu `.file-card-container.normal.success` muncul

        `set_input_files` pada `input.hidden-input` TIDAK BERFUNGSI di situs
        sekarang: Kimi hanya memproses file yang masuk lewat alur pemilih
        berkas sungguhan (file_chooser). Karena itu kita PAKSA lewat
        file_chooser, bukan langsung mengisi input."""
        # Buka popover bila belum terbuka. Tombol toolkit adalah TOGGLE —
        # kalau sudah terbuka, mengkliknya lagi justru MENUTUP.
        popover_open = page.locator('.toolkit-popover').count() > 0
        if not popover_open:
            try:
                self._click_element(page.locator(self._BTN_TOOLKIT).first)
                page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001
                pass

        # Klik "Add files & photos" → memicu file_chooser.
        add_files_item = page.locator(self._ITEM_ADD_FILES)
        try:
            if add_files_item.count() and add_files_item.first.is_visible():
                with page.expect_file_chooser(timeout=15000) as chooser:
                    self._click_element(add_files_item.first)
                chooser.value.set_files(paths)
                return
        except Exception:  # noqa: BLE001 - lanjut ke cadangan
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass

        # Cadangan: set_input_files langsung (mungkin berfungsi di versi
        # Kimi mendatang yang mengembalikan alur input langsung).
        for sel in (self._INPUT_ASLI, self.file_input_selector):
            try:
                if page.locator(sel).count() == 0:
                    continue
                page.set_input_files(sel, paths, timeout=8000)
                loaded = page.evaluate(
                    "(s) => { const el = document.querySelector(s); "
                    "return el && el.files ? el.files.length : 0; }", sel)
                if loaded:
                    return
            except Exception:  # noqa: BLE001
                pass

        # Cadangan terakhir: klik toolkit lagi (mungkin trigger file_chooser
        # di versi situs tertentu).
        try:
            with page.expect_file_chooser(timeout=10000) as chooser:
                self._click_element(page.locator(self._BTN_TOOLKIT).first)
            chooser.value.set_files(paths)
        except Exception as exc:  # noqa: BLE001
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            raise BrowserError(
                "gagal melampirkan file di Kimi — tombol 'Add files & photos' "
                f"atau input tersembunyi mungkin berubah (perbarui connectors/"
                f"kimi.py): {exc}"
            ) from exc

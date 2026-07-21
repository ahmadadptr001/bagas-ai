"""Connector kimi.com (web) — Kimi (Moonshot AI).

Selektor di bawah DIPETAKAN LANGSUNG ke www.kimi.com pada sesi nyata (2026-07-21,
di project agrishield): pesan uji dikirim, DOM-nya dibaca, jumlah elemen &
perubahan kelas selama streaming DIUKUR. Yang belum terverifikasi ditandai
eksplisit di komentarnya — jangan dianggap sudah terbukti.

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
    chat_url = "https://www.kimi.com/"
    # BELUM DIVERIFIKASI: bentuk URL percakapan diduga /chat/<id> dengan id
    # alfanumerik (bukan UUID heksadesimal seperti claude.ai, jadi pola bawaan
    # base.py takkan cocok). Aman bila ternyata meleset — pola yang tak cocok
    # membuat current_chat_id kosong, dan bagas-ai cukup tak melanjutkan chat
    # lama alih-alih membuka URL yang salah. Perbaiki di sini bila terlihat beda.
    chat_url_template = "https://www.kimi.com/chat/{id}"
    chat_id_pattern = r"/chat/([A-Za-z0-9_-]{8,})"

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
    # DIUKUR pada sesi nyata: untuk SATU jawaban 381 karakter, jumlah elemen yang
    # cocok adalah segment-assistant=4, markdown=4, chat-content=6. `.markdown`
    # di Kimi memuat jawaban UTUH (berbeda dari Qwen, di mana markdown terpecah
    # per-paragraf sehingga tak boleh didahulukan), jadi aman ditaruh di depan.
    #
    # Pembacaan selalu mengambil kecocokan TERAKHIR YANG ADA ISINYA (lihat
    # _JS_PILIH_ELEMEN & _read_last_message di base.py) — WAJIB, karena elemen
    # `segment-assistant` paling akhir di Kimi adalah wadah KOSONG untuk giliran
    # berikutnya, dan mengambilnya mentah-mentah membuat jawaban terbaca "".
    message_selector = (
        '[class*="segment-assistant"]',
        '[class*="markdown"]',
        '[class*="chat-content"]',
        "[class*='assistant']",
        "[class*='answer']",
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
    # BLOK BERPIKIR mode reasoning: Kimi menaruh proses berpikirnya DI DALAM
    # wadah jawaban, jadi tanpa ini ia ikut tampil di jawaban akhir. Pada jalur
    # agent bagas-ai itu lebih dari sekadar berisik — blok [[TOOL]] yang baru
    # DIRENCANAKAN di dalam proses berpikir bisa ikut terbaca lalu dieksekusi.
    #
    # Pola menargetkan CLASS (bukan teks) agar jawaban biasa tak salah terbuang,
    # dan base.py punya pengaman: bila pembuangan malah mengosongkan jawaban, ia
    # DIBATALKAN. BELUM diverifikasi ke DOM Kimi mode-reasoning — kalau blok
    # berpikir masih bocor ATAU jawaban malah hilang, sesuaikan daftar ini.
    thinking_selectors = (
        '[class*="thinking"]',
        '[class*="reasoning"]',
        '[class*="thought"]',
        '[class*="chain-of-thought"]',
    )

    # --- lampiran (mis. screenshot untuk debug visual) ---
    # DIPETAKAN dari klik manual pengguna:
    #   .toolkit-trigger-btn  -> popover .toolkit-popover berisi LABEL.toolkit-item
    #                            yang MEMBUNGKUS input[type=file].hidden-input
    #   setelah file dipilih  -> pratinjau .image-thumbnail (ber-kelas .loading
    #                            SELAMA unggahan berjalan)
    # Input file BARU ADA di DOM sesudah tombol toolkit diklik, jadi
    # set_input_files di awal tak akan menemukan apa pun (karena itu _upload
    # di-override di bawah).
    file_input_selector = "input.hidden-input, input[type='file']"
    # Kartu pratinjau yang SUDAH SELESAI diunggah — `:not(.loading)` penting:
    # kartunya muncul seketika dengan kelas `loading`, dan mengirim saat itu
    # berarti pesan berangkat sebelum gambarnya benar-benar terunggah.
    attach_item_selector = ".image-thumbnail:not(.loading)"

    # Tombol pembuka popover lampiran.
    _BTN_TOOLKIT = ".toolkit-trigger-btn"

    # Teks pemberitahuan limit Kimi belum pernah terlihat; sengaja DIKOSONGKAN
    # agar tak ada pola longgar yang salah menangkap jawaban biasa sebagai
    # "kuota habis" (pelajaran dari claude.py: pola seperti "usage limit" ikut
    # cocok dengan jawaban AI yang kebetulan membahas rate limit).
    limit_patterns = ()

    # /effort SENGAJA belum diisi: kontrol model & mode berpikir Kimi belum
    # pernah dipetakan ke DOM, dan menebak selektornya hanya menghasilkan
    # "opsi tak bisa diklik" yang membingungkan. Petakan dulu, baru isi
    # web_model_button & web_actions seperti di claude.py/qwen.py.

    def _upload(self, page: Any, paths: list[str]) -> None:
        """Lampirkan file, meniru persis yang dilakukan pengguna.

        Urutannya (terekam dari klik manual): klik `.toolkit-trigger-btn` supaya
        popover lampiran terbuka — DI SITULAH `input.hidden-input` muncul di DOM
        — lalu isi input itu langsung.

        Mengisi input lebih disukai daripada memancing dialog OS lewat
        expect_file_chooser: tak ada jendela sistem yang bisa menggantung, dan
        aman saat jendela browser berjalan tersembunyi di latar. Bila input tetap
        tak muncul (situs berubah), barulah file chooser dicoba sebagai cadangan.

        Keberhasilannya tidak dinilai di sini melainkan oleh _attach_files, yang
        menunggu kartu `.image-thumbnail` selesai (tanpa kelas `loading`)."""
        # Popover mungkin sudah terbuka dari percobaan sebelumnya; klik hanya
        # bila input-nya memang belum ada.
        if page.locator(self.file_input_selector).count() == 0:
            try:
                self._click_element(page.locator(self._BTN_TOOLKIT).first)
                page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001 - dinilai lewat percobaan di bawah
                pass

        try:
            page.set_input_files(self.file_input_selector, paths, timeout=8000)
            return
        except Exception:  # noqa: BLE001 - cadangan: dialog pemilih file
            pass

        try:
            with page.expect_file_chooser(timeout=15000) as chooser:
                self._click_element(page.locator(self._BTN_TOOLKIT).first)
            chooser.value.set_files(paths)
        except Exception as exc:  # noqa: BLE001
            try:  # jangan tinggalkan popover menggantung menutupi komposer
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            raise BrowserError(
                "gagal melampirkan file di Kimi — tombol toolkit atau input "
                f"tersembunyi mungkin berubah (perbarui connectors/kimi.py): {exc}"
            ) from exc

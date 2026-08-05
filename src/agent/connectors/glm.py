"""Connector chat.z.ai (web) — GLM (Z.ai / Zhipu).

Seluruh selektor di bawah DIPETAKAN LANGSUNG dari sesi login nyata pada
2026-08-06: pengguna memperagakan login, kirim pesan, kirim gambar, ganti model,
dan ganti effort sementara tiap klik & elemennya direkam; sesudah itu DOM-nya
dibaca ulang lewat CDP. Yang belum terbukti ditandai eksplisit — jangan
dianggap terverifikasi.

DUA HALAMAN, SATU YANG DIPAKAI. z.ai/chat (halaman depan) juga punya komposer,
tetapi ia cuma ruang tunggu: pesan pertama di sana MELEMPAR ke tab baru
chat.z.ai/c/<id>. Connector ini sengaja langsung ke chat.z.ai supaya tak pernah
ada tab kedua yang harus diikuti — dan di sana semua kontrol (model, effort,
mode, lampiran) tersedia lengkap.

Situsnya turunan Open WebUI: id-id seperti #chat-input, #send-message-button,
#upload-file-button berasal dari sana dan relatif stabil. Sebaliknya, id
bergaya `bits-c167` (komponen bits-ui) DIBUAT ULANG tiap render — jangan sekali
pun dipakai sebagai selector.
"""
from __future__ import annotations

from typing import Any

from .base import WebConnector
from .browser import BrowserError

# Pemicu menu. Dipisah jadi konstanta karena dipakai berulang di web_actions/
# web_modes, dan supaya jelas ketiganya elemen yang BERBEDA.
_BTN_MODEL = 'button.modelSelectorButton, [aria-label="Select a model"]'
# Chip effort di komposer, berlabel mis. "Deep Think  Max". Div ber-aria-haspopup
# (BUKAN <button>), jadi selector bergaya button[...] tak akan cocok.
_BTN_EFFORT = '.messageInputContainer [aria-haspopup="menu"]'
# Sakelar mode pencarian web di komposer. Perilakunya campuran: klik MENYALAKAN/
# mematikan Search (data-active), dan panel pilihan "Search / Advanced Search"
# muncul menyusul. _open_menu di base memang mengeklik pembuka sampai dua kali
# lalu menunggu itemnya muncul, jadi pola ini tertangani.
_BTN_MODE = ".messageInputContainer button.rounded-md"


class GlmConnector(WebConnector):
    service = "glm"
    label = "GLM (web)"
    chat_url = "https://chat.z.ai/"
    # DIVERIFIKASI live: percakapan berada di https://chat.z.ai/c/<uuid>
    # (terekam: /c/a6b91108-3213-4e19-9ae4-d686644affd9), jadi lanjut-chat &
    # --resume bisa kembali ke percakapan yang sama.
    chat_url_template = "https://chat.z.ai/c/{id}"
    chat_id_pattern = r"/c/([0-9a-fA-F-]{16,})"
    show_window = False

    # --- input ---
    # <textarea id="chat-input">, BUKAN contenteditable (terbaca langsung dari
    # elemennya saat pengguna mengetik). Kandidat kedua sekadar jaring pengaman.
    input_selector = ("textarea#chat-input", "textarea")
    input_is_contenteditable = False
    submit_key = "Enter"
    # DIVERIFIKASI dari klik pengguna: <button id="send-message-button"
    # type="submit" class="sendMessageButton …">.
    send_button_selector = "#send-message-button"
    # Dua elemen di sidebar memakai id yang SAMA (#sidebar-new-chat-button):
    # satu berteks "Chat", satu "New Chat". Karena itu yang berteks dicoba
    # duluan — dengan id polos, `.first` bisa jatuh ke tombol yang salah.
    new_chat_selector = (
        'button#sidebar-new-chat-button:has-text("New Chat")',
        "#sidebar-new-chat-button",
    )

    # --- deteksi belum-login ---
    # TERUKUR pada profil KOSONG (halaman tamu chat.z.ai): #chat-input TETAP ADA
    # dan terlihat, persis perangkap yang sudah dikenal di Kimi & Qwen. Jadi
    # "kotak input ada" bukan bukti login; tombol "Sign in"-lah penandanya.
    logged_out_selector = (
        'button:has-text("Sign in"), button:has-text("Sign In"), '
        'button:has-text("Log in"), button:has-text("Login")'
    )
    # BUKTI POSITIF sudah login, dari perbandingan sensus tombol tamu vs sesudah
    # login: tombol menu akun hanya ada sesudah login. Dipilih aria-label-nya,
    # bukan id `nux-user-menu-btn` — "nux" itu penamaan alur pengguna baru dan
    # bisa hilang begitu onboarding selesai.
    logged_in_selector = '[aria-label="Open User Menu"]'

    # --- jawaban ---
    # DIUKUR pada percakapan berisi 6 jawaban: `.chat-assistant` = tepat satu
    # elemen per jawaban, isinya utuh. `.markdown-prose` ikut menempel di elemen
    # yang sama DAN di pembungkus di dalamnya (dobel), jadi ia cuma cadangan.
    message_selector = (
        ".chat-assistant",
        ".markdown-prose",
        "[id^='message-']",
    )
    read_as_markdown = True
    # TERUKUR: selagi menjawab, #send-message-button DIGANTI tombol berhenti
    # tanpa id/aria — <button class="flex justify-center items-center p-2
    # bg-black rounded-full …"><span class="… rounded-xs"></span></button>.
    #
    # `:has(span.rounded-xs)` bukan hiasan: satu tombol LAIN di komposer juga
    # ber-`rounded-full`, yaitu silang penghapus kartu lampiran (tersembunyi
    # sampai di-hover, tapi tetap terhitung). Tanpa penyaring itu, kotak yang
    # sedang berisi lampiran membuat _is_done() selamanya False dan tiap
    # jawaban ditunggu sampai kehabisan waktu.
    #
    # DIUJI: 0 kecocokan saat diam, 1 selama 12 detik penuh saat menjawab, lalu
    # 0 lagi begitu selesai.
    stop_selectors = (
        '.messageInputContainer button.rounded-full:has(span.rounded-xs)',
    )
    # Sebelum kata pertama keluar, isi wadah jawaban cuma "Thinking..." — dan
    # sesudah blok berpikir dibuang, sisa teksnya bisa cuma "Thought Process".
    # Keduanya BUKAN jawaban.
    noise_pattern = r"(?:Thinking\.{0,3}|Thought Process)\s*"
    # Blok penalaran GLM ada DI DALAM wadah jawaban sebagai
    # `.thinking-chain-container` (terbaca dari pohon DOM jawaban sungguhan).
    # Wajib dibuang: di jalur agent, blok [[TOOL]] yang cuma DIRENCANAKAN di
    # dalam penalaran bisa ikut terbaca lalu benar-benar dieksekusi.
    strip_selectors = (
        ".thinking-chain-container",
        # Cadangan bila situs mengganti penamaannya.
        '[class*="thinking-chain"]',
        '[class*="reasoning"]',
    )

    # --- lampiran (screenshot dll) ---
    # DIUJI langsung: set_input_files ke <input type="file"> tersembunyi di
    # dalam komposer BERHASIL (kartu 0 -> 1, thumbnail muncul), jadi tak perlu
    # meniru klik menu seperti di Qwen/Kimi. Input-nya tanpa id/kelas, maka
    # selektornya sesempit mungkin lewat wadah komposer.
    file_input_selector = ".messageInputContainer input[type='file']"
    # Kartu pratinjau di komposer: <button class="relative group flex items-center
    # gap-2.5 …"> berisi nama berkas + "PNG · 358.5 KB", tersusun di dalam
    # `.chip-scroll`. Ini yang dihitung sebagai bukti "file sudah menempel".
    attach_item_selector = ".messageInputContainer .chip-scroll button"

    # Kandidat ITEM MENU — ketiga menunya dibangun dari komponen yang berbeda,
    # jadi daftar ARIA bawaan base tak cukup:
    #   [aria-label="model-item"]  -> daftar model (GLM-5.2, GLM-5-Turbo, …)
    #   button[data-selected]      -> tingkat effort (High / Max)
    #   [role="button"]            -> sakelar "Deep Think" di panel effort
    #   div.font-medium            -> item mode (Search / Advanced Search);
    #                                 divnya polos, tanpa role apa pun
    menu_item_selector = (
        '[aria-label="model-item"]',
        "button[data-selected]",
        '.messageInputContainer [role="button"]',
        "div.font-medium",
    )

    # --- /effort: tingkat berpikir (chip komposer) + varian model (bar atas) ---
    # TERCACAH dari menu yang dibuka sungguhan. Panel chip berisi dua tingkat
    # (High/Max, ditandai data-selected) plus sakelar "Deep Think" yang
    # menyalakan/mematikan penalarannya.
    web_model_button = _BTN_MODEL
    web_actions = (
        ("Berpikir: Max", ("Max",),
         "penalaran paling dalam (bawaan situs)", _BTN_EFFORT),
        ("Berpikir: High", ("High",),
         "penalaran lebih ringan — balasan lebih cepat", _BTN_EFFORT),
        ("Deep Think on/off", ("Deep Think",),
         "sakelar penalaran; menekan ulang mematikannya", _BTN_EFFORT),
        ("GLM-5.2", ("GLM-5.2",),
         "flagship: paling kuat untuk koding & tugas panjang", _BTN_MODEL),
        ("GLM-5-Turbo", ("GLM-5-Turbo",),
         "cepat untuk obrolan, koding, dan tugas agentic", _BTN_MODEL),
        ("GLM-5.1", ("GLM-5.1",), "flagship generasi sebelumnya", _BTN_MODEL),
        ("GLM-5V-Turbo", ("GLM-5V-Turbo",),
         "model VISI — pilih ini bila banyak melampirkan gambar", _BTN_MODEL),
        ("GLM-4.7", ("GLM-4.7",), "model klasik, ringan", _BTN_MODEL),
    )

    # --- /mode: pencarian web ---
    # TERUKUR, dan hasilnya bukan menu biasa melainkan SAKELAR + panel varian:
    #
    #   mati  --klik--> nyala (data-active=true) + panel "Search / Advanced
    #                   Search" terbuka
    #   nyala --klik--> MATI lagi (panel ikut tertutup)
    #   panel: klik "Advanced Search" -> tetap nyala, varian berpindah
    #
    # Konsekuensinya jalur umum di base TIDAK aman di sini: _open_menu boleh
    # mengeklik pembuka sampai dua kali, dan di situs ini klik kedua justru
    # MEMATIKAN pencarian — TERUKUR: set_web_option("Search") berakhir dengan
    # data-active=false, alias "mode terpilih" yang sebenarnya mati. Karena itu
    # _set_action_on_hub di bawah menangani mode search sendiri dan MEMASTIKAN
    # sakelarnya benar-benar menyala di akhir.
    web_modes = (
        ("Search", ("Advanced Search",),
         "cari di web (riset multi-putaran / Advanced Search)", _BTN_MODE),
        ("Search sederhana", ("Search",),
         "cari di web sekali jalan saja — lebih cepat", _BTN_MODE),
    )
    # Mematikan mode = mengeklik sakelar yang SEDANG menyala. Penyaring
    # [data-active="true"] wajib ada: tombolnya tetap ada saat mati, dan
    # mengekliknya justru MENYALAKAN pencarian — kebalikan dari yang diminta.
    web_mode_off_selector = (
        '.messageInputContainer button.rounded-md[data-active="true"]')

    # --- penanganan khusus sakelar search ---
    def _mode_menyala(self, page: Any) -> bool:
        """Sakelar pencarian web sedang menyala?"""
        try:
            btn = page.locator(_BTN_MODE).first
            return (btn.get_attribute("data-active") or "") == "true"
        except Exception:  # noqa: BLE001 - DOM sedang digambar ulang
            return False

    def _pilih_varian(self, page: Any, teks: str) -> bool:
        """Klik "Search"/"Advanced Search" di panel bila panelnya terbuka."""
        try:
            loc = page.locator(
                f'div.font-medium:has-text("{teks}")').locator(
                "visible=true").first
            if not loc.count():
                return False
            self._click_element(loc, timeout=6000)
            page.wait_for_timeout(700)
            return True
        except Exception:  # noqa: BLE001 - panel keburu tertutup
            return False

    def _set_action_on_hub(self, h: Any, label: str, path: tuple[str, ...],
                           opener: str = "") -> str:
        """Aksi model/effort dilayani base; mode search ditangani di sini.

        Alasannya ada di komentar web_modes: pembuka menu di situs ini merangkap
        sakelar, jadi "klik pembuka sekali lagi" — cara base menghadapi menu
        yang belum terbuka — punya efek samping mematikan fiturnya."""
        if opener != _BTN_MODE:
            return super()._set_action_on_hub(h, label, path, opener)

        page, _ = self._acquire_ready_page(h, lambda m: None, lambda: None)
        self._tutup_penghalang(page)
        self._normalize_layout(page)
        varian = path[0]
        for _ in range(3):
            if not self._mode_menyala(page):
                # Menyalakan sekaligus membuka panel variannya.
                self._click_element(page.locator(_BTN_MODE).first, timeout=8000)
                page.wait_for_timeout(1100)
            # Panel hanya terbuka sesaat setelah dinyalakan. Kalau sudah
            # tertutup, varian tak bisa dipindah tanpa mematikan-menyalakan
            # ulang — dan itu ditempuh HANYA bila memang perlu.
            if self._pilih_varian(page, varian):
                if self._mode_menyala(page):
                    return (f"'{label}' menyala di {self.label} "
                            f"(varian: {varian})")
            elif self._mode_menyala(page):
                # Sudah menyala tapi panelnya tak terlihat: matikan supaya
                # putaran berikutnya menyalakan ulang berikut panelnya.
                self._click_element(page.locator(_BTN_MODE).first, timeout=8000)
                page.wait_for_timeout(900)
        if self._mode_menyala(page):
            return (f"'{label}' menyala di {self.label}, tetapi varian "
                    f"'{varian}' tak bisa dipastikan — panelnya tak terbuka")
        raise BrowserError(
            f"sakelar pencarian web {self.label} tak mau menyala — "
            "layout komposernya mungkin berubah (lihat _BTN_MODE di "
            "connectors/glm.py)")

    # Pemberitahuan limit/galat/percakapan-penuh khas z.ai BELUM PERNAH terlihat
    # langsung. Sengaja DIKOSONGKAN daripada ditebak: pola longgar di sini
    # berakibat mahal — jawaban biasa yang kebetulan membahas "rate limit" bisa
    # membuat chat sehat dibuang atau giliran berhenti tanpa sebab. Isi kalau
    # sudah ada teks aslinya di layar.
    limit_patterns = ()
    error_patterns = ()
    context_full_patterns = ()
    busy_patterns = ()

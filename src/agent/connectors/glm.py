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
    # BATAS JUMLAH BERKAS per percakapan. Teks aslinya, terlihat langsung
    # sebagai toast di layar:
    #   "You can only chat with a maximum of 10 file(s) at a time."
    # Inilah penyebab kegagalan kirim yang membingungkan di sesi panjang: tiap
    # langkah web_preview menambah satu screenshot, dan sesudah berkas ke-10
    # unggahan berikutnya ditolak tanpa pratinjau. Angkanya dibuat lentur
    # (\d+) karena situs bisa mengubahnya kapan saja.
    attach_limit_patterns = (
        r"(?i)maximum of \d+ file",
        r"(?i)\bmaksimal \d+ (?:berkas|file)\b",
    )

    # Kartu pratinjau di komposer: <button class="relative group flex items-center
    # gap-2.5 …"> berisi nama berkas + "PNG · 358.5 KB", tersusun di dalam
    # `.chip-scroll`. Ini yang dihitung sebagai bukti "file sudah menempel".
    #
    # `>` (anak LANGSUNG) itu penting, bukan gaya penulisan: tiap kartu memuat
    # TIGA <button> (kartunya sendiri, thumbnail di dalamnya, dan silang
    # penghapusnya). TERUKUR: 3 berkas terbaca 9 dengan selektor keturunan —
    # dan penantian unggahan menunggu `sebelum + jumlah berkas`, jadi hitungan
    # yang membengkak membuatnya berhenti sebelum unggahannya benar-benar
    # tuntas, lalu pesan berangkat tanpa gambar.
    attach_item_selector = ".messageInputContainer .chip-scroll > button"
    # Silang penghapus di pojok tiap kartu (kelasnya `invisible … rounded-full`
    # — hanya terlihat saat kartunya di-hover, tapi tetap bisa diklik program).
    attach_clear_selector = (
        ".messageInputContainer .chip-scroll button.rounded-full")

    # Kandidat ITEM MENU untuk jalur BAWAAN base — dipakai HANYA oleh daftar
    # model, karena panel effort & mode ditangani sendiri di bawah.
    #
    # Sengaja cuma satu, dan itu penting: base memakai daftar ini juga untuk
    # menjawab "menunya sudah terbuka belum?". Kandidat yang selalu ada di
    # halaman (mis. `[role="button"]` — di situs ini pembungkus tombol lampiran
    # memakainya) membuat jawabannya SELALU "sudah", sehingga menu yang
    # sebenarnya belum terbuka tak pernah diklik ulang dan kegagalannya muncul
    # belakangan sebagai "item tak ditemukan".
    menu_item_selector = ('[aria-label="model-item"]',)

    # Kontrol DI DALAM panel — semuanya diverifikasi dari HTML aslinya.
    # Tingkat berpikir: <button data-selected="true|false"><span
    # class="font-medium">Max</span> …>. `:text-is()` (bukan :has-text) supaya
    # "High" tak ikut cocok pada teks lain yang memuatnya.
    _ITEM_EFFORT = 'button[data-selected]:has(span:text-is("{teks}"))'
    # Deep Think & Advanced Search BUKAN item menu melainkan SAKELAR sungguhan:
    #   <button role="switch" aria-checked="true" data-switch-root …>
    # Inilah sebab keluhan "mode search aktif tapi Advanced Search tak ikut
    # menyala": mengeklik BARIS/teksnya tak mengubah sakelar sama sekali.
    _SW_DEEP = ('div[role="button"]:has(span:text-is("Deep Think")) '
                'button[role="switch"]')
    _SW_ADV = ('div:has(> span:text-is("Advanced Search")) '
               'button[role="switch"]')

    # Situs MENYEMBUNYIKAN chip effort pada jendela sempit — terukur: ada di
    # 1600/1280/1024/900 px, hilang di 760 px. Jendela connector sendiri jalan
    # ~614 px, jadi tanpa penyeragaman ini /effort gagal dengan "menu tak mau
    # terbuka" padahal tombolnya memang tak dirender.
    min_layout_width = 1280
    min_layout_height = 800

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
        # DIPISAH jadi dua aksi bertujuan, bukan satu "on/off". Sakelar yang
        # dibalik buta bikin hasilnya bergantung keadaan sebelumnya — pengguna
        # menekan "Deep Think" untuk MENYALAKAN lalu justru mematikannya.
        ("Deep Think: nyala", ("Deep Think", "on"),
         "nyalakan penalaran (jawaban lebih dalam, lebih lambat)", _BTN_EFFORT),
        ("Deep Think: mati", ("Deep Think", "off"),
         "matikan penalaran — balasan paling cepat", _BTN_EFFORT),
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
    #
    # Di dalam panel, "Advanced Search" punya SAKELAR sendiri (role="switch")
    # yang berdiri terpisah dari sakelar Search. Menyalakan Search saja
    # meninggalkannya `aria-checked="false"` — persis yang dikeluhkan: "search
    # aktif tapi advanced-nya tak ikut nyala". Karena itu tiap mode di bawah
    # menyebutkan keadaan yang diinginkan secara eksplisit, lalu diverifikasi.
    web_modes = (
        ("Search", ("Advanced Search", "on"),
         "cari di web + Advanced Search menyala (riset multi-putaran)",
         _BTN_MODE),
        ("Search sederhana", ("Advanced Search", "off"),
         "cari di web sekali jalan saja — lebih cepat", _BTN_MODE),
    )
    # Mematikan mode = mengeklik sakelar yang SEDANG menyala. Penyaring
    # [data-active="true"] wajib ada: tombolnya tetap ada saat mati, dan
    # mengekliknya justru MENYALAKAN pencarian — kebalikan dari yang diminta.
    web_mode_off_selector = (
        '.messageInputContainer button.rounded-md[data-active="true"]')

    # --- penanganan khusus: panel effort & sakelar search ---
    def _mode_menyala(self, page: Any) -> bool:
        """Sakelar pencarian web sedang menyala?"""
        try:
            btn = page.locator(_BTN_MODE).first
            return (btn.get_attribute("data-active") or "") == "true"
        except Exception:  # noqa: BLE001 - DOM sedang digambar ulang
            return False

    def _klik_kuat(self, page: Any, loc: Any) -> None:
        """Klik yang juga menembus komponen yang menunggu POINTER, bukan click.

        Jendela connector berjalan tersembunyi, jadi klik mouse sungguhan gagal
        hit-test dan base jatuh ke dispatch 'click'. Sebagian komponen situs
        ini (bits-ui) membuka panelnya pada pointerdown — dispatch 'click' saja
        MENGUBAH keadaan sakelarnya tapi panelnya tak pernah muncul, dan itulah
        bentuk kegagalan yang menyesatkan: sakelar bergerak, panel tidak."""
        self._click_element(loc, timeout=8000)
        try:
            loc.dispatch_event("pointerdown")
            loc.dispatch_event("pointerup")
        except Exception:  # noqa: BLE001 - elemen lepas; hasilnya dinilai pemanggil
            pass
        page.wait_for_timeout(200)

    def _tutup_panel(self, page: Any) -> None:
        """Tutup panel/dropdown yang masih menggantung di atas komposer.

        Escape saja tak selalu cukup: jendela connector tersembunyi sehingga
        fokus papan ketik bisa tak berada di dalam panelnya. Karena itu chip
        effort juga diperiksa lewat aria-expanded dan ditutup dengan mengeklik
        pembukanya lagi."""
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
        try:
            chip = page.locator(_BTN_EFFORT).first
            if chip.count() and \
                    (chip.get_attribute("aria-expanded") or "") == "true":
                self._klik_kuat(page, chip)
                page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 - chip tak ada -> memang tak perlu
            pass

    def _pastikan_mode_nyala(self, page: Any) -> None:
        """Nyalakan sakelar pencarian web, dan BUKTIKAN ia menyala.

        Kegagalan dilaporkan dengan keadaan yang terbaca (nonaktif? lebar
        jendela?) — bukan tebakan "layout berubah" yang tak bisa
        ditindaklanjuti siapa pun."""
        for _ in range(3):
            if self._mode_menyala(page):
                return
            loc = page.locator(_BTN_MODE).first
            if not loc.count():
                break
            if loc.is_disabled():
                raise BrowserError(
                    f"tombol pencarian web {self.label} sedang NONAKTIF — "
                    "biasanya karena situs masih menyiapkan komposer atau "
                    "jawaban sebelumnya belum selesai. Coba /mode lagi "
                    "beberapa detik lagi.")
            self._klik_kuat(page, loc)
            page.wait_for_timeout(1100)
        if self._mode_menyala(page):
            return
        ada = page.locator(_BTN_MODE).count()
        lebar = page.evaluate("() => window.innerWidth")
        raise BrowserError(
            f"sakelar pencarian web {self.label} tak mau menyala "
            f"(tombol ditemukan: {ada}, lebar halaman: {lebar}px). Bila "
            "tombolnya 0, komposer situs berubah — perbarui _BTN_MODE di "
            "connectors/glm.py.")

    def _sakelar(self, page: Any, sel: str, nyala: bool) -> bool:
        """Setel sebuah role=switch ke keadaan yang DIMINTA, lalu buktikan.

        Bukan sekadar "klik": sakelar yang dibalik buta membuat hasil akhirnya
        bergantung pada keadaan sebelumnya, dan itulah bedanya "menyalakan"
        dengan "menekan tombol". Kembalikan True hanya bila keadaan akhirnya
        benar-benar sesuai."""
        try:
            loc = page.locator(sel).locator("visible=true").first
            if not loc.count():
                return False
            if ((loc.get_attribute("aria-checked") or "") == "true") == nyala:
                return True
            self._click_element(loc, timeout=6000)
            page.wait_for_timeout(700)
            return ((loc.get_attribute("aria-checked") or "") == "true") == nyala
        except Exception:  # noqa: BLE001 - panel keburu tertutup
            return False

    def _buka_panel_effort(self, page: Any) -> bool:
        """Buka chip "Deep Think · Max" dan pastikan isinya benar-benar ada."""
        for _ in range(2):
            chip = page.locator(_BTN_EFFORT).locator("visible=true").first
            if not chip.count():
                return False
            if (chip.get_attribute("aria-expanded") or "") != "true":
                self._klik_kuat(page, chip)      # bits-ui buka di pointerdown
                page.wait_for_timeout(900)
            if page.locator("button[data-selected]").count():
                return True
        return False

    def _atur_effort(self, page: Any, path: tuple[str, ...], label: str) -> str:
        if not self._buka_panel_effort(page):
            raise BrowserError(
                f"panel berpikir {self.label} tak mau terbuka. Chip "
                "'Deep Think' memang DISEMBUNYIKAN situs pada jendela sempit "
                "(hilang di bawah ±800 px) — periksa min_layout_width di "
                "connectors/glm.py bila ini terus terjadi.")
        try:
            if path[0] == "Deep Think":
                nyala = path[1] == "on"
                ok = self._sakelar(page, self._SW_DEEP, nyala)
                hasil = (f"Deep Think {'menyala' if nyala else 'mati'} "
                         f"di {self.label}")
            else:
                item = page.locator(
                    self._ITEM_EFFORT.format(teks=path[0])).first
                if not item.count():
                    raise BrowserError(f"tingkat '{path[0]}' tak ada di panel")
                self._click_element(item, timeout=6000)
                page.wait_for_timeout(600)
                ok = (item.get_attribute("data-selected") or "") == "true"
                hasil = f"'{label}' dipilih di {self.label}"
        finally:
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
        if not ok:
            raise BrowserError(
                f"'{label}' diklik tapi keadaannya tak berubah di "
                f"{self.label} — kontrolnya mungkin berpindah")
        return hasil

    def _atur_mode(self, page: Any, path: tuple[str, ...], label: str) -> str:
        """Nyalakan pencarian web, lalu setel sakelar Advanced Search-nya.

        Panel varian hanya muncul SESAAT sesudah sakelar utama dinyalakan;
        kalau sudah tertutup, satu-satunya jalan membukanya lagi adalah
        mematikan lalu menyalakan ulang."""
        adv = path[1] == "on"
        # Panel LAIN yang masih terbuka (mis. chip effort yang barusan dipakai)
        # menutupi komposer, dan selama itu panel varian search tak pernah
        # muncul — TERUKUR: dari keadaan "search menyala + panel effort
        # terbuka", sakelar Advanced tak pernah bisa disetel. Karena itu
        # halaman dibersihkan dulu.
        self._tutup_panel(page)
        self._pastikan_mode_nyala(page)
        for putaran in range(3):
            if self._sakelar(page, self._SW_ADV, adv):
                # Sakelar Advanced ada DI DALAM panel, dan menutup panel tak
                # membatalkannya — tapi keadaan sakelar utama tetap diperiksa
                # ulang, sebab klik yang meleset bisa mengenai sakelar utama.
                self._pastikan_mode_nyala(page)
                return (f"pencarian web {self.label} menyala; "
                        f"Advanced Search {'menyala' if adv else 'mati'}")
            if putaran == 2:
                break
            # Panelnya tertutup. Satu-satunya cara membukanya lagi adalah
            # mematikan lalu menyalakan ulang sakelar utama.
            self._klik_kuat(page, page.locator(_BTN_MODE).first)
            page.wait_for_timeout(900)
            self._pastikan_mode_nyala(page)
        # Menyerah pada varian — TAPI pencarian web JANGAN ditinggal mati.
        # Versi sebelumnya keluar lewat jalur "matikan lalu coba lagi" dan bila
        # panelnya tak pernah terbuka, pengguna menerima galat SEKALIGUS mode
        # yang padam; padahal bagian yang penting (pencarian menyala) berhasil.
        self._pastikan_mode_nyala(page)
        return (f"pencarian web {self.label} MENYALA, tetapi sakelar "
                f"'Advanced Search' tak bisa disetel ke "
                f"{'nyala' if adv else 'mati'} — panel variannya tak mau "
                "terbuka. Jalankan /mode sekali lagi bila perlu.")

    def _set_action_on_hub(self, h: Any, label: str, path: tuple[str, ...],
                           opener: str = "") -> str:
        """Daftar model dilayani base; panel effort & mode ditangani sendiri.

        Keduanya bukan menu biasa melainkan panel berisi SAKELAR, dan jalur
        umum base (klik teks item, klik pembuka lagi bila menu belum terbuka)
        justru merusak keadaannya — lihat komentar di web_modes."""
        if opener not in (_BTN_EFFORT, _BTN_MODE):
            return super()._set_action_on_hub(h, label, path, opener)

        page, _ = self._acquire_ready_page(h, lambda m: None, lambda: None)
        self._tutup_penghalang(page)
        self._normalize_layout(page)
        if opener == _BTN_EFFORT:
            return self._atur_effort(page, path, label)
        return self._atur_mode(page, path, label)

    # Pemberitahuan limit & percakapan-penuh khas z.ai BELUM PERNAH terlihat
    # langsung. Sengaja DIKOSONGKAN daripada ditebak: pola longgar di sini
    # berakibat mahal — jawaban biasa yang kebetulan membahas "rate limit" bisa
    # membuat chat sehat dibuang atau giliran berhenti tanpa sebab. Isi kalau
    # sudah ada teks aslinya di layar.
    limit_patterns = ()
    error_patterns = ()
    context_full_patterns = ()

    # GAGAL SEMENTARA DI SISI SITUS. Teks aslinya, terlihat langsung di layar
    # sebagai isi balasan (bukan spanduk terpisah):
    #
    #   No response, Please try again later.
    #   SyntaxError: Unexpected token '<', "<!doctypeh"... is not valid JSON
    #
    # Bacaannya jelas: backend z.ai membalas HALAMAN HTML (galat 5xx/proxy)
    # padahal situsnya menunggu JSON, lalu JSON.parse pecah di baris pertama —
    # "<!doctype…". Jadi ini bukan jawaban model dan bukan kuota habis,
    # melainkan servernya yang sedang tersandung; biasanya pulih sendiri.
    #
    # Tanpa pola ini teksnya diteruskan APA ADANYA sebagai jawaban GLM — persis
    # yang dilaporkan pengguna: tiga giliran berturut-turut "menjawab" kalimat
    # itu, dan agent-nya jalan terus di atas jawaban kosong. Dengan pola ini ia
    # jadi WebBusyError, dan core menunggu 15/40/75 detik lalu mengirim ulang
    # pesan yang sama ke percakapan yang sama.
    busy_patterns = (
        r"(?i)\bno response,?\s*please try again later\b",
        # Cadangan bila kalimat di atas berubah: kotak merahnya tetap muncul.
        # Sengaja MENGGABUNG dua penanda ("token '<'" + "not valid JSON")
        # supaya kalimat yang cuma menyebut salah satunya tak ikut tertangkap.
        r"(?i)unexpected token '<'.{0,60}not valid JSON",
    )
    # Lebih ketat dari bawaan 400 karena pola kedua di atas adalah galat
    # JavaScript yang LAZIM DIBAHAS saat ngoding — dan bagas-ai memang dipakai
    # untuk ngoding. Pemberitahuan aslinya cuma ±105 karakter, sedangkan
    # penjelasan model soal galat yang sama hampir pasti jauh lebih panjang.
    busy_max_chars = 220
    # Pemindaian SEHALAMAN (dipakai saat komposer menolak kirim) tak boleh
    # membaca isi percakapan: sekali galat di atas terjadi, kalimatnya menetap
    # di riwayat chat: tanpa pengecualian ini, tiap kegagalan kirim BERIKUTNYA
    # akan dilaporkan sebagai "server sibuk" karena membaca bekas yang lama.
    limit_exclude_selectors = (
        ".chat-assistant", ".markdown-prose", "[id^='message-']",
    )

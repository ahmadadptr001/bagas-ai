"""Connector gemini.google.com/app (web).

Seluruh selektor di bawah DIPETAKAN LANGSUNG ke situs pada sesi nyata yang sudah
login (2026-08-04): pesan uji dikirim lalu DOM-nya dibaca SELAMA streaming dan
sesudah selesai, menu mode & menu lampiran dibuka dan isinya disensus, satu
berkas benar-benar diunggah, dan seluruhnya diulang pada lima lebar viewport
(1500/1100/860/600/420 px) serta pada skema warna terang & gelap. Yang belum
pernah terlihat langsung ditandai eksplisit — jangan dianggap terbukti.

Isi file ini sebelumnya SALIN-TEMPEL dari connector Qwen: hampir semua
selektornya milik chat.qwen.ai, sehingga tak ada satu pun yang benar-benar
cocok di sini. Yang paling merusak, `send_button_selector` berbunyi
`'button, [aria-label="Kirim pesan"]'` — daftar berkoma yang alternatif
pertamanya `button` polos, jadi cadangan "klik tombol kirim" mengeklik tombol
PERTAMA di halaman (tombol sidebar).

TIGA sifat Gemini menentukan bentuk file ini:

1. UI-nya DITERJEMAHKAN mengikuti bahasa akun Google. Sesi pemetaan berjalan di
   akun berbahasa Indonesia: tombol kirimnya beraria-label "Kirim pesan", bukan
   "Send message". Karena itu TAK SATU PUN selektor wajib bersandar pada
   teks/aria-label — semuanya memakai nama elemen Angular (rich-textarea,
   message-content), data-test-id, atau nama ikon Material
   (`mat-icon[fonticon="…"]`) yang identik di semua bahasa. Aria-label dua
   bahasa hanya dipasang sebagai cadangan paling belakang, dan tak ada satu pun
   jalur yang bergantung padanya.

2. Layoutnya RESPONSIF dan itu MENGHAPUS kontrol, bukan sekadar menggesernya.
   TERUKUR: pemilih model masih ada pada 600 px tetapi LENYAP pada 430 px,
   sementara menu mode punya DUA salinan (desktop & mobile) yang sama-sama hadir
   di DOM. Karena itu min_layout_width dipasang — lihat _normalize_layout di
   base.py — dan item menu diseleksi `:visible` lebih dulu.

3. Tema terang/gelap hanya menambah kelas `dark-theme` pada <body>. TERUKUR
   pada kedua tema: sensus wadah jawaban, tombol komposer, tombol stop, dan
   kontrol mode memberi hasil yang persis sama. Karena itu tak ada selektor
   yang boleh bersandar pada warna atau kelas tema — dan sekaligus tak ada yang
   perlu diuji ulang tiap kali pengguna berganti tema.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .base import WebConnector
from .browser import BrowserError


def _ikon(nama: str) -> str:
    """Tombol yang memuat ikon Material `nama`.

    Nama ikon (atribut `fonticon`) TIDAK ikut diterjemahkan, sedangkan
    aria-label ikut — jadi inilah pegangan yang sama di akun berbahasa apa pun.
    """
    return f'button:has(mat-icon[fonticon="{nama}"])'


# Pembuka menu MODE (pemilih varian model) — satu-satunya kontrol /effort di
# Gemini, letaknya menempel di komposer. DIVERIFIKASI: elemennya <button>
# ber-data-test-id, jadi tak perlu tebak-tebakan [role="button"].
_BTN_MODE = '[data-test-id="bard-mode-menu-button"]'
# Pembuka laci "Upload & alat" (ikon plus) di komposer.
_BTN_UPLOAD = _ikon("plus")


class GeminiConnector(WebConnector):
    service = "gemini"
    label = "Gemini (web)"
    chat_url = "https://gemini.google.com/app"
    chat_url_template = "https://gemini.google.com/app/{id}"
    # DIVERIFIKASI: URL berubah jadi .../app/7ca3448599d4f3f5 begitu wadah
    # jawaban dibuat (detik ~5 sesudah kirim), bukan cuma di akhir giliran —
    # 16 digit heksadesimal, TANPA tanda hubung.
    #
    # Pola sebelumnya `/c/([0-9a-fA-F-]{16,})` milik situs lain dan TAK PERNAH
    # cocok di sini. Akibatnya bukan sekadar kosmetik: last_chat_id selalu ""
    # sehingga --resume dan "lanjutkan sesi" mustahil menyambung ke percakapan
    # yang sama, dan catatan chat buatan bagas-ai tak pernah terisi.
    chat_id_pattern = r"/app/([0-9a-fA-F]{8,})"

    # Layout MINIMAL yang dipaksakan bila jendela Chrome lebih sempit (lihat
    # sifat #2 di docstring). 1100x720 = ambang tempat seluruh kontrol komposer
    # TERUKUR masih hadir, dengan kelonggaran di atas titik putus yang terukur
    # (antara 430 px dan 600 px).
    min_layout_width = 1100
    min_layout_height = 720

    # --- input ---
    # DIVERIFIKASI: kotak inputnya editor Quill — <div class="ql-editor"
    # contenteditable="true"> di dalam <rich-textarea>, BUKAN <textarea>.
    #
    # `div.single-line-format` (kandidat satu-satunya sebelum ini) SENGAJA
    # dibuang, bukan digeser ke belakang: kelas itu menandai keadaan "isinya
    # masih satu baris" dan LEPAS begitu teks membungkus ke baris kedua.
    # Karena kotak input dipegang sebagai Locator yang diselesaikan ulang setiap
    # kali dipakai, selektor yang menguap di tengah jalan membuat _input_text()
    # mengembalikan "" — dan bagi _submit() kotak kosong berarti "pesan sudah
    # berangkat". Prompt bagas-ai selalu jauh lebih dari satu baris, jadi jalur
    # itu praktis selalu kena.
    input_selector = (
        "rich-textarea .ql-editor",
        '[data-test-id="textarea-wrapper"] [contenteditable="true"]',
        'rich-textarea [contenteditable="true"]',
        '.text-input-field [contenteditable="true"]',
    )
    input_is_contenteditable = True
    submit_key = "Enter"
    # Tombol kirim = ikon `arrow_upward` di komposer (aria-label "Kirim pesan" /
    # "Send message"). DIVERIFIKASI: tombolnya HANYA ada saat komposer berisi —
    # itu wajar dan tak mengganggu, sebab _submit() hanya memakainya sebagai
    # cadangan ketika teks ternyata masih tertinggal di kotak.
    #
    # SATU TOMBOL, TIGA WAJAH: <button> dalam send-button-container adalah
    # elemen YANG SAMA yang selagi Gemini menjawab berganti ikon `stop` (lihat
    # stop_selectors). Selektor wadah polos tetap cocok pada keadaan itu,
    # sehingga klik "tombol kirim" yang salah waktu malah MENGHENTIKAN jawaban
    # yang sedang berjalan. Karena itu TIAP kandidat di bawah menuntut ikon
    # arrow_upward / aria-label kirim — tombol dalam wajah lain tak pernah
    # terpilih. Tuple (bukan satu daftar berkoma) agar kandidat paling spesifik
    # benar-benar dicoba duluan; aria-label dua bahasa cuma jaring pengaman
    # terakhir.
    send_button_selector = (
        '[data-test-id="send-button-container"] button:has('
        'mat-icon[fonticon="arrow_upward"])',
        _ikon("arrow_upward"),
        'button[aria-label="Kirim pesan"]',
        'button[aria-label="Send message"]',
    )
    # Tombol "chat baru" di sidebar — DIVERIFIKASI hadir & terlihat pada KELIMA
    # lebar yang diuji (termasuk 420 px). Dipakai agar memulai percakapan baru
    # tak perlu memuat ulang seluruh SPA.
    new_chat_selector = ('[data-test-id="new-chat-button"]',)

    # --- deteksi belum-login ---
    #
    # DIUKUR LANGSUNG pada dua profil Chrome (2026-08-04): satu profil kosong
    # (tamu) dan satu profil bagas-ai yang sudah masuk. Anggapan lama — "Gemini
    # tak menyajikan komposer untuk tamu, pengunjung dilempar ke
    # accounts.google.com" — TERBUKTI SALAH, dan itulah sebab sesi yang belum
    # masuk dicap sudah login:
    #
    #   penanda                     tamu          sudah login
    #   --------------------------  ------------  ------------
    #   URL                         tetap /app    tetap /app     -> penanda URL tak pernah kena
    #   chat-history-container      ADA           ada            -> TAK membedakan
    #   rich-textarea .ql-editor    ADA, terlihat ada, terlihat  -> TAK membedakan
    #   new-chat-button             tidak ada     ADA
    #   a[href*="SignOutOptions"]   tidak ada     ADA
    #   a[href*="ServiceLogin"]     ADA           tidak ada
    #
    # Jadi tamu memang mendapat komposer yang tampak aktif — "input terlihat"
    # sama sekali bukan bukti — dan chat-history-container yang dulu dipakai
    # sebagai bukti positif justru hadir di kedua keadaan.
    #
    # Penanda BELUM login: tautan sign-in Google. Hadir pada tamu (tersembunyi,
    # tapi _looks_logged_out memeriksa KEBERADAAN, bukan visibilitas) dan
    # terbukti TIDAK ada pada sesi yang sudah masuk — jadi ia tak bisa
    # menjatuhkan sesi yang sehat.
    logged_out_selector = 'a[href*="ServiceLogin"]'
    # BUKTI POSITIF sudah login. Keduanya terbukti ABSEN pada tamu dan HADIR
    # saat sudah masuk. Dua penanda, bukan satu, supaya penggantian nama salah
    # satunya tak langsung membuat sesi yang sehat divonis "belum login":
    # new-chat-button milik Gemini sendiri, SignOutOptions milik bilah akun
    # Google yang dipakai seragam di seluruh produknya.
    logged_in_selector = (
        '[data-test-id="new-chat-button"], a[href*="SignOutOptions"]'
    )

    # --- jawaban ---
    # URUTAN INI HASIL PENGUKURAN, JANGAN DIBALIK.
    #
    # `model-response` dan `response-container` SENGAJA TIDAK dipakai walau
    # keduanya juga satu-elemen-per-jawaban: di dalamnya ada label pembaca layar
    # `.cdk-visually-hidden.screen-reader-model-response-label` berbunyi "Gemini
    # berkata" ("Gemini said"). TERUKUR pada jawaban yang sama —
    #   model-response  -> 37 karakter
    #   message-content -> 21 karakter
    # selisih 16 = persis "Gemini berkata\n\n". Label itu disembunyikan dengan
    # teknik clip (BUKAN display:none), jadi penyaring "lewati yang tak
    # terender" di base tak membuangnya: ia akan menempel di awal SETIAP
    # jawaban.
    #
    # `.markdown` ditaruh belakangan: di Gemini ia satu elemen per jawaban
    # (bukan per paragraf), tapi blok berpikir juga memakainya, jadi ia hanya
    # cadangan bila dua yang pertama berubah nama.
    message_selector = (
        "message-content",
        ".model-response-text",
        "message-content .markdown",
        ".markdown",
    )
    read_as_markdown = True
    # Tombol STOP (ikon `stop`) hanya ada selagi Gemini menjawab — DIUKUR lewat
    # sampling tiap 0,7 detik sepanjang satu giliran: bernilai 1 dari detik
    # pertama sampai jawaban tuntas, lalu 0. Ini juga penanda "sudah mulai"
    # yang menutup jendela ~4 detik pertama, ketika <message-content> bahkan
    # BELUM ADA di DOM.
    stop_selectors = (
        _ikon("stop"),
        '[aria-label="Hentikan respons"]',
        '[aria-label="Stop response"]',
    )
    # Selagi berpikir, teks yang muncul hanyalah indikator. Dua bahasa sekaligus
    # karena UI mengikuti bahasa akun.
    noise_pattern = (
        r"(?:(?:Berpikir|Sedang berpikir|Menampilkan proses berpikir|Menganalisis"
        r"|Thinking|Show thinking|Analyzing|Thought for|Berpikir selama)"
        r"[^\n]*\s*)+"
    )
    # Bagian yang bisa berada DI DALAM wadah jawaban tapi bukan jawaban:
    #   .cdk-visually-hidden -> label pembaca layar (lihat catatan di atas).
    #       Dipasang walau message-content saat ini bersih, karena inilah satu-
    #       satunya kebocoran yang tak tersaring sendiri oleh base.
    #   model-thoughts / .thoughts-content / thinking-overlay -> blok BERPIKIR.
    #       Pada sesi pemetaan panel berpikirnya tak dirender (yang ada hanya
    #       kelas penanda `has-thoughts`), jadi ketiganya BELUM TERBUKTI — tapi
    #       memasangnya tak berisiko: base membatalkan penyaringan yang malah
    #       mengosongkan jawaban.
    #   sources-list -> daftar rujukan hasil telusur, chrome UI, bukan isi.
    strip_selectors = (
        ".cdk-visually-hidden",
        "model-thoughts",
        ".thoughts-content",
        "thinking-overlay",
        "sources-list",
    )

    # --- lampiran (screenshot dll) ---
    # DIPETAKAN dari klik sungguhan, bukan tebakan:
    #   tombol ikon `plus` ("Upload & alat")
    #     -> mat-action-list[role="menu"]
    #        -> button[data-test-id="local-images-files-uploader-button"]
    #           (role="menuitem", ikon `attach_file`)
    #           -> membuka pemilih berkas OS
    # Dipakai expect_file_chooser sehingga tak ada dialog sistem yang
    # benar-benar terbuka.
    #
    # Nilai di bawah dipakai supports_attachments() sebagai penanda "situs ini
    # menerima lampiran"; pekerjaan sesungguhnya ada di _upload().
    file_input_selector = '[data-test-id="local-images-files-uploader-button"]'
    # Kartu pratinjau di komposer sesudah berkas menempel. DIVERIFIKASI dengan
    # mengunggah satu PNG: muncul <uploader-file-preview> (satu per berkas)
    # berisi .file-preview-chip. Menghitung kartu jauh lebih andal daripada
    # menunggu <img> di sekitar kotak input.
    #
    # BELUM TERPETAKAN: penanda "unggahan masih berjalan" (di Kimi berupa kelas
    # `loading`). Karena itu tak ada `:not(...)` yang dipasang asal-asalan di
    # sini; base sudah memberi jeda tambahan sesudah kartunya muncul, dan
    # _submit() memverifikasi kotak input benar-benar kosong.
    attach_item_selector = "uploader-file-preview"

    # --- /effort: pemilih mode ---
    # DISENSUS langsung dari menu yang terbuka: <gem-menu role="menu"> berisi
    # <gem-menu-item role="menuitem"> — tiga varian model ber-data-test-id
    # `bard-mode-option-<hash>`, lalu pemisah, lalu satu sakelar penalaran.
    #
    # `:visible` didahulukan karena menunya HADIR DUA KALI di DOM (salinan
    # desktop & mobile). Tanpa itu, `.first` bisa mendarat di salinan yang tak
    # terlihat — dan karena klik nyata yang gagal jatuh ke dispatch event,
    # kliknya "berhasil" pada elemen yang salah tanpa galat apa pun.
    menu_item_selector = (
        "gem-menu-item:visible",
        '[role="menuitem"]:visible',
        "gem-menu-item",
        '[role="menuitem"]',
    )
    web_model_button = _BTN_MODE
    # Nama varian ("3.5 Flash-Lite", dst) berupa angka+kata teknis yang TIDAK
    # diterjemahkan, jadi aman dipakai sebagai teks klik. Sakelar penalaran
    # (bukan varian model) ditulis sebagai beberapa alternatif dipisah "|" dan
    # ditangani _click_menu_text di bawah.
    #
    # Pecahan /model vs /effort: tiga varian pertama adalah MODEL (punya nama
    # sendiri di situsnya) -> web_models; sakelar penalaran adalah USAHA
    # BERPIKIR -> web_efforts.
    web_models = (
        ("3.5 Flash-Lite", ("3.5 Flash-Lite",),
         "jawaban tercepat", _BTN_MODE),
        ("3.6 Flash", ("3.6 Flash",),
         "bantuan serbaguna (bawaan)", _BTN_MODE),
        ("3.1 Pro", ("3.1 Pro",),
         "matematika & coding tingkat lanjut", _BTN_MODE),
    )
    web_efforts = (
        ("Penalaran diperluas",
         ("Penalaran yang diperluas|Extended reasoning|Expanded reasoning",),
         "sakelar: berpikir lebih dalam untuk masalah kompleks", _BTN_MODE),
    )

    # Teks pemberitahuan limit Gemini BELUM PERNAH terlihat pada sesi pemetaan,
    # jadi pola di bawah bertumpu pada bentuk kalimatnya saja. Dua penjaga
    # membuatnya tetap aman: pemindaian MELEWATI area percakapan & sidebar
    # (lihat limit_exclude_selectors), sehingga jawaban Gemini yang kebetulan
    # membahas "batas harian" — atau judul chat lama yang memuat frasa itu —
    # tak bisa membatalkan giliran yang sehat.
    limit_patterns = (
        r"reached your limit",
        r"telah mencapai batas",
    )
    limit_exclude_selectors = (
        ".conversation-container",
        "model-response",
        "user-query",
        '[data-test-id="chat-history-container"]',
    )

    # Item menu yang membuka pemilih berkas, urut dari yang paling spesifik.
    _ITEM_UPLOAD = (
        '[data-test-id="local-images-files-uploader-button"]',
        '[role="menuitem"]:has(mat-icon[fonticon="attach_file"])',
    )

    # Percakapan yang sedang tampil — dipakai memastikan "chat baru" benar-benar
    # sudah bersih (lihat _click_new_chat).
    _ISI_PERCAKAPAN = "message-content, user-query"

    def _click_new_chat(
        self, page: Any, check_cancel: Callable[[], None] | None = None
    ) -> bool:
        """Klik "chat baru", lalu TUNGGU percakapan lama lenyap dari DOM.

        Base menganggap chat baru siap begitu kotak input terlihat — dan di
        Gemini kotak itu terlihat SEPANJANG transisi, termasuk saat SPA masih
        membongkar percakapan sebelumnya. Prompt yang diketik tepat di jendela
        itu ikut hilang bersama transisinya: komposer memang jadi kosong
        (sehingga _submit menyimpulkan "terkirim"), tapi tak ada jawaban yang
        pernah datang, dan giliran berakhir dengan "tidak menghasilkan balasan
        baru" — TERAMATI sekali pada pengujian, sesudah percakapan panjang.

        Menunggu wadah pesan benar-benar KOSONG menutup jendela itu. Kalau
        dalam batas waktu belum juga bersih, sengaja mengembalikan False supaya
        base jatuh ke navigasi biasa (memuat ulang halaman chat) — lebih lambat,
        tapi keadaan bersihnya dijamin."""
        if not super()._click_new_chat(page, check_cancel):
            return False
        deadline = time.time() + 6.0
        while time.time() < deadline:
            if check_cancel is not None:
                check_cancel()
            try:
                if page.locator(self._ISI_PERCAKAPAN).count() == 0:
                    return True
            except Exception:  # noqa: BLE001 - DOM sedang dirender ulang
                pass
            page.wait_for_timeout(200)
        return False

    def _click_menu_text(self, page: Any, text: str) -> None:
        """Seperti base, tapi menerima BEBERAPA teks alternatif dipisah "|".

        Alasannya khusus Gemini: label item menu ikut bahasa akun, jadi satu
        teks pasti gagal di sebagian akun. Varian dicoba berurutan dan yang
        pertama ada yang diklik. Teks tanpa "|" berperilaku persis seperti
        sebelumnya."""
        varian = [v.strip() for v in text.split("|") if v.strip()]
        for v in varian:
            try:
                super()._click_menu_text(page, v)
                return
            except BrowserError:
                continue
        raise BrowserError(
            "item menu tak ditemukan untuk semua varian teks: "
            + " / ".join(varian)
        )

    def _upload(self, page: Any, paths: list[str]) -> None:
        """Lampirkan berkas lewat laci "Upload & alat", meniru klik pengguna.

        Kenapa bukan set_input_files: input berkasnya BARU dibuat saat item menu
        diklik, jadi tak ada apa pun untuk diisi sebelum laci terbuka. Klik itu
        sendiri yang memicu pemilih berkas, dan expect_file_chooser
        menangkapnya sehingga tak ada dialog OS yang sungguh terbuka.

        Klik memakai _click_element: jendela connector berjalan tersembunyi, dan
        di sana klik mouse sungguhan gagal hit-test sehingga perlu jatuh ke
        dispatch event."""
        try:
            self._click_element(page.locator(_BTN_UPLOAD).first)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(
                "tombol lampiran (ikon +) Gemini tak bisa diklik — komposer "
                f"mungkin berubah: {exc}"
            ) from exc
        page.wait_for_timeout(600)

        item = None
        for kandidat in self._ITEM_UPLOAD:
            try:
                loc = page.locator(kandidat)
                if loc.count():
                    item = loc.first
                    break
            except Exception:  # noqa: BLE001 - selektor tak sah -> kandidat lain
                continue
        if item is None:
            self._tutup_laci(page)
            raise BrowserError(
                "menu 'Upload & alat' Gemini terbuka tapi item unggah berkas "
                "tak ditemukan — data-test-id-nya mungkin berubah "
                f"({', '.join(self._ITEM_UPLOAD)})."
            )

        try:
            with page.expect_file_chooser(timeout=15000) as chooser:
                self._click_element(item)
            chooser.value.set_files(paths)
        except Exception as exc:  # noqa: BLE001
            self._tutup_laci(page)
            raise BrowserError(
                f"pemilih berkas Gemini tak terbuka: {exc}"
            ) from exc

    @staticmethod
    def _tutup_laci(page: Any) -> None:
        """Tutup laci lampiran yang terlanjur terbuka supaya tak menutupi
        komposer pada giliran berikutnya."""
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass

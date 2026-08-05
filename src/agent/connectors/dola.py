"""Connector Dola (web) — asisten AI ByteDance (dulu bernama Cici).

CATATAN DOMAIN, supaya tak membingungkan saat menelusuri nanti: cici.com
MENGALIHKAN ke https://www.dola.com/chat/ dan produknya berganti nama jadi
"Dola AI". DIVERIFIKASI dengan memuat cici.com lewat Playwright: HTTP 200,
`location.href` berakhir di www.dola.com/chat/, judul halaman "Dola AI - Your
everyday AI assistant". Alamat resminya dipakai langsung di sini; alias "cici"
tetap diterima saat memilih model karena nama itu masih melekat.

Kenapa connector ini ada: berbeda dari Kimi/Qwen/Gemini yang serba teks, Dola
punya pembuat GAMBAR dan VIDEO (tombol "Create Image" & "Create Video"
TERVERIFIKASI ada di halaman). Itulah alasan tunggal memilihnya — untuk kerja
kode & agentic, connector lain lebih tepat.

STATUS PEMETAAN SELEKTOR — dibaca apa adanya, jangan dianggap lebih:
  * TERVERIFIKASI sebagai TAMU: kotak input, tombol kirim, penanda Log In,
    item sidebar New Chat, dan ketiadaan input[type=file].
  * TERVERIFIKASI SESUDAH LOGIN, dengan SATU pesan terpendek ("hi") — kuota
    gratisnya terbatas, jadi seluruh pemetaan di bawah dipanen dari satu
    putaran itu saja: wadah pesan, penanda sedang-menulis, pola URL
    percakapan, dan penanda sudah-login. Caranya sensus kelas SEBELUM vs
    SELAMA vs SESUDAH jawaban, lalu membandingkan selisihnya — bukan menebak
    dari nama kelas.
  * BELUM TERVERIFIKASI: pemberitahuan kuota habis (butuh kuota yang
    benar-benar habis) dan jalur melampirkan berkas.

Selektor bertanda hash (mis. `loading-container-AGJEWI`, `login-btn-header-
CTKsn1`) TIDAK pernah dipakai utuh: akhiran acak itu berasal dari CSS-module
dan berganti tiap build situs, jadi yang dipakai hanya awalannya lewat
[class*=…].
"""
from __future__ import annotations

from .base import WebConnector


class DolaConnector(WebConnector):
    service = "dola"
    label = "Dola (web)"
    # Alamat resminya langsung, bukan cici.com yang cuma mengalihkan:
    # satu lompatan pengalihan lebih sedikit di tiap peluncuran.
    chat_url = "https://www.dola.com/chat/"
    # DIVERIFIKASI dari percakapan sungguhan: URL-nya
    #   https://www.dola.com/chat/38416165758749201
    # — id ANGKA, bukan uuid. Pola dibatasi ke digit supaya tak salah menangkap
    # potongan path lain.
    chat_url_template = "https://www.dola.com/chat/{id}"
    chat_id_pattern = r"/chat/(\d{8,})"

    show_window = False

    # --- input (TERVERIFIKASI sebagai tamu) ---
    # Halaman memakai Semi Design (design system ByteDance): kotak ketiknya
    # <textarea class="semi-input-textarea semi-input-textarea-autosize">,
    # BUKAN contenteditable. Tak ada elemen [contenteditable] sama sekali di
    # halaman itu — jadi urutan kandidatnya dimulai dari textarea.
    input_selector = (
        "textarea.semi-input-textarea",
        ".semi-input-textarea",
        "textarea",
    )
    input_is_contenteditable = False
    submit_key = "Enter"
    # TERVERIFIKASI: ada <button id="flow-end-msg-send"> di halaman. Id (bukan
    # kelas ber-hash) jauh lebih tahan perubahan build, jadi ia didahulukan.
    send_button_selector = (
        "#flow-end-msg-send, button[type=submit], "
        '[class*="send-btn"], [class*="send-button"]'
    )
    # TERVERIFIKASI dengan sensus DOM: "New Chat" BUKAN <button> maupun <a> —
    # ia <div class="group/sidebar_nav_item cursor-pointer …"> berisi
    # "New Chat\nCtrl Shift K". Tebakan pertama (button/a) TERBUKTI tak cocok
    # satu pun, jadi jangan dikembalikan.
    #
    # Yang dipakai: potongan kelas `sidebar_nav_item` (stabil, bukan hash acak)
    # DISARING dengan teksnya — kelas itu dipakai bersama oleh item sidebar
    # lain ("AI Creation"), jadi tanpa penyaring teks bisa salah klik.
    new_chat_selector = (
        '[class*="sidebar_nav_item"]:has-text("New Chat")',
        'div[class*="cursor-pointer"]:has-text("New Chat")',
        '[class*="new-chat"]',
    )

    # --- deteksi belum-login (TERVERIFIKASI sebagai tamu) ---
    # Halaman tamu menampilkan tombol "Log In" di header DAN panel "Log In to
    # Unlock More Features" berisi "Continue with Google". Sama seperti Kimi:
    # kotak input TETAP terlihat untuk tamu, jadi "input terlihat" bukan bukti
    # sudah login — tombol Log In inilah penandanya.
    logged_out_selector = (
        'button:has-text("Log In"), button:has-text("Log in"), '
        'button:has-text("Sign in"), button:has-text("Continue with Google")'
    )
    # DIVERIFIKASI dengan MEMBANDINGKAN halaman tamu vs halaman sesudah login:
    # "Chat History" di sidebar hanya ada sesudah login (halaman tamu memuat
    # Dola / New Chat / AI Creation / Log In, tanpa riwayat). Inilah yang
    # menutup jendela rawan saat kotak input sudah terlihat tapi sesinya masih
    # tamu.
    #
    # Nama akun & tombol profil sengaja TIDAK dipakai walau ikut terlihat:
    # id-nya `radix-:r3a:` — dibangkitkan Radix UI dan berubah tiap render.
    logged_in_selector = '[class*="text-dbx-text-secondary"]:has-text("Chat History")'

    # --- jawaban (DIVERIFIKASI dari satu percakapan sungguhan) ---
    #
    # PERINGATAN yang menentukan cara membacanya: wadah pesan Dola TIDAK
    # membedakan penanya dan penjawab. Diukur pada percakapan "hi" ->
    # "Hi there! 😊 How can I help you today?", ketiga kandidat di bawah cocok
    # ke DUA elemen — pesan pengguna DAN jawabannya — dengan kelas yang sama
    # persis (`container-qX9Csx md-box-root`, `v_list_row`, `inner-item-…`).
    # Tak ada satu pun kelas khusus asisten di DOM-nya.
    #
    # Karena itu yang membedakan adalah URUTAN: base.py mengambil kecocokan
    # TERAKHIR yang ada isinya, dan jawaban selalu datang sesudah pertanyaan.
    # Konsekuensinya stop_selectors di bawah WAJIB benar — kalau penantiannya
    # berhenti terlalu dini, yang terbaca sebagai "jawaban" adalah pesan
    # pengguna sendiri.
    message_selector = (
        '[class*="md-box-root"]',
        ".v_list_row",
        '[class*="inner-item"]',
    )
    read_as_markdown = True
    # DIVERIFIKASI dengan sensus kelas SEBELUM vs SELAMA vs SESUDAH menulis:
    # lima token kelas hanya hadir selama Dola menyusun jawaban, lalu hilang —
    # loading-container-AGJEWI, dot-flashing-mIsXoz, dot-BU8RO9, loading-i3Fu5w,
    # loading-border-AcFju9.
    #
    # Akhiran acaknya (AGJEWI, mIsXoz, …) berasal dari CSS-module dan berganti
    # tiap build situs, jadi yang dipakai AWALANNYA saja lewat [class*=…].
    stop_selectors = (
        '[class*="loading-container"]',
        '[class*="dot-flashing"]',
        '[class*="loading-border"]',
    )
    # Belum ada bagian bukan-jawaban yang teramati di dalam wadahnya (balasan
    # ujinya pendek & tanpa blok berpikir). Dibiarkan kosong daripada diisi
    # tebakan: strip_selectors yang salah membuang isi jawaban yang sah.
    strip_selectors = ()

    # --- kuota gratis habis (BELUM TERVERIFIKASI) ---
    # Dola membagi kuota gratis harian, dan pemberitahuannya baru muncul SESUDAH
    # prompt dikirim. Tanpa deteksi ini, bagas-ai menunggu jawaban yang memang
    # tak akan datang lalu gagal dengan pesan yang membingungkan.
    #
    # Pola di bawah menutup bentuk yang lazim dipakai produk ByteDance dalam
    # bahasa Inggris & Indonesia. Begitu bentuk aslinya terlihat sekali saja,
    # GANTI daftar ini dengan kalimat yang sungguh-sungguh muncul — tebakan
    # yang kelewat longgar bisa membatalkan giliran yang sebenarnya normal.
    limit_patterns = (
        r"(?:daily|free)\s+(?:limit|quota)\s+(?:reached|exceeded)",
        r"reached\s+(?:your|the)\s+(?:daily\s+)?limit",
        r"run\s+out\s+of\s+(?:free\s+)?(?:credits?|quota)",
        r"no\s+(?:more\s+)?(?:free\s+)?credits?\s+(?:left|remaining)",
        r"upgrade\s+to\s+continue",
        r"kuota\s+(?:gratis\s+)?(?:harian\s+)?(?:habis|telah habis)",
        r"batas\s+(?:harian|penggunaan)\s+(?:tercapai|habis)",
        r"(?:免费)?额度(?:已)?(?:用完|不足)",
    )
    # Jawaban model sendiri bisa MEMBAHAS kuota/limit, dan itu bukan tanda kuota
    # habis. Wadah percakapan karena itu dikecualikan dari pemindaian.
    limit_exclude_selectors = (
        '[class*="message"]',
        '[class*="conversation"]',
        '[class*="chat-list"]',
    )

    # --- lampiran (DIVERIFIKASI tak ada input langsung) ---
    # Diperiksa pada halaman tamu MAUPUN sesudah login, termasuk di dalam
    # percakapan yang sudah berjalan: `input[type=file]` berjumlah NOL di
    # ketiganya. Jadi unggahan Dola pasti dibuat saat menu/tombolnya diklik
    # (pola yang sama dengan Qwen), bukan lewat input tersembunyi.
    #
    # Dibiarkan kosong sampai jalur kliknya dipetakan: file_input_selector yang
    # salah membuat lampiran "berhasil" tanpa berkas yang benar-benar terkirim
    # — kegagalan senyap, jenis yang paling mahal.
    file_input_selector = ""

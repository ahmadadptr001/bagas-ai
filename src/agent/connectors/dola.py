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
  * TERVERIFIKASI sebagai TAMU (tanpa login, tanpa mengirim pesan sama sekali,
    karena kuota gratisnya terbatas): kotak input, tombol Log In, tombol kirim,
    tombol New Chat, dan ketiadaan input[type=file] di halaman tamu.
  * BELUM TERVERIFIKASI: wadah jawaban, penanda streaming, penanda sudah-login,
    pola URL percakapan, dan pemberitahuan kuota habis. Semuanya butuh SATU
    sesi login sungguhan untuk dipetakan. Yang tertulis di bawah adalah
    kandidat berlapis dengan cadangan longgar, BUKAN hasil pengukuran.

Selektor bertanda hash (mis. `login-btn-header-CTKsn1`) sengaja TIDAK dipakai
sendirian: akhiran acak begitu berasal dari CSS-module dan berubah tiap build
situs, jadi ia cuma dipakai sebagai kandidat terakhir.
"""
from __future__ import annotations

from .base import WebConnector


class DolaConnector(WebConnector):
    service = "dola"
    label = "Dola (web)"
    # Alamat resminya langsung, bukan cici.com yang cuma mengalihkan:
    # satu lompatan pengalihan lebih sedikit di tiap peluncuran.
    chat_url = "https://www.dola.com/chat/"
    # BELUM TERVERIFIKASI: bentuk URL percakapan hanya terlihat setelah ada chat
    # sungguhan. Pola bawaan base.py dipakai sampai terbukti lain; kalau ternyata
    # beda, yang gagal cuma "lanjutkan percakapan lama" — bukan chat barunya.
    chat_url_template = "https://www.dola.com/chat/{id}"
    chat_id_pattern = r"/chat/([A-Za-z0-9_-]{8,})"

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
    # BELUM TERVERIFIKASI. Dikosongkan dengan sengaja: bukti-positif yang salah
    # tebak lebih berbahaya daripada tidak ada — ia bisa membuat sesi tamu
    # dikira sudah login, lalu pesan dikirim ke halaman yang tak memprosesnya.
    # base.py menangani kekosongan ini dengan bersandar pada logged_out_selector.
    logged_in_selector = ""

    # --- jawaban (BELUM TERVERIFIKASI) ---
    # Kandidat disusun dari yang paling spesifik ke paling longgar. Yang paling
    # longgar sengaja tetap ada supaya giliran pertama punya peluang terbaca
    # walau namanya berbeda; begitu sesi login pertama dipetakan, daftar ini
    # HARUS dipersempit — kandidat longgar rawan menangkap bilah tombol di
    # bawah jawaban (pelajaran terukur dari connector Kimi).
    message_selector = (
        '[class*="message-content"]',
        '[class*="answer-content"]',
        '[class*="markdown"]',
        '[data-testid*="message"]',
        '[class*="assistant"]',
    )
    read_as_markdown = True
    # BELUM TERVERIFIKASI: tombol berhenti biasanya muncul selama menulis.
    stop_selectors = (
        '[class*="stop-btn"]',
        '[class*="stop-button"]',
        'button[aria-label*="Stop" i]',
    )
    # Bagian yang ada di dalam wadah jawaban tapi bukan jawaban. Ditebak dari
    # pola yang lazim; ditandai jelas supaya tak dikira hasil pengukuran.
    strip_selectors = (
        '[class*="thinking"]',
        '[class*="reasoning"]',
        '[class*="action-bar"]',
        '[class*="msg-action"]',
    )

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

    # --- lampiran (BELUM TERVERIFIKASI) ---
    # Halaman TAMU tak punya satu pun input[type=file] (terverifikasi), jadi
    # unggahan kemungkinan baru dipasang setelah login atau dibuat lewat menu.
    # Dikosongkan sampai terbukti: file_input_selector yang salah membuat
    # lampiran "berhasil" tanpa file yang benar-benar terkirim.
    file_input_selector = ""

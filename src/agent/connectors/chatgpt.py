"""Connector chatgpt.com (web) — OpenAI ChatGPT.

Selector dipetakan ke DOM chatgpt.com pada sesi yang sudah login.
ChatGPT memakai ProseMirror (contenteditable) untuk input, bukan textarea.

Kalau layout situs berubah & jawaban tak lagi terbaca, cukup sesuaikan
selector DI FILE INI; sisanya (kirim, tunggu, streaming, lampiran)
ditangani base.py.
"""
from __future__ import annotations

from typing import Any

from .base import WebConnector


class ChatGPTConnector(WebConnector):
    service = "chatgpt"
    label = "ChatGPT (web)"

    chat_url = "https://chatgpt.com/"
    chat_url_template = "https://chatgpt.com/c/{id}"
    # ChatGPT uses UUID v4 format: 8-4-4-4-12 hex digits with hyphens.
    chat_id_pattern = r"/c/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"

    # ChatGPT berjalan di LATAR seperti connector lain.
    show_window = False

    # --- input ---
    # ChatGPT memakai ProseMirror (div#prompt-textarea contenteditable).
    # Kandidat berikutnya sebagai cadangan bila situs mengubah selektornya.
    input_selector = (
        "div#prompt-textarea[contenteditable='true']",
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true'][data-testid='chat-input']",
        "div[contenteditable='true']",
    )
    input_is_contenteditable = True
    submit_key = "Enter"
    # Tombol kirim = tombol dengan data-testid "send-button" di dalam composer.
    # Diverifikasi 2026: tombol ini HANYA dirender saat komposer berisi teks
    # (saat kosong yang tampil tombol mikrofon) — jadi n=0 pada komposer
    # kosong bukan berarti selector usang. Varian aria Indonesia ditambah
    # karena UI mengikuti bahasa akun ('Kirim perintah').
    send_button_selector = (
        'button[data-testid="send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Kirim prompt"]',
        'button[aria-label="Kirim perintah"]',
    )
    # Tombol chat baru di sidebar.
    # 'create-new-chat-button' diverifikasi ada di DOM 2026 (n=2) dan PALING
    # spesifik; a[href="/"] ambigu (banyak tautan ke root) jadi cuma cadangan.
    new_chat_selector = (
        '[data-testid="create-new-chat-button"]',
        'nav a:has-text("New chat")',
        'button:has-text("New chat")',
        'button:has-text("Chat baru")',
        'a[href="/"]',
    )

    # --- deteksi belum-login ---
    # Tombol "Log in" / "Sign up" hanya muncul untuk tamu.
    logged_out_selector = (
        'button:has-text("Log in"), button:has-text("Sign up"), '
        'button:has-text("Log in with Google"), '
        'button:has-text("Masuk"), button:has-text("Daftar")'
    )
    # BUKTI POSITIF sudah login. WAJIB berupa SATU string CSS ber-koma:
    # _looks_logged_in memakai page.query_selector yang hanya menerima
    # string — tuple malah melempar TypeError yang ditelan, sehingga
    # deteksi login SELALU gagal walau pengguna sudah login (bug nyata:
    # connector menunggu login selamanya). 'accounts-profile-button' adalah
    # penanda 2026 (terverifikasi); 'profile-button' lama dipertahankan
    # sebagai cadangan.
    logged_in_selector = (
        '[data-testid="accounts-profile-button"], '
        'button[data-testid="profile-button"], '
        'button[aria-label="User menu"], '
        'a[href*="/auth/logout"]'
    )

    # --- baca jawaban ---
    # Blok jawaban assistant — DIVERIFIKASI: data-message-author-role="assistant"
    message_selector = (
        'div[data-message-author-role="assistant"]',
        'div.agent-turn',
    )
    read_as_markdown = True

    # Tombol stop generation — WAJIB ada. Tanpanya _is_done() selalu True
    # sehingga penantian hanya bersandar pada "teks berhenti berubah", yang
    # TERUKUR salah: teks sempat melompat saat DOM dirender ulang, memotong
    # jawaban di tengah jalan (persis bug yang ditemukan di Kimi).
    #   Nama atribut: stop_selectors (tuple), BUKAN stop_selector (singular).
    stop_selectors = (
        'button[aria-label="Stop generating"]',
        'button[aria-label="Hentikan pembuatan"]',
        'button[data-testid="stop-button"]',
    )

    # Pola noise yang harus dibuang dari jawaban (thinking process, dll).
    # ChatGPT menampilkan "Thinking..." / "Reasoning..." saat mode reasoning
    # aktif. Pola ini menangkap variasi Bahasa Inggris + Indonesia.
    noise_pattern = (
        r"(?:Thinking\.{0,3}|Thought for \d+|Thought Process|Reasoning"
        r"|Berpikir\.{0,3}|Sedang berpikir"
        r")\s*"
    )

    # Elemen yang harus dibuang dari jawaban.
    #
    # PENTING: `pre` dan `code` TIDAK boleh ada di sini! ChatGPT sering
    # menghasilkan blok kode program — bila dibuang, jawaban kehilangan
    # seluruh kode. Connector lain (Kimi, Gemini) juga tidak membuang
    # pre/code karena base sudah menangani rendering kode secara terpisah.
    #
    # Yang boleh dibuang: citation, thinking blocks, UI chrome.
    strip_selectors = (
        # Citation / reference markers (chrome UI situs).
        '.markdown CITATION',
        '[data-testid="citation"]',
        '[data-testid="citation-link"]',
        # Thinking/reasoning blocks — hanya class/atribut, bukan teks,
        # agar jawaban biasa tak salah terbuang.
        '[data-message-author-role="assistant"] [class*="thinking"]',
        '[data-message-author-role="assistant"] [class*="reasoning"]',
    )

    # --- deteksi limit pemakaian ---
    # ChatGPT menampilkan pesan limit di area chat. Tanpa pola ini,
    # bagas-ai menunggu jawaban yang tak akan datang lalu gagal dengan
    # pesan membingungkan "balasan tak terdeteksi".
    #
    # Terverifikasi gagal menangkap DUA spanduk asli ChatGPT (ditemukan
    # lewat uji pola _uji_chatgpt.py): "You've reached YOUR daily limit"
    # (pola lama menuntut kata "the") dan "You've used all of your
    # available messages" (urutan "all|your" + "available" tak cocok).
    limit_patterns = (
        r"(?i)you.?ve reached (?:the|your|a) "
        r"(?:daily |weekly |monthly |free |plan )?limit",
        r"(?i)you.?ve hit (?:the )?(?:free |your )?(?:plan )?limit",
        r"(?i)you.?ve (?:used|exhausted)\b[^.]{0,40}"
        r"\b(?:messages|requests|credits)\b",
        r"(?i)you need to (?:upgrade|subscribe) to continue",
        r"(?i)\bupgrade to continue\b",
        r"(?i)rate limit (?:reached|exceeded)",
        r"(?i)telah mencapai batas",
        r"(?i)pesan (?:anda|habis|mentok)",
    )
    limit_exclude_selectors = (
        'div[data-message-author-role="assistant"]',
        'div.agent-turn',
    )

    # --- deteksi server sibuk ---
    # Pemberitahuan sibuk selalu PENDEK. Ambang busy_max_chars (400) di base
    # menangkap ini; teks panjang tak akan dianggap sibuk.
    busy_patterns = (
        r"(?i)\b(?:our |the )?server(?:s)? (?:is|are) (?:currently )?busy\b",
        r"(?i)\b(?:server|sistem|service) sedang sibuk\b",
        r"(?i)\bplease (?:try again|wait) (?:later|in a (?:moment|few))\b",
        r"(?i)\bhigh demand\b.*\b(?:try again|later)\b",
    )

    # --- deteksi error situs ---
    # Permintaan ditolak, koneksi ke model gagal, percakapan rusak.
    # Pola 'something went wrong' DIPERKETAT menuntut akhir pesan/lanjutan
    # "please/try": kalimat longgar seperti itu sering muncul di TEKS
    # jawaban AI yang membahas penanganan error (walau area jawaban sudah
    # dikecualikan, teks di luar sana masih bisa salah terbaca).
    error_patterns = (
        r"(?i)\bsomething went wrong\b\s*(?:[.!…]|please\b|try\b|$)",
        r"(?i)there was an error (?:generating|creating|processing|loading)",
        r"(?i)\ban error occurred while (?:generating|processing|sending)\b",
        r"(?i)\bfailed to (?:get|fetch|load) (?:response|answer)\b",
        r"(?i)\bwe (?:couldn'?t|cannot) process (?:your )?(?:request|message)\b",
    )

    # --- deteksi konteks penuh ---
    # Percakapan terlalu panjang, mulai sesi baru.
    context_full_patterns = (
        r"(?i)this conversation (?:is too long|has reached|is getting long)",
        r"(?i)context length (?:exceeded|limit reached)",
        r"(?i)mulai percakapan baru",
    )

    # --- streaming ---
    # Penanda "sedang mengetik/streaming". ChatGPT menampilkan cursor
    # berkedip atau indikator teks di akhir jawaban.
    streaming_selector = (
        '.result-streaming',
        '[data-testid="stop-button"]',
    )

    # --- banner/galat yang menggantikan isi jawaban ---
    # ChatGPT kadang menampilkan CAPTCHA/verifikasi di tengah jalan.
    captcha_patterns = (
        r"(?i)\bverify (?:you|your) (?:are|'?re) (?:a )?human\b",
        r"(?i)\bplease verify\b",
        r"(?i)\bsolving (?:this|the) challenge\b",
    )
    captcha_selectors = (
        '[data-testid="cf-turnstile"]',
        'iframe[src*="challenges.cloudflare.com"]',
        '.cf-turnstile',
    )

    # --- dismiss popup/banner ---
    # ChatGPT sering menampilkan popup "Tips", "What's new", cookie consent
    # yang bisa menutupi komposer. Termasuk modal 'no-auth-login' yang
    # TERUKUR menghalangi klik komposer (absolute inset-0) — muncul
    # intermiten pada sesi Free; tombol penutupnya ditarget khusus supaya
    # tak salah mencet tombol lain di dalam modal itu.
    dismiss_selectors = (
        '#modal-no-auth-login [data-testid="close-button"]',
        '#modal-no-auth-login button[aria-label="Close"]',
        '#modal-no-auth-login button[aria-label="Tutup"]',
        'button:has-text("OK")',
        'button:has-text("Got it")',
        'button:has-text("Dismiss")',
        'button:has-text("Close")',
        'button:has-text("Tutup")',
        'button[data-testid="close-button"]',
        '[class*="cookie"] button',
        '[class*="modal"] button:has-text("OK")',
    )

    # --- lampiran ---
    # ChatGPT menerima lampiran lewat tombol "+" di komposer.
    # Input file biasanya dibuat dinamis saat tombol diklik.
    file_input_selector = (
        'input[type="file"]',
        'input[accept*="image"]',
        'input[accept*=".pdf"]',
    )
    # Kartu pratinjau lampiran di komposer — penanda "file sudah benar-benar
    # menempel". ChatGPT menampilkan kartu file di area komposer.
    attach_item_selector = (
        '[class*="group/file-tile"]',
        'form [role="group"][class*="file-tile"]',
        'button[aria-label*="Hapus file" i]',
        'button[aria-label*="Remove file" i]',
        '[data-testid="attachment-preview"]',
        '.composer-file-preview',
        '[class*="file-card"]',
        '[class*="file-tile"]',
    )
    attach_limit_patterns = (
        r"(?i)maximum of \d+ file",
        r"(?i)\bmaksimal \d+ (?:berkas|file)\b",
        r"(?i)you can only upload \d+",
        r"(?i)dapatkan plus untuk lebih banyak unggahan",
        r"(?i)get plus for more uploads",
        r"(?i)tunggu \d+ (?:hour|jam|menit|minute) untuk mengunggah lagi",
        r"(?i)wait \d+ (?:hour|minute) to upload again",
        r"(?i)upload limit reached",
    )
    # Tombol batal pada kartu lampiran.
    attach_clear_selector = (
        'button[aria-label*="Hapus file" i]',
        'button[aria-label*="Remove file" i]',
        '[data-testid="remove-attachment"]',
        'button[aria-label="Remove"]',
        'button[aria-label="Hapus"]',
    )

    def supports_context_files(self) -> bool:
        """ChatGPT Web membatasi unggahan file dokumen non-gambar pada akun biasa.

        Konteks proyek dikirim langsung sebagai teks pembuka agar tidak terhambat
        kuota/limit berkas.
        """
        return False

    # --- mode ---
    # Selector tombol pembuka menu model — dulu dipakai pemilih varian
    # (GPT-4o/o1/o3); kini varian dihilangkan (lihat web_models di bawah),
    # tapi selector ini dibiarkan: situs bisa memunculkannya kembali untuk
    # akun berbayar, dan web_model_button tak dipakai bila web_models kosong.
    menu_item_selector = (
        '[data-testid="model-switcher-dropdown"] [role="option"]',
        '[data-testid="model-switcher-dropdown"] button',
        '[role="option"]:visible',
        '[role="menuitem"]:visible',
    )
    web_model_button = (
        'button[data-testid="model-switcher-dropdown-button"]',
        'button[data-testid="model-switcher-dropdown-trigger"]',
        'button[data-testid="model-switcher-dropdown"]',
        'button[aria-haspopup="menu"]:has-text("ChatGPT")',
        'button:has-text("ChatGPT 4o")',
        'button:has-text("GPT-4o")',
    )

    # Varian model — KOSONG: chatgpt.com (diukur ulang 2026-08-29) tak lagi
    # menyediakan pemilih varian model (GPT-4o/o1/o3/…) di bilah atas untuk
    # akun gratis; daftar lama jadi tombol yang mustahil diklik. Tanpa
    # web_models, /model menampilkan "chatgpt-web" satu baris saja.
    web_models = ()
    # /mode juga kosong — dulu web_modes sekadar alias web_models.
    web_modes = ()

    min_layout_width = 1280
    min_layout_height = 800

    def _looks_logged_in(self, page: Any) -> bool:
        """Bukti login lewat COOKIE SESI dulu, baru selector DOM.

        Alasan nyata (terukur saat uji): jendela connector dilahirkan DI LUAR
        LAYAR — dan pada keadaan itu tombol akun di SIDEBAR bisa tak dirender
        ke DOM sama sekali, sehingga deteksi berbasis DOM menyimpang
        'belum login' walau sesinya hidup, lalu connector menunggu login
        selamanya. Cookie tak peduli layout, ukuran, maupun posisi jendela."""
        try:
            for ck in page.context.cookies(["https://chatgpt.com"]):
                if "session-token" in (ck.get("name") or "") \
                        and ck.get("value"):
                    return True
        except Exception:  # noqa: BLE001 - jatuh ke pemeriksaan DOM
            pass
        return super()._looks_logged_in(page)

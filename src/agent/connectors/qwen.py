"""Connector chat.qwen.ai (web).

Status verifikasi (probe langsung ke situs, 2026-07-20 — TANPA login karena
akun belum tersambung):
  TERVERIFIKASI : kotak input `textarea.message-input-textarea`
                  (placeholder "How can I help you today?"), tombol "Log in" /
                  "Sign up" muncul saat belum masuk, dan ada `input[type=file]`
                  (tombol "Upload Image") untuk melampirkan gambar.
  TERVERIFIKASI : Qwen WAJIB login — pesan dari tamu tidak diproses sama sekali,
                  padahal kotak inputnya tetap aktif. Karena itu
                  `logged_out_selector` WAJIB diisi; tanpa itu bagas-ai mengira
                  sudah masuk lalu menunggu jawaban yang tak akan pernah ada.
  BELUM DICEK   : wadah pesan jawaban, penanda "sedang mengetik", pola URL
                  percakapan (untuk lanjut sesi/--resume), teks pemberitahuan
                  limit, serta tombol model/mode untuk /effort. Semuanya butuh
                  sesi yang SUDAH login untuk dipetakan.

Karena itu fitur yang belum bisa diverifikasi sengaja DIMATIKAN (bukan ditebak):
`chat_url_template` kosong -> lanjut-sesi tak ditawarkan; `web_actions` kosong
-> /effort tak menjanjikan tombol yang belum tentu ada; `limit_patterns` kosong
-> tak ada deteksi limit yang bisa salah tangkap. Setelah login sekali, tinggal
petakan DOM-nya lalu isi bagian di bawah — kerangkanya sudah siap semua.
"""
from __future__ import annotations

from .base import WebConnector


class QwenConnector(WebConnector):
    service = "qwen"
    label = "Qwen (web)"
    chat_url = "https://chat.qwen.ai/"

    # --- input (terverifikasi) ---
    # Kelas spesifik didahulukan; `textarea` polos sebagai cadangan bila situs
    # mengganti nama kelasnya.
    input_selector = "textarea.message-input-textarea, textarea"
    input_is_contenteditable = False
    submit_key = "Enter"

    # --- deteksi belum-login (terverifikasi) ---
    # Qwen menampilkan kotak input untuk tamu tetapi TIDAK memproses pesannya,
    # jadi tombol Log in/Sign up-lah penanda sesungguhnya.
    logged_out_selector = (
        'button:has-text("Log in"), button:has-text("Sign up"), '
        'button:has-text("Get Started")'
    )

    # --- lampiran gambar (terverifikasi ADA; alur unggah belum diuji live) ---
    file_input_selector = 'input[type="file"]'

    # --- jawaban (BELUM diverifikasi: butuh sesi login) ---
    # Beberapa kandidat dicoba berurutan; bila semuanya meleset, connector
    # berhenti dengan pesan yang menyebut file ini agar mudah disetel.
    message_selector = (
        ".markdown-body",
        "[class*='markdown']",
        "[class*='response-message']",
        "[class*='assistant']",
    )
    read_as_markdown = True   # aman: jatuh ke teks polos bila ekstraksi kosong

    # --- sengaja DIKOSONGKAN sampai bisa dipetakan pada sesi yang sudah login ---
    chat_url_template = ""    # -> supports_resume() False, tak ada menu pilih sesi
    web_actions = ()          # -> /effort tak menawarkan tombol yang belum pasti
    limit_patterns = ()       # -> tak ada deteksi limit yang bisa salah tangkap

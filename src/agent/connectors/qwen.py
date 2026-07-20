"""Connector chat.qwen.ai (web).

Seluruh selektor di bawah DIVERIFIKASI LANGSUNG ke situs pada sesi yang sudah
login (2026-07-20): pesan uji dikirim, lalu DOM-nya dipetakan.

Catatan penting soal KONTROL: Qwen menaruh kontrolnya di DUA tempat berbeda —
pemilih MODEL di bar ATAS, dan pemilih MODE tepat di dekat kotak input. Keduanya
juga bukan elemen <button> melainkan [role="button"], sehingga selector bergaya
`button[aria-label=...]` TIDAK akan cocok. Tiap aksi /effort di bawah karena itu
membawa selector tombol pembukanya sendiri.
"""
from __future__ import annotations

from .base import WebConnector

# Tombol pembuka menu (keduanya [role="button"], bukan <button>).
_BTN_MODEL = '[aria-label="Select Model"]'   # bar ATAS
_BTN_MODE = '[aria-label="Select Mode"]'     # dekat kotak input


class QwenConnector(WebConnector):
    service = "qwen"
    label = "Qwen (web)"
    chat_url = "https://chat.qwen.ai/"
    # URL percakapan: https://chat.qwen.ai/c/<uuid> (terverifikasi) -> lanjut
    # sesi & --resume bisa menyambung ke chat yang sama.
    chat_url_template = "https://chat.qwen.ai/c/{id}"
    chat_id_pattern = r"/c/([0-9a-fA-F-]{16,})"

    # --- input ---
    # Kandidat BERURUTAN: kelas spesifik dulu, `textarea` polos hanya cadangan
    # bila situs mengganti nama kelasnya. (Dulu satu string berkoma — itu keliru:
    # daftar CSS tak berprioritas, jadi kotak pencarian yang muncul lebih dulu di
    # DOM bisa terpilih dan prompt diketik ke sana.)
    input_selector = ("textarea.message-input-textarea", "textarea")
    input_is_contenteditable = False
    submit_key = "Enter"

    # --- deteksi belum-login ---
    # Qwen menampilkan kotak input untuk TAMU tetapi tak memproses pesannya,
    # jadi tombol Log in/Sign up-lah penanda sesungguhnya.
    logged_out_selector = (
        'button:has-text("Log in"), button:has-text("Sign up"), '
        'button:has-text("Get Started")'
    )

    # --- jawaban ---
    # `[class*="markdown"]` memberi teks jawaban BERSIH ("8821"), sedangkan
    # wadah `assistant` masih tercampur "Thinking completed".
    message_selector = (
        "[class*='markdown']",
        "[class*='answer']",
        "[class*='response-message']",
    )
    read_as_markdown = True
    # Tombol Stop hanya ada selagi Qwen menjawab -> penanda paling andal.
    stop_selectors = ('[aria-label="Stop"]', '[role="button"]:has-text("Stop")')
    # Saat masih berpikir, satu-satunya teks yang muncul adalah indikator ini.
    noise_pattern = r"(?:Thinking[^\n]*\s*)+"

    # --- lampiran (screenshot dll) ---
    file_input_selector = 'input[type="file"]'

    # --- /effort: kontrol ATAS (model) & BAWAH (mode) ---
    web_model_button = _BTN_MODEL
    web_actions = (
        ("Qwen3.7-Plus", ("Qwen3.7-Plus",),
         "model cepat & seimbang (bar atas)", _BTN_MODEL),
        ("Qwen3.7-Max", ("Qwen3.7-Max",),
         "model flagship, penalaran terkuat (bar atas)", _BTN_MODEL),
        ("Qwen3.8-Max-Preview", ("Qwen3.8-Max-Preview",),
         "pratinjau model generasi berikutnya (bar atas)", _BTN_MODEL),
        ("Mode: Deep Research", ("Deep Research",),
         "riset mendalam bertahap (tombol dekat input)", _BTN_MODE),
        ("Mode: Web Dev", ("Web Dev",),
         "mode bantu ngoding web (tombol dekat input)", _BTN_MODE),
    )

    # Teks pemberitahuan limit Qwen belum pernah terlihat; dikosongkan agar tak
    # ada pola longgar yang salah menangkap jawaban biasa.
    limit_patterns = ()

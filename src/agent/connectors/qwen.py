"""Connector chat.qwen.ai (web).

Kalau Qwen mengubah layout & jawaban tak lagi terbaca, cukup sesuaikan SELECTOR
di bawah — sisanya ditangani base.py. Nama kelas CSS Qwen sering teracak, jadi
pakai penanda yang relatif stabil: textarea input & wadah markdown jawaban.
"""
from __future__ import annotations

from .base import WebConnector


class QwenConnector(WebConnector):
    service = "qwen"
    label = "Qwen (web)"
    chat_url = "https://chat.qwen.ai/"
    # Qwen memakai textarea untuk input.
    input_selector = "textarea"
    input_is_contenteditable = False
    # Jawaban Qwen dirender sebagai markdown — beberapa kandidat wadah.
    message_selector = (
        ".markdown-body",
        "[class*='markdown']",
        "[class*='messageContent']",
    )
    submit_key = "Enter"
    # Qwen: kontrol UI (mode berpikir/varian) belum diverifikasi ke situs live,
    # jadi /effort untuk Qwen web belum menawarkan tombol (dikosongkan agar tak
    # memberi opsi yang gagal). Bisa diisi setelah dicek langsung ke chat.qwen.ai.
    web_actions = ()

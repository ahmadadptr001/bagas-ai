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
    # Jawaban Qwen dirender sebagai markdown di wadah .markdown-body.
    message_selector = ".markdown-body"
    submit_key = "Enter"

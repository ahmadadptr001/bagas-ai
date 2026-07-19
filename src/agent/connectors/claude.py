"""Connector claude.ai (web).

Kalau claude.ai mengubah layout & jawaban tak lagi terbaca, cukup sesuaikan
SELECTOR di bawah — sisanya (kirim, tunggu, streaming) ditangani base.py.
"""
from __future__ import annotations

from typing import Any

from .base import WebConnector


class ClaudeConnector(WebConnector):
    service = "claude"
    label = "Claude (web)"
    chat_url = "https://claude.ai/new"
    # Kotak input Claude = editor ProseMirror (contenteditable), bukan textarea.
    input_selector = 'div[contenteditable="true"]'
    input_is_contenteditable = True
    # Tiap balasan asisten dibungkus .font-claude-message.
    message_selector = "div.font-claude-message"
    submit_key = "Enter"

    def _is_done(self, page: Any) -> bool:
        # Saat Claude masih mengetik, ada wadah dengan data-is-streaming="true".
        try:
            return page.query_selector('[data-is-streaming="true"]') is None
        except Exception:  # noqa: BLE001
            return True

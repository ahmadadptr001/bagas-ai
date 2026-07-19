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
    # Wadah balasan — DIVERIFIKASI LANGSUNG ke claude.ai (2026-07-19):
    #   .standard-markdown   = teks jawaban BERSIH (tanpa jejak "Thought for..",
    #                          tanpa prefix sr-only "Claude responded:").
    #   div[data-is-streaming] = wadah pesan asisten (cadangan; teks agak kotor).
    #   .font-claude-response  = wrapper respons (cadangan terakhir).
    # (Selektor lama .font-claude-message SUDAH TIDAK ADA -> dulu jawaban tak
    #  pernah terbaca; inilah akar bug "browser jawab, terminal kosong".)
    message_selector = (
        ".standard-markdown",
        "div[data-is-streaming]",
        ".font-claude-response",
    )
    submit_key = "Enter"
    # Penanda "sedang mengetik": atribut data-is-streaming="true" (verified).
    streaming_selector = '[data-is-streaming="true"]'

    # Tombol/opsi UI yang bisa diklik program lewat /effort (DIVERIFIKASI live):
    #   tombol pemilih model: data-testid="model-selector-dropdown"
    #   varian model (role=menuitemradio): Sonnet 5 / Haiku 4.5 (free), Opus 4.8 (Pro)
    #   submenu Effort (role=menuitem "Effort") -> Low/Medium/High/Extra/Max.
    #   ("Medium" ambigu dg pembuka submenu -> cocokkan "Default" utk level Medium.)
    web_model_button = 'button[data-testid="model-selector-dropdown"]'
    web_actions = (
        ("Sonnet 5", ("Sonnet 5",), "model cepat & efisien (free)"),
        ("Haiku 4.5", ("Haiku 4.5",), "model tercepat (free)"),
        ("Opus 4.8", ("Opus 4.8",), "model tugas kompleks (butuh Pro)"),
        ("Effort: Low", ("Effort", "Low"), "usaha berpikir minimal"),
        ("Effort: Medium", ("Effort", "Default"), "usaha berpikir sedang (default)"),
        ("Effort: High", ("Effort", "High"), "usaha berpikir tinggi"),
        ("Effort: Extra", ("Effort", "Extra"), "usaha berpikir ekstra"),
        ("Effort: Max", ("Effort", "Max"), "usaha berpikir maksimum"),
    )

    def _is_done(self, page: Any) -> bool:
        # Saat Claude masih mengetik, ada wadah dengan data-is-streaming="true".
        try:
            return page.query_selector('[data-is-streaming="true"]') is None
        except Exception:  # noqa: BLE001
            return True

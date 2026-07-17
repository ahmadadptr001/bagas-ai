"""Jembatan interaksi agar tool (mis. ask_user/dropdown) bisa meminta input dari
antarmuka yang MENJALANKAN giliran itu — bukan sekadar antarmuka "aktif".

Penting: bagas-ai bisa jalan berbarengan di CLI dan di bot Telegram (satu proses).
Kalau tugas dipicu dari Telegram, pertanyaan agent HARUS muncul di Telegram, bukan
di terminal (konsepnya: tak menyentuh laptop). Karena itu handler bisa dipasang
per-KONTEKS eksekusi lewat ContextVar (menyebar ke thread via asyncio.to_thread
yang menyalin context), dengan handler global CLI sebagai default/cadangan.
"""
from __future__ import annotations

import contextvars
from typing import Callable

# handler(question, options, multiple) -> label terpilih (atau gabungan bila multiple)
ChoiceHandler = Callable[[str, list[str], bool], str]

# Default global (dipasang antarmuka utama, mis. CLI-terminal).
_default_handler: ChoiceHandler | None = None
# Handler per-konteks eksekusi (mis. Telegram); menang atas default bila diset.
_ctx_handler: contextvars.ContextVar[ChoiceHandler | None] = contextvars.ContextVar(
    "bagasai_choice_handler", default=None
)


def set_choice_handler(handler: ChoiceHandler | None) -> None:
    """Pasang handler DEFAULT global (dipakai bila tak ada handler konteks)."""
    global _default_handler
    _default_handler = handler


def set_context_handler(handler: ChoiceHandler | None):
    """Pasang handler khusus konteks eksekusi saat ini (kembalikan token untuk reset)."""
    return _ctx_handler.set(handler)


def reset_context_handler(token) -> None:
    try:
        _ctx_handler.reset(token)
    except Exception:
        pass


def ask_choice(question: str, options: list[str], multiple: bool = False) -> str:
    handler = _ctx_handler.get() or _default_handler
    if handler is None:
        return (
            "[tidak interaktif] Tidak bisa menampilkan pilihan di antarmuka ini. "
            "Ajukan pertanyaan sebagai teks biasa saja."
        )
    return handler(question, options, multiple)

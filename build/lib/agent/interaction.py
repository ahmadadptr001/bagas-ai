"""Jembatan interaksi agar tool (mis. ask_user/dropdown) bisa meminta input
dari antarmuka aktif tanpa tahu detail antarmukanya.

Antarmuka (CLI) memasang handler lewat set_choice_handler(). Bila tidak ada
handler (mode non-interaktif seperti API), tool memberi tahu bahwa klarifikasi
tidak bisa dilakukan.
"""
from __future__ import annotations

from typing import Callable

# handler(question, options, multiple) -> label terpilih (atau gabungan bila multiple)
ChoiceHandler = Callable[[str, list[str], bool], str]

_handler: ChoiceHandler | None = None


def set_choice_handler(handler: ChoiceHandler | None) -> None:
    global _handler
    _handler = handler


def ask_choice(question: str, options: list[str], multiple: bool = False) -> str:
    if _handler is None:
        return (
            "[tidak interaktif] Tidak bisa menampilkan pilihan di antarmuka ini. "
            "Ajukan pertanyaan sebagai teks biasa saja."
        )
    return _handler(question, options, multiple)

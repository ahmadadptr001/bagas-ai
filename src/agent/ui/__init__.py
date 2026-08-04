"""Komponen antarmuka terminal milik bagas-ai sendiri.

Isinya prompt interaktif (menu pilih, centang, konfirmasi, input teks) yang
menggantikan InquirerPy — lihat ui/menu.py untuk alasan & bentuknya.
"""
from __future__ import annotations

from .menu import Choice, checkbox, confirm, filepath, inquirer, secret, select, text

__all__ = [
    "Choice", "checkbox", "confirm", "filepath", "inquirer", "secret",
    "select", "text",
]

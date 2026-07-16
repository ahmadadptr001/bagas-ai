"""Tool klarifikasi: menampilkan pilihan (inquirer) ke pengguna saat ada yang
kurang jelas, alih-alih menebak. Mendukung pilih satu atau banyak."""
from __future__ import annotations

from .. import interaction
from .base import tool


@tool
def ask_user(question: str, options: list[str], multiple: bool = False) -> str:
    """Tanyakan klarifikasi ke pengguna dengan pilihan interaktif (inquirer) saat instruksi ambigu atau kurang detail, DARIPADA menebak. Kembalikan pilihan pengguna.

    question: pertanyaan yang jelas.
    options: daftar 2-6 pilihan.
    multiple: set true bila pengguna boleh memilih lebih dari satu.
    """
    if not options:
        return "[error] ask_user butuh minimal satu opsi."
    return interaction.ask_choice(question, list(options), bool(multiple))

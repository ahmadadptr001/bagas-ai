"""Tool klarifikasi: menampilkan menu pilihan ke pengguna saat ada yang kurang
jelas, alih-alih menebak. Mendukung pilih satu atau banyak.

Menunya digambar antarmuka yang menjalankan giliran ini (lihat interaction.py):
terminal memakai prompt bagas-ai di ui/menu.py, Telegram memakai tombol."""
from __future__ import annotations

from .. import interaction
from .base import tool


@tool
def ask_user(question: str, options: list[str], multiple: bool = False) -> str:
    """Tanyakan klarifikasi ke pengguna dengan menu pilihan interaktif saat instruksi ambigu atau kurang detail, DARIPADA menebak. Kembalikan pilihan pengguna.

    question: pertanyaan yang jelas.
    options: daftar 2-6 pilihan.
    multiple: set true bila pengguna boleh memilih lebih dari satu.
    """
    if not options:
        return "[error] ask_user butuh minimal satu opsi."
    return interaction.ask_choice(question, list(options), bool(multiple))

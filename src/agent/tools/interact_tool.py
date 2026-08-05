"""Tool klarifikasi: menampilkan menu pilihan ke pengguna saat ada yang kurang
jelas, alih-alih menebak. Mendukung pilih satu atau banyak.

Menunya digambar antarmuka yang menjalankan giliran ini (lihat interaction.py):
terminal memakai prompt bagas-ai di ui/menu.py, Telegram memakai tombol."""
from __future__ import annotations

from .. import interaction
from .base import tool


@tool
def ask_user(question: str, options: list[str], multiple: bool = False) -> str:
    """Tanyakan klarifikasi ke pengguna lewat menu pilihan interaktif saat instruksi ambigu atau ada beberapa pendekatan yang sama-sama masuk akal, DARIPADA menebak. Kembalikan jawaban pengguna. Pengguna SELALU bisa mengetik jawabannya sendiri di menu itu, jadi opsimu tak perlu mencakup segala kemungkinan.

    question: pertanyaan yang jelas & spesifik.
    options: 2-6 pilihan konkret yang bisa dibandingkan. Sebutkan
        konsekuensinya dalam beberapa kata, mis. "Halaman sendiri
        /karya/[id] (URL bisa dibagikan)".
    multiple: bentuk menunya, dan ini HARUS dipilih sadar — salah pilih
        membuat pengguna terjebak.
        false (bawaan) = SATU jawaban. Untuk pilihan yang saling MENIADAKAN:
            "modal ATAU halaman sendiri", "hapus ATAU biarkan", "mana yang
            dikerjakan lebih dulu".
        true = BOLEH BANYAK. Untuk pilihan yang bisa berdampingan: "fitur mana
            saja yang dipasang", "berkas mana saja yang diubah", "bagian mana
            saja yang perlu diperbaiki".
        Uji cepatnya: kalau memilih dua sekaligus MASUK AKAL, pakai true.
    """
    if not options:
        return "[error] ask_user butuh minimal satu opsi."
    return interaction.ask_choice(question, list(options), bool(multiple))

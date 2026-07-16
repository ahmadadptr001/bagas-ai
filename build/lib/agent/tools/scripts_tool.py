"""Tool script memory: menulis, menjalankan, dan mendaftar skrip reusable.

Dipakai agent saat tugas butuh kemampuan yang belum ada (scraping web, konversi
PDF, olah data): tulis skrip Python -> simpan -> jalankan -> pakai lagi nanti.
Kalau butuh library eksternal, agent bisa memasangnya lewat run_command
('pip install ...').
"""
from __future__ import annotations

from .. import scripts
from .base import tool


@tool
def save_script(name: str, code: str, description: str = "") -> str:
    """Simpan skrip Python reusable ke 'script memory' agar bisa dijalankan lagi nanti. Pakai ini ketika sebuah tugas (scraping, konversi PDF, olah data, dsb.) sebaiknya dijadikan alat tetap.

    name: nama pendek unik (huruf/angka/underscore), tanpa .py.
    code: isi lengkap skrip Python.
    description: deskripsi singkat fungsi skrip.
    """
    return scripts.save(name, code, description)


@tool
def run_script(name: str, args: str = "") -> str:
    """Jalankan skrip yang tersimpan di script memory dan kembalikan output-nya.

    name: nama skrip yang tersimpan.
    args: argumen command-line opsional (dipisah spasi).
    """
    return scripts.run(name, args)


@tool
def list_scripts() -> str:
    """Daftar semua skrip yang tersimpan di script memory beserta deskripsinya."""
    items = scripts.index_list()
    if not items:
        return "Belum ada skrip tersimpan."
    return "\n".join(
        f"- {it['name']}: {it.get('description') or '(tanpa deskripsi)'}"
        for it in items
    )

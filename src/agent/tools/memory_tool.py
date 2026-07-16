"""Tool memory jangka panjang: agent bisa mengingat & melupakan fakta."""
from __future__ import annotations

from .. import longmem
from .base import tool


@tool
def remember(fact: str) -> str:
    """Simpan sebuah fakta/preferensi penting tentang pengguna ke memory jangka panjang agar diingat di sesi berikutnya. Contoh: 'Nama pengguna Bagas', 'Pengguna memakai Windows', 'Suka jawaban singkat'.

    fact: kalimat fakta yang ingin diingat.
    """
    return longmem.add(fact)


@tool
def forget(keyword: str) -> str:
    """Hapus fakta dari memory jangka panjang yang mengandung kata kunci tertentu.

    keyword: kata kunci untuk mencocokkan fakta yang akan dihapus.
    """
    return longmem.remove(keyword)


@tool
def list_memory() -> str:
    """Tampilkan semua fakta yang saat ini diingat di memory jangka panjang."""
    facts = longmem.all_facts()
    if not facts:
        return "Memory jangka panjang masih kosong."
    return "\n".join(f"- {f}" for f in facts)

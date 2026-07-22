"""Tool pencarian: cari BERKAS lewat pola nama, dan cari TEKS di dalam berkas.

Kenapa ini penting untuk agent yang jumlah gilirannya mahal (tiap giliran =
satu bolak-balik ke situs AI web): tanpa keduanya, satu-satunya cara menemukan
"di mana fungsi X didefinisikan" adalah list_dir lalu read_file berkali-kali —
belasan giliran habis hanya untuk mencari, sebelum pekerjaan sebenarnya dimulai.
Peta proyek (projectindex) memberi gambaran struktur, tapi tidak bisa menjawab
pertanyaan tentang ISI.

Keduanya sengaja melewati folder yang tak pernah relevan (.git, node_modules,
venv, __pycache__, dist/build) — bukan sekadar demi kecepatan, tapi supaya
hasilnya tidak tenggelam oleh ribuan kecocokan di dependensi.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .. import config
from .base import tool

ROOT = config.PROJECT_ROOT

# Folder yang TIDAK pernah ditelusuri.
_LEWATI = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".nuxt", ".cache", "target", "vendor", ".idea", ".vscode",
    "site-packages", ".tox", "coverage", ".gradle",
}
# Berkas biner tak ada gunanya digrep dan bikin hasil berantakan.
_EKS_BINER = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".ogg", ".flac",
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyd", ".class",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".db", ".sqlite",
}
_MAKS_BYTE = 2_000_000   # berkas raksasa (bundel/minified) dilewati


def _telusuri(akar: Path):
    """Semua berkas di bawah `akar`, melewati folder & berkas yang tak relevan."""
    for dirpath, dirnames, filenames in os.walk(akar):
        dirnames[:] = [d for d in dirnames
                       if d not in _LEWATI and not d.startswith(".")]
        for nama in filenames:
            p = Path(dirpath) / nama
            if p.suffix.lower() in _EKS_BINER:
                continue
            yield p


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


@tool
def glob_files(pattern: str, max_results: int = 100) -> str:
    """Cari BERKAS berdasarkan pola nama, mis. '*.js', 'src/**/*.py', 'test_*'.

    Pakai ini alih-alih menelusuri folder satu per satu dengan list_dir.

    pattern: pola nama berkas ('**' berarti sub-folder mana pun).
    max_results: batas jumlah hasil (default 100).
    """
    pat = (pattern or "").strip().replace("\\", "/")
    if not pat:
        return "[error] pattern kosong."
    # Pola tanpa pemisah folder dicocokkan ke NAMA berkas di kedalaman mana pun —
    # itu yang dimaksud orang saat menulis '*.py'.
    hanya_nama = "/" not in pat
    hasil = []
    for p in _telusuri(ROOT):
        rel = _rel(p)
        cocok = (fnmatch.fnmatch(p.name, pat) if hanya_nama
                 else fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "**/" + pat))
        if cocok:
            hasil.append(rel)
            if len(hasil) >= max_results:
                break
    if not hasil:
        return f"Tidak ada berkas yang cocok dengan '{pattern}'."
    hasil.sort()
    kepala = f"{len(hasil)} berkas cocok '{pattern}':"
    return kepala + "\n" + "\n".join("  " + h for h in hasil)


@tool
def search_text(query: str, pattern: str = "", regex: bool = False,
                max_results: int = 60) -> str:
    """Cari TEKS di dalam berkas proyek dan kembalikan berkas:baris:isinya.

    Ini cara tercepat menjawab "di mana X didefinisikan/dipakai" tanpa membaca
    banyak berkas satu per satu.

    query: teks yang dicari (atau pola regex bila regex=true).
    pattern: batasi ke berkas tertentu, mis. '*.py' (kosong = semua).
    regex: true bila `query` adalah regex.
    max_results: batas jumlah baris hasil (default 60).
    """
    q = query or ""
    if not q:
        return "[error] query kosong."
    try:
        rx = re.compile(q if regex else re.escape(q), re.IGNORECASE)
    except re.error as e:
        return f"[error] regex tidak sah: {e}"
    pat = (pattern or "").strip().replace("\\", "/")
    hanya_nama = "/" not in pat

    hasil: list[str] = []
    n_berkas = 0
    for p in _telusuri(ROOT):
        if pat:
            rel0 = _rel(p)
            if not (fnmatch.fnmatch(p.name, pat) if hanya_nama
                    else fnmatch.fnmatch(rel0, pat)
                    or fnmatch.fnmatch(rel0, "**/" + pat)):
                continue
        try:
            if p.stat().st_size > _MAKS_BYTE:
                continue
            teks = p.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue      # biner / tak terbaca -> lewati diam-diam
        if not rx.search(teks):
            continue
        n_berkas += 1
        rel = _rel(p)
        for i, baris in enumerate(teks.splitlines(), 1):
            if rx.search(baris):
                potong = baris.strip()
                if len(potong) > 200:
                    potong = potong[:200] + "…"
                hasil.append(f"{rel}:{i}: {potong}")
                if len(hasil) >= max_results:
                    break
        if len(hasil) >= max_results:
            hasil.append(f"… (dipotong di {max_results} baris; persempit query "
                         "atau pakai pattern)")
            break
    if not hasil:
        lingkup = f" pada berkas '{pattern}'" if pattern else ""
        return f"Tidak ditemukan '{query}'{lingkup}."
    return f"{len(hasil)} baris cocok di {n_berkas} berkas:\n" + "\n".join(hasil)

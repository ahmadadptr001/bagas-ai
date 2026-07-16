"""Tool file: baca/tulis/daftar file di dalam ROOT PROJECT (folder terminal
saat `bagasai` dipanggil). Dibatasi agar tidak keluar dari root project."""
from __future__ import annotations

from pathlib import Path

from .. import config
from .base import tool

ROOT = config.PROJECT_ROOT


def _safe_path(path: str) -> Path:
    """Resolusikan `path` relatif terhadap root project & pastikan tetap di dalamnya.

    Mencegah path traversal keluar dari project (mis. '../../etc/passwd').
    """
    target = (ROOT / path).resolve()
    root = ROOT.resolve()
    if root != target and root not in target.parents:
        raise ValueError(
            f"Akses ditolak: '{path}' berada di luar root project ({root})."
        )
    return target


@tool
def read_file(path: str) -> str:
    """Baca isi sebuah file teks di dalam root project.

    path: path relatif terhadap root project.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"File tidak ditemukan: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > 20000:
        text = text[:20000] + "\n... [dipotong]"
    return text


@tool
def write_file(path: str, content: str) -> str:
    """Tulis (atau timpa) sebuah file teks di dalam root project. Sebelum menulis, pertimbangkan cek dulu apakah file sudah ada (read_file/list_dir) agar tidak menimpa tanpa perlu.

    path: path relatif terhadap root project.
    content: isi file.
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    target.write_text(content, encoding="utf-8")
    verb = "Ditimpa" if existed else "Dibuat"
    return f"{verb}: {target.relative_to(ROOT.resolve())} ({len(content)} karakter)."


@tool
def delete_file(path: str) -> str:
    """Hapus sebuah file di dalam root project. Pertimbangkan matang-matang karena sulit dibatalkan.

    path: path relatif terhadap root project.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"File tidak ditemukan: {path}"
    target.unlink()
    return f"Dihapus: {target.relative_to(ROOT.resolve())}"


@tool
def list_dir(path: str = ".") -> str:
    """Daftar isi sebuah folder di dalam root project. Berguna untuk cek apa yang sudah ada sebelum membuat sesuatu.

    path: path relatif terhadap root project (default: root project).
    """
    target = _safe_path(path)
    if not target.is_dir():
        return f"Folder tidak ditemukan: {path}"
    entries = []
    for p in sorted(target.iterdir()):
        kind = "dir " if p.is_dir() else "file"
        size = p.stat().st_size if p.is_file() else "-"
        entries.append(f"[{kind}] {p.name} ({size})")
    return "\n".join(entries) if entries else "(kosong)"

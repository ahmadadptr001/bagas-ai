"""Tool file: baca/tulis/daftar file di dalam ROOT PROJECT (folder terminal
saat `bagasai` dipanggil) DAN folder konteks tambahan (fitur add-dir).
Dibatasi agar tidak keluar dari folder-folder yang diizinkan."""
from __future__ import annotations

from pathlib import Path

from .. import config, workspace
from .base import tool

ROOT = config.PROJECT_ROOT


def _safe_path(path: str) -> Path:
    """Resolusikan `path` & pastikan berada di dalam salah satu root yang diizinkan.

    Root yang diizinkan = root project + semua folder yang ditambahkan lewat
    add-dir. Path relatif diresolusi terhadap root project; path ABSOLUT dipakai
    apa adanya (untuk mengakses folder konteks). Mencegah path traversal keluar.
    """
    p = Path(path).expanduser()
    target = p.resolve() if p.is_absolute() else (ROOT / p).resolve()
    for root in workspace.allowed_roots():
        r = root.resolve()
        if target == r or r in target.parents:
            return target
    raise ValueError(
        f"Akses ditolak: '{path}' di luar folder yang diizinkan "
        f"(root project + folder add-dir). Untuk folder konteks, pakai path absolut."
    )


def _display(target: Path) -> str:
    """Tampilkan path relatif terhadap root pemiliknya (atau absolut bila di luar)."""
    for root in workspace.allowed_roots():
        try:
            return str(target.relative_to(root.resolve()))
        except ValueError:
            continue
    return str(target)


@tool
def read_file(path: str) -> str:
    """Baca isi sebuah file teks di dalam root project atau folder konteks (add-dir).

    path: relatif terhadap root project, atau path ABSOLUT untuk file di folder
    konteks tambahan.
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
    """Tulis (atau timpa) sebuah file teks di root project atau folder konteks (add-dir). Sebelum menulis, pertimbangkan cek dulu apakah file sudah ada (read_file/list_dir) agar tidak menimpa tanpa perlu.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    content: isi file.
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    target.write_text(content, encoding="utf-8")
    verb = "Ditimpa" if existed else "Dibuat"
    return f"{verb}: {_display(target)} ({len(content)} karakter)."


@tool
def delete_file(path: str) -> str:
    """Hapus sebuah file di root project atau folder konteks (add-dir). Pertimbangkan matang-matang karena sulit dibatalkan.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"File tidak ditemukan: {path}"
    target.unlink()
    return f"Dihapus: {_display(target)}"


@tool
def list_dir(path: str = ".") -> str:
    """Daftar isi sebuah folder di root project atau folder konteks (add-dir). Berguna untuk cek apa yang sudah ada sebelum membuat sesuatu.

    path: relatif terhadap root project (default: root project), atau path ABSOLUT
    untuk folder konteks tambahan.
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

"""Layanan dokumen teks untuk editor file di sidebar Textual.

Pembacaan dibatasi pada teks UTF-8 berukuran wajar. Penyimpanan selalu
membuat checkpoint baru terlebih dahulu sehingga ``undo_changes`` dapat
memulihkan kondisi sebelum edit, lalu mengganti file secara atomik.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config
from .tools.checkpoint import begin_turn, snapshot
from .tools.files import _syntax_check

MAX_EDITOR_BYTES = 2 * 1024 * 1024


class EditorFileError(RuntimeError):
    """Berkas tidak aman atau tidak cocok dibuka di editor teks."""


@dataclass
class TextFileDocument:
    path: Path
    text: str
    digest: str
    newline: str = "\n"
    utf8_bom: bool = False


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project_file(path: Path | str) -> Path:
    target = Path(path).resolve()
    root = config.PROJECT_ROOT.resolve()
    if target != root and root not in target.parents:
        raise EditorFileError("Editor sidebar hanya boleh membuka file proyek.")
    if not target.is_file():
        raise EditorFileError("File sudah tidak ada atau bukan berkas biasa.")
    return target


def load_text_file(path: Path | str) -> TextFileDocument:
    """Baca file proyek sebagai UTF-8 tanpa mengubah byte apa pun."""
    target = _project_file(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise EditorFileError(f"File tidak dapat dibaca: {exc}") from exc
    if size > MAX_EDITOR_BYTES:
        raise EditorFileError(
            f"File terlalu besar untuk editor ({size / (1024 * 1024):.1f} MB; "
            f"batas {MAX_EDITOR_BYTES // (1024 * 1024)} MB)."
        )
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise EditorFileError(f"File tidak dapat dibaca: {exc}") from exc
    if b"\x00" in raw:
        raise EditorFileError("File biner tidak dapat dibuka di editor teks.")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig" if bom else "utf-8")
    except UnicodeDecodeError as exc:
        raise EditorFileError(
            "Encoding file bukan UTF-8; editor membatalkan agar isi tidak rusak."
        ) from exc
    newline = "\r\n" if b"\r\n" in raw else ("\r" if b"\r" in raw else "\n")
    # TextArea bekerja paling stabil dengan LF. Gaya newline asli diterapkan
    # kembali saat penyimpanan.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return TextFileDocument(target, normalized, _digest(raw), newline, bom)


def save_text_file(document: TextFileDocument, text: str) -> str:
    """Simpan dokumen secara atomik dengan conflict guard dan checkpoint."""
    target = _project_file(document.path)
    try:
        current = target.read_bytes()
    except OSError as exc:
        raise EditorFileError(f"File tidak dapat diperiksa: {exc}") from exc
    if _digest(current) != document.digest:
        raise EditorFileError(
            "File berubah di luar editor. Tutup lalu buka ulang agar perubahan "
            "orang/proses lain tidak tertimpa."
        )

    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    on_disk = normalized.replace("\n", document.newline)
    encoding = "utf-8-sig" if document.utf8_bom else "utf-8"
    payload = on_disk.encode(encoding)

    # Editor adalah aksi tersendiri, bukan bagian dari giliran AI yang mungkin
    # baru selesai. Kelompok checkpoint terpisah membuat undo hanya membalik
    # satu penyimpanan editor ini.
    begin_turn()
    snapshot(target)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".bagasai-editor-", suffix=".tmp",
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(payload)
        try:
            shutil.copymode(target, tmp)
        except OSError:
            pass
        os.replace(tmp, target)
    except OSError as exc:
        raise EditorFileError(f"Gagal menyimpan file: {exc}") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    document.text = normalized
    document.digest = _digest(payload)
    syntax = _syntax_check(target)
    detail = f"Tersimpan · backup undo dibuat · {len(payload)} byte"
    if syntax:
        detail += f" · {syntax}"
    return detail


__all__ = [
    "EditorFileError", "MAX_EDITOR_BYTES", "TextFileDocument",
    "load_text_file", "save_text_file",
]

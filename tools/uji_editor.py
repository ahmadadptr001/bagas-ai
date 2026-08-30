# -*- coding: utf-8 -*-
"""Regresi layanan editor: format, conflict guard, batas, dan checkpoint."""
from __future__ import annotations

import codecs
import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="uji_editor_"))
os.environ["BAGASAI_PROJECT_ROOT"] = str(_ROOT)
os.environ["BAGASAI_HOME"] = str(_ROOT / "home")

from agent.editor import EditorFileError, load_text_file, save_text_file
from agent.tools.checkpoint import undo_changes


def main() -> int:
    # BOM dan CRLF harus kembali persis seperti format awal file.
    target = _ROOT / "format.txt"
    original = codecs.BOM_UTF8 + "satu\r\ndua\r\n".encode("utf-8")
    target.write_bytes(original)
    doc = load_text_file(target)
    assert doc.text == "satu\ndua\n"
    assert doc.utf8_bom is True and doc.newline == "\r\n"
    hasil = save_text_file(doc, "satu\ndua diubah\n")
    saved = target.read_bytes()
    assert saved == codecs.BOM_UTF8 + "satu\r\ndua diubah\r\n".encode("utf-8")
    assert "backup undo dibuat" in hasil
    undo_changes()
    assert target.read_bytes() == original
    print("  BOM + CRLF dipertahankan dan backup dapat di-undo: OK")

    # Perubahan proses lain tidak boleh ditimpa oleh buffer yang sudah basi.
    target.write_text("awal\n", encoding="utf-8")
    doc = load_text_file(target)
    target.write_text("perubahan eksternal\n", encoding="utf-8")
    try:
        save_text_file(doc, "isi editor\n")
    except EditorFileError as exc:
        assert "berubah di luar editor" in str(exc)
    else:
        raise AssertionError("conflict guard tidak menolak buffer basi")
    assert target.read_text(encoding="utf-8") == "perubahan eksternal\n"
    print("  conflict guard mencegah overwrite perubahan eksternal: OK")

    binary = _ROOT / "binary.bin"
    binary.write_bytes(b"abc\x00def")
    try:
        load_text_file(binary)
    except EditorFileError as exc:
        assert "biner" in str(exc)
    else:
        raise AssertionError("file biner seharusnya ditolak")
    print("  file biner ditolak tanpa dimodifikasi: OK")

    outside_dir = Path(tempfile.mkdtemp(prefix="uji_editor_luar_"))
    outside = outside_dir / "luar.txt"
    outside.write_text("jangan sentuh", encoding="utf-8")
    try:
        load_text_file(outside)
    except EditorFileError as exc:
        assert "file proyek" in str(exc)
    else:
        raise AssertionError("path di luar proyek seharusnya ditolak")
    assert outside.read_text(encoding="utf-8") == "jangan sentuh"
    print("  path di luar root proyek ditolak: OK")
    print("OK - layanan editor aman")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

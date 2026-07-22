#!/usr/bin/env python3
"""Pre-commit hook: naikkan versi PATCH di pyproject.toml lalu stage ulang.

Menutup akar "update tak ngefek": instalasi non-editable DILEWATI pip bila versi
paket sama ("Requirement already satisfied"). Dengan versi naik OTOMATIS tiap
commit, pip selalu melihat rilis baru & benar-benar menyalin kode ke
site-packages. (updater.py juga sudah --force-reinstall sebagai pengaman kedua,
jadi keduanya saling menutupi.)

Hook ini SENGAJA tak pernah menghambat commit: format tak dikenali / error apa
pun -> keluar diam-diam, commit tetap jalan.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    pp = root / "pyproject.toml"
    try:
        text = pp.read_text(encoding="utf-8")
    except OSError:
        return
    m = re.search(r'^(version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', text, re.M)
    if not m:
        return  # format versi tak dikenali -> jangan ganggu commit
    major, minor, patch = int(m.group(2)), int(m.group(3)), int(m.group(4))
    baru = f"{m.group(1)}{major}.{minor}.{patch + 1}{m.group(5)}"
    pp.write_text(text[:m.start()] + baru + text[m.end():], encoding="utf-8")
    try:
        # Stage ulang supaya versi baru ikut di commit YANG SAMA.
        subprocess.run(["git", "add", str(pp)], cwd=str(root),
                       capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        pass
    print(f"[bump] versi -> {major}.{minor}.{patch + 1}")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - hook TAK boleh menggagalkan commit
        sys.exit(0)

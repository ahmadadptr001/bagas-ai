"""Clipboard terminal dengan dukungan Windows untuk seleksi teks."""
from __future__ import annotations

import subprocess
import sys

from .. import termux


def copy_text(app, text: str) -> bool:
    if not text:
        return False
    copied = False
    try:
        app.copy_to_clipboard(text)
        copied = True
    except Exception:
        pass
    if termux.IS_TERMUX:
        # OSC 52 (app.copy_to_clipboard) belum tentu sampai ke Android;
        # Termux:API menulis langsung ke clipboard sistem.
        try:
            copied = termux.salin(text) or copied
        except Exception:
            pass
    if sys.platform == "win32" and not app.is_headless:
        try:
            result = subprocess.run(
                ["clip.exe"], input=text.encode("utf-16"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            copied = copied or result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return copied

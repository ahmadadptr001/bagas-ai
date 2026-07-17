"""Deteksi sistem operasi & sinkronisasi ke memory jangka panjang.

Tiap bagas-ai dijalankan, OS dideteksi lalu disimpan ke memory:
  - belum ada  -> ditambahkan,
  - sudah ada & sama  -> DILEWATI (tidak menulis ulang),
  - sudah ada & beda  -> diperbarui.

OS juga disisipkan ke system prompt (lihat prompts.py) supaya bagas-ai
MENYESUAIKAN semua perintah terminal dengan OS yang terdeteksi (PowerShell/cmd
di Windows, bash di Linux/macOS).
"""
from __future__ import annotations

import platform

from . import longmem

# Prefix stabil sebagai "kunci" fakta OS di memory (untuk upsert).
_PREFIX = "Sistem operasi pengguna:"


def label() -> str:
    """Nama OS + versi yang ringkas & manusiawi."""
    system = platform.system()
    if system == "Windows":
        rel = platform.release()
        return f"Windows {rel}".strip()
    if system == "Darwin":
        mac, _, _ = platform.mac_ver()
        return f"macOS {mac}".strip() if mac else "macOS"
    if system:
        return f"{system} {platform.release()}".strip()
    return "Linux"


def shell_hint() -> str:
    """Shell/terminal khas OS ini — untuk mengarahkan sintaks perintah."""
    system = platform.system()
    if system == "Windows":
        return "PowerShell / cmd"
    if system == "Darwin":
        return "bash/zsh"
    return "bash"


def summary() -> str:
    """Ringkasan satu baris: OS + shell. Dipakai di system prompt & memory."""
    return f"{label()} (shell: {shell_hint()})"


def _fact() -> str:
    return (
        f"{_PREFIX} {summary()}. "
        "Sesuaikan SEMUA perintah terminal dengan OS ini."
    )


def sync_to_memory() -> str:
    """Deteksi OS & sinkronkan ke memory. Return 'added'|'updated'|'unchanged'."""
    try:
        return longmem.upsert(_PREFIX, _fact())
    except Exception:
        return "unchanged"

"""Tool eksekusi kode & perintah — DENGAN PENGAMANAN.

Berjalan di dalam folder workspace, dengan timeout, dan bisa dimatikan total
lewat env ALLOW_CODE_EXEC=false.
"""
from __future__ import annotations

import subprocess
import sys

from .. import config
from .base import tool

WORKSPACE = str(config.PROJECT_ROOT.resolve())


def _guard() -> str | None:
    if not config.ALLOW_CODE_EXEC:
        return (
            "[dinonaktifkan] Eksekusi kode dimatikan. Set ALLOW_CODE_EXEC=true "
            "di .env untuk mengaktifkan."
        )
    return None


@tool
def run_python(code: str) -> str:
    """Jalankan potongan kode Python dan kembalikan output-nya (stdout+stderr). Berguna untuk perhitungan, memproses data, atau memverifikasi kode.

    code: kode Python yang akan dijalankan.
    """
    blocked = _guard()
    if blocked:
        return blocked
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=config.CODE_EXEC_TIMEOUT,
            cwd=WORKSPACE,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout] Kode melebihi {config.CODE_EXEC_TIMEOUT} detik."
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.strip() or "(tidak ada output)"
    if len(out) > 10000:
        out = out[:10000] + "\n... [dipotong]"
    return f"exit_code={proc.returncode}\n{out}"


@tool
def run_command(command: str) -> str:
    """Jalankan sebuah perintah shell di dalam folder workspace dan kembalikan output-nya.

    command: perintah shell (mis. 'ls -la', 'pip list').
    """
    blocked = _guard()
    if blocked:
        return blocked
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=config.CODE_EXEC_TIMEOUT,
            cwd=WORKSPACE,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout] Perintah melebihi {config.CODE_EXEC_TIMEOUT} detik."
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.strip() or "(tidak ada output)"
    if len(out) > 10000:
        out = out[:10000] + "\n... [dipotong]"
    return f"exit_code={proc.returncode}\n{out}"

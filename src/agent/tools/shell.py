"""Tool eksekusi kode & perintah — DENGAN PENGAMANAN.

Berjalan di dalam folder workspace, NON-INTERAKTIF (stdin ditutup supaya perintah
yang biasanya bertanya tidak menggantung), dengan timeout yang MEMBUNUH SELURUH
POHON PROSES (bukan cuma shell induk), dan bisa dimatikan total lewat env
ALLOW_CODE_EXEC=false.
"""
from __future__ import annotations

import os
import signal
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


def _popen(args, *, shell: bool) -> subprocess.Popen:
    """Jalankan proses NON-INTERAKTIF di grup/sesi sendiri agar bisa dibunuh tuntas."""
    kwargs = dict(
        cwd=WORKSPACE,
        stdin=subprocess.DEVNULL,   # kunci: perintah interaktif dapat EOF, tak menggantung
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # gabung stderr ke stdout
        text=True,
        shell=shell,
    )
    if os.name == "nt":
        # Grup proses baru -> taskkill /T bisa menyapu seluruh anak (node, dll).
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # Sesi baru -> os.killpg membunuh seluruh grup proses.
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Bunuh proses beserta SEMUA anak-cucunya (agar tak ada yang tertinggal hidup)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _execute(args, *, shell: bool, timeout: int) -> tuple[int | None, str, bool]:
    """Return (exit_code|None, output, timed_out). Tak akan menggantung selamanya."""
    proc = _popen(args, shell=shell)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        # Ambil output yang sempat keluar (jangan menggantung lagi di sini).
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
        return None, out or "", True


def _clip(out: str, limit: int = 10000) -> str:
    out = out.strip() or "(tidak ada output)"
    if len(out) > limit:
        out = out[:limit] + "\n... [dipotong]"
    return out


@tool
def run_python(code: str) -> str:
    """Jalankan potongan kode Python dan kembalikan output-nya (stdout+stderr). Berguna untuk perhitungan, memproses data, atau memverifikasi kode.

    code: kode Python yang akan dijalankan.
    """
    blocked = _guard()
    if blocked:
        return blocked
    rc, out, timed_out = _execute(
        [sys.executable, "-c", code], shell=False, timeout=config.CODE_EXEC_TIMEOUT
    )
    if timed_out:
        return (
            f"[GAGAL/timeout] Kode melebihi {config.CODE_EXEC_TIMEOUT} detik dan "
            f"dihentikan — JANGAN anggap berhasil.\n" + _clip(out, 4000)
        )
    if rc != 0:
        return (
            f"[GAGAL] exit_code={rc} — kode TIDAK berhasil. Jangan anggap selesai; "
            f"baca error lalu perbaiki.\n" + _clip(out)
        )
    return f"exit_code={rc}\n{_clip(out)}"


@tool
def run_command(command: str) -> str:
    """Jalankan sebuah perintah shell di dalam folder workspace dan kembalikan output-nya. Perintah dijalankan NON-INTERAKTIF (stdin ditutup) — untuk perintah yang biasanya bertanya (mis. create-next-app, npm init, yarn create), WAJIB tambahkan flag non-interaktif seperti '--yes'/'-y'/'--defaults', jika tidak akan gagal/terpotong. Perintah yang berjalan lama dibatasi waktu & seluruh prosesnya dihentikan bila melebihi batas.

    command: perintah shell (mis. 'npm install', 'npx create-next-app my-app --yes').
    """
    blocked = _guard()
    if blocked:
        return blocked
    rc, out, timed_out = _execute(
        command, shell=True, timeout=config.COMMAND_TIMEOUT
    )
    if timed_out:
        return (
            f"[GAGAL/timeout] Perintah melewati {config.COMMAND_TIMEOUT} detik dan "
            f"dihentikan beserta seluruh subprosesnya — perintah BELUM selesai, "
            f"JANGAN anggap berhasil. Kemungkinan menunggu input interaktif atau "
            f"memang sangat lama. Gunakan flag non-interaktif (mis. '--yes'), atau "
            f"pecah menjadi langkah lebih kecil. Output sejauh ini:\n"
            + _clip(out, 4000)
        )
    if rc != 0:
        return (
            f"[GAGAL] exit_code={rc} — perintah TIDAK berhasil. Jangan anggap "
            f"selesai; baca error di bawah lalu perbaiki.\n" + _clip(out)
        )
    return f"exit_code={rc}\n{_clip(out)}"

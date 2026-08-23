"""Tool eksekusi kode & perintah — DENGAN PENGAMANAN.

Berjalan di dalam folder workspace, NON-INTERAKTIF (stdin ditutup supaya perintah
yang biasanya bertanya tidak menggantung), dengan timeout yang MEMBUNUH SELURUH
POHON PROSES (bukan cuma shell induk), dan bisa dimatikan total lewat env
ALLOW_CODE_EXEC=false.
"""
from __future__ import annotations

import atexit
import collections
import itertools
import os
import signal
import subprocess
import sys
import threading
import time

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


def _popen(args, *, shell: bool, stdin_pipe: bool = False) -> subprocess.Popen:
    """Jalankan proses di grup/sesi sendiri agar bisa dibunuh tuntas.

    stdin default DITUTUP (perintah interaktif dapat EOF, tak menggantung).
    stdin_pipe=True (dipakai run_command_bg) membuka saluran input sehingga
    proses latar bisa dikirimi ketikan lewat bg_send."""
    kwargs = dict(
        cwd=WORKSPACE,
        stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # gabung stderr ke stdout
        text=True,
        # Tanpa ini Python pakai encoding default sistem (cp1252 di Windows),
        # padahal proses anak (npm/git/node) menghasilkan UTF-8. Akibatnya
        # UnicodeDecodeError di thread pembaca subprocess. errors="replace"
        # memastikan byte yang benar-benar bukan UTF-8 tidak pernah crash.
        encoding="utf-8",
        errors="replace",
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


# ---------------------------------------------------------------------------
# Perintah LATAR (menetap) — multitasking: proses jalan terus sementara bagas-ai
# tetap bisa merespons & memakai tool lain. Untuk server dev, watch, dll.
# ---------------------------------------------------------------------------
_BG: dict[str, dict] = {}
_bg_seq = itertools.count(1)


def _bg_reader(entry: dict) -> None:
    """Baca output proses latar ke buffer bergulir (agar pipe tak penuh/nge-blok)."""
    proc = entry["proc"]
    try:
        for line in iter(proc.stdout.readline, ""):
            entry["lines"].append(line.rstrip("\n"))
    except Exception:
        pass
    finally:
        try:
            entry["rc"] = proc.wait()
        except Exception:
            entry["rc"] = None
        entry["running"] = False


def _cleanup_bg() -> None:
    for e in list(_BG.values()):
        if e.get("running"):
            try:
                _kill_tree(e["proc"])
            except Exception:
                pass


atexit.register(_cleanup_bg)


@tool
def run_command_bg(command: str) -> str:
    """Jalankan perintah yang MENETAP / berjalan lama (server dev, watcher, bot) di LATAR belakang, lalu SEGERA kembali tanpa menunggu selesai — sehingga kamu bisa lanjut merespons & memakai tool lain (multitasking). PAKAI ini untuk 'npm run dev', 'npm start', 'vite', 'uvicorn', 'flask run', 'watch', dsb. JANGAN pakai run_command untuk perintah menetap (akan menggantung). Cek keluaran dengan bg_output, kirim input/jawaban prompt dengan bg_send, hentikan dengan bg_stop.

    command: perintah menetap yang akan dijalankan di latar (mis. 'npm run dev').
    """
    blocked = _guard()
    if blocked:
        return blocked
    proc = _popen(command, shell=True, stdin_pipe=True)
    bid = f"bg{next(_bg_seq)}"
    entry = {
        "id": bid, "command": command, "proc": proc,
        "lines": collections.deque(maxlen=1000), "running": True,
        "rc": None, "start": time.time(),
    }
    _BG[bid] = entry
    threading.Thread(target=_bg_reader, args=(entry,), daemon=True).start()
    time.sleep(0.8)  # beri jeda: tangkap output awal / deteksi gagal-cepat
    head = "\n".join(list(entry["lines"])[-20:])
    if not entry["running"]:
        return (
            f"[bg {bid}] perintah langsung BERHENTI (exit_code={entry['rc']}). "
            f"Mungkin gagal start — periksa output:\n{head or '(tidak ada output)'}"
        )
    return (
        f"[bg {bid}] BERJALAN di latar (PID {proc.pid}). Perintah TIDAK menggantung — "
        f"kamu bisa LANJUT merespons/pakai tool lain sekarang. "
        f"Lihat log: bg_output('{bid}') · hentikan: bg_stop('{bid}').\n"
        f"Output awal:\n{head or '(belum ada output)'}"
    )


@tool
def bg_output(bg_id: str, lines: int = 40) -> str:
    """Ambil keluaran TERBARU dari sebuah perintah latar (yang dijalankan run_command_bg), serta status jalan/berhentinya. Berguna untuk memantau server dev, mengecek apakah sudah siap, atau membaca error.

    bg_id: id perintah latar (mis. 'bg1').
    lines: berapa baris terakhir yang ditampilkan (default 40).
    """
    e = _BG.get(bg_id)
    if not e:
        aktif = ", ".join(_BG.keys()) or "(tidak ada)"
        return f"[bg] id '{bg_id}' tak ditemukan. Perintah latar yang ada: {aktif}"
    n = max(1, int(lines) if str(lines).isdigit() or isinstance(lines, int) else 40)
    tail = list(e["lines"])[-n:]
    status = "BERJALAN" if e["running"] else f"BERHENTI (exit_code={e['rc']})"
    body = "\n".join(tail) or "(belum ada output)"
    return f"[bg {bg_id}] {status} · {e['command']}\n{body}"


def _tulis_stdin_berbatas(proc: subprocess.Popen, data: str,
                          batas: float = 5.0) -> Exception | None:
    """Tulis ke stdin proses latar DENGAN BATAS WAKTU; None bila sukses.

    Tanpa batas, `stdin.write` pada proses yang TIDAK membaca stdin (sudah
    macet / buffer pipa penuh) menggantung TANPA AKHIR — dan karena tool
    dieksekusi di thread giliran, seluruh giliran ikut terkunci sampai
    pengguna menekan Ctrl+C. Tulis di thread bantu lalu tunggu sebentar:
    kegagalan dilaporkan jujur, giliran tetap hidup."""
    hasil: dict = {"galat": None}

    def _tulis() -> None:
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except Exception as exc:  # noqa: BLE001 - dilaporkan ke pemanggil
            hasil["galat"] = exc

    th = threading.Thread(target=_tulis, daemon=True)
    th.start()
    th.join(batas)
    if th.is_alive():
        return TimeoutError(
            f"proses tidak membaca stdin (tulis tak selesai dalam {batas:.0f} detik)")
    return hasil["galat"]


@tool
def bg_send(bg_id: str, text: str, press_enter: bool = True) -> str:
    """Kirim INPUT (ketikan) ke sebuah perintah latar yang sedang berjalan — untuk menjawab prompt interaktif ('Ok to proceed? y'), mengetik ke REPL, atau perintah CLI yang menunggu jawaban. Setelahnya cek reaksi prosesnya via output yang dikembalikan / bg_output.

    bg_id: id perintah latar (mis. 'bg1', dari run_command_bg).
    text: teks yang dikirim ke stdin proses itu.
    press_enter: true (default) = akhiri dengan Enter (newline).
    """
    e = _BG.get(bg_id)
    if not e:
        aktif = ", ".join(_BG.keys()) or "(tidak ada)"
        return f"[bg] id '{bg_id}' tak ditemukan. Perintah latar yang ada: {aktif}"
    if not e["running"]:
        return (f"[error] {bg_id} sudah BERHENTI (exit_code={e['rc']}) — "
                "tak ada proses yang bisa menerima input.")
    proc = e["proc"]
    if proc.stdin is None:
        return f"[error] {bg_id} tidak membuka saluran input (stdin tertutup)."
    data = text if (text.endswith("\n") or not press_enter) else text + "\n"
    galat = _tulis_stdin_berbatas(proc, data)
    if galat is not None:
        return (f"[GAGAL] tak bisa mengirim input ke {bg_id}: {galat}. "
                "Prosesnya kemungkinan tidak sedang membaca stdin "
                "(atau baru saja berhenti — cek bg_output).")
    # Beri proses waktu sejenak bereaksi supaya output balasannya ikut terbawa.
    time.sleep(0.8)
    tail = "\n".join(list(e["lines"])[-15:])
    status = "BERJALAN" if e["running"] else f"BERHENTI (exit_code={e['rc']})"
    return (f"[bg {bg_id}] input terkirim: {text!r}. Status: {status}.\n"
            f"Output terbaru:\n{tail or '(belum ada output baru)'}")


@tool
def bg_stop(bg_id: str) -> str:
    """Hentikan (matikan) sebuah perintah latar beserta seluruh subprosesnya. Pakai bila server/watcher sudah tak diperlukan atau perlu di-restart.

    bg_id: id perintah latar (mis. 'bg1'), atau 'all' untuk menghentikan semua.
    """
    if bg_id == "all":
        stopped = []
        for e in list(_BG.values()):
            if e["running"]:
                _kill_tree(e["proc"])
                e["running"] = False
                stopped.append(e["id"])
        return f"[bg] dihentikan: {', '.join(stopped) or '(tidak ada yang berjalan)'}"
    e = _BG.get(bg_id)
    if not e:
        return f"[bg] id '{bg_id}' tak ditemukan."
    if e["running"]:
        _kill_tree(e["proc"])
        e["running"] = False
        return f"[bg {bg_id}] dihentikan."
    return f"[bg {bg_id}] memang sudah berhenti (exit_code={e['rc']})."


@tool
def bg_list() -> str:
    """Daftar semua perintah latar (run_command_bg) beserta statusnya."""
    if not _BG:
        return "(tidak ada perintah latar)"
    rows = []
    for e in _BG.values():
        st = "BERJALAN" if e["running"] else f"berhenti(exit={e['rc']})"
        rows.append(f"{e['id']}: {st} · {e['command']}")
    return "\n".join(rows)

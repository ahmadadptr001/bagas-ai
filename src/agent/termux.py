"""Dukungan Termux/Android — deteksi lingkungan & perintah bawaan Android.

Termux adalah lingkungan Linux (bionic), bukan distribusi desktop: banyak
pustaka ber-wheel manylinux tak bisa dipasang, Playwright/Chromium tak ada,
dan aplikasi tak boleh menyentuh mikrofon atau layar langsung. Yang tersedia
justru jembatan Android sendiri:

    screencap              tangkapan layar (bawaan /system/bin)
    termux-notification    notifikasi Android (paket termux-api)
    termux-open            buka berkas/URL di aplikasi bawaan
    termux-clipboard-set   clipboard Android
    termux-media-player    putar audio lewat pemutar Android
    termux-speech-to-text  dikte suara via mesin STT Android

Modul ini mengumpulkan semua perbedaan itu di satu tempat supaya modul lain
cukup bertanya ``aktif()`` dan memakai helper yang aman: setiap fungsi di sini
mengembalikan False/"" saat perkakasnya tidak ada, tak pernah melempar.

PENTING: modul ini hanya memakai pustaka bawaan Python. Ia diimpor modul inti
(termasuk yang berjalan sebelum dependensi terpasang), jadi tak boleh menarik
rich/dotenv/apa pun.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def aktif() -> bool:
    """True bila proses ini berjalan di dalam Termux (Android).

    Termux mengekspor ``TERMUX_VERSION`` di setiap shell yang ia jalankan;
    ``PREFIX`` juga selalu menunjuk ke ``.../com.termux/files/usr``. Salah
    satunya cukup — sebagian skrip (mis. cron Termux) hanya mewarisi PREFIX.
    """
    if os.environ.get("TERMUX_VERSION"):
        return True
    return "com.termux" in os.environ.get("PREFIX", "")


#: Dievaluasi sekali; lingkungan Termux tidak berubah selama proses hidup.
IS_TERMUX = aktif()


def perintah(nama: str) -> str:
    """Path executable Termux/Android, atau "" bila tak tersedia."""
    return shutil.which(nama) or ""


def _jalankan(args: list[str], timeout: float = 20.0,
              masukan: bytes | None = None) -> subprocess.CompletedProcess | None:
    """Jalankan perintah tanpa shell; None bila tak bisa dijalankan.

    Semua pemanggil memperlakukan kegagalan sebagai fitur yang tidak tersedia,
    bukan galat yang menjatuhkan sesi.
    """
    try:
        return subprocess.run(
            args, input=masukan, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _getprop(nama: str) -> str:
    exe = perintah("getprop") or "/system/bin/getprop"
    if not Path(exe).is_file():
        return ""
    hasil = _jalankan([exe, nama], timeout=5)
    return (hasil.stdout or "").strip() if hasil and hasil.returncode == 0 else ""


def versi_android() -> str:
    """Versi Android (mis. "14"), atau "" bila tak terbaca."""
    return _getprop("ro.build.version.release")


def model_perangkat() -> str:
    """Merek + model perangkat (mis. "Samsung SM-G991B")."""
    merek = _getprop("ro.product.manufacturer")
    model = _getprop("ro.product.model")
    return " ".join(x for x in (merek, model) if x).strip()


def chipset() -> str:
    """Nama SoC/CPU perangkat, atau "" bila tak terbaca."""
    return (_getprop("ro.soc.model") or _getprop("ro.board.platform")
            or _getprop("ro.hardware"))


# --- tangkapan layar ---------------------------------------------------------

def screencap_tersedia() -> bool:
    exe = perintah("screencap") or "/system/bin/screencap"
    return Path(exe).is_file()


def screenshot(tujuan: Path | str) -> bool:
    """Simpan tangkapan layar Android ke ``tujuan`` (PNG).

    ``screencap -p`` adalah binary bawaan Android dan tidak butuh root.
    Termux hanya perlu menulis ke folder miliknya sendiri (mis. proyek di
    ``$HOME``), bukan ke /sdcard.
    """
    if not screencap_tersedia():
        return False
    exe = perintah("screencap") or "/system/bin/screencap"
    path = Path(tujuan)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    hasil = _jalankan([exe, "-p", str(path)], timeout=20)
    try:
        if (hasil and hasil.returncode == 0 and path.is_file()
                and path.stat().st_size > 0):
            return True
    except OSError:
        pass
    # Jangan tinggalkan PNG separuh jadi yang nanti terbaca sebagai gambar sah.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


# --- audio -------------------------------------------------------------------

def media_player_tersedia() -> bool:
    return bool(perintah("termux-media-player"))


def putar_audio(berkas: Path | str) -> bool:
    """Mulai memutar berkas lewat pemutar Android (kembali seketika)."""
    exe = perintah("termux-media-player")
    if not exe:
        return False
    hasil = _jalankan([exe, "play", str(berkas)], timeout=10)
    return bool(hasil and hasil.returncode == 0)


def hentikan_audio() -> None:
    exe = perintah("termux-media-player")
    if exe:
        _jalankan([exe, "stop"], timeout=10)


def durasi_audio(berkas: Path | str) -> float:
    """Perkiraan durasi (detik) untuk menunggu pemutaran selesai.

    ffprobe dipakai bila ada (ffmpeg dipasang installer Termux); tanpa itu,
    ukuran berkas MP3 diperkirakan pada 48 kbps — cukup untuk menjaga antrean
    ucapan tidak saling menimpa.
    """
    exe = perintah("ffprobe")
    if exe:
        hasil = _jalankan(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(berkas)], timeout=10)
        try:
            durasi = float((hasil.stdout or "").strip()) if hasil else 0.0
            if durasi > 0:
                return durasi
        except (TypeError, ValueError):
            pass
    try:
        return max(1.0, Path(berkas).stat().st_size * 8 / 48_000.0)
    except OSError:
        return 8.0


def getar(ms: int = 80) -> bool:
    """Getarkan perangkat (Termux:API), bila tersedia."""
    exe = perintah("termux-vibrate")
    if not exe:
        return False
    hasil = _jalankan([exe, "-d", str(int(ms))], timeout=5)
    return bool(hasil and hasil.returncode == 0)


# --- notifikasi, clipboard, buka berkas --------------------------------------

def notifikasi(judul: str, pesan: str) -> bool:
    exe = perintah("termux-notification")
    if exe:
        hasil = _jalankan(
            [exe, "--title", judul, "--content", pesan], timeout=15)
        if hasil and hasil.returncode == 0:
            return True
    toast = perintah("termux-toast")
    if toast:
        hasil = _jalankan([toast, "--short", f"{judul}: {pesan}"], timeout=10)
        return bool(hasil and hasil.returncode == 0)
    return False


def buka(sasaran: str) -> bool:
    exe = perintah("termux-open")
    if not exe:
        return False
    hasil = _jalankan([exe, sasaran], timeout=20)
    return bool(hasil and hasil.returncode == 0)


def salin(teks: str) -> bool:
    exe = perintah("termux-clipboard-set")
    if not exe:
        return False
    hasil = _jalankan([exe], timeout=15, masukan=teks)
    return bool(hasil and hasil.returncode == 0)


def tempel() -> str:
    exe = perintah("termux-clipboard-get")
    if not exe:
        return ""
    hasil = _jalankan([exe], timeout=15)
    return (hasil.stdout or "") if hasil and hasil.returncode == 0 else ""


def api_tersedia() -> bool:
    """True bila paket `termux-api` terpasang (aplikasi Termux:API terpisah)."""
    return bool(perintah("termux-notification")
                or perintah("termux-toast")
                or perintah("termux-clipboard-get"))


# --- dikte suara -------------------------------------------------------------

def stt_tersedia() -> bool:
    return bool(perintah("termux-speech-to-text"))


def tts_tersedia() -> bool:
    return bool(perintah("termux-tts-speak"))


def dengar_teks(timeout: float = 60.0,
                berhenti=None) -> tuple[bool, str]:
    """Dikte satu ucapan lewat mesin STT Android.

    ``termux-speech-to-text`` membuka dialog/rekaman Android, lalu mencetak
    hasil terakhir ke stdout. Prosesnya dijalankan sebagai Popen supaya tombol
    "berhenti" (``berhenti`` = threading.Event) bisa menghentikannya.

    Return ``(berhasil, teks)``. Gagal TIDAK melempar: fitur yang tak tersedia
    tak boleh menjatuhkan sesi.
    """
    exe = perintah("termux-speech-to-text")
    if not exe:
        return False, ""
    try:
        proses = subprocess.Popen(
            [exe], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return False, ""
    batas = time.monotonic() + max(5.0, timeout)
    try:
        while proses.poll() is None:
            if berhenti is not None and getattr(berhenti, "is_set", lambda: False)():
                proses.terminate()
                break
            if time.monotonic() >= batas:
                proses.terminate()
                break
            time.sleep(0.2)
        try:
            keluaran, _ = proses.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proses.kill()
            keluaran, _ = proses.communicate()
    except Exception:  # noqa: BLE001 - dikte tak boleh menjatuhkan sesi
        try:
            proses.kill()
        except Exception:  # noqa: BLE001
            pass
        return False, ""
    baris = [b.strip() for b in (keluaran or "").splitlines() if b.strip()]
    return (proses.returncode == 0, baris[-1] if baris else "")

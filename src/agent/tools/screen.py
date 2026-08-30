"""Tool tangkapan layar (screenshot) — untuk DEBUG VISUAL.

Hasilnya bukan sekadar file: bila model yang aktif bisa MELIHAT gambar (mis.
connector AI web), bagas-ai otomatis melampirkan PNG-nya ke pesan berikutnya.
Penanda `[GAMBAR] <path>` di akhir hasil tool itulah yang dibaca core untuk tahu
file mana yang harus dilampirkan — jadi tool lain yang menghasilkan gambar
(diagram, grafik) cukup memakai penanda yang sama agar ikut terlampir.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from .base import tool
from .files import _display, _safe_path

# Folder default penyimpanan (relatif root project) & batas sisi terpanjang
# gambar. Layar 4K menghasilkan PNG belasan MB — terlalu berat untuk diunggah
# dan tak menambah informasi untuk debugging tampilan.
_SC_DIR = "screenshots"
_MAX_EDGE = 1920

# Penanda yang DIBACA core: file gambar ini dilampirkan ke pesan berikutnya.
IMAGE_MARK = "[GAMBAR]"

# Screenshot /live memakai nama unik per giliran. Path TIDAK boleh dipakai
# ulang: model API menyimpan path lampiran di riwayat, sehingga menimpa file
# yang sama akan membuat screenshot lama tiba-tiba berubah menjadi layar baru.
_LIVE_PREFIX = "live-"
LIVE_SCREEN_PATH = f"{_SC_DIR}/live-current.png"  # cleanup versi lama


class ScreenCaptureError(RuntimeError):
    """Tangkapan layar tidak tersedia pada sesi desktop saat ini."""


def screen_capture_available() -> tuple[bool, str]:
    """Periksa dependensi screenshot tanpa mengambil gambar."""
    try:
        from PIL import ImageGrab  # noqa: F401
    except ImportError:
        return False, "Pillow belum terpasang — jalankan: pip install pillow"
    return True, ""


def capture_screen(path: str = LIVE_SCREEN_PATH) -> Path:
    """Ambil layar dan kembalikan path PNG absolut.

    Fungsi non-tool ini dipakai UI /live agar path bisa dilampirkan langsung
    ke prompt tanpa mengurai teks hasil ``take_screenshot``.
    """
    tersedia, alasan = screen_capture_available()
    if not tersedia:
        raise ScreenCaptureError(alasan)

    from PIL import ImageGrab

    rel = path.strip() or LIVE_SCREEN_PATH
    if not rel.lower().endswith((".png", ".jpg", ".jpeg")):
        rel += ".png"
    try:
        target = _safe_path(rel)
    except ValueError as exc:
        raise ScreenCaptureError(str(exc)) from exc

    try:
        # all_screens: ikutkan semua monitor (hanya didukung di Windows).
        try:
            img = ImageGrab.grab(all_screens=True)
        except TypeError:
            img = ImageGrab.grab()
    except Exception as exc:  # noqa: BLE001 — layar terkunci/headless
        raise ScreenCaptureError(
            f"tak bisa mengambil screenshot: {exc}. Pastikan ada sesi "
            "desktop aktif (bukan lewat SSH/headless)."
        ) from exc

    if max(img.size) > _MAX_EDGE:
        img.thumbnail((_MAX_EDGE, _MAX_EDGE))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Jangan menyimpan PNG dengan ekstensi JPEG: lampiran kemudian
        # dideteksi dari ekstensi dan provider akan menerima MIME yang salah.
        if target.suffix.lower() in (".jpg", ".jpeg"):
            img.convert("RGB").save(target, "JPEG", quality=95, optimize=True)
        else:
            img.save(target, "PNG", optimize=True)
    except OSError as exc:
        raise ScreenCaptureError(
            f"tak bisa menyimpan {_display(target)}: {exc}") from exc
    return target


def capture_live_screen() -> Path:
    """Ambil screenshot unik untuk satu giliran /live."""
    return capture_screen(
        f"{_SC_DIR}/{_LIVE_PREFIX}{time.time_ns()}.png")


def clear_live_capture() -> None:
    """Hapus hanya screenshot sementara yang dibuat oleh /live."""
    try:
        folder = _safe_path(_SC_DIR)
        _safe_path(LIVE_SCREEN_PATH).unlink(missing_ok=True)
        if folder.is_dir():
            for target in folder.glob(f"{_LIVE_PREFIX}*.png"):
                if target.is_file():
                    target.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _proses_dari_pid(pid: int) -> str:
    """Nama executable dari PID lewat Win32 murni (tanpa dependensi)."""
    import ctypes
    from ctypes import wintypes

    # PROCESS_QUERY_LIMITED_INFORMATION: cukup untuk nama image, tak butuh
    # hak akses yang lebih tinggi (dengan itu proses elevasi pun terbaca).
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        ukuran = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(ukuran.value)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(ukuran)):
            return buf.value.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@tool
def active_window() -> str:
    """Cek window/aplikasi mana yang SEDANG DIAKTIFKAN pengguna (foreground).

    Jawabannya diambil LANGSUNG dari OS — judul window plus nama prosesnya —
    jadi model teks pun tahu pasti aplikasi mana yang sedang terbuka, tanpa
    perlu bisa melihat screenshot. Ini cara termutakhir menjawab "user sedang
    di window mana?"; pakai take_screenshot/read_image_local hanya bila isi
    TAMPILANNYA yang perlu diperiksa.
    """
    import ctypes

    if sys.platform == "win32":
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ("[error] tak ada window foreground (layar mungkin "
                    "terkunci / sesi headless).")
        panjang = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(1, panjang) + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, panjang + 1)
        judul = buf.value.strip()
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proses = _proses_dari_pid(pid.value) if pid.value else ""
        if not judul and not proses:
            return "[error] judul window foreground tak terbaca."
        return (f"Window aktif: {judul or '(tanpa judul)'}\n"
                f"Proses: {proses or 'tak diketahui'} (pid {pid.value or '?'})")
    if sys.platform == "darwin":
        import subprocess
        try:
            proses = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of '
                 "first process whose frontmost is true"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"[error] tak bisa menanyakannya ke macOS: {exc}"
        return (f"Window aktif: aplikasi {proses or 'tak diketahui'} "
                "(judul window tidak diekspos AppleScript)")
    # Linux/X11: dua langkah xprop (root -> window aktif -> atributnya).
    import subprocess
    try:
        root = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                              capture_output=True, text=True, timeout=10)
        wid = root.stdout.split("#")[-1].strip().split()[0]
        info = subprocess.run(
            ["xprop", "-id", wid, "WM_CLASS", "_NET_WM_NAME"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        return f"[error] tak bisa membacanya dari window manager: {exc}"
    return f"Window aktif (id {wid}):\n{info.stdout.strip()}"


@tool
def take_screenshot(path: str = "", note: str = "") -> str:
    """Ambil tangkapan layar (screenshot) layar pengguna lalu simpan sebagai PNG.

    Pakai ini untuk DEBUG VISUAL — melihat tampilan aplikasi/error yang sedang
    dilihat pengguna. Bila modelmu bisa melihat gambar, file hasilnya OTOMATIS
    dilampirkan ke pesan berikutnya sehingga kamu benar-benar melihatnya; tak
    perlu meminta pengguna mengirim gambar manual.

    path: nama file tujuan (opsional). Default: screenshots/sc-<waktu>.png
    note: catatan singkat soal apa yang sedang didebug (opsional).
    """
    rel = path.strip() or f"{_SC_DIR}/sc-{time.strftime('%Y%m%d-%H%M%S')}.png"
    try:
        target = capture_screen(rel)
    except ScreenCaptureError as exc:
        return f"[GAGAL] {exc}"

    kb = max(1, target.stat().st_size // 1024)
    try:
        from PIL import Image
        with Image.open(target) as img:
            w, h = img.size
    except Exception:  # noqa: BLE001 — metadata bukan syarat lampiran
        w, h = 0, 0
    ukuran = f"{w}x{h}px" if w and h else "ukuran tak diketahui"
    return (
        f"Screenshot tersimpan: {_display(target)} — {ukuran}, {kb} KB"
        + (f"\nCatatan: {note}" if note.strip() else "")
        + f"\n{IMAGE_MARK} {target}"
    )

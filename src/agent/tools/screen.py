"""Tool tangkapan layar (screenshot) — untuk DEBUG VISUAL.

Hasilnya bukan sekadar file: bila model yang aktif bisa MELIHAT gambar (mis.
connector AI web), bagas-ai otomatis melampirkan PNG-nya ke pesan berikutnya.
Penanda `[GAMBAR] <path>` di akhir hasil tool itulah yang dibaca core untuk tahu
file mana yang harus dilampirkan — jadi tool lain yang menghasilkan gambar
(diagram, grafik) cukup memakai penanda yang sama agar ikut terlampir.
"""
from __future__ import annotations

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

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
    try:
        from PIL import ImageGrab
    except ImportError:
        return ("[GAGAL] Pillow belum terpasang — jalankan: pip install pillow")

    rel = path.strip() or f"{_SC_DIR}/sc-{time.strftime('%Y%m%d-%H%M%S')}.png"
    if not rel.lower().endswith((".png", ".jpg", ".jpeg")):
        rel += ".png"
    try:
        target = _safe_path(rel)
    except ValueError as e:
        return f"[GAGAL] {e}"

    try:
        # all_screens: ikutkan semua monitor (hanya didukung di Windows).
        try:
            img = ImageGrab.grab(all_screens=True)
        except TypeError:
            img = ImageGrab.grab()
    except Exception as e:  # noqa: BLE001 - mis. layar terkunci / tanpa GUI
        return (f"[GAGAL] tak bisa mengambil screenshot: {e}. "
                "Pastikan ada sesi desktop aktif (bukan lewat SSH/headless).")

    full_w, full_h = img.size
    if max(img.size) > _MAX_EDGE:
        img.thumbnail((_MAX_EDGE, _MAX_EDGE))

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        img.save(target, "PNG", optimize=True)
    except OSError as e:
        return f"[GAGAL] tak bisa menyimpan {_display(target)}: {e}"

    kb = max(1, target.stat().st_size // 1024)
    w, h = img.size
    ukuran = f"{w}x{h}px" + (f" (dari {full_w}x{full_h})" if (w, h) != (full_w, full_h) else "")
    return (
        f"Screenshot tersimpan: {_display(target)} — {ukuran}, {kb} KB"
        + (f"\nCatatan: {note}" if note.strip() else "")
        + f"\n{IMAGE_MARK} {target}"
    )

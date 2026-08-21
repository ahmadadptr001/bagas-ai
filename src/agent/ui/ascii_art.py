"""Render gambar sebagai blok warna Minecraft-style untuk terminal.

Digunakan saat pengguna me-drag gambar ke terminal: alih-alih hanya
menampilkan path file, bagas-ai menampilkan pratinjau pixelated dari
gambar dalam kotak kecil di bawah box chat, lalu mengisi prompt
dengan [foto].
"""
from __future__ import annotations

from pathlib import Path

# Ekstensi gambar yang dikenali
_IMG_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif",
})

# Lebar & tinggi blok pixel (sesuai permintaan: ~1/4 lebar chat box)
BLOCK_W = 18   # kolom pixel (20 total - 2 untuk border │)
BLOCK_H = 8    # baris pixel


def is_image_path(text: str) -> bool:
    """True bila ``text`` adalah path file gambar yang valid & ada di disk."""
    text = text.strip().strip('"').strip("'").strip()
    if not text:
        return False
    if text.startswith("file:///"):
        import urllib.parse
        import urllib.request
        try:
            text = urllib.request.url2pathname(urllib.parse.unquote(text[7:]))
        except Exception:
            pass
    try:
        p = Path(text)
        return p.is_file() and p.suffix.lower() in _IMG_EXT
    except Exception:
        return False


def _load_pixels(
    path: str | Path, width: int, max_height: int,
) -> tuple[list[list[tuple[int, int, int]]], tuple[int, int]] | None:
    """Load & resize gambar, kembalikan pixel RGB 2D + dimensi asli."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(path)
        orig_size = img.size
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        img = img.convert("RGB")
        aspect = img.height / img.width
        new_h = max(1, min(max_height, int(width * aspect * 0.55)))
        img = img.resize((width, new_h), Image.LANCZOS)
        flat = list(img.getdata())
        rows = [flat[r * width: r * width + width] for r in range(new_h)]
        return rows, orig_size
    except Exception:
        return None


def image_to_blocks(
    path: str | Path,
    width: int = BLOCK_W,
    max_height: int = BLOCK_H,
) -> tuple[object, tuple[int, int]] | None:
    """Konversi gambar menjadi blok warna Minecraft-style via Rich Text."""
    try:
        from rich.text import Text as _RT
    except ImportError:
        return None
    result = _load_pixels(path, width, max_height)
    if result is None:
        return None
    rows, orig_size = result
    text = _RT()
    for i, row in enumerate(rows):
        if i > 0:
            text.append("\n")
        for r, g, b in row:
            text.append(" ", style=f"on rgb({r},{g},{b})")
    return text, orig_size


def image_to_blocks_pixels(
    path: str | Path,
    width: int = BLOCK_W,
    max_height: int = BLOCK_H,
) -> tuple[list[list[tuple[int, int, int]]], tuple[int, int]] | None:
    """Konversi gambar menjadi pixel RGB 2D untuk rendering prompt_toolkit.

    Returns
    -------
    (rows, (orig_w, orig_h)) atau None bila gagal.
    rows: list of baris, setiap baris = list of (r, g, b) tuples.
    """
    return _load_pixels(path, width, max_height)


def image_dimensions(path: str | Path) -> tuple[int, int] | None:
    """Kembalikan (width, height) gambar, atau None bila gagal dibaca."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None

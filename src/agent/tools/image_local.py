"""Pembacaan gambar lokal dengan Python, tanpa mengunggah berkas.

Tool di modul ini sengaja dipisahkan dari ``analyze_image``. Tool lama itu
melampirkan gambar ke model vision, sedangkan ``read_image_local`` hanya membuka
berkas di laptop lewat Pillow dan mengembalikan hasil berbentuk teks. Dengan
demikian path maupun byte gambar tidak pernah masuk ke request provider.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import warnings
from pathlib import Path

from .base import tool
from .files import _display, _safe_path

_FORMAT = {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF", "ICO"}
_EKSTENSI = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif",
             ".tiff", ".ico"}
_RAMPA = " .:-=+*#%@"


def _warna_dominan(img, jumlah: int = 5) -> list[tuple[int, tuple[int, int, int]]]:
    """Warna dominan dari salinan kecil; tidak memuat seluruh piksel ke list."""
    sampel = img.convert("RGB")
    sampel.thumbnail((160, 160))
    # quantize() memberi kelompok warna yang jauh lebih bermakna daripada
    # menghitung RGB persis (foto hampir tidak punya dua piksel identik).
    palet = sampel.quantize(colors=jumlah)
    warna = palet.getpalette() or []
    hitung = sorted(palet.getcolors() or [], reverse=True)
    total = max(1, sum(n for n, _ in hitung))
    hasil = []
    for n, indeks in hitung[:jumlah]:
        awal = indeks * 3
        rgb = tuple(warna[awal:awal + 3])
        if len(rgb) == 3:
            hasil.append((round(n * 100 / total), rgb))
    return hasil


def _peta_cahaya(img, lebar: int = 32, tinggi_maks: int = 14) -> str:
    """Sketsa luminans kecil agar struktur kasar bisa dibaca sebagai teks."""
    abu = img.convert("L")
    w, h = abu.size
    if not w or not h:
        return ""
    # Karakter terminal kira-kira dua kali lebih tinggi daripada lebarnya.
    tinggi = max(1, min(tinggi_maks, round((h / w) * lebar * 0.48)))
    kecil = abu.resize((lebar, tinggi))
    px = list(kecil.getdata())
    baris = []
    for y in range(tinggi):
        row = px[y * lebar:(y + 1) * lebar]
        baris.append("".join(_RAMPA[min(len(_RAMPA) - 1,
                                           v * len(_RAMPA) // 256)]
                             for v in row))
    return "\n".join(baris)


def _ocr_lokal(target: Path, bahasa: str, maks: int) -> tuple[str, str]:
    """OCR via executable Tesseract lokal. Tidak memakai jaringan."""
    exe = shutil.which("tesseract")
    if not exe:
        return "", ("tidak tersedia (Tesseract belum terpasang; gambar tetap "
                    "dibaca untuk metadata dan struktur visual)")
    lang = (bahasa or "eng").strip()
    # Nama bahasa Tesseract hanya identifier seperti eng / ind / eng+ind.
    if not lang or any(not (c.isalnum() or c in "+_-") for c in lang):
        return "", f"bahasa OCR tidak sah: {bahasa!r}"
    try:
        proses = subprocess.run(
            [exe, str(target), "stdout", "-l", lang],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"gagal dijalankan: {exc}"
    if proses.returncode != 0:
        alasan = " ".join((proses.stderr or "error tak diketahui").split())
        return "", f"gagal: {alasan[:240]}"
    teks = (proses.stdout or "").strip()
    if not teks:
        return "", "aktif, tetapi tidak menemukan teks"
    if len(teks) > maks:
        teks = teks[:maks].rstrip() + f"\n… OCR dipotong pada {maks} karakter"
    return teks, f"berhasil ({len(teks)} karakter)"


def _qr_lokal(target: Path) -> str:
    """Baca QR dengan OpenCV bila paketnya kebetulan tersedia."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        data = np.fromfile(str(target), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            return ""
        detektor = cv2.QRCodeDetector()
        try:
            ok, nilai, _titik, _ = detektor.detectAndDecodeMulti(img)
            if ok:
                isi = [str(v).strip() for v in nilai if str(v).strip()]
                if isi:
                    return " | ".join(isi)[:1200]
        except (AttributeError, ValueError):
            pass
        nilai, _titik, _ = detektor.detectAndDecode(img)
        return str(nilai).strip()[:1200]
    except Exception:  # noqa: BLE001 - OpenCV sepenuhnya opsional
        return ""


def _sidik_file(target: Path) -> str:
    """SHA-256 streaming; gambar besar tidak disalin utuh ke memori."""
    sidik = hashlib.sha256()
    with target.open("rb") as berkas:
        while blok := berkas.read(1024 * 1024):
            sidik.update(blok)
    return sidik.hexdigest()[:16]


@tool
def read_image_local(path: str, ocr: bool = True,
                     ocr_language: str = "eng",
                     max_ocr_chars: int = 4000) -> str:
    """Baca gambar secara LOKAL lewat Python tanpa mengunggah file atau byte gambarnya ke provider.

    Menghasilkan format, dimensi, mode warna, frame, transparansi, statistik
    cahaya, warna dominan, sketsa luminans, QR (bila OpenCV tersedia), dan OCR
    lokal (bila executable Tesseract tersedia). Ini bukan model vision semantik:
    untuk mengenali objek/adegan secara mendalam tetap perlu model vision dan
    tool analyze_image yang memang mengirim gambar.

    path: path gambar di root project / folder konteks yang diizinkan.
    ocr: coba ambil teks dengan Tesseract lokal (default true).
    ocr_language: kode bahasa Tesseract, mis. eng, ind, atau eng+ind.
    max_ocr_chars: batas teks OCR yang dikembalikan (200-12000).
    """
    try:
        target = _safe_path(path)
    except (OSError, ValueError) as exc:
        return f"[error] path gambar ditolak: {exc}"
    if not target.is_file():
        return f"[error] gambar tidak ditemukan: {_display(target)}"
    if target.suffix.lower() not in _EKSTENSI:
        return (f"[error] {_display(target)} bukan format gambar lokal yang "
                f"didukung ({', '.join(sorted(_EKSTENSI))}).")
    ukuran = target.stat().st_size
    if ukuran == 0:
        return f"[error] {_display(target)} kosong (0 byte)."

    try:
        from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError
    except ImportError:
        return "[error] Pillow belum terpasang. Jalankan: pip install Pillow"

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(target) as sumber:
                format_asli = (sumber.format or "tidak diketahui").upper()
                if format_asli not in _FORMAT:
                    return f"[error] format gambar {format_asli} tidak didukung."
                dimensi_asli = sumber.size
                mode_asli = sumber.mode
                frame = int(getattr(sumber, "n_frames", 1) or 1)
                orientasi = None
                try:
                    orientasi = sumber.getexif().get(274)
                except Exception:  # noqa: BLE001 - EXIF rusak bukan fatal
                    pass
                sumber.seek(0)
                gambar = ImageOps.exif_transpose(sumber).copy()
                gambar.load()
    except (UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        return f"[error] gambar tidak dapat didekode dengan aman: {exc}"

    try:
        abu = gambar.convert("L")
        abu.thumbnail((256, 256))
        stat = ImageStat.Stat(abu)
        terang = round(stat.mean[0], 1)
        kontras = round(stat.stddev[0], 1)
        dominan = _warna_dominan(gambar)
        peta = _peta_cahaya(gambar)
    except (OSError, ValueError) as exc:
        return f"[error] piksel gambar gagal dibaca: {exc}"

    alfa = "tidak"
    if "A" in gambar.getbands():
        kanal = gambar.getchannel("A")
        hist = kanal.histogram()
        total = max(1, sum(hist))
        transparan = sum(hist[:-1])
        alfa = f"ya ({transparan * 100 / total:.1f}% piksel tidak opak)"

    warna_teks = ", ".join(
        f"#{r:02X}{g:02X}{b:02X} ~{persen}%"
        for persen, (r, g, b) in dominan
    ) or "tidak terbaca"
    rasio = dimensi_asli[0] / max(1, dimensi_asli[1])
    sha = _sidik_file(target)
    baris = [
        "PEMBACAAN GAMBAR LOKAL — file tidak diunggah",
        f"Path: {_display(target)}",
        f"Format: {format_asli}; ukuran file: {ukuran} byte; SHA-256: {sha}…",
        (f"Dimensi: {dimensi_asli[0]}×{dimensi_asli[1]} px; rasio: "
         f"{rasio:.3f}; mode: {mode_asli}; frame: {frame}"),
        f"Transparansi: {alfa}; orientasi EXIF: {orientasi or 'tidak ada'}",
        f"Kecerahan rata-rata: {terang}/255; kontras: {kontras}",
        f"Warna dominan: {warna_teks}",
    ]
    qr = _qr_lokal(target)
    if qr:
        baris.append(f"QR lokal: {qr}")
    if peta:
        baris.extend(["Sketsa luminans (terang → @):", peta])

    if ocr:
        batas = max(200, min(int(max_ocr_chars), 12000))
        teks_ocr, status_ocr = _ocr_lokal(target, ocr_language, batas)
        baris.append(f"OCR lokal: {status_ocr}")
        if teks_ocr:
            baris.extend(["Teks OCR:", teks_ocr])
    else:
        baris.append("OCR lokal: dilewati (ocr=false)")
    baris.append(
        "Batas kemampuan: pembacaan ini tidak mengenali objek atau makna adegan "
        "seperti model vision; hasil di atas murni pemrosesan Python lokal."
    )
    return "\n".join(baris)


__all__ = ["read_image_local"]

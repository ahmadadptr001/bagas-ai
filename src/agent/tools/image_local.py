"""Pembacaan gambar lokal dengan Python, tanpa mengunggah berkas.

Tool di modul ini sengaja dipisahkan dari ``analyze_image``. Tool lama itu
melampirkan gambar ke model vision, sedangkan ``read_image_local`` hanya membuka
berkas di laptop lewat Pillow dan mengembalikan hasil berbentuk teks. Dengan
demikian path maupun byte gambar tidak pernah masuk ke request provider.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
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


def _cari_tesseract() -> str:
    """Cari executable Tesseract: lewat PATH dulu, lalu lokasi pasang bawaan.

    Lokasi kedua ini penting: pemasang UB-Mannheim memperbarui PATH lewat
    registry, dan terminal yang SUDAH terbuka tidak melihat perubahan itu
    sampai dibuka ulang. Bersandar pada ``which`` saja berarti OCR tiba-tiba
    "tidak tersedia" justru di sesi yang baru saja memasangnya."""
    exe = shutil.which("tesseract")
    if exe:
        return exe
    if os.name == "nt":
        lokal = os.environ.get("LOCALAPPDATA", "")
        kandidat = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        if lokal:
            kandidat.append(os.path.join(
                lokal, "Programs", "Tesseract-OCR", "tesseract.exe"))
        for c in kandidat:
            if c and Path(c).is_file():
                return c
    return ""


def _ocr_lokal(target: Path, bahasa: str, maks: int) -> tuple[str, str]:
    """OCR via executable Tesseract lokal. Tidak memakai jaringan."""
    exe = _cari_tesseract()
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


def _analisis_cv(target: Path) -> list[str]:
    """Ekstrak ciri visual numerik dengan OpenCV lokal bila tersedia."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        data = np.fromfile(str(target), dtype=np.uint8)
        bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if bgr is None:
            return []
        h, w = bgr.shape[:2]
        skala = min(1.0, 1280.0 / max(h, w, 1))
        if skala < 1.0:
            bgr = cv2.resize(bgr, (max(1, round(w * skala)),
                                   max(1, round(h * skala))),
                             interpolation=cv2.INTER_AREA)
        abu = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        tepi = cv2.Canny(cv2.GaussianBlur(abu, (3, 3), 0), 80, 160)
        kepadatan = float(np.count_nonzero(tepi) * 100.0 / tepi.size)
        ketajaman = float(cv2.Laplacian(abu, cv2.CV_64F).var())
        hasil = [
            f"Analisis OpenCV: skala kerja {bgr.shape[1]}x{bgr.shape[0]} px",
            f"  ketajaman Laplacian: {ketajaman:.1f} "
            f"({'tajam' if ketajaman >= 120 else 'sedang' if ketajaman >= 35 else 'buram'})",
            f"  kepadatan tepi: {kepadatan:.1f}% "
            f"({'detail/tekstur tinggi' if kepadatan >= 18 else 'detail sedang' if kepadatan >= 7 else 'area polos dominan'})",
        ]
        mh, mw = abu.shape
        nama = (("kiri-atas", "kanan-atas"), ("kiri-bawah", "kanan-bawah"))
        for yy in range(2):
            bagian = []
            for xx in range(2):
                blok = abu[yy * mh // 2:(yy + 1) * mh // 2,
                           xx * mw // 2:(xx + 1) * mw // 2]
                nilai = float(np.mean(blok)) if blok.size else 0.0
                bagian.append(f"{nama[yy][xx]} {nilai:.0f}/255")
            hasil.append("  terang kuadran: " + ", ".join(bagian))
        kontur, _ = cv2.findContours(tepi, cv2.RETR_EXTERNAL,
                                     cv2.CHAIN_APPROX_SIMPLE)
        besar = [c for c in kontur if cv2.contourArea(c) >= tepi.size * 0.01]
        hasil.append(f"  kontur besar terdeteksi: {len(besar)}")
        try:
            cascade = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            detector = cv2.CascadeClassifier(cascade)
            wajah = detector.detectMultiScale(abu, 1.1, 5, minSize=(30, 30))
            hasil.append(f"  wajah terdeteksi (Haar lokal): {len(wajah)}")
        except Exception:  # noqa: BLE001
            pass
        return hasil
    except Exception:  # noqa: BLE001 - dependensi tambahan opsional
        return []


def _parse_titik(spec: str, w: int, h: int) -> list[tuple[int, int]]:
    """Parse titik ``x,y`` (piksel atau persen), boleh dipisah ``;``."""
    hasil = []
    for bagian in (spec or "").split(";"):
        try:
            x_raw, y_raw = [v.strip().lower() for v in bagian.split(",", 1)]
            def nilai(raw: str, ukuran: int) -> int:
                if raw.endswith("%"):
                    return round(float(raw[:-1]) * ukuran / 100)
                return round(float(raw))
            hasil.append((max(0, min(w - 1, nilai(x_raw, w))),
                          max(0, min(h - 1, nilai(y_raw, h)))))
        except (ValueError, TypeError):
            continue
    return hasil


def _analisis_fokus(gambar, target: Path, zoom: int, fokus: str,
                    scan_grid: bool, ocr: bool, bahasa: str,
                    maks_ocr: int) -> list[str]:
    """Zoom/crop area lokal dan ubah hasilnya menjadi bukti tekstual."""
    from PIL import ImageStat
    w, h = gambar.size
    zoom = max(1, min(int(zoom), 8))
    if scan_grid:
        zoom = max(zoom, 3)
    titik = _parse_titik(fokus, w, h) if fokus else []
    if scan_grid:
        titik = [(round(w * x), round(h * y))
                 for y in (.2, .5, .8) for x in (.2, .5, .8)]
    if not titik:
        return []
    hasil = [f"Inspeksi zoom lokal: {len(titik)} titik, faktor {zoom}x"]
    cw, ch = max(16, w // zoom), max(16, h // zoom)
    for i, (cx, cy) in enumerate(titik, 1):
        left = max(0, min(w - cw, cx - cw // 2))
        top = max(0, min(h - ch, cy - ch // 2))
        crop = gambar.crop((left, top, left + cw, top + ch))
        abu = crop.convert("L")
        stat = ImageStat.Stat(abu)
        hasil.append(
            f"  titik {i} ({cx},{cy}) area {left},{top}-{left+cw},{top+ch}: "
            f"{cw}x{ch}px, terang {stat.mean[0]:.0f}/255, "
            f"kontras {stat.stddev[0]:.1f}")
        if ocr:
            sementara = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    sementara = Path(f.name)
                crop.save(sementara, "PNG")
                teks, status = _ocr_lokal(sementara, bahasa, maks_ocr)
                hasil.append(f"    OCR titik {i}: {status}")
                if teks:
                    hasil.append(f"    teks titik {i}: {teks}")
            except (OSError, ValueError) as exc:
                hasil.append(f"    OCR titik {i}: gagal ({exc})")
            finally:
                if sementara is not None:
                    sementara.unlink(missing_ok=True)
    return hasil


@tool
def read_image_local(path: str, ocr: bool = True,
                     ocr_language: str = "eng",
                     max_ocr_chars: int = 4000,
                     zoom: int = 1, focus: str = "",
                     scan_grid: bool = False) -> str:
    """Baca gambar secara LOKAL lewat Python tanpa mengunggah file atau byte gambarnya ke provider.

    Menghasilkan format, dimensi, mode warna, frame, transparansi, statistik
    cahaya, warna dominan, sketsa luminans, ciri OpenCV (ketajaman, tepi,
    kuadran, kontur, wajah bila cascade tersedia), QR, dan OCR lokal. Ini
    bukan caption semantik seperti model vision: hasilnya adalah fakta lokal
    yang bisa dipakai model teks untuk menalar gambar tanpa upload.

    path: path gambar di root project / folder konteks yang diizinkan.
    ocr: coba ambil teks dengan Tesseract lokal (default true).
    ocr_language: kode bahasa Tesseract, mis. eng, ind, atau eng+ind.
    max_ocr_chars: batas teks OCR yang dikembalikan (200-12000).
    zoom: faktor zoom crop lokal 1-8 (default 1, tidak membuat crop).
    focus: titik x,y dalam piksel atau persen, beberapa titik dipisah ';'.
        Contoh '80%,20%;400,300'. Kosong berarti tidak crop.
    scan_grid: bila true, inspeksi otomatis 9 titik (grid 3x3).
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

    ciri_cv = _analisis_cv(target)
    if ciri_cv:
        baris.extend(ciri_cv)

    try:
        zoom_diminta = int(zoom)
    except (TypeError, ValueError):
        zoom_diminta = 1
    fokus_diminta = str(focus or "")
    if zoom_diminta > 1 or fokus_diminta.strip() or scan_grid:
        batas_zoom = max(1, min(zoom_diminta, 8))
        baris.extend(_analisis_fokus(
            gambar, target, batas_zoom, fokus_diminta, bool(scan_grid), bool(ocr),
            ocr_language, max(200, min(int(max_ocr_chars), 12000))))

    if ocr:
        batas = max(200, min(int(max_ocr_chars), 12000))
        teks_ocr, status_ocr = _ocr_lokal(target, ocr_language, batas)
        baris.append(f"OCR lokal: {status_ocr}")
        if teks_ocr:
            baris.extend(["Teks OCR:", teks_ocr])
    else:
        baris.append("OCR lokal: dilewati (ocr=false)")
    try:
        from .vision_local import describe_image
        vision = describe_image(target)
        if vision:
            baris.extend(["Analisis vision lokal (Gemma 3n E2B):", vision])
        else:
            baris.append("Vision lokal: tidak aktif (Ollama/model Gemma 3n E2B belum tersedia)")
    except Exception:
        baris.append("Vision lokal: backend tidak tersedia; metadata/OCR tetap digunakan")
    baris.append(
        "Batas: hasil vision adalah bantuan lokal dan sebaiknya diverifikasi untuk "
        "teks kecil atau detail yang ambigu."
    )
    return "\n".join(baris)


__all__ = ["read_image_local"]

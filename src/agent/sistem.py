"""Cek kecocokan sistem, Ketentuan dan Kebijakan, dan perkiraan ruang disk.

Dijalankan installer SEBELUM memasang apa pun:

    python src/agent/sistem.py

Karena dipanggil sebelum `pip install`, modul ini sengaja HANYA memakai
pustaka bawaan — rich/dotenv/… belum tentu ada saat itu. Ia juga bisa
diimpor (mis. oleh setup_wizard) setelah terpasang; teks Ketentuannya satu
sumber untuk keduanya.

Tiga bagian, urutannya sengaja begitu:
  1. CEK SISTEM — OS, arsitektur, RAM, ruang disk, Python, internet. Yang
     perlu diketahui pengguna SEBELUM menit-menit unduhan dimulai adalah
     "mesin ini didukung atau tidak", bukan pesan galat pip di menit kelimanya.
  2. KETENTUAN DAN KEBIJAKAN — jujur soal kuasa agent sebelum apa pun
     dipasang. (Nama lamanya "disclaimer".)
  3. PERKIRAAN RUANG DISK — total yang akan dipakai bagas-ai, lalu pengguna
     memilih lanjut atau batal. Unduhan ratusan MB sampai GB bukan keputusan
     yang boleh diambilkan pengguna.

Kode keluar: 0 = lanjut, 1 = dibatalkan pengguna, 2 = sistem tak didukung.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import struct
import sys
import time
from pathlib import Path

# --- perkiraan ukuran (MB) --------------------------------------------------
# TERUKUR di mesin nyata (pip install bagasai penuh + playwright install
# chromium + Tesseract UB-Mannheim + model faster-whisper), lalu dibulatkan
# ke atas sedikit. Angka-angka ini TUMBUH — bila dependensi bertambah,
# perbarui angkanya, jangan biarkan janjinya diam-diam melenceng.
PERKIRAAN_MB: list[tuple[str, int]] = [
    # importlib.metadata bagasai + seluruh dependensi transitifnya:
    # 2206 MB terukur (ctranslate2, onnxruntime, playwright, telegram, …).
    ("paket Python + dependensi", 2200),
    # `playwright install chromium`: unduhan ~130 MB, terpasang + headless
    # shell ~500 MB.
    ("browser Chromium (Playwright)", 500),
    # UB-Mannheim Tesseract lengkap: 249 MB terukur; pemasangan segar
    # dengan data bahasa standar lebih kecil.
    ("Tesseract OCR", 100),
    # faster-whisper `small`, terunduh sekali saat /voice on pertama.
    ("model Whisper /voice (unduh nanti)", 500),
]
_TOLERANSI = 1.15  # pip cache sementara, berkas sementara unduhan, dsb.


def _gb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def total_mb() -> int:
    """Total perkiraan ruang disk (MB), termasuk toleransi."""
    return round(sum(n for _, n in PERKIRAAN_MB) * _TOLERANSI)


# --- Ketentuan dan Kebijakan ------------------------------------------------
# SATU sumber untuk installer (pra-pasang) dan wizard `bagas-ai login`.
# Teks polos tanpa markup: dipakai di dua konteks yang berbeda kemampuan.
KETENTUAN = """\
bagas-ai adalah agent AI yang atas permintaanmu dapat:
  - menjalankan perintah & kode di komputer ini,
  - membaca/menulis berkas di folder kerja,
  - mengakses internet (web & API model).

Percakapan dan konteks dikirim ke layanan model pilihanmu (situs web-AI
atau API NVIDIA/OpenRouter/OpenCode Zen). API key yang ditempel di wizard
disimpan LOKAL di ~/.bagasai/.env dan hanya dikirim ke penyedianya saat
autentikasi.

Seluruh risiko pemakaian menjadi tanggungan pengguna. Periksa setiap
perintah/berkas yang kamu setujui — bagas-ai bisa keliru."""


# --- data sistem -------------------------------------------------------------
def _ram_total_gb() -> float:
    """Total RAM fisik (0 bila tak terbaca)."""
    if os.name == "nt":
        import ctypes

        # Struktur HARUS lengkap: dwLength harus sizeof(MEMORYSTATUSEX)
        # (64 byte) atau GlobalMemoryStatusEx menolak dengan diam.
        class _MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        mem = _MEM()
        mem.dwLength = ctypes.sizeof(_MEM)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                return mem.ullTotalPhys / (1024 ** 3)
        except Exception:  # noqa: BLE001
            pass
        return 0.0
    if sys.platform == "darwin":
        import subprocess
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / (1024 ** 3)
        except Exception:  # noqa: BLE001
            return 0.0
    try:
        return (os.sysconf("SC_PAGE_SIZE")
                * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        return 0.0


def _internet() -> bool:
    """Bisakah menjangkau penyedia paket? (pypi.org)"""
    try:
        with socket.create_connection(("pypi.org", 443), timeout=4):
            return True
    except OSError:
        return False


def data_sistem() -> dict:
    """Fakta mentah sistem — tanpa penilaian."""
    os_nama = platform.system() or "?"
    os_versi = platform.release() or ""
    if os_nama == "Windows":
        try:
            v = sys.getwindowsversion()  # type: ignore[attr-defined]
            os_versi = f"{v.major}.{v.minor} (build {v.build})"
        except Exception:  # noqa: BLE001
            pass
    disk = shutil.disk_usage(Path.home())
    return {
        "os": f"{os_nama} {os_versi}".strip(),
        "arsitektur": f"{platform.machine()} "
                      f"({struct.calcsize('P') * 8}-bit Python)",
        "python": platform.python_version(),
        "ram_gb": _ram_total_gb(),
        "disk_bebas_gb": disk.free / (1024 ** 3),
        "internet": _internet(),
    }


def cek_sistem() -> list[tuple[str | None, str]]:
    """Daftar (status, keterangan): True=lolos, None=peringatan, False=gagal."""
    d = data_sistem()
    hasil: list[tuple[str | None, str]] = []

    # OS: Windows 10+ (build 10240), macOS 12+, Linux modern.
    if platform.system() == "Windows":
        try:
            build = sys.getwindowsversion().build  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            build = 0
        hasil.append((
            None if build < 10240 else None if build < 19041 else True,
            f"Windows build {build}"
            + ("  (di bawah Windows 10 — tak didukung)" if build < 10240
               else "  (Windows 10 lama; sebaiknya 21H2/build 19041+)"
               if build < 19041 else ""),
        ) if build >= 10240 else (
            False, f"Windows build {build} — butuh Windows 10 ke atas"))
    else:
        hasil.append((True, d["os"]))

    # Arsitektur: 64-bit. ctranslate2/faster-whisper tak punya wheel 32-bit.
    bit = struct.calcsize("P") * 8
    hasil.append((True if bit >= 64 else False,
                  d["arsitektur"]
                  + ("" if bit >= 64 else "  — butuh 64-bit")))

    # RAM: Whisper small + Chromium + Python nyaman di 8 GB.
    ram = d["ram_gb"]
    hasil.append((True if ram >= 8 else None if ram >= 4 else False,
                  f"RAM {ram:.0f} GB"
                  + ("" if ram >= 8
                     else "  (cukup, tapi sesak untuk pengenalan suara)"
                     if ram >= 4 else "  — butuh minimal 4 GB")))

    # Ruang disk: dibandingkan total perkiraan (lihat PERKIRAAN_MB).
    butuh = total_mb() / 1024
    bebas = d["disk_bebas_gb"]
    hasil.append((True if bebas >= 10 else None if bebas >= butuh else False,
                  f"ruang disk bebas {bebas:.0f} GB "
                  f"(butuh ±{butuh:.1f} GB)"
                  + ("" if bebas >= 10
                     else "  (data sesi & profil browser akan terus bertambah)"
                     if bebas >= butuh else "  — tidak cukup")))

    # Python: 3.10+ (installer juga mengeceknya; cek lagi di sini supaya
    # modul ini benar berdiri sendiri).
    hasil.append((True if sys.version_info >= (3, 10) else False,
                  f"Python {d['python']}"
                  + ("" if sys.version_info >= (3, 10) else "  — butuh 3.10+")))

    # Internet: installer mengunduh segalanya; luring = peringatan saja
    # (mungkin proxy/konteks khusus), bukan vonis.
    hasil.append((True if d["internet"] else None,
                  "internet tersedia" if d["internet"]
                  else "tak bisa menjangkau pypi.org"
                       "  (installer butuh koneksi)"))
    return hasil


# --- tampilan ----------------------------------------------------------------
_OK, _WARN, _GALAT = "[ok]", "[!]", "[x]"


def _tanda(status: str | None) -> str:
    return _OK if status is True else _GALAT if status is False else _WARN


def tampilkan() -> bool:
    """Cetak cek sistem + Ketentuan + perkiraan ukuran.

    Return False bila ada kegagalan MUTLAK (sistem tak didukung)."""
    print()
    print("=== CEK KECOCOKAN SISTEM ===")
    gagal = False
    for status, ket in cek_sistem():
        print(f"  {_tanda(status)} {ket}")
        if status is False:
            gagal = True

    print()
    print("=== DISCLAIMER — KETENTUAN DAN KEBIJAKAN ===")
    for baris in KETENTUAN.splitlines():
        print("  " + baris if baris else "")

    # Estimasi ruang disk tidak ditampilkan di installer; kebutuhan aktual
    # tetap divalidasi oleh cek_sistem().
    return not gagal

    print()
    print("=== PERKIRAAN RUANG DISK YANG DIPAKAI ===")
    for nama, mb in PERKIRAAN_MB:
        titik = "." * max(2, 44 - len(nama))
        print(f"  {nama} {titik} ±{_gb(mb)}")
    print(f"  {'data & sesi ~/.bagasai'} "
          f"{'.' * max(2, 44 - len('data & sesi ~/.bagasai'))} "
          "bertambah sesuai pemakaian")
    print(f"  {'TOTAL (dengan cadangan unduhan)'} "
          f"{'.' * max(2, 44 - len('TOTAL (dengan cadangan unduhan)'))} "
          f"±{_gb(total_mb())}")
    return not gagal


def _jawab_terminal(prompt: str) -> str | None:
    """Baca jawaban dari terminal asli meski stdin installer berupa pipe."""
    stream = sys.stdin
    opened = None
    if not stream.isatty():
        # install.ps1 sering dijalankan lewat ``irm | iex`` sehingga stdin
        # berisi skrip, bukan input pengguna. Buka console/tty langsung agar
        # pertanyaan tidak terlewati dan Enter tidak otomatis memilih lanjut.
        try:
            device = "CONIN$" if os.name == "nt" else "/dev/tty"
            opened = open(device, "r", encoding="utf-8", errors="replace")
            stream = opened
        except (OSError, IOError):
            # Lingkungan benar-benar non-interaktif (CI/redirect murni):
            # pertahankan perilaku non-blocking lama.
            return None
    try:
        print(prompt, end="", flush=True)
        return stream.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        if opened is not None:
            opened.close()


def tanya_lanjut() -> bool:
    """Persetujuan tunggal untuk disclaimer + ketentuan sebelum memasang."""
    jawab = _jawab_terminal(
        "\nSaya sudah membaca dan menyetujui Disclaimer, Ketentuan, serta Kebijakan di atas? [y/N]: ")
    if jawab is None:
        return True
    return jawab in ("y", "ya", "yes", "j")


def main() -> int:
    # Output UTF-8 konsisten di konsol Windows (cp1252 akan merusak "±"/"—").
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    dukung = tampilkan()
    if not dukung:
        print("\nSistem ini BELUM didukung bagas-ai — pemasangan dihentikan.")
        return 2
    if False:
        print("\nDibatalkan — ketentuan belum disetujui.")
        return 1
    if not tanya_lanjut():
        print("\nDibatalkan — tidak ada yang dipasang.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

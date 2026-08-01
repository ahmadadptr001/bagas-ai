"""Copot bagas-ai SEKALIGUS seluruh datanya (~/.bagasai).

KENAPA INI SEBUAH PERINTAH, BUKAN HOOK `pip uninstall`
------------------------------------------------------
pip TIDAK punya titik-kait (hook) uninstall — sama sekali, di semua versi.
Saat mencopot, pip hanya menghapus berkas yang tercatat di RECORD milik paket;
tak ada kode paket yang dijalankan (format wheel memang sengaja melarangnya,
dan `setup.py` pun hanya jalan saat MEMBANGUN/memasang, tak pernah saat
mencopot). Jadi mustahil membuat `pip uninstall bagasai` ikut menghapus
~/.bagasai: data itu berada di luar RECORD, dan tak ada satu baris pun kode
kita yang dieksekusi pada saat itu.

Yang BISA dilakukan — dan itulah isi modul ini — adalah menyediakan satu
perintah yang melakukan KEDUANYA sekaligus:

    bagas-ai uninstall

URUTAN KERJA (penting, sebab dua hal saling mengunci di Windows):
  1. Chrome connector dibunuh dulu; selama hidup ia MENGUNCI folder profil di
     ~/.bagasai/browser sehingga penghapusan gagal separuh jalan.
  2. Folder data dihapus di proses ini juga -> hasilnya langsung terlihat &
     bisa dilaporkan jujur ke pengguna.
  3. Pencopotan paket DIJADWALKAN lewat proses pendamping yang menunggu proses
     ini keluar. Alasannya sama dengan pada updater: kita sedang berjalan DARI
     bagasai.exe, dan pip tak bisa menghapus .exe yang masih dipakai
     (WinError 32). Pendamping juga menghapus ulang folder data sesudah pip
     selesai — jaring pengaman untuk berkas yang tadi masih terkunci.

Log pendamping ditulis ke folder TEMP (bukan ~/.bagasai — folder itu justru
sedang dihapus), dan path-nya diberitahukan sebelum keluar supaya kegagalan
tetap bisa ditelusuri, bukan lenyap tanpa jejak.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config

# Nama distribusi di pip (lihat [project].name di pyproject.toml).
PACKAGE = "bagasai"


def data_dir() -> Path:
    """Folder data & konfigurasi yang akan dihapus.

    Sengaja membaca config.CONFIG_HOME, bukan menulis ulang "~/.bagasai":
    lokasinya bisa dipindah lewat BAGASAI_HOME, dan yang harus dihapus adalah
    yang BENAR-BENAR dipakai."""
    return Path(config.CONFIG_HOME)


def installed_version() -> str:
    """Versi paket bagasai yang terpasang, atau "" bila tak terpasang via pip.

    Dipakai untuk membedakan dua keadaan yang penampakannya mirip tapi
    penanganannya beda: terpasang normal (pip uninstall wajar) vs dijalankan
    dari salinan repo/`python run.py` (tak ada yang bisa dicopot pip)."""
    try:
        from importlib.metadata import version

        return version(PACKAGE)
    except Exception:  # noqa: BLE001 - belum terpasang / metadata rusak
        return ""


def _ukuran(folder: Path) -> tuple[int, int]:
    """(jumlah berkas, total byte) — untuk menunjukkan apa yang dipertaruhkan."""
    berkas = 0
    total = 0
    for akar, _dirs, names in os.walk(folder):
        for n in names:
            berkas += 1
            try:
                total += (Path(akar) / n).stat().st_size
            except OSError:
                pass
    return berkas, total


def ukuran_terbaca(n: int) -> str:
    satuan = ("B", "KB", "MB", "GB")
    nilai = float(n)
    for s in satuan:
        if nilai < 1024 or s == satuan[-1]:
            return f"{nilai:.1f} {s}" if s != "B" else f"{int(nilai)} B"
        nilai /= 1024
    return f"{nilai:.1f} GB"


def ringkasan() -> dict:
    """Apa saja yang akan hilang — dipakai untuk layar konfirmasi."""
    d = data_dir()
    ada = d.is_dir()
    berkas, byte = _ukuran(d) if ada else (0, 0)
    return {
        "data_dir": str(d),
        "data_ada": ada,
        "berkas": berkas,
        "byte": byte,
        "ukuran": ukuran_terbaca(byte),
        "versi": installed_version(),
    }


def _tutup_browser() -> None:
    """Bunuh Chrome connector yang mengunci folder profil (best-effort).

    Tanpa ini, rmtree pada ~/.bagasai berhenti di tengah dengan sisa berkas
    profil yang terkunci — folder "terhapus" tapi sebenarnya masih ada."""
    try:
        from .connectors import browser
    except Exception:  # noqa: BLE001 - Playwright tak terpasang: tak ada yang perlu ditutup
        return
    for fn in ("reset_hub", "_kill_profile_browsers"):
        try:
            getattr(browser, fn)()
        except Exception:  # noqa: BLE001 - pembersihan tak boleh menggagalkan pencopotan
            pass


def hapus_data(percobaan: int = 5) -> tuple[bool, str]:
    """Hapus folder data. Return (berhasil, catatan).

    Diulang beberapa kali: sesudah Chrome dibunuh, Windows kadang butuh
    sedetik-dua detik untuk benar-benar melepas kunci berkasnya."""
    d = data_dir()
    if not d.exists():
        return True, "folder data memang sudah tidak ada"
    _tutup_browser()
    for _ in range(percobaan):
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return True, ""
        time.sleep(1.0)
    sisa, _byte = _ukuran(d)
    return False, (f"{sisa} berkas masih terkunci proses lain — akan dicoba "
                   "lagi otomatis sesudah bagas-ai ini ditutup")


def _daftar_exe() -> list[str]:
    """Console-script bagas-ai (.exe di Windows) yang mengunci pencopotan.

    Memakai helper updater agar sumbernya SATU: skema pemasangan biasa maupun
    --user (Python dari Microsoft Store memakai yang kedua)."""
    try:
        from . import updater

        return [str(d / f"{n}{'.exe' if os.name == 'nt' else ''}")
                for d in updater._script_dirs()
                for n in updater._SCRIPT_NAMES]
    except Exception:  # noqa: BLE001
        return []


def jadwalkan_pencopotan(hapus_juga_data: bool) -> tuple[bool, str]:
    """Lepas proses pendamping: tunggu proses ini keluar -> pip uninstall.

    Return (berhasil dilepas, path log). Pola & alasannya sama persis dengan
    updater._schedule_post_exit_install; bedanya skrip + log ditaruh di TEMP,
    karena ~/.bagasai justru sedang dilenyapkan."""
    temp = Path(tempfile.gettempdir())
    log = temp / "bagasai_uninstall.log"
    skrip = temp / "bagasai_uninstall.py"
    argv = [sys.executable, "-m", "pip", "uninstall", "-y", PACKAGE]
    # Kosong = pendamping tak menyentuh data sama sekali (mode --keep-data).
    data_untuk_pendamping = str(data_dir()) if hapus_juga_data else ""
    kode = (
        "import os, shutil, subprocess, sys, time\n"
        f"induk = {os.getpid()}\n"
        f"argv = {argv!r}\n"
        f"log = {str(log)!r}\n"
        f"exes = {_daftar_exe()!r}\n"
        f"data = {data_untuk_pendamping!r}\n"
        "catatan = []\n"
        # 1) Tunggu proses induk (bagas-ai yang menjadwalkan) benar-benar keluar.
        "batas = time.time() + 900\n"
        "while time.time() < batas:\n"
        "    try:\n"
        "        os.kill(induk, 0)\n"
        "    except OSError:\n"
        "        break\n"
        "    time.sleep(1.0)\n"
        "else:\n"
        "    sys.exit(0)\n"
        # 2) Menunggu satu PID tak cukup: pengguna bisa membuka bagas-ai lagi.
        #    Ukuran langsung "tak ada yang memakai" = exe-nya bisa dibuka tulis.
        "def bebas():\n"
        "    for p in exes:\n"
        "        if not os.path.exists(p):\n"
        "            continue\n"
        "        try:\n"
        "            open(p, 'ab').close()\n"
        "        except OSError:\n"
        "            return False\n"
        "    return True\n"
        "tunggu = time.time() + 900\n"
        "while time.time() < tunggu and not bebas():\n"
        "    time.sleep(2.0)\n"
        "hasil = 'GAGAL'\n"
        "for ke in range(6):\n"
        "    if not bebas():\n"
        "        catatan.append('percobaan %d: exe masih terkunci' % (ke + 1))\n"
        "        time.sleep(15.0)\n"
        "        continue\n"
        "    try:\n"
        "        r = subprocess.run(argv, capture_output=True, text=True,\n"
        "                           timeout=900)\n"
        "        keluaran = (r.stdout or '') + (r.stderr or '')\n"
        "        if r.returncode == 0:\n"
        "            hasil = 'SUKSES'\n"
        "            catatan.append(keluaran[-2000:])\n"
        "            break\n"
        "        catatan.append('percobaan %d gagal:' % (ke + 1))\n"
        "        catatan.append(keluaran[-2000:])\n"
        "    except Exception as exc:\n"
        "        catatan.append('percobaan %d error: %r' % (ke + 1, exc))\n"
        "    time.sleep(15.0)\n"
        # 3) Sapu bersih data SESUDAH pip selesai. Penghapusan di proses induk
        #    bisa menyisakan berkas yang saat itu masih terkunci; di sini tak
        #    ada lagi yang memegangnya.
        "if data and os.path.isdir(data):\n"
        "    for _ in range(10):\n"
        "        shutil.rmtree(data, ignore_errors=True)\n"
        "        if not os.path.isdir(data):\n"
        "            break\n"
        "        time.sleep(2.0)\n"
        "    catatan.append('data terhapus: %s' % (not os.path.isdir(data)))\n"
        "try:\n"
        "    open(log, 'w', encoding='utf-8').write(\n"
        "        time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + hasil + chr(10)\n"
        "        + chr(10).join(catatan))\n"
        "except OSError:\n"
        "    pass\n"
    )
    try:
        skrip.write_text(kode, encoding="utf-8")
    except OSError as exc:
        return False, f"tak bisa menulis skrip pendamping: {exc}"

    bendera = 0
    if os.name == "nt":
        bendera = (getattr(subprocess, "DETACHED_PROCESS", 0)
                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(
            [sys.executable, str(skrip)],
            cwd=str(temp), creationflags=bendera,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        return True, str(log)
    except Exception as exc:  # noqa: BLE001
        return False, f"gagal melepas proses pendamping: {exc}"

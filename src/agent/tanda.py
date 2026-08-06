"""Penanda TUGAS SELESAI: getaran + dering.

Kenapa ada: giliran yang panjang membuat orang berpindah jendela. Saat AI
akhirnya selesai, satu-satunya tanda selama ini adalah teks di terminal yang
sedang tak dilihat siapa pun — jadi pekerjaan yang sudah rampung bisa menunggu
bermenit-menit sebelum ketahuan. Getaran & dering menembus jendela lain.

DIBUNYIKAN TEPAT SEKALI, DI UJUNG GILIRAN: sesudah jawaban akhir tampil DAN
sesudah suaranya selesai dibacakan (bila /mic hidup). Bukan di tiap langkah —
tanda yang berbunyi terus-menerus berhenti berarti apa-apa. Tidak dibunyikan
bila gilirannya dibatalkan: merayakan yang tak selesai itu keliru.

SOAL "GETAR" — BATAS YANG NYATA
-------------------------------
Yang diminta pengguna: LAPTOPNYA yang bergetar, bukan bunyi. Itu tak bisa
dipenuhi, dan sebabnya perangkat keras, bukan pilihan rancangan: laptop tidak
punya motor getar sama sekali. Tak ada API sistem operasi untuknya karena tak
ada yang bisa digerakkan. (Yang punya motor getar dan menempel ke PC cuma
gamepad — dan itu pun harus ada dulu.)

Yang PALING MENDEKATI dan benar-benar terasa: dua dengung pendek berfrekuensi
rendah (110 Hz) lewat pengeras suara. Di kebanyakan laptop, nada serendah itu
menggetarkan bodinya — bukan tipuan, memang beginilah "getaran" satu-satunya
yang tersedia. Mesinnya sama dengan penanda "sampai di kesimpulan"
(suara.getar), jadi rasanya seragam.

Kedipan jendela taskbar sempat dipakai di sini lalu DIBUANG atas permintaan
pengguna: ia menuntut mata, sedangkan yang dicari justru tanda yang tak perlu
dilihat.

BUNYINYA BISA DIGANTI SENDIRI
-----------------------------
Selera bunyi itu urusan pribadi, dan "suaranya jelek" keluhan yang sah.
Jawabannya bukan mengganti selera bawaan berkali-kali, melainkan menyediakan
tempat menaruh bunyi pilihan sendiri:

    ~/.bagasai/suara/selesai-punyaku.wav     <- WAV apa pun; tak pernah ditimpa
    BAGASAI_SUARA_SELESAI=/path/ke/berkas.wav

Bawaannya diambil dari repo bagas-ai saat pemasangan/pembaruan (unduh()) —
itulah berkas yang didengar semua pemasangan baru.

Kalau unduhannya gagal (luring, repo tak terjangkau), yang dibunyikan BUKAN
berkas itu melainkan nada lonceng yang dibangkitkan di laptop. Bunyinya jelas
berbeda, dan itu memang disengaja: penanda yang tetap ada lebih berguna
daripada penanda yang bisu hanya karena satu berkas gagal diambil. Begitu
`bagas-ai update` berjalan dengan koneksi, berkas aslinya menggantikannya.
"""
from __future__ import annotations

import logging
import math
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

# Tempat berkas bunyi disimpan (sekali unduh, dipakai semua sesi).
DIR = config.CONFIG_HOME / "suara"
BERKAS = DIR / "selesai.wav"
# BERKAS MILIK PENGGUNA — kalau ada, ia yang dipakai dan pembaruan TAK PERNAH
# menyentuhnya. Selera bunyi itu urusan pribadi ("suaranya jelek banget" adalah
# keluhan yang sah), dan satu-satunya jawaban yang benar untuk itu bukan
# mengganti selera bawaan berulang kali, melainkan memberi tempat menaruh
# bunyi pilihan sendiri. Taruh WAV apa pun di sini:
#     ~/.bagasai/suara/selesai-punyaku.wav
PUNYAKU = DIR / "selesai-punyaku.wav"


def berkas_bunyi() -> Path:
    """Berkas yang akan dibunyikan: milik pengguna dulu, baru bawaan."""
    ganti = str(getattr(config, "SUARA_SELESAI", "") or "").strip()
    if ganti:
        p = Path(ganti).expanduser()
        if p.is_file():
            return p
    return PUNYAKU if PUNYAKU.is_file() else BERKAS

# Diambil dari repo bagas-ai sendiri — bukan dari layanan pihak ketiga yang
# bisa hilang atau berganti lisensi. Mengikuti REPO/BRANCH yang sama dengan
# pembaruan, jadi fork pun mengambil berkasnya sendiri.
_MENTAH = "https://raw.githubusercontent.com/{repo}/{cabang}/assets/selesai.wav"

_LAJU = 44100
# D6 -> A6 (kuint naik), serangan lembut & ekor panjang — sepadan dengan
# berkas bawaan di assets/. Bentuk pertamanya (tiga nada, serangan
# mendadak) terdengar seperti bip alarm murahan.
_NADA = ((1174.66, 0.00, 1.10), (1760.00, 0.11, 1.30))


def _url() -> str:
    repo = str(getattr(config, "REPO_URL", "")).rstrip("/")
    nama = repo.split("github.com/")[-1].removesuffix(".git")
    return _MENTAH.format(repo=nama or "ahmadadptr001/bagas-ai",
                          cabang=getattr(config, "REPO_BRANCH", "master"))


def _bangkitkan(path: Path) -> bool:
    """Tulis nada penanda dari nol (cadangan bila unduhan gagal)."""
    try:
        n = int(_LAJU * 1.55)
        buf = [0.0] * n
        for freq, mulai, lama in _NADA:
            i0 = int(mulai * _LAJU)
            for i in range(int(lama * _LAJU)):
                if i0 + i >= n:
                    break
                t = i / _LAJU
                # Serangan 25 ms (bukan 8) + peluruhan pelan. Yang menentukan
                # "enak" atau "murah" bukan nadanya melainkan SERANGANNYA:
                # 25 ms terdengar seperti lonceng kaca, 8 ms seperti ketukan
                # plastik. Harmoniknya pun ditipiskan.
                env = min(1.0, t / 0.025) * math.exp(-t * 2.6)
                buf[i0 + i] += 0.30 * env * (
                    math.sin(2 * math.pi * freq * t)
                    + 0.12 * math.sin(4 * math.pi * freq * t)
                    + 0.05 * math.sin(6 * math.pi * freq * t))
        puncak = max((abs(x) for x in buf), default=1.0) or 1.0
        data = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, x / puncak * 0.72)) * 32767))
            for x in buf)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_LAJU)
            w.writeframes(data)
        return True
    except Exception:  # noqa: BLE001
        log.debug("gagal membangkitkan nada penanda", exc_info=True)
        return False


def unduh(paksa: bool = False) -> str:
    """Pastikan berkas bunyinya ada. Return keterangan singkat untuk ditampilkan.

    Dipanggil saat pemasangan & tiap pembaruan. Aman dipanggil berulang: tanpa
    `paksa`, berkas yang sudah ada dibiarkan."""
    # Berkas pilihan pengguna TAK PERNAH ditimpa, bahkan oleh `paksa`. Yang
    # diperbarui cuma bunyi bawaan.
    if PUNYAKU.is_file():
        return ""
    if BERKAS.is_file() and BERKAS.stat().st_size > 1024 and not paksa:
        return ""
    DIR.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request
        with urllib.request.urlopen(_url(), timeout=20) as r:
            isi = r.read()
        if len(isi) > 1024 and isi[:4] == b"RIFF":
            BERKAS.write_bytes(isi)
            return f"bunyi penanda selesai diunduh ({len(isi) // 1024} KB)"
        raise ValueError("isi berkas bukan WAV")
    except Exception as exc:  # noqa: BLE001 - luring / repo tak terjangkau
        log.debug("unduh bunyi penanda gagal: %s", exc)
    # Cadangan: nada lonceng yang dibangkitkan sendiri. Bunyinya JELAS BERBEDA
    # dari berkas bawaan, dan pengguna diberi tahu — penanda yang tetap ada
    # lebih berguna daripada yang bisu, tapi menyamarkan bedanya cuma membuat
    # orang mengira bunyinya salah pasang.
    if _bangkitkan(BERKAS):
        return ("bunyi bawaan tak bisa diunduh — sementara memakai nada "
                "buatan sendiri (jalankan `bagas-ai update` saat daring)")
    return ""


def _bunyikan() -> None:
    berkas = berkas_bunyi()
    if not berkas.is_file():
        unduh()
        berkas = berkas_bunyi()
    if not berkas.is_file():
        return
    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(str(berkas), winsound.SND_FILENAME)
            return
        except Exception:  # noqa: BLE001
            log.debug("winsound gagal", exc_info=True)
    try:
        import numpy as np
        import sounddevice as sd
        with wave.open(str(berkas), "rb") as w:
            laju, kanal = w.getframerate(), w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="int16")
        # Bunyi pilihan pengguna bisa STEREO (berkas bawaannya memang stereo).
        # Tanpa dibentuk ulang per-kanal, sampelnya terbaca berurutan sebagai
        # satu kanal — hasilnya berbunyi dua kali lebih cepat dan sember.
        if kanal > 1:
            data = data.reshape(-1, kanal)
        sd.play(data.astype("float32") / 32768.0, laju, blocking=True)
        return
    except Exception:  # noqa: BLE001
        log.debug("pemutaran lewat sounddevice gagal", exc_info=True)
    # Jalan terakhir: pemutar bawaan sistem.
    try:
        if sys.platform == "darwin":
            subprocess.run(["afplay", str(berkas)], timeout=10)
        else:
            subprocess.run(["aplay", "-q", str(berkas)], timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _getarkan() -> None:
    """GETARAN, bukan kedipan jendela.

    Kedipan taskbar sempat dipakai di sini dan DIGANTI atas permintaan
    pengguna: yang dicari tanda yang TERASA tanpa melihat layar sama sekali,
    sedangkan kedipan justru menuntut mata. Laptop tak punya motor getar, jadi
    yang paling mendekati rasanya adalah dua dengung pendek berfrekuensi rendah
    — mesin yang sama dengan penanda "sampai di kesimpulan" (suara._GETAR),
    supaya rasanya seragam di seluruh bagas-ai."""
    from . import suara
    suara.getar(latar=False)


def selesai(latar: bool = True, tunggu: Any = None) -> None:
    """Bunyikan penanda "tugas selesai" + kedipkan jendelanya.

    `tunggu` = fungsi yang menahan sampai hasil akhirnya selesai DIBACAKAN
    (lihat suara.tunggu_diam). Penanda yang berbunyi menimpa kalimat terakhir
    justru merusak kabar yang sedang disampaikan — jadi ia mengantre di
    belakangnya, bukan berebut.

    `latar=True` (bawaan): dijalankan di thread sendiri supaya penantian +
    bunyinya tak menahan terminal kembali ke kotak ketikan."""
    def _jalan() -> None:
        if tunggu is not None:
            try:
                tunggu()
            except Exception:  # noqa: BLE001 - penanda tak boleh ikut gagal
                log.debug("penantian sebelum penanda gagal", exc_info=True)
        _getarkan()
        _bunyikan()

    if not latar:
        _jalan()
        return
    threading.Thread(target=_jalan, daemon=True, name="bagasai-tanda").start()

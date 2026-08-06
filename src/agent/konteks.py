"""Riwayat & konteks yang DITITIPKAN sebagai berkas, bukan diketik.

Pesan pembuka bagas-ai dulu memuat semuanya sekaligus — aturan protokol tool,
peta proyek, memori, riwayat — dan di proyek ini saja itu ±40 rb karakter yang
harus DIKETIK satu per satu ke komposer situs. Tiga akibatnya sudah terlihat:
situs MEMOTONG pesan yang kepanjangan (dan yang terpotong justru aturan
mainnya), mengetiknya makan waktu sendiri tiap sesi, dan tiap kali pekerjaan
pindah chat tembok teks itu diketik LAGI.

TIGA hal di modul ini ditentukan oleh percobaan langsung di chat.z.ai, bukan
oleh selera — dan ketiganya gagal dengan cara yang DIAM:

1. BERKASNYA .txt, ISINYA JSON. Berkas berekstensi .json ditolak situs tanpa
   sepatah kata: kartu unggahannya tak pernah muncul sampai batas 90 detik.
   Isi yang sama persis dengan nama .txt (atau .md) terbaca sempurna. Isinya
   tetap JSON supaya terstruktur & bisa dibaca balik oleh bagas-ai (baca()).

2. DIPECAH JADI BEBERAPA BAGIAN. Satu berkas 47 KB terbaca; 63 KB dan 126 KB
   TIDAK — model menjawab "berkas tidak bisa saya buka" walau unggahannya
   sukses. Tapi DUA berkas @39 KB dalam satu pesan terbaca dua-duanya. Jadi
   batasnya per-berkas, dan jalan keluarnya memecah, bukan mengecilkan.

3. KODE PERIKSA TAK BOLEH ADA DI NAMA BERKAS. Kode itu bukti bahwa ISI berkas
   terbaca. Waktu ia ikut tertulis di nama berkas, balasan "berkas <nama> tidak
   bisa saya buka" justru LULUS pemeriksaan — mengutip nama berkas, bukan
   isinya.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Berapa KELOMPOK berkas terakhir yang disimpan di ~/.bagasai/konteks. Bukan
# sampah: berkas inilah wujud persis apa yang dibaca model, jadi saat sesuatu
# berjalan aneh ia satu-satunya bukti.
SIMPAN = 8

AWALAN = "memory"
# Ekstensi berkasnya, bukan format isinya (lihat catatan 1 di atas).
EKSTENSI = ".txt"
# "memory-20260806-160000-ab12-b2dari3.txt" -> kelompok "memory-20260806-160000-ab12"
_BAGIAN_RE = re.compile(r"^(?P<grup>.+)-b(?P<no>\d+)dari(?P<total>\d+)$")

# Kunci payload yang memuat riwayat percakapan verbatim. Bagian pertama berisi
# KONTEKS saja; riwayatnya tinggal di bagian-bagian berikutnya.
_KUNCI_RIWAYAT = "percakapan_terakhir_apa_adanya"


def kode_periksa() -> str:
    """Kode acak pendek yang WAJIB dikutip balik oleh model.

    Ini satu-satunya bukti bahwa berkasnya benar-benar dibaca. Situs bisa
    menerima unggahan lalu diam-diam tak mengurainya, dan tak ada galat apa pun
    yang muncul dari kegagalan seperti itu — tanpa kode ini bagas-ai bisa
    bekerja sepanjang sesi dengan model yang belum pernah melihat satu baris
    pun riwayatnya."""
    return secrets.token_hex(3)


def _teks(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def bagi(payload: dict[str, Any], maks: int = 0,
         maks_bagian: int = 0) -> list[dict[str, Any]]:
    """Pecah payload jadi bagian-bagian yang masing-masing muat dibaca situs.

    Bagian 1 = KONTEKS saja (lingkungan, peta proyek, memori). Bagian 2 dst =
    potongan riwayat verbatim, urut lama ke baru. Pemisahan itu yang membuat
    pemangkasan di bawah sederhana: kalau bagiannya kebanyakan, yang dibuang
    potongan riwayat PALING LAMA — konteksnya tak pernah ikut terbuang.

    Tiap bagian dapat kode periksanya sendiri, jadi model yang cuma membaca
    bagian pertama tak bisa lolos."""
    maks = maks or int(config.KONTEKS_MAKS_BYTES)
    maks_bagian = maks_bagian or int(config.KONTEKS_MAKS_BAGIAN)

    riwayat = payload.get(_KUNCI_RIWAYAT) or {}
    giliran = list(riwayat.get("giliran") or [])
    inti = {k: v for k, v in payload.items() if k != _KUNCI_RIWAYAT}

    potongan: list[list[dict]] = []
    kini: list[dict] = []
    besar = 400                       # ancar-ancar untuk bingkai bagiannya
    for baris in giliran:
        b = len(_teks(baris)) + 2
        if kini and besar + b > maks:
            potongan.append(kini)
            kini, besar = [], 400
        kini.append(baris)
        besar += b
    if kini:
        potongan.append(kini)

    # Kebanyakan bagian? Yang dibuang yang PALING LAMA. Pekerjaan terakhir itu
    # yang menentukan langkah berikutnya; awal percakapan sudah diwakili
    # ringkasan_giliran di bagian pertama.
    terpotong = bool(riwayat.get("dipotong_dari_awal"))
    if len(potongan) > max(maks_bagian - 1, 1):
        potongan = potongan[-(maks_bagian - 1):]
        terpotong = True

    total = 1 + len(potongan)
    bagian = [dict(inti)]
    for isi in potongan:
        bagian.append({
            "berkas": inti.get("berkas", "memory-bagas-ai"),
            "lanjutan_dari_bagian_sebelumnya": True,
            _KUNCI_RIWAYAT: {
                "keterangan": riwayat.get("keterangan", ""),
                "dipotong_dari_awal": terpotong,
                "giliran": isi,
            },
        })
    for i, b in enumerate(bagian, start=1):
        b["bagian"] = f"{i} dari {total}"
        b["kode_periksa"] = kode_periksa()
        if total > 1:
            b["catatan_bagian"] = (
                f"Ingatan ini dipecah jadi {total} berkas karena satu berkas "
                "besar tak terbaca utuh oleh situs. Baca SEMUANYA, urut, "
                "sebelum membalas."
            )
    return bagian


def tulis(payload: dict[str, Any], awalan: str = AWALAN) -> list[Path]:
    """Tulis payload jadi satu/beberapa berkas siap unggah, urut bagian.

    Nama berkasnya ikut terbaca pengguna di layar situs, jadi dibuat
    menjelaskan diri sendiri: "memory-20260806-160000-ab12-b2dari3.txt".
    Penanda acaknya SENGAJA bukan kode_periksa (lihat catatan 3 di atas)."""
    config.KONTEKS_DIR.mkdir(parents=True, exist_ok=True)
    grup = f"{awalan}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    bagian = bagi(payload)
    total = len(bagian)
    keluar: list[Path] = []
    for i, isi in enumerate(bagian, start=1):
        path = config.KONTEKS_DIR / f"{grup}-b{i}dari{total}{EKSTENSI}"
        path.write_text(_teks(isi), encoding="utf-8")
        keluar.append(path)
    bersihkan()
    return keluar


def daftar(awalan: str = AWALAN) -> list[list[Path]]:
    """Ingatan tersimpan sebagai KELOMPOK berkas, terbaru dulu."""
    try:
        semua = [p for p in config.KONTEKS_DIR.glob(f"{awalan}-*")
                 if p.suffix in (EKSTENSI, ".json")]
    except OSError:
        return []
    grup: dict[str, list[Path]] = {}
    for p in sorted(semua):
        m = _BAGIAN_RE.match(p.stem)
        grup.setdefault(m.group("grup") if m else p.stem, []).append(p)
    return sorted(grup.values(),
                  key=lambda ps: max(x.stat().st_mtime for x in ps),
                  reverse=True)


def terbaru(awalan: str = AWALAN) -> list[Path]:
    """Kelompok berkas ingatan terakhir ([] bila belum ada)."""
    semua = daftar(awalan)
    return semua[0] if semua else []


def sekelompok(path: Path | str) -> list[Path]:
    """Semua bagian yang sekelompok dengan berkas ini (untuk /send-compact
    <path>: pengguna menyebut satu berkas, yang dikirim seluruh bagiannya)."""
    p = Path(path)
    m = _BAGIAN_RE.match(p.stem)
    if not m:
        return [p]
    for grup in daftar():
        if p in grup:
            return grup
    return [p]


def baca(path: Path | str) -> dict[str, Any]:
    """Isi satu bagian (dict kosong bila tak terbaca/rusak)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def kode(paths: list[Path]) -> list[str]:
    """Kode periksa tiap bagian, urut. Model harus mengutip SEMUANYA."""
    keluar = []
    for p in paths:
        k = str(baca(p).get("kode_periksa") or "")
        if k:
            keluar.append(k)
    return keluar


def bersihkan(simpan: int = SIMPAN) -> int:
    """Buang kelompok lama, sisakan `simpan` yang terbaru."""
    dibuang = 0
    for grup in daftar()[max(simpan, 0):]:
        for p in grup:
            try:
                p.unlink()
                dibuang += 1
            except OSError:      # sedang dipakai / hak akses — tak fatal
                log.debug("gagal menghapus berkas memory lama: %s", p)
    return dibuang


def ukuran(paths: Any) -> int:
    """Besar total berkas dalam byte (0 bila tak terbaca).

    Dipakai penghitung panjang percakapan: isi berkas ikut masuk ke konteks
    model, jadi mengabaikannya membuat simpanan otomatis datang terlambat."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    total = 0
    for p in paths or []:
        try:
            total += Path(p).stat().st_size
        except OSError:
            pass
    return total

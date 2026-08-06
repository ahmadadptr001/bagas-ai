"""Ingatan & konteks yang DITITIPKAN sebagai berkas, bukan diketik.

Pesan pembuka bagas-ai dulu memuat semuanya sekaligus — aturan protokol tool,
peta proyek, memori, riwayat — dan di proyek ini saja itu ±40 rb karakter yang
harus DIKETIK satu per satu ke komposer situs. Situs memotong pesan sepanjang
itu, mengetiknya makan waktu, dan tiap pindah chat ia diketik ulang.

DUA JENIS BERKAS, DAN INI BUKAN RINCIAN SEPELE:

  memory-*   ingatan percakapan milik PENGGUNA. Lahir dari /compact (atau
             simpanan otomatis), berisi riwayat verbatim. Inilah yang dikirim
             /send-compact.
  konteks-*  berkas pembuka yang dibuat bagas-ai SENDIRI tiap memulai chat di
             situs: peta proyek + memori, tanpa riwayat. Sekali pakai.

Keduanya dulu satu kolam dengan nama yang sama, dan /send-compact memilih "yang
paling baru". Akibatnya persis seperti yang dilaporkan: sesudah /compact,
giliran berikutnya menulis berkas konteks yang jauh lebih baru dan JAUH lebih
kecil (2,4 KB, nol giliran riwayat) — lalu itulah yang terkirim, sementara
ingatan 43 KB yang baru saja dipadatkan tak pernah berangkat. Sekarang
/send-compact hanya melihat memory-*, dan berkas yang barusan ditulis /compact
diingat path-nya apa adanya.

DIPETAKAN PER SESI. Tiap sesi terminal punya foldernya sendiri
(~/.bagasai/konteks/sesi-<id>/), jadi "ingatan sesi yang mana" bisa dijawab
dengan melihat foldernya — bukan dengan menebak dari cap waktu di satu
tumpukan.

BERKASNYA .txt, ISINYA JSON. Diuji di chat.z.ai: berkas berekstensi .json
DITOLAK diam-diam (kartu unggahannya tak pernah muncul sampai batas 90 detik),
sedangkan .txt dengan isi persis sama terbaca. Isinya tetap JSON supaya
terstruktur & bisa dibaca balik oleh bagas-ai sendiri (baca()).

KODE PERIKSA DI UJUNG BERKAS. Kode itu bukti bahwa isi berkas terbaca. Ia tak
boleh ada di NAMA berkas (balasan "berkas <nama> tak bisa dibuka" pernah lolos
justru karena mengutip nama), dan tak boleh di kepala berkas: situs terbukti
memotong berkas besar dari belakang, jadi kode di awal tetap terkutip walau
sisanya tak pernah terbaca.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Berapa KELOMPOK berkas terakhir yang disimpan per sesi, dan berapa FOLDER
# sesi yang ditahan. Bukan sampah: berkas inilah wujud persis apa yang dibaca
# model, jadi saat sesuatu berjalan aneh ia satu-satunya bukti.
SIMPAN = 6
SIMPAN_SESI = 12

# Ingatan pengguna (dari /compact) vs berkas pembuka buatan bagas-ai sendiri.
# Dipisah namanya supaya /send-compact tak pernah lagi salah ambil.
AWALAN = "memory"
AWALAN_KONTEKS = "konteks"
# Ekstensi berkasnya, bukan format isinya (lihat catatan di atas).
EKSTENSI = ".txt"

# "memory-20260806-160000-ab12-b2dari3" -> kelompok "memory-20260806-160000-ab12"
# Berkas yang TIDAK dipecah tak memakai akhiran ini sama sekali — satu berkas
# ya satu berkas, tanpa embel-embel "b1dari1" yang cuma bikin bingung.
_BAGIAN_RE = re.compile(r"^(?P<grup>.+)-b(?P<no>\d+)dari(?P<total>\d+)$")

# Kunci payload yang memuat riwayat percakapan verbatim. Bagian pertama berisi
# KONTEKS saja; riwayatnya tinggal di bagian-bagian berikutnya.
_KUNCI_RIWAYAT = "percakapan_terakhir_apa_adanya"


def kode_periksa() -> str:
    """Kode acak pendek yang WAJIB dikutip balik oleh model.

    Ini satu-satunya bukti bahwa berkasnya benar-benar dibaca sampai habis.
    Situs bisa menerima unggahan lalu diam-diam tak mengurainya (atau
    memotongnya di tengah), dan tak ada galat apa pun yang muncul dari
    kegagalan seperti itu."""
    return secrets.token_hex(3)


def _teks(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


# --------------------------------------------------------------- folder sesi
def nama_sesi(sesi: str = "") -> str:
    """Nama folder untuk sebuah sesi terminal ("" -> tanpa sesi)."""
    bersih = re.sub(r"[^A-Za-z0-9_.-]", "", str(sesi or ""))[:40]
    return f"sesi-{bersih}" if bersih else "tanpa-sesi"


def dir_sesi(sesi: str = "", buat: bool = False) -> Path:
    """Folder ingatan milik satu sesi terminal.

    Dipisah per sesi supaya pertanyaan "ini ingatan percakapan yang mana?"
    dijawab oleh struktur folder, bukan oleh tebakan dari cap waktu."""
    d = config.KONTEKS_DIR / nama_sesi(sesi)
    if buat:
        d.mkdir(parents=True, exist_ok=True)
    return d


def bagi(payload: dict[str, Any], maks: int = 0,
         maks_bagian: int = 0) -> list[dict[str, Any]]:
    """Pecah payload jadi bagian-bagian — HANYA bila melewati batas ukuran.

    Selama masih muat dalam satu berkas, ia TETAP satu berkas: pemecahan bukan
    tujuan, ia cuma jalan keluar saat berkasnya kebesaran.

    Kalau memang harus dipecah: bagian 1 = KONTEKS saja (lingkungan, peta
    proyek, memori), bagian 2 dst = potongan riwayat verbatim urut lama ke
    baru. Pemisahan itu yang membuat pemangkasan sederhana — kalau bagiannya
    kebanyakan, yang dibuang potongan riwayat PALING LAMA, konteksnya tak
    pernah ikut terbuang.

    Tiap bagian dapat kode periksanya sendiri di UJUNG berkas, jadi model yang
    berhenti di bagian pertama tak bisa lolos."""
    maks = maks or int(config.KONTEKS_MAKS_BYTES)
    maks_bagian = maks_bagian or int(config.KONTEKS_MAKS_BAGIAN)

    # Muat dalam satu berkas? Selesai — jangan dipecah tanpa sebab.
    if len(_teks(payload).encode("utf-8")) <= maks:
        return [_lengkapi(dict(payload), 1, 1)]

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
    return [_lengkapi(b, i, total) for i, b in enumerate(bagian, start=1)]


def _lengkapi(b: dict[str, Any], i: int, total: int) -> dict[str, Any]:
    """Tambahkan penanda bagian + kode periksa DI UJUNG berkas."""
    if total > 1:
        b["bagian"] = f"{i} dari {total}"
        b["catatan_bagian"] = (
            f"Ingatan ini dipecah jadi {total} berkas karena satu berkas "
            "sebesar itu tidak terbaca utuh oleh situs — isinya dipotong "
            f"diam-diam. Baca SEMUA berkas, urut dari bagian 1 sampai bagian "
            f"{total}, masing-masing dari baris pertama sampai baris terakhir. "
            "Jangan berhenti di berkas pertama dan jangan melompati bagian "
            "tengah."
        )
    # kode_periksa PALING AKHIR — lihat catatan di kepala modul.
    b["kode_periksa"] = kode_periksa()
    return b


def tulis(payload: dict[str, Any], awalan: str = AWALAN, sesi: str = "",
          on_progress: Any = None) -> list[Path]:
    """Tulis payload ke folder sesinya; kembalikan daftar berkasnya, urut.

    Satu berkas selama muat; dipecah hanya bila melewati batas. Nama berkasnya
    ikut terbaca pengguna di layar situs, jadi dibuat menjelaskan diri sendiri.
    Penanda acaknya SENGAJA bukan kode_periksa (lihat catatan kepala modul)."""
    folder = dir_sesi(sesi, buat=True)
    grup = f"{awalan}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    bagian = bagi(payload)
    total = len(bagian)
    keluar: list[Path] = []
    for i, isi in enumerate(bagian, start=1):
        if on_progress:
            on_progress((i - 1) / total,
                        "menulis berkas ingatan" if total == 1
                        else f"menulis bagian {i}/{total}")
        akhiran = "" if total == 1 else f"-b{i}dari{total}"
        path = folder / f"{grup}{akhiran}{EKSTENSI}"
        path.write_text(_teks(isi), encoding="utf-8")
        keluar.append(path)
    if on_progress:
        on_progress(1.0, "merapikan simpanan lama")
    bersihkan()
    return keluar


def _grup_dari(path: Path) -> str:
    m = _BAGIAN_RE.match(path.stem)
    return m.group("grup") if m else path.stem


def daftar(awalan: str = AWALAN, sesi: str | None = None) -> list[list[Path]]:
    """Ingatan tersimpan sebagai KELOMPOK berkas, TERBARU dulu.

    `sesi=None` -> seluruh sesi (dipakai /send-compact sesudah /new: ingatan
    yang mau dibawa justru milik sesi SEBELUMNYA). `sesi="<id>"` -> satu sesi.
    """
    folder = [dir_sesi(sesi)] if sesi is not None else _semua_folder()
    grup: dict[str, list[Path]] = {}
    for d in folder:
        try:
            berkas = [p for p in d.glob(f"{awalan}-*")
                      if p.suffix in (EKSTENSI, ".json")]
        except OSError:
            continue
        for p in sorted(berkas):
            grup.setdefault(f"{d.name}/{_grup_dari(p)}", []).append(p)
    return sorted(grup.values(),
                  key=lambda ps: max(x.stat().st_mtime for x in ps),
                  reverse=True)


def _semua_folder() -> list[Path]:
    """Folder sesi + folder induk (berkas peninggalan versi tanpa folder sesi)."""
    try:
        anak = [d for d in config.KONTEKS_DIR.iterdir() if d.is_dir()]
    except OSError:
        anak = []
    return [config.KONTEKS_DIR] + sorted(anak)


def terbaru(awalan: str = AWALAN, sesi: str | None = None) -> list[Path]:
    """Kelompok berkas terakhir ([] bila belum ada)."""
    semua = daftar(awalan, sesi)
    return semua[0] if semua else []


def sekelompok(path: Path | str) -> list[Path]:
    """Semua bagian yang sekelompok dengan berkas ini.

    Dipakai `/send-compact <path>`: pengguna menyebut satu berkas, yang dikirim
    seluruh bagiannya — bukan sepotong."""
    p = Path(path)
    grup = _grup_dari(p)
    try:
        sekitar = sorted(x for x in p.parent.glob(f"{grup}*")
                         if x.suffix in (EKSTENSI, ".json"))
    except OSError:
        sekitar = []
    return sekitar or [p]


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


def jumlah_giliran(paths: list[Path]) -> int:
    """Berapa giliran percakapan yang tersimpan di kelompok berkas ini.

    Dipakai untuk melaporkan isinya apa adanya: berkas berisi NOL giliran itu
    konteks kosong, dan pengguna berhak tahu itu SEBELUM mengirimnya."""
    n = 0
    for p in paths:
        riw = baca(p).get(_KUNCI_RIWAYAT) or {}
        n += len(riw.get("giliran") or [])
    return n


def bersihkan(simpan: int = SIMPAN, simpan_sesi: int = SIMPAN_SESI) -> int:
    """Buang kelompok lama (per sesi) & folder sesi yang paling lama."""
    dibuang = 0
    for d in _semua_folder():
        for awalan in (AWALAN, AWALAN_KONTEKS):
            grup: dict[str, list[Path]] = {}
            try:
                berkas = [p for p in d.glob(f"{awalan}-*")
                          if p.suffix in (EKSTENSI, ".json")]
            except OSError:
                continue
            for p in sorted(berkas):
                grup.setdefault(_grup_dari(p), []).append(p)
            urut = sorted(grup.values(),
                          key=lambda ps: max(x.stat().st_mtime for x in ps),
                          reverse=True)
            for kelompok in urut[max(simpan, 0):]:
                for p in kelompok:
                    try:
                        p.unlink()
                        dibuang += 1
                    except OSError:      # sedang dipakai / hak akses
                        log.debug("gagal menghapus berkas ingatan: %s", p)
    # Folder sesi yang paling lama ikut dibuang seluruhnya, kalau tidak
    # foldernya menumpuk selamanya walau isinya sudah rapi.
    folder = [d for d in _semua_folder() if d != config.KONTEKS_DIR]
    if len(folder) > simpan_sesi:
        folder.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in folder[simpan_sesi:]:
            try:
                shutil.rmtree(d)
                dibuang += 1
            except OSError:
                log.debug("gagal menghapus folder sesi lama: %s", d)
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

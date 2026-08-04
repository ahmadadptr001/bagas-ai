"""Izin akses berkas DI LUAR folder kerja.

Aturan dasar bagas-ai: tool berkas hanya boleh menyentuh ROOT PROJECT (folder
terminal saat `bagas-ai` dipanggil) dan folder konteks yang ditambahkan sendiri
lewat `add-dir`. Sebelum ini, path di luar itu langsung DITOLAK — aman, tapi
sering menghalangi pekerjaan yang sah (menyalin aset dari folder Download,
membaca config di home, membandingkan dua proyek bersebelahan) tanpa jalan
keluar selain keluar dari sesi dan menjalankan `bagas-ai add-dir` dulu.

Sekarang penolakan itu diganti PERTANYAAN: pengguna yang memutuskan, per
folder, dan jawabannya diingat supaya tak ditanya berulang untuk folder yang
sama. Pertanyaannya lewat interaction.ask_choice sehingga muncul di antarmuka
yang benar — terminal bila giliran dijalankan dari terminal, Telegram bila
dari Telegram (lihat interaction.py).

Tiga hal yang sengaja dijaga di sini:

  1. TAK ADA izin diam-diam. Bila tak ada antarmuka yang bisa bertanya (mis.
     server API), jawabannya TOLAK — bukan "ya" karena tak ada yang menjawab.
  2. Penolakan juga DIINGAT. Tanpa itu, agent yang mencoba path yang sama
     berulang kali (hal yang sangat lazim saat ia mencari berkas) memunculkan
     pertanyaan bertubi-tubi yang tak bisa dihentikan pengguna.
  3. Izin diberikan per-FOLDER, bukan per-berkas. Berkas datang berkelompok
     (baca satu, tulis tetangganya), dan bertanya per berkas akan sama
     melelahkannya dengan menolak semuanya.

`--skip-permissions` mematikan seluruh mekanisme ini (lihat __main__.py):
semua path dianggap boleh. Itu memang membuang lapisan pengaman, jadi CLI
menampilkannya terang-terangan di layar saat aktif.
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import config, interaction, workspace

# Folder yang diizinkan SELAMA SESI ini (tidak ditulis ke disk).
_izin_sesi: set[str] = set()
# Folder yang sudah ditolak — jangan tanya lagi (lihat alasan 2 di docstring).
_tolak_sesi: set[str] = set()
_kunci = threading.Lock()

# Lewati seluruh pemeriksaan. Nilai awalnya dari .env
# (BAGASAI_SKIP_PERMISSIONS); flag `--skip-permissions` menyalakannya lewat
# set_skip() saat perintah dijalankan.
_lewati = bool(config.SKIP_PERMISSIONS)

# Label opsi — dipakai juga untuk MENCOCOKKAN jawaban, karena
# interaction.ask_choice mengembalikan teks labelnya.
_SEKALI = "Izinkan sekali ini saja"
_SESI = "Izinkan folder ini selama sesi"
_PERMANEN = "Izinkan permanen (jadikan folder konteks)"
_TOLAK = "Tolak"

# Kata kerja per tool supaya pertanyaannya menyebut apa yang HENDAK dilakukan,
# bukan sekadar "mengakses". Yang tak terdaftar memakai kata umum.
_KERJA = {
    "read_file": "membaca", "list_dir": "melihat isi", "search_text": "mencari di",
    "glob_files": "mencari berkas di", "write_file": "MENULIS",
    "append_file": "MENAMBAH isi", "edit_file": "MENGUBAH",
    "delete_file": "MENGHAPUS", "move_file": "MEMINDAHKAN",
    "copy_file": "menyalin ke", "make_dir": "MEMBUAT folder",
    "zip_create": "membuat arsip di", "zip_extract": "mengekstrak ke",
    "attach_file": "melampirkan", "analyze_image": "membaca gambar",
}


def set_skip(nilai: bool) -> None:
    """Nyalakan/matikan mode lewati-izin (dipanggil dari __main__)."""
    global _lewati
    _lewati = bool(nilai)


def skip_aktif() -> bool:
    return _lewati


def _folder(target: Path) -> Path:
    """Folder yang jadi satuan izin: dirinya bila folder, induknya bila berkas.

    Path yang BELUM ADA (mis. berkas yang hendak ditulis) dianggap berkas —
    induknyalah yang diizinkan, sehingga menulis beberapa berkas di folder yang
    sama cukup ditanya sekali."""
    try:
        if target.is_dir():
            return target
    except OSError:  # noqa: PERF203 - path aneh/tak terbaca
        pass
    return target.parent


def _sudah_diizinkan(folder: Path) -> bool:
    """True bila folder ini — atau salah satu INDUKNYA — sudah diizinkan.

    Perbandingan memakai Path, bukan awalan string: `C:\\data2` berawalan sama
    dengan `C:\\data` sebagai teks, padahal itu folder yang sama sekali lain."""
    with _kunci:
        daftar = list(_izin_sesi)
    for s in daftar:
        p = Path(s)
        if folder == p or p in folder.parents:
            return True
    return False


def _sudah_ditolak(folder: Path) -> bool:
    with _kunci:
        return str(folder) in _tolak_sesi


def dalam_wilayah(target: Path) -> bool:
    """True bila path ada di root project / folder konteks (tak perlu izin)."""
    for root in workspace.allowed_roots():
        try:
            r = root.resolve()
        except OSError:
            continue
        if target == r or r in target.parents:
            return True
    return False


def _pendek(p: Path, maks: int = 58) -> str:
    """Path yang dipendekkan di TENGAH — awal & nama berkasnya tetap terbaca."""
    s = str(p)
    if len(s) <= maks:
        return s
    ekor = Path(s).name
    kepala = s[: max(0, maks - len(ekor) - 4)]
    return f"{kepala}…\\{ekor}" if "\\" in s else f"{kepala}…/{ekor}"


def minta_izin(target: Path, tool: str = "") -> bool:
    """Tanyakan izin akses `target` di luar wilayah kerja. True = boleh.

    Dipanggil dari satu tempat saja: tools/files.py::_safe_path, gerbang yang
    dilewati SEMUA tool berkas (files.py, extras.py, media.py)."""
    if _lewati:
        return True
    folder = _folder(target)
    if _sudah_diizinkan(folder):
        return True
    if _sudah_ditolak(folder):
        return False

    kerja = _KERJA.get(tool or "", "mengakses")
    # Baris kedua dipisah newline, bukan spasi: antarmuka terminal membungkus
    # judul panjang per baris, jadi path-nya mendarat utuh di barisnya sendiri.
    tanya = f"Izinkan bagas-ai {kerja} di luar folder proyek?\n{_pendek(target)}"
    opsi = [_SEKALI, _SESI, _PERMANEN, _TOLAK]
    try:
        jawab = (interaction.ask_choice(tanya, opsi, False) or "").strip()
    except (KeyboardInterrupt, EOFError):
        jawab = _TOLAK

    if jawab.startswith(_SEKALI):
        return True
    if jawab.startswith(_SESI):
        with _kunci:
            _izin_sesi.add(str(folder))
        return True
    if jawab.startswith(_PERMANEN):
        try:
            workspace.add(str(folder))
        except ValueError:
            pass       # folder hilang di tengah jalan -> cukup izin sesi
        with _kunci:
            _izin_sesi.add(str(folder))
        return True

    # Termasuk jawaban "[tidak interaktif] …" dari interaction: tak ada yang
    # bisa ditanya berarti TIDAK, bukan ya.
    with _kunci:
        _tolak_sesi.add(str(folder))
    return False


def alasan_ditolak(path: str) -> str:
    """Pesan untuk AI ketika akses ditolak — sekaligus memberi jalan keluarnya."""
    if _lewati:
        return ""
    return (
        f"[DITOLAK] Akses ke '{path}' ada DI LUAR folder proyek dan pengguna "
        "tidak mengizinkannya. Jangan mencoba path itu lagi; kerjakan dengan "
        "berkas di dalam proyek, atau minta pengguna menjalankan "
        "`/add-dir <folder>` bila folder itu memang perlu."
    )


def ringkasan() -> dict:
    """Keadaan izin saat ini (untuk ditampilkan di antarmuka)."""
    with _kunci:
        return {
            "lewati": _lewati,
            "izin_sesi": sorted(_izin_sesi),
            "tolak_sesi": sorted(_tolak_sesi),
        }


def reset_sesi() -> None:
    """Lupakan semua keputusan sesi (dipakai pengujian & sesi baru)."""
    with _kunci:
        _izin_sesi.clear()
        _tolak_sesi.clear()

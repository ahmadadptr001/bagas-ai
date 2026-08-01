"""Checkpoint otomatis + tool undo_changes — jaring pengaman rollback.

Sebelum sebuah tool MENGUBAH/menghapus file, pre-image file itu disalin dulu ke
~/.bagasai/checkpoints/<proyek>/turn-<waktu>/ (sekali per file per giliran —
yang disimpan adalah keadaan SEBELUM giliran menyentuhnya). Tool undo_changes
memulihkan seluruh giliran terakhir: file yang diubah dikembalikan, file yang
dibuat dihapus. Panggil berulang untuk mundur giliran demi giliran.

Prinsip keselamatan:
  - snapshot() TIDAK PERNAH melempar — cadangan gagal tak boleh menggagalkan
    mutasi yang diminta. Kegagalannya tercatat di manifest giliran itu supaya
    undo bisa jujur bila ada file yang tak bisa dipulihkan.
  - Pemulihan menulis lewat file sementara + os.replace (atomik per file),
    jadi crash di tengah undo tak meninggalkan file setengah tertulis.
  - Riwayat dibatasi (_MAX_TURNS) & file raksasa dilewati (_MAX_FILE) agar
    disk tak membengkak diam-diam.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .. import config
from .base import tool

_MAX_FILE = 5 * 1024 * 1024   # per file; lebih besar dari ini dicatat "dilewati"
_MAX_TURNS = 20               # riwayat checkpoint per proyek

_lock = threading.Lock()
# Keadaan giliran BERJALAN. `dir` None = belum ada mutasi giliran ini (folder
# checkpoint dibuat malas, hanya bila ada yang perlu disimpan).
_state: dict = {"dir": None, "manifest": [], "seq": 0}


def _base_dir() -> Path:
    """Folder checkpoint proyek ini. Nama memuat hash path lengkap supaya dua
    proyek bernama sama (mis. dua folder `app`) tak saling menimpa."""
    root = str(config.PROJECT_ROOT.resolve())
    h = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()[:10]
    nama = config.PROJECT_ROOT.name or "root"
    return config.CONFIG_HOME / "checkpoints" / f"{nama}-{h}"


def begin_turn() -> None:
    """Tandai giliran baru: mutasi berikutnya membuka kelompok checkpoint baru.
    Dipanggil core di awal tiap Agent.run()."""
    with _lock:
        _state["dir"] = None
        _state["manifest"] = []
        _state["seq"] = 0


def _tulis_manifest(tdir: Path) -> None:
    data = json.dumps(_state["manifest"], ensure_ascii=False, indent=1)
    tmp = tdir / "manifest.json.tmp"
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, tdir / "manifest.json")


def _prune(base: Path, keep: int) -> None:
    """Buang checkpoint tertua bila melebihi jatah. Folder giliran BERJALAN
    tak pernah ikut terbuang karena selalu yang termuda."""
    dirs = sorted(d for d in base.iterdir()
                  if d.is_dir() and d.name.startswith("turn-"))
    for d in dirs[:-keep] if keep > 0 else dirs:
        shutil.rmtree(d, ignore_errors=True)


def snapshot(target: Path | str) -> None:
    """Simpan pre-image `target` (file, bukan folder) SEKALI per giliran.

    Dipanggil tool mutasi SEBELUM menyentuh file. Aman dipanggil untuk file
    yang belum ada (dicatat sebagai 'akan dibuat' -> undo menghapusnya).
    TIDAK PERNAH melempar."""
    try:
        t = Path(target)
        key = str(t.resolve())
        with _lock:
            if any(e["path"] == key for e in _state["manifest"]):
                return               # pre-image giliran ini sudah tersimpan
            tdir = _state["dir"]
            if tdir is None:
                base = _base_dir()
                base.mkdir(parents=True, exist_ok=True)
                # -1: sisakan satu slot untuk giliran yang akan dibuat ini.
                _prune(base, _MAX_TURNS - 1)
                tdir = base / f"turn-{int(time.time() * 1000):013d}"
                tdir.mkdir(parents=True, exist_ok=True)
                _state["dir"] = tdir
            entry: dict = {"path": key, "existed": t.is_file(), "backup": None}
            if entry["existed"]:
                if t.stat().st_size > _MAX_FILE:
                    entry["skipped"] = f"file > {_MAX_FILE // (1024 * 1024)} MB"
                else:
                    _state["seq"] += 1
                    nama = f"f{_state['seq']:04d}"
                    shutil.copy2(t, tdir / nama)
                    entry["backup"] = nama
            _state["manifest"].append(entry)
            _tulis_manifest(tdir)
    except Exception:  # noqa: BLE001 - cadangan tak boleh menggagalkan mutasi
        pass


def _pulihkan_atomik(sumber: Path, tujuan: Path) -> None:
    """Salin `sumber` ke `tujuan` lewat file sementara + os.replace (atomik)."""
    tujuan.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(tujuan.parent), prefix=".bagasai-undo-")
    os.close(fd)
    try:
        shutil.copy2(sumber, tmp)
        os.replace(tmp, tujuan)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


@tool
def undo_changes() -> str:
    """BATALKAN semua perubahan file dari giliran TERAKHIR yang mengubah file: file yang diubah/dihapus dikembalikan ke isi sebelumnya, file yang dibuat ikut dihapus. Panggil lagi untuk mundur satu giliran lebih jauh. Pakai saat perubahanmu ternyata salah arah/merusak dan lebih cepat mulai ulang daripada menambal.

    Cadangan diambil otomatis sebelum tiap write_file/edit_file/append_file/
    delete_file/move_file/copy_file/replace_in_files, dikelompokkan per giliran.
    """
    base = _base_dir()
    if not base.is_dir():
        return ("[undo] Belum ada checkpoint untuk proyek ini — belum ada "
                "giliran yang mengubah file sejak fitur ini aktif.")
    dirs = sorted((d for d in base.iterdir()
                   if d.is_dir() and (d / "manifest.json").is_file()),
                  key=lambda d: d.name)
    if not dirs:
        return ("[undo] Belum ada checkpoint untuk proyek ini — belum ada "
                "giliran yang mengubah file sejak fitur ini aktif.")
    tdir = dirs[-1]
    try:
        manifest = json.loads((tdir / "manifest.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"[GAGAL] manifest checkpoint rusak ({tdir.name}): {exc}"

    dipulihkan: list[str] = []
    dihapus: list[str] = []
    masalah: list[str] = []
    # Urutan DIBALIK: mutasi terakhir dibatalkan lebih dulu — penting saat satu
    # giliran menyentuh file yang sama lewat beberapa jalur (mis. move lalu edit).
    for e in reversed(manifest):
        p = Path(e["path"])
        try:
            if e.get("backup"):
                _pulihkan_atomik(tdir / e["backup"], p)
                dipulihkan.append(str(p))
            elif e.get("skipped"):
                masalah.append(f"{p} — tak dicadangkan ({e['skipped']}), "
                               "tak bisa dipulihkan")
            elif not e.get("existed"):
                if p.is_file():
                    p.unlink()
                    dihapus.append(str(p))
                elif p.is_dir():
                    # copy_file/move_file bisa membuat FOLDER; menghapus folder
                    # rekursif terlalu berisiko untuk undo otomatis — laporkan.
                    masalah.append(f"{p} — folder buatan giliran itu tidak "
                                   "dihapus otomatis (hapus manual bila perlu)")
                # sudah tak ada -> tak perlu apa-apa
        except OSError as exc:
            masalah.append(f"{p} — gagal dipulihkan: {exc}")

    if not masalah:
        shutil.rmtree(tdir, ignore_errors=True)
    with _lock:
        # Giliran BERJALAN baru saja di-undo -> mutasi berikutnya harus membuka
        # kelompok baru, bukan menimpa manifest yang sudah dipulihkan/terhapus.
        if _state["dir"] == tdir:
            _state["dir"] = None
            _state["manifest"] = []
            _state["seq"] = 0

    sisa = len(dirs) - (0 if masalah else 1)
    baris = [f"[undo] Checkpoint {tdir.name} dipulihkan:"]
    if dipulihkan:
        baris.append("  dikembalikan: " + ", ".join(dipulihkan))
    if dihapus:
        baris.append("  dihapus (file buatan giliran itu): " + ", ".join(dihapus))
    if not dipulihkan and not dihapus:
        baris.append("  (tak ada file yang perlu disentuh)")
    if masalah:
        baris.append("  ⚠ masalah:\n    " + "\n    ".join(masalah))
        baris.append("  Checkpoint DIPERTAHANKAN karena ada yang gagal — "
                     "perbaiki penyebabnya lalu ulangi bila perlu.")
    baris.append(f"  Sisa checkpoint yang bisa di-undo lagi: {sisa}.")
    return "\n".join(baris)

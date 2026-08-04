"""Tool git BACA-SAJA: status, diff, log, blame ringkas.

Kenapa ini ada, dan kenapa hanya baca:

Sebelum ini bagas-ai punya 7 tool untuk memotong video dan NOL untuk git —
padahal ia agent yang kerjanya mengubah kode di repo git. Akibatnya pertanyaan
paling sering muncul di tengah giliran ("apa saja yang sudah kuubah?", "apa
memang begini bentuk aslinya?") hanya bisa DITEBAK oleh model, dan tebakan
salah persis yang muncul ke pengguna sebagai blunder.

`git_diff` menjawabnya dalam SATU panggilan. Itu mahal artinya di sini: satu
panggilan tool = satu bolak-balik ke situs AI web (belasan detik), jadi tool
yang menjawab banyak sekaligus jauh lebih berharga daripada tool yang menjawab
sedikit-sedikit.

MUTASI SENGAJA TAK DISEDIAKAN (tak ada git_commit/checkout/reset/push):
  - Repo pengguna memasang hook pre-commit yang MENAIKKAN VERSI tiap commit.
    Commit yang dibuat AI berarti versi naik tanpa pengguna memutuskannya —
    efek samping yang menyebar sampai ke rilis.
  - Rollback sudah dipegang checkpoint.undo_changes (tersimpan di disk, 20
    giliran) — jadi git tak dibutuhkan sebagai jaring pengaman.
  - commit/push adalah keputusan pengguna soal APA yang layak jadi riwayat
    permanen. Itu bukan detail teknis yang pantas didelegasikan diam-diam.
"""
from __future__ import annotations

import subprocess

from .. import config
from .base import tool

_TIMEOUT = 20
# Diff dipotong di sini, bukan dibiarkan utuh: satu `git diff` di repo yang
# ramai bisa puluhan ribu baris, dan situs AI web MEMOTONG pesan panjang — yang
# terpotong justru jadi tak terbaca sama sekali. Lebih baik dipotong rapi di
# sini sambil memberi tahu sisanya berapa.
_MAKS_BARIS = 400


def _git(*args: str) -> tuple[bool, str]:
    """Jalankan git di root proyek. Return (berhasil, keluaran/pesan galat)."""
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(config.PROJECT_ROOT), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        return False, "[error] git tidak terpasang / tak ada di PATH."
    except subprocess.TimeoutExpired:
        return False, f"[error] git {' '.join(args)} melewati {_TIMEOUT} detik."
    except Exception as e:  # noqa: BLE001
        return False, f"[error] gagal menjalankan git: {e}"
    keluar = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        pesan = keluar.strip() or f"exit code {p.returncode}"
        if "not a git repository" in pesan.lower():
            return False, ("[error] folder proyek ini BUKAN repo git, jadi tool "
                           "git tak bisa dipakai. Pakai read_file/search_text.")
        return False, f"[error] git {' '.join(args)}: {pesan}"
    return True, keluar


def _potong(teks: str, maks: int = _MAKS_BARIS) -> str:
    baris = teks.splitlines()
    if len(baris) <= maks:
        return teks.strip() or "(kosong)"
    sisa = len(baris) - maks
    return "\n".join(baris[:maks]) + (
        f"\n… [dipotong, {sisa} baris lagi — persempit dengan argumen `path`]")


@tool
def git_status() -> str:
    """Lihat berkas apa saja yang BERUBAH, BARU, atau TERHAPUS di repo sejak commit terakhir, plus nama branch-nya. Panggil ini lebih dulu saat kamu perlu tahu apa yang sudah tersentuh di sesi ini — satu panggilan menggantikan menebak-nebak atau membaca ulang banyak berkas.

    Tidak mengubah apa pun.
    """
    ok, cabang = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not ok:
        return cabang
    ok, keluar = _git("status", "--porcelain=v1", "--untracked-files=normal")
    if not ok:
        return keluar
    baris = [b for b in keluar.splitlines() if b.strip()]
    if not baris:
        return (f"branch {cabang.strip()} — bersih, tak ada perubahan yang "
                "belum di-commit.")
    # Kode porcelain v1 = DUA karakter di kolom tetap (X=index, Y=worktree),
    # lalu spasi, lalu path. WAJIB dipotong per posisi, bukan dipecah di spasi
    # pertama: berkas yang cuma diubah di worktree berformat " M path" —
    # kolom pertamanya SPASI, jadi pemisahan berbasis spasi menghasilkan kode
    # kosong dan seluruh berkas salah dilabeli "belum dilacak".
    arti = {"M": "diubah", "A": "ditambahkan", "D": "dihapus", "R": "dipindah",
            "C": "disalin", "U": "konflik", "T": "ganti tipe"}
    out = [f"branch {cabang.strip()} — {len(baris)} berkas:"]
    for b in baris[:200]:
        kode, nama = b[:2], b[3:].strip()
        if kode == "??":
            label = "baru (belum dilacak)"
        else:
            x, y = kode[0], kode[1]
            # Y (worktree) lebih relevan bagi model: itu keadaan berkas di disk
            # sekarang. X dipakai hanya bila worktree-nya bersih (sudah di-stage).
            tanda = y if y != " " else x
            label = arti.get(tanda, f"kode {kode!r}")
            if x != " " and x != "?":
                label += ", sudah di-stage"
        out.append(f"  {label}: {nama}")
    if len(baris) > 200:
        out.append(f"  … {len(baris) - 200} berkas lagi")
    return "\n".join(out)


@tool
def git_diff(path: str = "", staged: bool = False, stat_only: bool = False) -> str:
    """Lihat ISI perubahan yang belum di-commit (baris + dan -). Inilah cara tercepat menjawab "apa yang sudah kuubah sejauh ini" tanpa membaca ulang berkasnya — dan cara memastikan perubahanmu benar-benar seperti yang kamu maksud sebelum menyatakan selesai.

    path: batasi ke satu berkas/folder (kosong = semua yang berubah).
    staged: true = yang sudah di-`git add` saja.
    stat_only: true = ringkasan per berkas (berapa baris berubah), tanpa isinya.
        Pakai ini dulu bila perubahannya banyak, baru diff penuh per berkas.
    """
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if stat_only:
        args.append("--stat")
    if (path or "").strip():
        args += ["--", path.strip()]
    ok, keluar = _git(*args)
    if not ok:
        return keluar
    if not keluar.strip():
        lingkup = f" pada {path}" if path.strip() else ""
        kondisi = "yang sudah di-stage" if staged else "yang belum di-commit"
        return f"tak ada perubahan {kondisi}{lingkup}."
    return _potong(keluar)


@tool
def git_log(limit: int = 10, path: str = "") -> str:
    """Lihat riwayat commit terakhir (hash pendek, waktu relatif, penulis, judul). Berguna untuk memahami KONVENSI proyek — gaya pesan commit, bahasa yang dipakai, seberapa besar satu commit — sebelum kamu mengusulkan perubahan yang akan masuk ke riwayat yang sama.

    limit: berapa commit terakhir (1-50, default 10).
    path: batasi ke riwayat satu berkas/folder (kosong = seluruh repo).
    """
    n = max(1, min(int(limit or 10), 50))
    args = ["log", f"-{n}", "--no-color", "--date=relative",
            "--pretty=format:%h  %ad  %an  %s"]
    if (path or "").strip():
        args += ["--", path.strip()]
    ok, keluar = _git(*args)
    if not ok:
        return keluar
    return keluar.strip() or "(belum ada commit)"


@tool
def git_show(ref: str = "HEAD", path: str = "", stat_only: bool = False) -> str:
    """Lihat isi sebuah commit: pesan lengkap + perubahannya. Pakai untuk memahami bagaimana suatu bagian dulu diubah dan APA ALASANNYA — pesan commit proyek ini sering memuat alasan yang tak tertulis di kode.

    ref: hash/branch/tag (default HEAD = commit terakhir).
    path: batasi ke satu berkas (kosong = seluruh commit).
    stat_only: true = pesan + daftar berkas saja, tanpa isi perubahannya.
    """
    args = ["show", "--no-color", (ref or "HEAD").strip()]
    if stat_only:
        args.append("--stat")
    if (path or "").strip():
        args += ["--", path.strip()]
    ok, keluar = _git(*args)
    if not ok:
        return keluar
    return _potong(keluar)

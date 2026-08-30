"""Pembaruan bagas-ai dari GitHub — menangani SEMUA cara instalasi.

Kasus yang didukung:
- Instalasi via installer yang meng-clone repo ke ~/.bagasai/src (install.sh /
  install.ps1 tanpa folder lokal): repo git sudah ada -> tinggal pull + reinstall.
- Instalasi via installer DARI dalam folder proyek, atau `pip install` biasa
  (salinan non-editable tanpa repo git penopang): pembaruan DISIAPKAN dengan
  meng-clone repo ke ~/.bagasai/src, lalu reinstall dari sana.
- Checkout pengembangan (editable): pull + reinstall editable.

Reinstall MEMPERTAHANKAN cara pasang aslinya (mis. `--user`) supaya kode yang
benar-benar dijalankan ikut ter-update, bukan cuma repo-nya.
"""
from __future__ import annotations

import logging as _logging

log = _logging.getLogger(__name__)

import json
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from pathlib import Path

from . import config


def _run(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def _git_available() -> bool:
    return shutil.which("git") is not None


def _pkg_path() -> Path | None:
    try:
        from . import __file__ as pkg_file  # .../agent/__init__.py

        return Path(pkg_file).resolve()
    except Exception:
        return None


def _repo_dir() -> Path:
    """Lokasi clone repo untuk pembaruan (dibuat installer / oleh kita)."""
    return config.CONFIG_HOME / "src"


def find_repo() -> Path | None:
    """Temukan folder repo git (berisi .git) penopang instalasi ini, bila ada."""
    candidates: list[Path] = []
    pkg = _pkg_path()
    if pkg:
        candidates.append(pkg)
    candidates.append(Path(__file__).resolve())
    candidates.append(_repo_dir())        # lokasi clone installer / auto-setup
    candidates.append(config.ROOT_DIR)    # checkout pengembangan

    seen: set[Path] = set()
    for c in candidates:
        chain = [c, *c.parents] if (c.exists() or c.parents) else [c]
        for p in chain:
            if p in seen:
                continue
            seen.add(p)
            try:
                if (p / ".git").exists():
                    return p
            except OSError:
                continue
    return None


def _bukti_editable() -> bool:
    """True bila ADA jejak instalasi editable bagasai di site-packages.

    Jejaknya: `__editable__.bagasai-*.pth` (pip modern) atau `bagasai.egg-link`
    (setuptools lama)."""
    calon: list[Path] = []
    for f in (site.getsitepackages, site.getusersitepackages):
        try:
            hasil = f()
        except Exception:  # noqa: BLE001
            continue
        calon.extend(Path(p) for p in
                     ([hasil] if isinstance(hasil, str) else hasil))
    pkg = _pkg_path()
    if pkg:
        calon.append(pkg.parent.parent)
    for d in calon:
        try:
            if not d.is_dir():
                continue
            if any(d.glob("__editable__*bagasai*")) or (d / "bagasai.egg-link").exists():
                return True
        except OSError:
            continue
    return False


def _is_editable(repo: Path) -> bool:
    """True bila paket terpasang BENAR-BENAR mode editable.

    Dua syarat, dan yang kedua tidak boleh dihilangkan: kode aktif memang berada
    di repo/src, DAN ada jejak instalasi editable yang sungguhan.

    Kenapa syarat kedua perlu — TERAMATI, bukan hipotetis: menjalankan updater
    dari checkout sumber (mis. `python -m agent update` dengan src/ di sys.path)
    membuat agent.__file__ menunjuk ke repo, sehingga syarat pertama saja sudah
    terpenuhi. Dulu itu cukup untuk memasang dengan `pip install -e`, dan
    instalasi SALINAN milik pengguna diam-diam berubah jadi editable yang
    menunjuk ke working tree — termasuk suntingan yang belum di-commit. Update
    tak boleh mengubah CARA paket terpasang; ia cuma boleh memperbarui isinya.
    """
    pkg = _pkg_path()
    if not pkg:
        return False
    try:
        di_repo = str((repo / "src").resolve()) in str(pkg)
    except Exception:  # noqa: BLE001
        return False
    return di_repo and _bukti_editable()


def _is_user_install() -> bool:
    """True bila paket terpasang di user site-packages (pip install --user)."""
    pkg = _pkg_path()
    if not pkg:
        return False
    try:
        usp = site.getusersitepackages()
    except Exception:
        return False
    if not usp:
        return False
    try:
        return str(pkg).startswith(str(Path(usp).resolve()))
    except Exception:
        return False


# --- versi & sidik isi: sumber kebenaran BERLAPIS --------------------------
#
# GitHub saja TIDAK cukup untuk menjawab "apakah aku sudah terbaru". Yang
# benar-benar dijalankan adalah salinan di site-packages, dan ia bisa menyimpang
# dari repo tanpa satu pun tanda: pip melewati pemasangan bila nomor versinya
# sama, build/lib bisa menyimpan kode basi, dan modul yang sudah DIHAPUS dari
# repo bisa tertinggal sebagai berkas yatim yang tetap bisa diimpor.
#
# TERUKUR di laptop ini: metadata melaporkan 1.0.53 dan isi tiap berkas cocok,
# tapi dua modul peninggalan versi lama (connectors/claude.py,
# interfaces/winmouse.py) masih nangkring di site-packages — tak terlihat oleh
# pemeriksaan versi mana pun yang cuma membandingkan angka.
#
# Maka versi diperiksa dari TIGA tempat — paket yang benar-benar terpasang,
# pyproject di repo lokal, dan pyproject di upstream — plus sidik isi berkas,
# satu-satunya yang bisa membuktikan ketiganya memang sama.

_RE_VERSI = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _installed_pkg_dir() -> Path | None:
    """Folder paket `agent` yang BENAR-BENAR diimpor proses ini."""
    pkg = _pkg_path()
    return pkg.parent if pkg else None


def installed_version() -> str:
    """Versi paket menurut metadata yang terpasang ('' bila tak diketahui)."""
    try:
        from importlib.metadata import version
        return version("bagasai")
    except Exception:  # noqa: BLE001
        return ""


def _versi_teks(teks: str) -> str:
    m = _RE_VERSI.search(teks or "")
    return m.group(1) if m else ""


def repo_version(repo: Path) -> str:
    """Versi menurut pyproject.toml di repo."""
    try:
        return _versi_teks((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError:
        return ""


def _remote_version(repo: Path, upstream: str) -> str:
    """Versi menurut pyproject.toml di upstream — tanpa perlu menariknya."""
    if not upstream:
        return ""
    r = _run(["git", "show", f"{upstream}:pyproject.toml"], repo, timeout=60)
    return _versi_teks(r.stdout) if r.returncode == 0 else ""


def _berkas_py(root: Path) -> dict[str, bytes]:
    """Isi tiap modul .py di bawah `root`, akhir-barisnya dinormalkan.

    Normalisasi CRLF penting: repo bisa ter-checkout dengan akhir baris Windows
    sementara salinan terpasang memakai LF (atau sebaliknya), dan tanpa ini
    setiap berkas akan tampak berbeda padahal isinya identik."""
    keluar: dict[str, bytes] = {}
    if not root or not root.is_dir():
        return keluar
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if "__pycache__" in rel:
            continue
        try:
            keluar[rel] = p.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            continue
    return keluar


def _bandingkan(repo: Path) -> tuple[bool, list[str]]:
    """(sinkron, daftar beda) antara paket TERPASANG dan sumber di repo."""
    dipasang = _installed_pkg_dir()
    sumber = repo / "src" / "agent"
    if not dipasang or not sumber.is_dir():
        return True, []
    try:
        if dipasang.resolve() == sumber.resolve():
            return True, []      # dijalankan langsung dari sumber (editable/dev)
    except OSError:
        pass
    a, b = _berkas_py(dipasang), _berkas_py(sumber)
    if not a or not b:
        return True, []
    beda: list[str] = []
    for rel in sorted(set(b) - set(a)):
        beda.append(f"belum terpasang: {rel}")
    for rel in sorted(set(a) - set(b)):
        beda.append(f"yatim (sisa versi lama): {rel}")
    for rel in sorted(set(a) & set(b)):
        if a[rel] != b[rel]:
            beda.append(f"isinya basi: {rel}")
    return not beda, beda


def versions() -> dict:
    """Laporan versi dari SEMUA sumber sekaligus, bukan cuma GitHub.

    Kunci: terpasang / repo / remote (nomor versi), commit_lokal & commit_remote,
    `sinkron` (isi berkas terpasang == sumber di repo) dan `beda` (rinciannya)."""
    info: dict = {
        "terpasang": installed_version(),
        "lokasi": str(_installed_pkg_dir() or ""),
        "repo": "", "remote": "",
        "commit_lokal": "", "commit_remote": "",
        "sinkron": True, "beda": [],
    }
    repo = find_repo()
    if not repo:
        return info
    info["repo_dir"] = str(repo)
    info["repo"] = repo_version(repo)
    if _git_available():
        info["commit_lokal"] = _run(
            ["git", "rev-parse", "--short", "HEAD"], repo).stdout.strip()
        up = _upstream(repo)
        if up:
            info["commit_remote"] = _run(
                ["git", "rev-parse", "--short", up], repo).stdout.strip()
            info["remote"] = _remote_version(repo, up)
    info["sinkron"], info["beda"] = _bandingkan(repo)
    return info


def clone_repo() -> dict:
    """Clone repo ke ~/.bagasai/src untuk MENGAKTIFKAN pembaruan.

    Return {ok: bool, repo?: Path, cloned?: bool, status?, detail?}.
    """
    if not _git_available():
        return {"ok": False, "status": "no_git"}
    if not config.REPO_URL:
        return {"ok": False, "status": "no_repo"}
    dest = _repo_dir()
    if (dest / ".git").exists():
        return {"ok": True, "repo": dest}
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = _run(
        ["git", "clone", "--depth", "1", "--branch", config.REPO_BRANCH,
         config.REPO_URL, str(dest)],
        dest.parent, timeout=600,
    )
    if r.returncode != 0:
        return {
            "ok": False,
            "status": "clone_error",
            "detail": (r.stderr or r.stdout).strip()[:300],
        }
    return {"ok": True, "repo": dest, "cloned": True}


def _upstream(repo: Path) -> str:
    r = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for cand in (f"origin/{config.REPO_BRANCH}", "origin/main", "origin/master"):
        if _run(["git", "rev-parse", cand], repo).returncode == 0:
            return cand
    return ""


def check() -> dict:
    """Periksa apakah ada pembaruan. Return dict dengan kunci 'status'.

    status: no_git | setup_needed | no_repo | fetch_error | no_upstream |
            up_to_date | update_available
    """
    if not _git_available():
        return {"status": "no_git"}

    repo = find_repo()
    if not repo:
        # Instalasi tanpa repo git penopang (salinan pip / installer dari folder).
        # Pembaruan BISA disiapkan dengan clone saat apply().
        if config.REPO_URL:
            return {
                "status": "setup_needed",
                "repo_url": config.REPO_URL,
                "branch": config.REPO_BRANCH,
            }
        return {"status": "no_repo"}

    if _run(["git", "rev-parse", "--is-inside-work-tree"], repo).returncode != 0:
        return {"status": "no_repo"}

    fetch = _run(["git", "fetch", "--quiet"], repo, timeout=120)
    if fetch.returncode != 0:
        return {
            "status": "fetch_error",
            "detail": (fetch.stderr or fetch.stdout).strip()[:200],
            "repo": str(repo),
        }

    upstream = _upstream(repo)
    if not upstream:
        return {"status": "no_upstream", "repo": str(repo)}

    local = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    remote = _run(["git", "rev-parse", upstream], repo).stdout.strip()
    if not remote:
        return {"status": "no_upstream", "repo": str(repo)}
    if local == remote:
        # Repo sudah mutakhir BUKAN berarti yang berjalan juga mutakhir. Yang
        # dieksekusi adalah salinan di site-packages, dan ia bisa tertinggal
        # tanpa satu pun tanda — inilah kenapa versi tak boleh diperiksa dari
        # GitHub saja.
        sinkron, beda = _bandingkan(repo)
        dasar = {
            "local": local[:7], "repo": str(repo),
            "versi_terpasang": installed_version(),
            "versi_repo": repo_version(repo),
        }
        if not sinkron:
            return {"status": "stale_install", "beda": beda[:12], **dasar}
        return {"status": "up_to_date", **dasar}

    behind = _run(["git", "rev-list", "--count", f"HEAD..{upstream}"], repo).stdout.strip()
    log = _run(["git", "log", "--oneline", "-8", f"HEAD..{upstream}"], repo).stdout.strip()
    return {
        "status": "update_available",
        "local": local[:7],
        "remote": remote[:7],
        "behind": behind or "?",
        "log": log,
        "upstream": upstream,
        "repo": str(repo),
    }


def _ensure_pip() -> None:
    """Pastikan modul pip tersedia untuk interpreter ini. Sebagian instalasi
    (mis. beberapa Python Store / venv minimal) bisa memunculkan 'No module named
    pip' saat `-m pip` — perbaiki dengan ensurepip, aman bila sudah ada."""
    try:
        _run([sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
             config.CONFIG_HOME, timeout=180)
    except Exception:
        pass


def _purge_build(repo: Path) -> None:
    """Buang artefak build sebelum pip membangun ulang.

    setuptools memakai `build/lib/` sebagai CACHE: file cuma disalin ulang dari
    `src/` bila sumbernya lebih baru. Bila build/ ketinggalan (mis. pernah ikut
    ter-commit, atau timestamp-nya tersegarkan oleh checkout), pip membungkus
    KODE LAMA tapi menyematkan nomor versi baru — update lapor sukses, versi naik,
    isinya basi, dan --force-reinstall pun cuma memasang ulang wheel basi yang
    sama. Membuang build/ membuat setiap pembaruan dibangun dari sumber apa adanya.
    """
    # HANYA build/. `dist/` sengaja TIDAK disentuh: ia bukan cache — isinya
    # wheel/sdist hasil rilis yang mungkin sengaja disimpan pengguna, dan untuk
    # instalasi editable `repo` adalah checkout kerja pengguna sendiri. Menghapus
    # dist/ berarti membuang artefak rilis tanpa peringatan & tanpa jalan pulih,
    # padahal yang meracuni pembaruan cuma build/lib.
    try:
        shutil.rmtree(repo / "build")
    except (OSError, FileNotFoundError):
        pass
    try:
        for egg in (repo / "src").glob("*.egg-info"):
            shutil.rmtree(egg, ignore_errors=True)
    except OSError:
        pass


_SCRIPT_NAMES = ("bagas-ai", "bagasai", "bagas")


def _script_dirs() -> list[Path]:
    """Folder tempat console-script (bagasai.exe dkk) dipasang, untuk skema biasa
    MAUPUN --user (Python Store memakai yang kedua)."""
    dirs: list[Path] = []
    for scheme in (None, os.name + "_user"):
        try:
            p = sysconfig.get_path("scripts") if scheme is None \
                else sysconfig.get_path("scripts", scheme)
        except Exception:  # noqa: BLE001
            continue
        if p:
            d = Path(p)
            if d not in dirs:
                dirs.append(d)
    return dirs


def _liberate_scripts() -> list[tuple[Path, Path]]:
    """Geser console-script yang TIDAK sedang berjalan supaya pip bebas menimpanya.

    CATATAN PENTING (terukur, jangan diandalkan berlebihan): anggapan umum bahwa
    "Windows mengizinkan exe yang sedang berjalan di-RENAME" TIDAK berlaku untuk
    console-script buatan pip. Diuji langsung pada bagasai.exe yang sedang jalan:
        rename GAGAL: [WinError 32] ... being used by another process
    Exe-nya dipetakan sebagai image section tanpa FILE_SHARE_DELETE, jadi rename
    ikut ditolak, bukan cuma tulis/hapus.

    Fungsi ini tetap berguna: bagas-ai memasang TIGA nama (bagas-ai/bagasai/
    bagas) sedangkan yang berjalan biasanya cuma satu, jadi dua sisanya bisa
    digeser dan tak lagi menggagalkan pip. Untuk exe yang benar-benar sedang
    berjalan, satu-satunya jalan yang jujur adalah memasang SESUDAH proses itu
    keluar -> lihat _schedule_post_exit_install().

    Return daftar (asal, tujuan) supaya bisa dikembalikan bila pip tetap gagal.
    """
    if os.name != "nt":
        return []
    dipindah: list[tuple[Path, Path]] = []
    stempel = time.strftime("%Y%m%d%H%M%S")
    for d in _script_dirs():
        if not d.is_dir():
            continue
        # Bersihkan sisa geseran update-update sebelumnya (kini tak lagi dipakai).
        for sisa in d.glob("*.bagasai-old-*"):
            try:
                sisa.unlink()
            except OSError:
                pass
        for nama in _SCRIPT_NAMES:
            src = d / f"{nama}.exe"
            if not src.exists():
                continue
            dst = d / f"{nama}.exe.bagasai-old-{stempel}"
            try:
                src.rename(dst)
                dipindah.append((src, dst))
            except OSError:
                pass  # tak bisa digeser -> biar pip yang melapor apa adanya
    return dipindah


def _restore_scripts(dipindah: list[tuple[Path, Path]]) -> None:
    """Kembalikan exe yang digeser — dipakai bila pip tetap gagal, supaya pengguna
    tidak berakhir TANPA perintah bagas-ai sama sekali."""
    for src, dst in dipindah:
        if src.exists() or not dst.exists():
            continue
        try:
            dst.rename(src)
        except OSError:
            pass


_PENDING_LOG = "pembaruan_tertunda.log"


def _schedule_post_exit_install(repo: Path, argv: list[str]) -> bool:
    """Jadwalkan pemasangan untuk dijalankan BEGITU bagas-ai ini keluar.

    Ini jawaban jujur atas exe-yang-terkunci: selama proses ini hidup, file
    bagasai.exe TAK bisa ditimpa maupun di-rename (terbukti WinError 32 pada
    keduanya). Menyuruh pengguna "tutup lalu jalankan update lagi" berarti
    pembaruan gagal diam-diam berkali-kali — persis keluhan yang memicu seluruh
    rangkaian perbaikan ini.

    Maka sebuah proses PENDAMPING dilepas (detached): ia menunggu PID ini
    hilang, baru menjalankan pip. Saat itu tak ada lagi yang mengunci exe, jadi
    pemasangan tuntas tanpa campur tangan pengguna — cukup tutup bagas-ai
    seperti biasa. Hasilnya ditulis ke log di CONFIG_HOME supaya kegagalan tetap
    bisa ditelusuri, bukan lenyap tanpa jejak.

    Return True bila pendamping berhasil dilepas."""
    log = config.CONFIG_HOME / _PENDING_LOG
    skrip = config.CONFIG_HOME / "pembaruan_tertunda.py"
    # --quiet DIBUANG untuk jalur terjadwal: bila gagal, log HARUS memuat
    # sebabnya. Terbukti perlu — log sebelumnya cuma berisi "GAGAL" tanpa satu
    # baris pun penjelasan, sehingga kegagalan berulang mustahil ditelusuri.
    bersih = [a for a in argv if a != "--quiet"]
    exes = [str(d / f"{n}.exe") for d in _script_dirs() for n in _SCRIPT_NAMES]
    kode = (
        "import os, subprocess, sys, time\n"
        f"induk = {os.getpid()}\n"
        f"argv = {bersih!r}\n"
        f"log = {str(log)!r}\n"
        f"cwd = {str(repo)!r}\n"
        f"exes = {exes!r}\n"
        "catatan = []\n"
        # 1) Tunggu induk keluar. Batas 15 menit supaya pendamping tak jadi
        #    proses abadi bila bagas-ai dibiarkan terbuka semalaman.
        "batas = time.time() + 900\n"
        "while time.time() < batas:\n"
        "    try:\n"
        "        os.kill(induk, 0)\n"
        "    except OSError:\n"
        "        break\n"
        "    time.sleep(1.0)\n"
        "else:\n"
        "    sys.exit(0)\n"
        # 2) Menunggu SATU pid saja TIDAK cukup, dan ini terbukti gagal di
        #    pemakaian nyata: pengguna menutup bagas-ai lalu MEMBUKANYA LAGI,
        #    dan instance baru mengunci exe yang sama sehingga pip tetap kena
        #    WinError 32. Jadi tunggu sampai exe-nya benar-benar bisa dibuka
        #    untuk ditulis — itu ukuran langsung "tak ada yang memakai".
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
        # 3) Tetap diulang beberapa kali: bagas-ai bisa dibuka lagi tepat di
        #    sela antara pemeriksaan dan pemasangan.
        "hasil = 'GAGAL'\n"
        "for ke in range(6):\n"
        "    if not bebas():\n"
        "        catatan.append('percobaan %d: exe masih terkunci' % (ke + 1))\n"
        "        time.sleep(20.0)\n"
        "        continue\n"
        "    try:\n"
        "        r = subprocess.run(argv, cwd=cwd, capture_output=True,\n"
        "                           text=True, timeout=900)\n"
        "        keluaran = (r.stdout or '') + (r.stderr or '')\n"
        "        if r.returncode == 0:\n"
        "            hasil = 'SUKSES'\n"
        "            catatan.append(keluaran[-2000:])\n"
        "            break\n"
        "        catatan.append('percobaan %d gagal:' % (ke + 1))\n"
        "        catatan.append(keluaran[-2000:])\n"
        "    except Exception as exc:\n"
        "        catatan.append('percobaan %d error: %r' % (ke + 1, exc))\n"
        "    time.sleep(20.0)\n"
        "try:\n"
        "    open(log, 'w', encoding='utf-8').write(\n"
        "        time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + hasil + chr(10)\n"
        "        + chr(10).join(catatan))\n"
        "except OSError:\n"
        "    pass\n"
    )
    try:
        config.CONFIG_HOME.mkdir(parents=True, exist_ok=True)
        skrip.write_text(kode, encoding="utf-8")
    except OSError:
        return False

    # Lepas benar-benar terpisah: tak boleh ikut mati saat terminal bagas-ai
    # ditutup, dan tak boleh menahan proses ini keluar.
    bendera = 0
    if os.name == "nt":
        bendera = (getattr(subprocess, "DETACHED_PROCESS", 0)
                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(
            [sys.executable, str(skrip)],
            cwd=str(config.CONFIG_HOME), creationflags=bendera,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _salin_pohon(sumber: Path, tujuan: Path) -> tuple[int, int, list[str]]:
    """Timpa `tujuan` dengan isi `sumber` & buang berkas yatim.

    Return (jumlah_disalin, jumlah_dibuang, daftar_galat)."""
    tujuan.mkdir(parents=True, exist_ok=True)
    galat: list[str] = []
    punya_sumber: set[str] = set()
    n_salin = 0
    for p in sorted(sumber.rglob("*")):
        rel = p.relative_to(sumber)
        if "__pycache__" in rel.parts:
            continue
        punya_sumber.add(rel.as_posix())
        t = tujuan / rel
        try:
            if p.is_dir():
                t.mkdir(parents=True, exist_ok=True)
                continue
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, t)
            n_salin += 1
        except OSError as e:
            galat.append(f"{rel.as_posix()}: {e}")
    # Berkas yatim WAJIB dibuang: modul yang sudah dihapus dari repo tapi masih
    # ada di site-packages tetap bisa diimpor, jadi kode mati bisa hidup lagi
    # diam-diam. .pyc lama ikut disapu supaya tak menyembunyikan sumber yang
    # sudah tiada.
    n_hapus = 0
    for p in sorted(tujuan.rglob("*"), key=lambda x: -len(x.parts)):
        rel = p.relative_to(tujuan)
        yatim = rel.as_posix() not in punya_sumber
        if "__pycache__" in rel.parts:
            yatim = True
        if not yatim:
            continue
        try:
            if p.is_file():
                p.unlink()
                n_hapus += 1
            elif p.is_dir():
                p.rmdir()
        except OSError:
            pass          # sisa yang bandel tak sebanding menggagalkan update
    return n_salin, n_hapus, galat


def _pasang_distinfo(singgah: Path, site_dir: Path) -> None:
    """Pindahkan *.dist-info hasil build supaya `pip show` & metadata ikut baru."""
    baru = [d for d in singgah.glob("bagasai-*.dist-info") if d.is_dir()]
    if not baru:
        return
    for lama in site_dir.glob("bagasai-*.dist-info"):
        if lama.is_dir():
            shutil.rmtree(lama, ignore_errors=True)
    try:
        shutil.copytree(baru[0], site_dir / baru[0].name, dirs_exist_ok=True)
    except OSError:
        pass


def _pasang_langsung(repo: Path) -> dict:
    """Pasang kode ke site-packages TANPA menyentuh .exe yang terkunci.

    Ini jawaban atas "update selalu tertunda lalu gagal". Penyebabnya bukan
    kebetulan dan bukan sesekali: yang mengunci bagas-ai.exe adalah PERINTAH
    UPDATE ITU SENDIRI. Menjalankan `bagas-ai update` berarti bagas-ai.exe
    sedang berjalan, jadi pip praktis TAK PERNAH bisa menimpanya — pemasangan
    selalu diundur ke "nanti setelah ditutup", dan di sana pun sering gagal.
    Pengguna melihatnya sebagai pembaruan yang tak pernah benar-benar terjadi.

    Kuncinya: .exe console-script hanyalah STUB peluncur — isinya path
    interpreter + nama fungsi entry point, TANPA sepotong pun kode paket. Kode
    yang dijalankan ada di site-packages/agent, dan berkas .py di sana tidak
    dikunci Windows meski modulnya sedang diimpor (yang terkunci cuma .exe).
    Jadi paket dibangun ke folder singgah lalu disalin sendiri ke tempatnya:
    pembaruan langsung berlaku, .exe lama tetap meluncurkan kode BARU, dan tak
    ada yang perlu ditunggu."""
    tujuan = _installed_pkg_dir()
    if tujuan is None:
        return {"ok": False, "detail": "lokasi paket terpasang tak diketahui"}
    try:
        if tujuan.resolve() == (repo / "src" / "agent").resolve():
            return {"ok": True, "catatan": "kode aktif langsung dari repo"}
    except OSError:
        pass
    singgah = Path(tempfile.mkdtemp(prefix="bagasai-pasang-"))
    try:
        _purge_build(repo)
        r = _run([sys.executable, "-m", "pip", "install", "--no-deps",
                  "--quiet", "--upgrade", "--target", str(singgah), str(repo)],
                 repo, timeout=600)
        if r.returncode != 0 and "no module named pip" in (
                r.stderr + r.stdout).lower():
            _ensure_pip()
            r = _run([sys.executable, "-m", "pip", "install", "--no-deps",
                      "--quiet", "--upgrade", "--target", str(singgah),
                      str(repo)], repo, timeout=600)
        if r.returncode != 0:
            return {"ok": False,
                    "detail": (r.stderr or r.stdout).strip()[:300]}
        sumber = singgah / "agent"
        if not (sumber / "__init__.py").is_file():
            return {"ok": False, "detail": "hasil build tak memuat paket agent"}
        n_salin, n_hapus, galat = _salin_pohon(sumber, tujuan)
        _pasang_distinfo(singgah, tujuan.parent)
        if galat:
            return {"ok": False, "detail": "; ".join(galat[:3])}
        return {"ok": True, "disalin": n_salin, "dibuang": n_hapus}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        shutil.rmtree(singgah, ignore_errors=True)


def _kabar_browser() -> str:
    """Keterangan singkat browser yang akan dipakai — "" bila tak ada kabar.

    Pembaruan bisa MENGGANTI browser bawaan (itu yang terjadi saat bawaannya
    pindah ke Brave). Pengguna yang belum memasangnya tak boleh mengetahuinya
    dari gejala: yang jalan diam-diam browser lain, dan kalau tak satu pun
    browser asli ada, Chromium bundel — yang paling sering diblok situs.

    Sengaja TIDAK memasang apa pun. Memasang browser di tengah `update` adalah
    perubahan sistem yang tak diminta; yang pantas dilakukan di sini cuma
    mengatakannya, lengkap dengan perintahnya."""
    try:
        from . import config as _cfg
        from .connectors.browser import _pilih_exe
        diminta = (_cfg.CONNECTOR_BROWSER_CHANNEL or "").strip()
        if not diminta:
            return ""
        exe, dipakai = _pilih_exe(diminta)
        if dipakai == diminta:
            return ""
        if exe:
            return (f"browser bawaan sekarang {diminta}, tapi ia belum "
                    f"terpasang — untuk sementara dipakai {dipakai}. "
                    f"Pasang {diminta}: winget install --id Brave.Brave -e"
                    if diminta == "brave" else
                    f"browser bawaan {diminta} belum terpasang — "
                    f"dipakai {dipakai}.")
        return (f"tak ada browser asli yang terpasang ({diminta} pun tidak), "
                "jadi bagas-ai jatuh ke Chromium bawaan Playwright — itu yang "
                "paling sering diblok situs. Pasang Brave atau Chrome.")
    except Exception:  # noqa: BLE001 - kabar tambahan, jangan gagalkan update
        log.debug("pemeriksaan browser gagal", exc_info=True)
        return ""


def _pasang_tesseract() -> str:
    """Pastikan Tesseract OCR terpasang; kembalikan kabar ("" bila tak ada kabar).

    read_image_local (/image) memakai executable Tesseract untuk OCR lokal.
    Beda dengan browser (lihat _kabar_browser — sengaja TIDAK dipasang di tengah
    update), Tesseract dipasang otomatis di sini: paketnya kecil, pemasangannya
    cakupan-pengguna tanpa menyentuh sistem, dan tanpanya satu fitur lengkap
    mati diam-diam ("OCR lokal: tidak tersedia") tanpa satu pun pesan kepada
    pengguna. Setelah terpasang pun PATH sesi lama belum melihatnya, makanya
    pencariannya juga lewat lokasi bawaan (lihat _cari_tesseract)."""
    try:
        from .tools.image_local import _cari_tesseract
        if _cari_tesseract():
            return ""
    except Exception:  # noqa: BLE001 - kabar tambahan, jangan gagalkan update
        return ""
    if os.name != "nt":
        return ("Tesseract OCR belum terpasang - OCR lokal (/image) nonaktif. "
                "Pasang: sudo apt install tesseract-ocr (Debian/Ubuntu) atau "
                "brew install tesseract (macOS).")
    winget = shutil.which("winget")
    if not winget:
        return ("Tesseract OCR belum terpasang dan winget tidak ada - OCR lokal "
                "(/image) nonaktif. Pasang manual: "
                "winget install --id UB-Mannheim.TesseractOCR -e")
    args = [winget, "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
            "--silent", "--disable-interactivity",
            "--accept-package-agreements", "--accept-source-agreements"]
    # Satu percobaan saja: paket ini CUMA mendukung cakupan mesin (diuji:
    # --scope user pulang "No applicable installer found"), dan kode keluar
    # winget tak bisa dipercaya sebagai ukuran sukses (saat paket sudah ada
    # pun ia non-zero). Penentu sebenarnya adalah _cari_tesseract() di bawah.
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        keluaran = ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        keluaran = f"{type(exc).__name__}: {exc}"
    try:
        if _cari_tesseract():
            return ("Tesseract OCR terpasang otomatis - OCR lokal (/image) "
                    "siap dipakai.")
    except Exception:  # noqa: BLE001
        return ""
    # Bawa penyebabnya: kegagalan senyap pernah membuat 'Gagal memasang'
    # mustahil ditelusuri (bandingkan _schedule_post_exit_install).
    return ("Gagal memasang Tesseract OCR otomatis - OCR lokal (/image) "
            "nonaktif. Pasang manual: "
            "winget install --id UB-Mannheim.TesseractOCR -e"
            + (f"  (winget: {keluaran[-200:]})" if keluaran else ""))


def _pasang_vision_gemma() -> str:
    """Tarik Gemma 3n E2B bila Ollama memang tersedia di mesin pengguna."""
    if os.environ.get("BAGASAI_SKIP_VISION_MODEL") == "1":
        return "Vision lokal dilewati (BAGASAI_SKIP_VISION_MODEL=1)."
    ollama = shutil.which("ollama")
    if not ollama:
        return "Vision lokal siap setelah Ollama dipasang: ollama pull gemma3n:e2b"
    try:
        listed = subprocess.run([ollama, "list"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20)
        if "gemma3n" in (listed.stdout or "").lower():
            return "Model vision lokal Gemma 3n E2B sudah tersedia."
        pulled = subprocess.run([ollama, "pull", "gemma3n:e2b"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=1800)
        if pulled.returncode == 0:
            return "Model vision lokal Gemma 3n E2B terpasang."
        return "Gagal menarik Gemma 3n E2B; coba ulang: ollama pull gemma3n:e2b"
    except (OSError, subprocess.SubprocessError):
        return "Vision lokal belum terpasang; coba: ollama pull gemma3n:e2b"


def _reinstall(repo: Path) -> dict:
    """Pasang ulang dari `repo`, mempertahankan cara pasang asli (--user, editable)."""
    editable = _is_editable(repo)
    base = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"]
    flags: list[str] = []
    if not editable and _is_user_install():
        flags.append("--user")
    if editable:
        target = ["-e", str(repo)]
    else:
        # KUNCI (penyebab "update tak ngefek"): instalasi non-editable DILEWATI
        # pip bila versi paket sama ("Requirement already satisfied"). Nomor versi
        # di pyproject nyaris tak pernah dinaikkan per commit, jadi git pull
        # menarik kode baru ke repo TAPI site-packages tetap basi — update terasa
        # kosong padahal git sudah terbaru. --force-reinstall memaksa menimpa apa
        # pun versinya; --no-deps menjaga cepat & aman (dependensi yang sudah
        # terpasang tak diutak-atik). Sumber kebenaran versi di sini adalah COMMIT
        # git, bukan string versi.
        target = ["--force-reinstall", "--no-deps", str(repo)]

    _purge_build(repo)
    # Geser exe yang mungkin sedang berjalan SEBELUM pip menyentuhnya (Windows).
    # BERLAKU JUGA untuk editable: pip tetap menulis ulang skrip konsol, jadi
    # exe yang terkunci tetap menggagalkan perintahnya (TERAMATI: `pip install
    # -e .` mati dengan WinError 32 pada bagas-ai.exe saat bagas-ai berjalan).
    # Bedanya cuma keparahannya — pada editable kode sudah aktif dari repo.
    digeser = _liberate_scripts()

    inst = _run(base + flags + target, repo, timeout=600)
    blob = (inst.stderr + inst.stdout).lower()
    # 'No module named pip' -> interpreter belum punya pip; pasang lalu ulangi.
    if inst.returncode != 0 and "no module named pip" in blob:
        _ensure_pip()
        inst = _run(base + flags + target, repo, timeout=600)
        blob = (inst.stderr + inst.stdout).lower()
    # Fallback PEP 668 (Linux/macOS "externally-managed-environment").
    if inst.returncode != 0 and "externally-managed" in blob:
        inst = _run(base + flags + ["--break-system-packages"] + target, repo, timeout=600)
        blob = (inst.stderr + inst.stdout).lower()

    # Windows: .exe (bagas-ai.exe) TERKUNCI karena bagas-ai-nya SENDIRI sedang
    # berjalan saat `bagas-ai update` -> pip gagal menimpa skrip. Ini penyebab
    # umum "update gagal". Untuk instalasi editable, KODE sudah ter-update lewat
    # git pull, jadi ini TIDAK fatal — cukup restart.
    locked = inst.returncode != 0 and any(
        s in blob for s in ("winerror 32", "being used by another process",
                            "access is denied", "permission denied")
    )
    # Gagal total -> kembalikan exe yang tadi digeser, jangan tinggalkan pengguna
    # tanpa perintah `bagas-ai` sama sekali.
    if inst.returncode != 0:
        _restore_scripts(digeser)

    ok = inst.returncode == 0
    cara = "pip"
    # VERIFIKASI, bukan percaya kode-keluar pip. pip bisa melaporkan sukses
    # padahal yang terpasang masih kode lama (cache build/lib, versi dianggap
    # sama, berkas yatim tertinggal) — persis kegagalan senyap yang membuat
    # "sudah update" terasa bohong.
    sinkron, beda = _bandingkan(repo)
    if editable:
        sinkron, beda = True, []      # kode aktif langsung dari repo

    # Jalur langsung dipakai bila pip GAGAL (umumnya .exe terkunci oleh perintah
    # update itu sendiri) MAUPUN bila pip mengaku sukses tapi hasilnya tak
    # cocok. Dua-duanya berakhir sebagai pembaruan yang tidak terjadi.
    langsung: dict = {}
    if not editable and (not ok or not sinkron):
        langsung = _pasang_langsung(repo)
        if langsung.get("ok"):
            sinkron, beda = _bandingkan(repo)
            if sinkron:
                ok, cara, locked = True, "langsung", False

    # Menjadwalkan pemasangan untuk "nanti setelah ditutup" kini benar-benar
    # jalan TERAKHIR: ia hanya menunda masalah, dan pengguna melaporkan bahwa
    # penundaan itulah yang paling sering berujung gagal.
    dijadwalkan = False
    if not ok and locked:
        dijadwalkan = _schedule_post_exit_install(repo, base + flags + target)

    detail = ""
    if not ok:
        detail = (langsung.get("detail")
                  or (inst.stderr or inst.stdout).strip())[:250]
    return {
        "ok": ok,
        "cara": cara,
        "locked": locked,
        "scheduled": dijadwalkan,
        "terverifikasi": sinkron,
        "beda": beda[:8],
        "detail": detail,
        "editable": editable,
    }


# --- Cek otomatis saat startup (non-blocking, hasil di-cache) ---------------

def _cache_file() -> Path:
    return config.CONFIG_HOME / "update_check.json"


def read_cache() -> dict:
    """Baca hasil cek update terakhir (untuk notifikasi instan saat startup)."""
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        _cache_file().write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def background_refresh(min_interval: float = 3 * 3600) -> None:
    """Perbarui cache status update di LATAR — tak memblokir startup, aman gagal.

    Hanya benar-benar menghubungi GitHub bila cache sudah lebih tua dari
    `min_interval` detik, supaya tak boros jaringan saat sering dijalankan.
    """
    try:
        cache = read_cache()
        now = time.time()
        if cache.get("ts") and (now - float(cache["ts"])) < min_interval:
            return  # baru saja dicek

        def _worker() -> None:
            try:
                res = check()
                res["ts"] = time.time()
                _write_cache(res)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        pass


def apply(force: bool = True) -> dict:
    """Siapkan repo bila perlu, tarik pembaruan, lalu pasang ulang.

    force=True (bawaan) berarti POKOKNYA DIPASANG: setiap hambatan yang dulu
    memulangkan "gagal" kini punya jalan memutar yang tak merusak apa pun —
      * checkout pengembangan tak bisa fast-forward (ada kerja lokal) ->
        pembaruan dipasang dari klon terpisah di ~/.bagasai/src; checkout
        pengguna TIDAK disentuh sama sekali;
      * fetch gagal (offline) tapi salinan terpasang ternyata basi ->
        tetap dipasang ulang dari repo lokal yang sudah ada;
      * .exe terkunci oleh perintah update itu sendiri -> kode disalin
        langsung ke site-packages (lihat _pasang_langsung), tanpa penundaan.

    status: no_git | no_repo | clone_error | pull_error | fetch_error | updated
    """
    if not _git_available():
        return {"status": "no_git"}

    repo = find_repo()
    pull_out = ""
    beralih = ""
    if not repo:
        # Belum ada repo penopang -> siapkan dengan clone (mengaktifkan update
        # untuk instalasi salinan / installer-dari-folder).
        c = clone_repo()
        if not c.get("ok"):
            return {"status": c.get("status", "no_repo"), "detail": c.get("detail", "")}
        repo = c["repo"]
        pull_out = "repo disiapkan (clone baru)"
    elif repo.resolve() == _repo_dir().resolve():
        # Repo KELOLAAN kita (~/.bagasai/src): tak ada kerja pengguna di sini, jadi
        # aman diselaraskan PAKSA ke remote. `git pull --ff-only` sering GAGAL di
        # sini gara-gara perubahan lokal sepele / riwayat menyimpang -> itulah
        # penyebab umum "update gagal".
        fetch = _run(["git", "fetch", "--all", "--quiet"], repo, timeout=180)
        if fetch.returncode != 0:
            # Tanpa fetch sukses, reset hanya menyelaraskan ke upstream BASI ->
            # akan keliru melaporkan "updated" padahal tidak menarik apa pun.
            # Tapi bila salinan TERPASANG ternyata basi terhadap repo lokal,
            # masih ada pekerjaan nyata yang bisa diselesaikan sekarang juga:
            # pasang ulang dari yang sudah ada. Itu jauh lebih berguna daripada
            # memulangkan "gagal" pada pengguna yang cuma sedang offline.
            sinkron, _ = _bandingkan(repo)
            if not (force and not sinkron):
                return {
                    "status": "fetch_error",
                    "detail": (fetch.stderr or fetch.stdout).strip()[:300],
                    "repo": str(repo),
                }
            pull_out = ("tak bisa menghubungi remote — dipasang ulang dari "
                        "salinan lokal yang sudah ada")
        else:
            up = _upstream(repo) or f"origin/{config.REPO_BRANCH}"
            r = _run(["git", "reset", "--hard", up], repo, timeout=120)
            if r.returncode != 0:
                return {
                    "status": "pull_error",
                    "detail": (r.stderr or r.stdout).strip()[:300],
                    "repo": str(repo),
                }
            _run(["git", "clean", "-fd"], repo, timeout=120)
            pull_out = f"diselaraskan ke {up}"
    else:
        # Checkout PENGEMBANGAN milik pengguna -> JANGAN dipaksa reset: kerja
        # yang belum di-commit bisa lenyap, dan itu kerugian yang jauh lebih
        # besar daripada gagal update.
        pull = _run(["git", "pull", "--ff-only"], repo, timeout=180)
        if pull.returncode != 0:
            detail = (pull.stderr or pull.stdout).strip()[:300]
            if not force:
                return {
                    "status": "pull_error",
                    "detail": (detail + "  — ada perubahan lokal / riwayat "
                               "menyimpang. Commit/stash dulu, atau update "
                               "lewat installer."),
                    "repo": str(repo),
                }
            # force: jangan menyerah DAN jangan menyentuh kerja pengguna —
            # ambil pembaruannya dari klon terpisah milik kita sendiri.
            c = clone_repo()
            if not c.get("ok"):
                return {"status": c.get("status", "no_repo"),
                        "detail": c.get("detail", "") or detail}
            asal = repo
            repo = c["repo"]
            f2 = _run(["git", "fetch", "--all", "--quiet"], repo, timeout=180)
            if f2.returncode == 0:
                up = _upstream(repo) or f"origin/{config.REPO_BRANCH}"
                _run(["git", "reset", "--hard", up], repo, timeout=120)
                _run(["git", "clean", "-fd"], repo, timeout=120)
            beralih = (f"checkout-mu di {asal} tak bisa fast-forward (ada "
                       "perubahan lokal), jadi pembaruan dipasang dari klon "
                       f"terpisah di {repo}. Checkout-mu TIDAK disentuh.")
            pull_out = beralih
        else:
            pull_out = pull.stdout.strip()[:300]

    reinst = _reinstall(repo)
    # Bersihkan cache notifikasi startup supaya tak lagi menampilkan "usang".
    _write_cache({"status": "up_to_date", "ts": time.time()})
    note = beralih
    if reinst["ok"] and reinst.get("cara") == "langsung":
        note = ((note + "  ") if note else "") + (
            "berkas .exe sedang dipakai oleh perintah update ini sendiri, jadi "
            "kodenya dipasang langsung ke site-packages — pembaruan SUDAH "
            "aktif, tak ada yang perlu ditunggu atau ditutup.")
    elif not reinst["ok"] and reinst.get("locked"):
        if reinst.get("scheduled"):
            note = ("pemasangan langsung maupun lewat pip sama-sama tak bisa "
                    "diselesaikan sekarang, jadi dijadwalkan berjalan sendiri "
                    "begitu bagas-ai ditutup. "
                    f"(log: ~/.bagasai/{_PENDING_LOG})")
        else:
            note = ("kode sudah ditarik, tapi pemasangannya belum berhasil. "
                    + ("Kode aktif langsung dari repo — cukup jalankan ulang."
                       if reinst.get("editable") else
                       f"Rincian: {reinst.get('detail', '')}"))
    elif not reinst.get("terverifikasi"):
        note = ((note + "  ") if note else "") + (
            "pemasangan selesai tapi isi paket masih belum sama dengan repo: "
            + "; ".join(reinst.get("beda") or [])[:200])
    # Aset yang tak ikut di dalam paket Python DIAMBIL DI SINI — sekali per
    # pembaruan, bukan saat pertama kali dibutuhkan. Kalau menunggu dibutuhkan,
    # penanda "tugas selesai" pertama sesudah update berbunyi terlambat (atau
    # tak berbunyi sama sekali kalau laptopnya sedang luring saat itu).
    try:
        from . import tanda as _tanda
        _tanda.unduh(paksa=True)
    except Exception:  # noqa: BLE001 - aset opsional, jangan gagalkan update
        log.debug("unduh aset penanda gagal", exc_info=True)
    # Dependensi eksternal yang dijanjikan fitur (/image) diambil di sini juga,
    # bukan menunggu fiturnya dipakai — sama seperti aset penanda di atas.
    tesseract = _pasang_tesseract()
    if tesseract:
        note = ((note + "  ") if note else "") + tesseract
    vision = _pasang_vision_gemma()
    if vision:
        note = ((note + "  ") if note else "") + vision
    return {
        "status": "updated",
        # Browser yang akan benar-benar dipakai sesudah pembaruan ini. Ikut
        # dilaporkan karena bawaannya bisa BERUBAH lewat pembaruan (mis. ke
        # Brave) — dan pengguna yang belum memasangnya berhak tahu bahwa yang
        # jalan nanti bukan browser yang tertulis di setelan.
        "browser": _kabar_browser(),
        "pull": pull_out,
        # Instalasi editable: kode aktif langsung dari repo -> git pull SUDAH
        # meng-update meski pip gagal menimpa .exe yang terkunci.
        "reinstalled": reinst["ok"] or bool(reinst.get("locked") and reinst.get("editable")),
        # Bukti, bukan sekadar kode-keluar pip: isi paket terpasang == sumber repo.
        "verified": bool(reinst.get("terverifikasi")),
        "diff": reinst.get("beda") or [],
        "how": reinst.get("cara", ""),
        "locked": bool(reinst.get("locked")),
        # Terkunci TAPI sudah dijadwalkan: pemasangan berjalan sendiri sesudah
        # bagas-ai ditutup, jadi ini bukan kegagalan yang menuntut tindakan.
        "scheduled": bool(reinst.get("scheduled")),
        "note": note,
        "ocr": tesseract,
        "vision": vision,
        "pip_detail": reinst["detail"],
        "repo": str(repo),
        "version": repo_version(repo),
    }

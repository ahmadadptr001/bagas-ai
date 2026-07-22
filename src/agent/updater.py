"""Pembaruan bagas-ai dari GitHub — menangani SEMUA cara instalasi.

Kasus yang didukung:
- Instalasi via installer yang meng-clone repo ke ~/.bagasai/src (install.sh /
  install.ps1 tanpa folder lokal): repo git sudah ada -> tinggal pull + reinstall.
- Instalasi via installer DARI dalam folder proyek, atau `pip install` biasa
  (salinan non-editable tanpa repo git penopang): auto-update DISIAPKAN dengan
  meng-clone repo ke ~/.bagasai/src, lalu reinstall dari sana.
- Checkout pengembangan (editable): pull + reinstall editable.

Reinstall MEMPERTAHANKAN cara pasang aslinya (mis. `--user`) supaya kode yang
benar-benar dijalankan ikut ter-update, bukan cuma repo-nya.
"""
from __future__ import annotations

import json
import os
import shutil
import site
import subprocess
import sys
import sysconfig
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
    """Lokasi clone repo untuk auto-update (dibuat installer / oleh kita)."""
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


def _is_editable(repo: Path) -> bool:
    """True bila paket terpasang mode editable (agent.__file__ ada di repo/src)."""
    pkg = _pkg_path()
    if not pkg:
        return False
    try:
        return str((repo / "src").resolve()) in str(pkg)
    except Exception:
        return False


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


def clone_repo() -> dict:
    """Clone repo ke ~/.bagasai/src untuk MENGAKTIFKAN auto-update.

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
        # Auto-update BISA disiapkan dengan clone saat apply().
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
        return {"status": "up_to_date", "local": local[:7], "repo": str(repo)}

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

    # Terkunci oleh bagas-ai ini sendiri -> jadwalkan pemasangan begitu ia keluar.
    # Tanpa ini pengguna cuma diberi tahu "tutup lalu ulangi", dan pada praktiknya
    # pembaruan gagal diam-diam berkali-kali.
    dijadwalkan = False
    if locked:
        dijadwalkan = _schedule_post_exit_install(repo, base + flags + target)

    return {
        "ok": inst.returncode == 0,
        "locked": locked,
        "scheduled": dijadwalkan,
        "detail": "" if inst.returncode == 0 else (inst.stderr or inst.stdout).strip()[:200],
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


def apply() -> dict:
    """Siapkan repo bila perlu, tarik pembaruan, lalu pasang ulang.

    status: no_git | no_repo | clone_error | pull_error | updated
    """
    if not _git_available():
        return {"status": "no_git"}

    repo = find_repo()
    pull_out = ""
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
            return {
                "status": "fetch_error",
                "detail": (fetch.stderr or fetch.stdout).strip()[:300],
                "repo": str(repo),
            }
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
        # Checkout PENGEMBANGAN milik pengguna -> JANGAN paksa (bisa hilang kerja).
        pull = _run(["git", "pull", "--ff-only"], repo, timeout=180)
        if pull.returncode != 0:
            detail = (pull.stderr or pull.stdout).strip()[:300]
            return {
                "status": "pull_error",
                "detail": (detail + "  — ada perubahan lokal / riwayat menyimpang. "
                           "Commit/stash dulu, atau update lewat installer."),
                "repo": str(repo),
            }
        pull_out = pull.stdout.strip()[:300]

    reinst = _reinstall(repo)
    # Bersihkan cache notifikasi startup supaya tak lagi menampilkan "usang".
    _write_cache({"status": "up_to_date", "ts": time.time()})
    note = ""
    if not reinst["ok"] and reinst.get("locked"):
        if reinst.get("scheduled"):
            note = ("skrip bagas-ai sedang dipakai (kamu menjalankan update DARI "
                    "bagas-ai), jadi .exe-nya belum bisa ditimpa sekarang. "
                    "Pemasangan SUDAH DIJADWALKAN dan akan berjalan sendiri "
                    "begitu bagas-ai ditutup — cukup TUTUP lalu buka lagi, tak "
                    f"perlu mengetik apa pun. (log: ~/.bagasai/{_PENDING_LOG})")
        else:
            note = ("skrip bagas-ai sedang dipakai (kamu menjalankan update DARI "
                    "bagas-ai), jadi file .exe tak bisa ditimpa. "
                    + ("Kode sudah ter-update — cukup TUTUP lalu buka lagi bagas-ai."
                       if reinst.get("editable") else
                       "Tutup semua bagas-ai lalu jalankan `bagas-ai update` sekali lagi."))
    return {
        "status": "updated",
        "pull": pull_out,
        # Instalasi editable: kode aktif langsung dari repo -> git pull SUDAH
        # meng-update meski pip gagal menimpa .exe yang terkunci.
        "reinstalled": reinst["ok"] or bool(reinst.get("locked") and reinst.get("editable")),
        "locked": bool(reinst.get("locked")),
        # Terkunci TAPI sudah dijadwalkan: pemasangan berjalan sendiri sesudah
        # bagas-ai ditutup, jadi ini bukan kegagalan yang menuntut tindakan.
        "scheduled": bool(reinst.get("scheduled")),
        "note": note,
        "pip_detail": reinst["detail"],
        "repo": str(repo),
    }

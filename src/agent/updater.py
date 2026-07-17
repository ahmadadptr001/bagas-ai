"""Pembaruan bagasAI dari GitHub — menangani SEMUA cara instalasi.

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
import shutil
import site
import subprocess
import sys
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


def _reinstall(repo: Path) -> dict:
    """Pasang ulang dari `repo`, mempertahankan cara pasang asli (--user, editable)."""
    editable = _is_editable(repo)
    base = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"]
    flags: list[str] = []
    if not editable and _is_user_install():
        flags.append("--user")
    target = ["-e", str(repo)] if editable else [str(repo)]

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

    return {
        "ok": inst.returncode == 0,
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
    else:
        pull = _run(["git", "pull", "--ff-only"], repo, timeout=180)
        if pull.returncode != 0:
            return {
                "status": "pull_error",
                "detail": (pull.stderr or pull.stdout).strip()[:300],
                "repo": str(repo),
            }
        pull_out = pull.stdout.strip()[:300]

    reinst = _reinstall(repo)
    # Bersihkan cache notifikasi startup supaya tak lagi menampilkan "usang".
    _write_cache({"status": "up_to_date", "ts": time.time()})
    return {
        "status": "updated",
        "pull": pull_out,
        "reinstalled": reinst["ok"],
        "pip_detail": reinst["detail"],
        "repo": str(repo),
    }

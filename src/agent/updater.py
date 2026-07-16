"""Pembaruan bagasAI dari GitHub.

Mendeteksi repo git yang menopang instalasi ini (hasil clone installer di
~/.bagasai/src, atau checkout dev), membandingkan dengan remote, lalu menarik
pembaruan (git pull) dan memasang ulang bila perlu. Kalau tidak ada pembaruan,
memberi tahu bahwa bagasAI sudah versi terbaru.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from . import config


def _run(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def _git_available() -> bool:
    return shutil.which("git") is not None


def find_repo() -> Path | None:
    """Temukan folder repo git (yang berisi .git) penopang instalasi ini."""
    candidates: list[Path] = []
    try:
        from . import __file__ as pkg_file  # .../src/agent/__init__.py

        candidates.append(Path(pkg_file).resolve())
    except Exception:
        pass
    candidates.append(Path(__file__).resolve())
    candidates.append(config.CONFIG_HOME / "src")  # lokasi clone installer
    candidates.append(config.ROOT_DIR)  # checkout pengembangan

    seen: set[Path] = set()
    for c in candidates:
        chain = [c, *c.parents] if c.exists() or c.parents else [c]
        for p in chain:
            if p in seen:
                continue
            seen.add(p)
            if (p / ".git").exists():
                return p
    return None


def _is_editable(repo: Path) -> bool:
    """True bila paket terpasang mode editable (agent.__file__ ada di repo/src)."""
    try:
        from . import __file__ as pkg_file

        return str((repo / "src").resolve()) in str(Path(pkg_file).resolve())
    except Exception:
        return False


def _upstream(repo: Path) -> str:
    r = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # Fallback umum.
    for cand in ("origin/main", "origin/master"):
        if _run(["git", "rev-parse", cand], repo).returncode == 0:
            return cand
    return ""


def check() -> dict:
    """Periksa apakah ada pembaruan. Return dict dengan kunci 'status'.

    status: no_repo | no_git | fetch_error | no_upstream | up_to_date |
            update_available
    """
    repo = find_repo()
    if not repo:
        return {"status": "no_repo"}
    if not _git_available():
        return {"status": "no_git", "repo": str(repo)}
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


def apply() -> dict:
    """Tarik pembaruan (git pull --ff-only) lalu pasang ulang bila perlu.

    status: no_repo | pull_error | updated
    """
    repo = find_repo()
    if not repo:
        return {"status": "no_repo"}

    pull = _run(["git", "pull", "--ff-only"], repo, timeout=180)
    if pull.returncode != 0:
        return {
            "status": "pull_error",
            "detail": (pull.stderr or pull.stdout).strip()[:300],
            "repo": str(repo),
        }

    # Pasang ulang agar perubahan dependency / entry point ikut terpasang.
    editable = _is_editable(repo)
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"]
    cmd += (["-e", str(repo)] if editable else [str(repo)])
    inst = _run(cmd, repo, timeout=600)

    return {
        "status": "updated",
        "pull": pull.stdout.strip()[:300],
        "reinstalled": inst.returncode == 0,
        "pip_detail": ""
        if inst.returncode == 0
        else (inst.stderr or inst.stdout).strip()[:200],
        "repo": str(repo),
    }

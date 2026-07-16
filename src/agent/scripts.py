"""Script memory: skrip Python reusable yang ditulis agent sendiri.

Saat menghadapi tugas yang belum bisa dilakukan tool bawaan (mis. scraping web,
konversi PDF, olah data), agent bisa menulis skrip, menyimpannya di sini, dan
memakainya lagi nanti. Index disimpan di ~/.bagasai/scripts/index.json.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Any

from . import config


def _load_index() -> list[dict[str, Any]]:
    if not config.SCRIPTS_INDEX.is_file():
        return []
    try:
        return json.loads(config.SCRIPTS_INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_index(items: list[dict[str, Any]]) -> None:
    config.SCRIPTS_INDEX.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _safe_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())
    if not name:
        raise ValueError("Nama skrip tidak valid.")
    return name


def save(name: str, code: str, description: str = "") -> str:
    name = _safe_name(name)
    path = config.SCRIPTS_DIR / f"{name}.py"
    path.write_text(code, encoding="utf-8")

    index = [it for it in _load_index() if it["name"] != name]
    index.append(
        {
            "name": name,
            "description": description.strip(),
            "path": str(path),
            "updated": time.time(),
        }
    )
    _save_index(index)
    return f"Skrip '{name}' disimpan ke script memory."


def run(name: str, args: str = "") -> str:
    name = _safe_name(name)
    path = config.SCRIPTS_DIR / f"{name}.py"
    if not path.is_file():
        return f"Skrip '{name}' tidak ditemukan. Lihat daftar via list_scripts."

    cmd = [sys.executable, str(path)] + (args.split() if args else [])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.CODE_EXEC_TIMEOUT,
            cwd=str(config.PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return f"[timeout] Skrip '{name}' melebihi {config.CODE_EXEC_TIMEOUT} detik."
    out = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(tidak ada output)"
    if len(out) > 10000:
        out = out[:10000] + "\n... [dipotong]"
    return f"exit_code={proc.returncode}\n{out}"


def index_list() -> list[dict[str, Any]]:
    return _load_index()


def as_prompt_block() -> str:
    """Ringkasan skrip tersimpan untuk system prompt."""
    items = _load_index()
    if not items:
        return ""
    lines = "\n".join(
        f"- {it['name']}: {it.get('description') or '(tanpa deskripsi)'}"
        for it in items
    )
    return (
        "Skrip reusable yang sudah kamu simpan (bisa dijalankan via run_script):\n"
        f"{lines}\n"
    )

"""Preferensi pengguna yang tersimpan (model & effort terakhir dipakai).

Disimpan di ~/.bagasai/prefs.json agar bagas-ai memakai model terakhir saat
dijalankan lagi.
"""
from __future__ import annotations

import json

from . import config

_PREFS_FILE = config.CONFIG_HOME / "prefs.json"


def load() -> dict:
    if not _PREFS_FILE.is_file():
        return {}
    try:
        return json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save(**kwargs) -> None:
    data = load()
    data.update({k: v for k, v in kwargs.items() if v is not None})
    try:
        _PREFS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def get_model() -> str | None:
    return load().get("model")


def get_effort() -> str | None:
    return load().get("effort")


def get_total_tokens() -> int:
    try:
        return int(load().get("total_tokens", 0))
    except (TypeError, ValueError):
        return 0


def set_total_tokens(n: int) -> None:
    save(total_tokens=int(n))

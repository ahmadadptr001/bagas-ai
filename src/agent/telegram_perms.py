"""Izin akses bot Telegram — siapa yang boleh mengontrol laptop ini lewat Telegram.

Sumber kebenaran gabungan: ID dari env `TELEGRAM_ALLOWED_IDS` (permanen) + ID yang
diizinkan lewat perintah `/permissions-bot` (disimpan di ~/.bagasai/telegram_perms.json).
Pengirim yang belum diizinkan masuk daftar 'pending' agar pemilik bisa menyetujui
dari CLI. Karena lewat Telegram bagas-ai bisa menjalankan perintah & menulis file,
akses sengaja dibatasi.
"""
from __future__ import annotations

import json
import time

from . import config

_STORE = config.CONFIG_HOME / "telegram_perms.json"


def _load() -> dict:
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8"))
        d.setdefault("allowed", [])
        d.setdefault("pending", {})
        return d
    except Exception:
        return {"allowed": [], "pending": {}}


def _save(d: dict) -> None:
    try:
        _STORE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except OSError:
        pass


def allowed_ids() -> set[int]:
    """ID yang diizinkan = dari env + yang disetujui lewat /permissions-bot."""
    ids = set(config.TELEGRAM_ALLOWED_IDS)
    for x in _load().get("allowed", []):
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            pass
    return ids


def is_allowed(chat_id: int) -> bool:
    return chat_id in allowed_ids()


def add_allowed(chat_id: int, name: str | None = None) -> None:
    d = _load()
    if int(chat_id) not in [int(x) for x in d["allowed"]]:
        d["allowed"].append(int(chat_id))
    d["pending"].pop(str(chat_id), None)
    _save(d)


def remove_allowed(chat_id: int) -> None:
    d = _load()
    d["allowed"] = [int(x) for x in d["allowed"] if int(x) != int(chat_id)]
    _save(d)


def add_pending(chat_id: int, name: str) -> bool:
    """Catat permintaan izin. True bila BARU (belum pernah tercatat)."""
    d = _load()
    key = str(chat_id)
    is_new = key not in d["pending"]
    d["pending"][key] = {"name": name or "?", "ts": time.time()}
    _save(d)
    return is_new


def pending() -> dict:
    """{chat_id(str): {name, ts}} — permintaan yang menunggu persetujuan."""
    return _load().get("pending", {})


def clear_pending(chat_id: int) -> None:
    d = _load()
    d["pending"].pop(str(chat_id), None)
    _save(d)


def deny(chat_id: int) -> None:
    """Tolak: buang dari pending & allowed."""
    remove_allowed(chat_id)
    clear_pending(chat_id)

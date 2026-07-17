"""Memory jangka panjang: fakta yang diingat bagas-ai lintas SEMUA sesi.

Disimpan di ~/.bagasai/memory.json. Fakta di-inject ke system prompt setiap
sesi baru, sehingga bagas-ai "mengingat" preferensi/informasi tentang pengguna.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import config


def _load_raw() -> list[dict[str, Any]]:
    if not config.MEMORY_FILE.is_file():
        return []
    try:
        return json.loads(config.MEMORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(items: list[dict[str, Any]]) -> None:
    config.MEMORY_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def all_facts() -> list[str]:
    return [it["fact"] for it in _load_raw()]


def add(fact: str) -> str:
    fact = fact.strip()
    if not fact:
        return "Fakta kosong, tidak disimpan."
    items = _load_raw()
    if any(it["fact"].lower() == fact.lower() for it in items):
        return "Sudah ada di memory."
    items.append({"fact": fact, "added": time.time()})
    _save_raw(items)
    return f"Disimpan ke memory jangka panjang: {fact}"


def upsert(prefix: str, fact: str) -> str:
    """Simpan fakta ber-'kunci' (prefix): tambah bila belum ada, perbarui bila
    berbeda, dan LEWATI (tak menulis) bila sudah ada & sama persis.

    Return: 'added' | 'updated' | 'unchanged'. Berguna untuk fakta yang unik &
    bisa berubah, mis. sistem operasi pengguna.
    """
    fact = fact.strip()
    items = _load_raw()
    prefix_l = prefix.strip().lower()
    idx = next(
        (i for i, it in enumerate(items)
         if str(it.get("fact", "")).strip().lower().startswith(prefix_l)),
        None,
    )
    if idx is None:
        items.append({"fact": fact, "added": time.time()})
        _save_raw(items)
        return "added"
    if str(items[idx].get("fact", "")).strip() == fact:
        return "unchanged"  # sama -> jangan tulis ulang
    items[idx] = {"fact": fact, "added": time.time()}
    _save_raw(items)
    return "updated"


def remove(substring: str) -> str:
    items = _load_raw()
    kept = [it for it in items if substring.lower() not in it["fact"].lower()]
    removed = len(items) - len(kept)
    _save_raw(kept)
    return f"{removed} fakta dihapus dari memory."


def as_prompt_block() -> str:
    """Blok teks berisi memori untuk disisipkan ke system prompt."""
    facts = all_facts()
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return (
        "Hal yang kamu ingat tentang pengguna (memory jangka panjang):\n"
        f"{lines}\n"
    )

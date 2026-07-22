"""Riwayat WAKTU turn connector web -> dasar ETA yang JUJUR.

ETA yang benar-benar akurat MUSTAHIL untuk AI web: waktu selesai ditentukan
modelnya, tak bisa diketahui di muka. Jadi yang ditampilkan bukan janji, melainkan
DESKRIPSI dari data nyata pengguna sendiri — "biasanya ~Xs" — dihitung dari median
turn-turn terakhir. Bila sampel belum cukup, medians() mengembalikan None dan UI
sengaja TAK menampilkan ETA apa pun: lebih baik diam daripada menyesatkan.

Dua besaran direkam per service:
  - start_latency: detik dari prompt terkirim sampai jawaban MULAI mengalir
    (durasi fase "berpikir" — paling bervariasi, makanya cuma jadi hint).
  - answer_dur:    detik dari jawaban mulai sampai selesai (durasi "menjawab" —
    fase dengan sinyal token nyata, jadi layak dijadikan bar + perkiraan).
"""
from __future__ import annotations

import json
import threading
from statistics import median
from typing import Any

from . import config

_MAX = 40   # sampel per service yang disimpan (buang yang paling lama)
_MIN = 4    # di bawah ini: belum cukup -> jangan tampilkan ETA
_lock = threading.Lock()


def _path():
    return config.CONFIG_HOME / "web_timing.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def record(service: str, start_latency: float, answer_dur: float) -> None:
    """Catat satu turn: latensi mulai-menjawab & durasi menjawab (detik)."""
    if not service:
        return
    # Nilai tak masuk akal (negatif / nol) TAK dicatat supaya median tak ternoda.
    if start_latency < 0 or answer_dur <= 0:
        return
    with _lock:
        data = _load()
        rows = data.get(service)
        if not isinstance(rows, list):
            rows = []
        rows.append([round(float(start_latency), 2), round(float(answer_dur), 2)])
        data[service] = rows[-_MAX:]
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


def medians(service: str) -> dict[str, Any] | None:
    """{'start': med_latensi, 'answer': med_durasi, 'n': jumlah}, atau None bila
    sampel belum cukup untuk memberi perkiraan yang bertanggung jawab."""
    if not service:
        return None
    rows = _load().get(service)
    if not isinstance(rows, list) or len(rows) < _MIN:
        return None
    pairs = [r for r in rows if isinstance(r, list) and len(r) == 2]
    if len(pairs) < _MIN:
        return None
    return {
        "start": median(p[0] for p in pairs),
        "answer": median(p[1] for p in pairs),
        "n": len(pairs),
    }

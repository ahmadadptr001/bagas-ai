"""Connector web-AI: hubungkan bagas-ai ke antarmuka chat berbasis browser
(kimi.com, chat.qwen.ai, gemini.google.com, dst) lewat otomasi browser
(Playwright).

Dipakai sebagai "model": lihat models.py (pseudo-model dengan field `connector`)
dan core.Agent._run_connector. Instance connector di-CACHE per-service supaya
sesi browser tetap hidup lintas giliran (tak login/buka ulang tiap pesan).
"""
from __future__ import annotations

from .base import WebConnector
from .browser import (
    BrowserError, WebBusyError, WebLimitError, playwright_available,
)
from .dola import DolaConnector
from .kimi import KimiConnector
from .qwen import QwenConnector
from .gemini import GeminiConnector

# service -> kelas connector.
# Connector "claude" DIHAPUS (2026-08-01): claude.ai menolak protokol [[TOOL]]
# sebagai upaya injeksi, jadi giliran berakhir jadi penolakan alih-alih kerja.
_REGISTRY: dict[str, type[WebConnector]] = {
    "kimi": KimiConnector,
    "qwen": QwenConnector,
    "gemini": GeminiConnector,
    "dola": DolaConnector,
}

# service -> instance (cache; sesi browser bertahan lintas giliran).
_INSTANCES: dict[str, WebConnector] = {}


def get_connector(service: str) -> WebConnector:
    """Kembalikan instance connector untuk sebuah service (dibuat sekali)."""
    key = (service or "").strip().lower()
    if key not in _REGISTRY:
        raise BrowserError(f"connector '{service}' tidak dikenal")
    if key not in _INSTANCES:
        _INSTANCES[key] = _REGISTRY[key]()
    return _INSTANCES[key]


__all__ = [
    "WebConnector",
    "BrowserError",
    "playwright_available",
    "get_connector",
]

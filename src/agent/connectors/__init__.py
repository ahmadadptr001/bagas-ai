"""Connector web-AI: hubungkan bagas-ai ke antarmuka chat berbasis browser
(kimi.com, chat.qwen.ai, gemini.google.com, dst) lewat otomasi browser
(Playwright).

Dipakai sebagai "model": lihat models.py (pseudo-model dengan field `connector`)
dan core.Agent._run_connector. Instance connector di-CACHE per-service supaya
sesi browser tetap hidup lintas giliran (tak login/buka ulang tiap pesan).
"""
from __future__ import annotations

from .base import WebConnector
# SELURUH kelas galat browser wajib ikut diekspor di sini, tanpa kecuali.
#
# Bukan sekadar kerapian: core.py menangkapnya sebagai `connectors.XxxError`,
# dan Python menilai tiap klausa `except` SATU PER SATU saat galat terjadi.
# Satu nama yang tak ada di sini berubah jadi AttributeError DI TENGAH
# penanganan galat — ia menggantikan galat aslinya dan lolos dari seluruh
# klausa di bawahnya. TERJADI SUNGGUHAN: WebChatRusakError tak pernah
# diekspor, sehingga SETIAP kegagalan browser yang bukan WebBusyError muncul
# ke pengguna sebagai "module 'agent.connectors' has no attribute
# 'WebChatRusakError'" — dan penanganan kuota habis, pemulihan chat rusak,
# serta pesan galat yang ramah tak pernah sekali pun berjalan.
from .browser import (
    BrowserError, WebBusyError, WebChatRusakError, WebKonteksPenuhError,
    WebLampiranPenuhError, WebLimitError, playwright_available,
)
from .dola import DolaConnector
from .chatgpt import ChatGPTConnector
from .glm import GlmConnector
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
    "glm": GlmConnector,
    "chatgpt": ChatGPTConnector,
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
    "WebBusyError",
    "WebChatRusakError",
    "WebKonteksPenuhError",
    "WebLampiranPenuhError",
    "WebLimitError",
    "playwright_available",
    "get_connector",
]

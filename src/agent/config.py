"""Konfigurasi terpusat untuk bagasAI.

Dirancang agar bekerja dari terminal mana pun (seperti CLI global):
- API key & pengaturan dibaca dari (urutan prioritas):
    1. environment variable asli (mis. diset di sistem)
    2. ~/.bagasai/.env   <- lokasi config global
    3. ./.env            <- folder tempat perintah dijalankan
    4. .env di root repo <- untuk pengembangan
- Root project = folder terminal saat `bagasai` dipanggil (cwd); di situlah
  agent membaca/menulis file & menjalankan kode (override: BAGASAI_PROJECT_ROOT).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "bagasAI"

# Lokasi config global (seperti ~/.claude untuk Claude CLI).
CONFIG_HOME = Path(os.getenv("BAGASAI_HOME", Path.home() / ".bagasai"))
CONFIG_HOME.mkdir(parents=True, exist_ok=True)

# Root repo (untuk mode pengembangan): src/agent/config.py -> naik 2 level.
ROOT_DIR = Path(__file__).resolve().parents[2]

# Muat .env dari beberapa lokasi. load_dotenv TIDAK menimpa variabel yang sudah
# ada, jadi yang dimuat lebih dulu menang (kecuali env var asli yang selalu menang).
# Urutan: .env di folder saat ini > .env di root repo > ~/.bagasai/.env (fallback global).
for _candidate in (
    Path.cwd() / ".env",
    ROOT_DIR / ".env",
    CONFIG_HOME / ".env",
):
    if _candidate.is_file():
        load_dotenv(_candidate, override=False)

# ROOT PROJECT = folder tempat terminal berada saat `bagasai` dipanggil.
# Inilah yang dianggap "project" oleh agent: tempat ia baca/tulis file &
# menjalankan kode (mirip Claude Code yang bekerja di folder yang sedang dibuka).
PROJECT_ROOT = Path(os.getenv("BAGASAI_PROJECT_ROOT", Path.cwd())).resolve()

# Lokasi penyimpanan sesi percakapan (per folder project) & memory jangka panjang.
SESSIONS_DIR = CONFIG_HOME / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_FILE = CONFIG_HOME / "memory.json"
ACTIVE_FILE = CONFIG_HOME / "active.json"

# "Script memory": skrip reusable yang ditulis agent sendiri (scraping, konversi
# PDF, dll) agar bisa dipakai lagi di kemudian hari.
SCRIPTS_DIR = CONFIG_HOME / "scripts"
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
SCRIPTS_INDEX = SCRIPTS_DIR / "index.json"


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


# --- Kredensial & endpoint ---
NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_BASE_URL: str = os.getenv(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
).strip()

# --- Model (semua di-host NVIDIA) ---
# Default chat: DeepSeek-V4-Pro. Bisa diganti via /model, dan model terakhir
# yang dipakai tersimpan (lihat prefs.py).
CHAT_MODEL: str = os.getenv("CHAT_MODEL", "deepseek-ai/deepseek-v4-pro").strip()
# Model untuk analisis gambar (VLM NVIDIA resmi).
VISION_MODEL: str = os.getenv(
    "VISION_MODEL", "meta/llama-3.2-90b-vision-instruct"
).strip()

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# --- Auto-update (samakan dengan installer install.sh/install.ps1) ---
# Dipakai `bagasAI update` untuk menyiapkan/menarik pembaruan dari GitHub, bahkan
# bila instalasi berupa salinan (pip install biasa) tanpa repo git penopang.
REPO_URL: str = os.getenv(
    "BAGASAI_REPO", "https://github.com/ahmadadptr001/bagas-ai"
).strip()
REPO_BRANCH: str = os.getenv("BAGASAI_BRANCH", "master").strip()

# --- Perilaku agent ---
MAX_TOOL_ITERATIONS: int = int(os.getenv("MAX_TOOL_ITERATIONS", "8"))
TEMPERATURE: float = float(os.getenv("TEMPERATURE", "0.6"))
# Timeout per request (detik). Model besar NVIDIA bisa lambat saat cold-start.
REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "120"))
# Total waktu (detik) bagasAI bertahan mencoba ulang saat NVIDIA rate-limit /
# throttle ("worker local total request limit reached", dll) SEBELUM menyerah.
# Free tier ~40 RPM reset tiap menit, jadi default 5 menit cukup untuk pulih
# lalu MELANJUTKAN progres tanpa membatalkan tugas.
RETRY_MAX_SECONDS: float = float(os.getenv("RETRY_MAX_SECONDS", "300"))

# --- Keamanan ---
ALLOW_CODE_EXEC: bool = _get_bool("ALLOW_CODE_EXEC", True)
CODE_EXEC_TIMEOUT: int = int(os.getenv("CODE_EXEC_TIMEOUT", "30"))

ENV_FILE = CONFIG_HOME / ".env"


def has_api_key() -> bool:
    return bool(NVIDIA_API_KEY) and not NVIDIA_API_KEY.startswith("nvapi-xxxx")


def require_api_key() -> None:
    """Pastikan API key terisi; jika tidak, beri pesan yang jelas."""
    if not has_api_key():
        raise RuntimeError(
            f"NVIDIA_API_KEY belum diisi.\n"
            f"Jalankan '{APP_NAME} setup' lalu edit {ENV_FILE}\n"
            f"Ambil key gratis di https://build.nvidia.com (Get API Key)."
        )

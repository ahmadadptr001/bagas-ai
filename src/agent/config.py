"""Konfigurasi terpusat untuk bagas-ai.

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

APP_NAME = "bagas-ai"

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


# --- Model ---
# bagas-ai tidak lagi memakai model ber-API-key: SELURUH model kini berbasis
# browser (lihat models.py & agent/connectors), memakai akun pengguna sendiri.
# Karena itu tak ada NVIDIA_API_KEY / NVIDIA_BASE_URL / VISION_MODEL lagi, dan
# tak ada kredensial apa pun yang perlu diisi saat instalasi.
#
# Nilai lama peninggalan era NVIDIA yang mungkin masih tersimpan di .env
# (mis. "z-ai/glm-5.2") diabaikan: models.spec_for_id memetakannya ke model
# bawaan, jadi pengguna lama otomatis mendarat di model yang benar-benar jalan
# alih-alih terjebak di ID yang sudah tak ada.
CHAT_MODEL: str = os.getenv("CHAT_MODEL", "web/kimi").strip()
if not CHAT_MODEL.startswith("web/"):
    CHAT_MODEL = "web/kimi"

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))  # menerima '123' & '-100...'; token aneh dilewati
        except ValueError:
            pass
    return out


# Daftar chat/user ID Telegram yang BOLEH mengontrol bagas-ai (pisah koma). Karena
# lewat Telegram bagas-ai bisa menjalankan perintah & menulis file di laptopmu,
# batasi HANYA ke ID milikmu. Bila kosong, bot memakai "trust-on-first-use":
# pengirim PERTAMA otomatis jadi pemilik (dan diberi tahu ID-nya untuk disimpan).
TELEGRAM_ALLOWED_IDS: set[int] = _parse_ids(os.getenv("TELEGRAM_ALLOWED_IDS", ""))

# --- Auto-update (samakan dengan installer install.sh/install.ps1) ---
# Dipakai `bagas-ai update` untuk menyiapkan/menarik pembaruan dari GitHub, bahkan
# bila instalasi berupa salinan (pip install biasa) tanpa repo git penopang.
REPO_URL: str = os.getenv(
    "BAGASAI_REPO", "https://github.com/ahmadadptr001/bagas-ai"
).strip()
REPO_BRANCH: str = os.getenv("BAGASAI_BRANCH", "master").strip()

# --- Perilaku agent ---
MAX_TOOL_ITERATIONS: int = int(os.getenv("MAX_TOOL_ITERATIONS", "8"))
# Jaring pengaman anti-loop-liar: berapa kali panggilan tool yang PERSIS SAMA
# (atau kegagalan beruntun) boleh terjadi sebelum agent dipaksa berhenti memakai
# tool & menyimpulkan. Mencegah AI mengulang-ulang pekerjaan tanpa henti.
MAX_DUPLICATE_TOOL_CALLS: int = int(os.getenv("MAX_DUPLICATE_TOOL_CALLS", "3"))

# Setelan khusus endpoint API — TEMPERATURE, REQUEST_TIMEOUT, RETRY_MAX_SECONDS,
# STREAM_STALL_TIMEOUT, MAX_STALLS_PER_CALL, MAX_TOOL_CALLS, AUTO_FALLBACK,
# MAX_ESCALATIONS — DIHAPUS bersama model ber-API-key. Padanannya di jalur
# browser hidup di tempat yang sesuai: batas waktu & polling ada di
# WebConnector (start_timeout/answer_timeout), penantian saat server penuh
# ditangani WebBusyError + tunggu-lalu-ulangi, dan penjaga anti-mengoceh ada di
# _MAX_REPLY_CHARS. Nilai lama yang mungkin masih tertulis di .env diabaikan
# begitu saja — tak perlu dibersihkan manual.

# --- Keamanan ---
ALLOW_CODE_EXEC: bool = _get_bool("ALLOW_CODE_EXEC", True)
CODE_EXEC_TIMEOUT: int = int(os.getenv("CODE_EXEC_TIMEOUT", "30"))
# Timeout untuk perintah shell (run_command) — lebih longgar karena bisa lama
# (mis. install dependency / scaffolding). Perintah dijalankan NON-INTERAKTIF
# (stdin ditutup) & seluruh pohon prosesnya dibunuh bila melewati batas ini.
COMMAND_TIMEOUT: int = int(os.getenv("COMMAND_TIMEOUT", "300"))
# Cek sintaks OTOMATIS tiap kali write_file menulis file kode (.py/.js/.json/dll).
# Ringan (hanya parsing, tak menjalankan kode) & memastikan bagas-ai selalu
# memverifikasi hasil ngoding-nya. Matikan dengan AUTO_SYNTAX_CHECK=false.
AUTO_SYNTAX_CHECK: bool = _get_bool("AUTO_SYNTAX_CHECK", True)

# --- Connector web-AI (fitur /model claude-web, qwen-web, kimi-web via browser) ---
# Default (false): jendela Chrome MUNCUL sekali untuk login lalu DI-MINIMIZE —
# semua proses & jawaban tampil di TERMINAL, pengguna tak menyentuh browser.
# Jendela tetap ada (bukan headless) karena Cloudflare (mis. claude.ai) menolak
# sesi headless. Set CONNECTOR_HEADLESS=true untuk memaksa headless sejati (tanpa
# jendela sama sekali) — hanya cocok untuk situs yang lolos tanpa Cloudflare.
CONNECTOR_HEADLESS: bool = _get_bool("CONNECTOR_HEADLESS", False)
# Biarkan jendela browser TERLIHAT untuk SEMUA connector (jangan disembunyikan
# ke latar sesudah login). Berguna saat ingin MENGAMATI seluruh proses menjawab
# — langkah berpikir, pencarian web, pengetikan jawaban — bukan cuma hasil
# akhirnya di terminal. Tiap connector juga bisa meminta ini sendiri lewat
# atribut `show_window` (Kimi memakainya secara bawaan).
CONNECTOR_SHOW: bool = _get_bool("CONNECTOR_SHOW", False)
# Pakai Google CHROME asli (channel="chrome") alih-alih Chromium bawaan Playwright
# — lebih jarang terdeteksi/di-blok (mis. Cloudflare claude.ai) & lebih familiar.
# Bila Chrome tak terpasang, otomatis fallback ke Chromium bawaan. Kosongkan
# ("") untuk memaksa Chromium bawaan.
CONNECTOR_BROWSER_CHANNEL: str = os.getenv("CONNECTOR_BROWSER_CHANNEL", "chrome").strip()
# Tiap sesi bagas-ai membuat SATU percakapan baru di situs AI web, jadi lama-lama
# menumpuk. Batas ini menyimpan hanya N percakapan TERBARU yang dibuat bagas-ai;
# sisanya dihapus otomatis. HANYA menyentuh chat buatan bagas-ai (tercatat di
# ~/.bagasai/browser/<service>_chats.json) — percakapan pribadimu tak disentuh.
# 0 = jangan pernah hapus otomatis (bersihkan manual lewat /web).
CONNECTOR_KEEP_CHATS: int = int(os.getenv("CONNECTOR_KEEP_CHATS", "20"))

ENV_FILE = CONFIG_HOME / ".env"

# has_api_key()/require_api_key() DIHAPUS bersama model ber-API-key. bagas-ai
# tak lagi punya kredensial wajib: yang dibutuhkan cuma login browser (sekali,
# lewat jendela Chrome) dan — bila ingin memakai Telegram — token botnya.

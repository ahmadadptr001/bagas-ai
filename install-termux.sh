#!/usr/bin/env bash
# ============================================================================
# bagas-ai — installer Termux/Android.
#
# Jalankan dari dalam folder proyek:
#   bash install-termux.sh
# atau dari mana saja (mengunduh repo lebih dulu):
#   curl -fsSL <URL>/install-termux.sh | bash
#
# Yang dipasang:
#   Python + TUI + dependensi murni-Python (requirements-termux.txt),
#   Pillow/numpy/psutil dari paket Termux, Tesseract OCR (opsional),
#   Termux:API (opsional, untuk dikte & notifikasi), dan OpenCode Termux
#   (binary Android aarch64 dari guysoft/opencode-termux).
#
# Yang SENGAJA dilewati karena tak ada di Android/bionic:
#   Playwright/Chromium, Whisper + sounddevice, Ollama/Gemma vision, PyMuPDF.
#   Dikte memakai mesin STT Android (termux-speech-to-text); suara memakai
#   TTS Android (termux-tts-speak).
#
# Variabel lingkungan (opsional):
#   BAGASAI_REPO        URL repo alternatif
#   BAGASAI_BRANCH      cabang alternatif (default: master)
#   BAGASAI_SKIP_LOGIN  isi "1" untuk melewati wizard di akhir
# ============================================================================
set -euo pipefail
INSTALL_HOME="${BAGASAI_HOME:-${HOME}/.bagasai}"
mkdir -p "$INSTALL_HOME/logs" "$INSTALL_HOME/src"
INSTALL_HOME="$(cd "$INSTALL_HOME" && pwd -P)"
[ "$INSTALL_HOME" != "/" ] || { printf '%s\n' 'Direktori instalasi tidak valid.' >&2; exit 1; }
INSTALL_LOG="$INSTALL_HOME/logs/install-termux-$(date +%Y%m%d-%H%M%S).log"
export BAGASAI_INSTALL_LOG="$INSTALL_LOG"
STEP_INDEX=0

# ---------- Util & warna ----------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; RED=$'\033[31m'
  CYN=$'\033[36m'; MAG=$'\033[35m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; GRN=""; RED=""; CYN=""; MAG=""; YLW=""; RST=""
fi
say()  { printf '%s\n' "$*"; }
step() { STEP_INDEX=$((STEP_INDEX + 1)); printf '\n  %s%02d  %s%s\n' "$CYN" "$STEP_INDEX" "$*" "$RST"; }
ok()   { printf '  %s %s\n' "${GRN}✓${RST}" "$*"; }
warn() { printf '  %s %s\n' "${YLW}!${RST}" "$*"; }
err()  { printf '  %s %s\n' "${RED}✗${RST}" "$*" >&2; }
note() { printf '    %s\n' "${DIM}$*${RST}"; }
die()  { err "$*"; exit 1; }

REPO_URL="${BAGASAI_REPO:-https://github.com/ahmadadptr001/bagas-ai}"
REPO_URL="${REPO_URL%/}"
REPO_BRANCH="${BAGASAI_BRANCH:-master}"

printf '\n  %sbagas-ai%s\n  %sPemasangan untuk Termux/Android%s\n' "$CYN$BOLD" "$RST" "$DIM" "$RST"

# --- 0. Pastikan benar-benar Termux ---
DI_TERMUX=0
[ -n "${TERMUX_VERSION:-}" ] && DI_TERMUX=1
case "${PREFIX:-}" in *com.termux*) DI_TERMUX=1 ;; esac
if [ "$DI_TERMUX" != "1" ]; then
  warn "Lingkungan ini tidak terlihat seperti Termux."
  warn "Skrip ini memasang paket lewat 'pkg'; di Linux biasa gunakan ./install.sh."
  die "Dibatalkan agar tidak merusak sistem non-Termux."
fi
[ -n "${PREFIX:-}" ] || die "PREFIX Termux tidak ditemukan."
export PATH="$PREFIX/bin:$HOME/.local/bin:$PATH"

# --- 1. Paket dasar Termux ---
step "Paket dasar Termux"
pkg update -y >>"$INSTALL_LOG" 2>&1 || warn "pkg update gagal — lanjut dengan indeks yang ada."
pasang_pkg() {
  pkg install -y "$@" >>"$INSTALL_LOG" 2>&1 || warn "sebagian paket belum tersedia: $*"
}
pasang_pkg python clang make pkg-config openssl libffi git curl ripgrep unzip ffmpeg
pasang_pkg python-pillow python-numpy python-psutil
pasang_pkg tesseract
pasang_pkg tesseract-data-ind || true
pasang_pkg termux-api
command -v python >/dev/null 2>&1 || die "Python gagal dipasang lewat pkg."
ok "$(python --version 2>&1)"
note "Termux:API: pasang juga aplikasi 'Termux:API' dari F-Droid agar dikte/notifikasi hidup."

# --- 2. Dapatkan sumber kode ---
step "Kode sumber"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || pwd)"
SRC=""
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q "bagasai" "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
  SRC="$SCRIPT_DIR"
  ok "Sumber: folder saat ini"
else
  DEST="$INSTALL_HOME/src"
  if [ -f "$DEST/pyproject.toml" ] && [ -f "$DEST/src/agent/install_support.py" ]; then
    SRC="$DEST"
    ok "Sumber sudah tersedia"
  else
    command -v git >/dev/null 2>&1 || die "git tak tersedia — jalankan 'pkg install git' lalu ulangi."
    [ -e "$DEST" ] && mv "$DEST" "$INSTALL_HOME/src-lama-$(date +%Y%m%d-%H%M%S)-$$"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$DEST" >>"$INSTALL_LOG" 2>&1 \
      || die "Kode sumber gagal diunduh. Log: $INSTALL_LOG"
    SRC="$DEST"
    ok "Kode sumber diunduh"
  fi
fi
# --- 3. Cek kecocokan sistem + ketentuan ---
step "Pemeriksaan perangkat"
SISTEM_RC=0
python "$SRC/src/agent/sistem.py" || SISTEM_RC=$?
case "$SISTEM_RC" in
  0) ;;
  1) warn "Dibatalkan — tidak ada yang dipasang."; exit 0 ;;
  *) die "Sistem ini belum didukung — pemasangan dihentikan." ;;
esac

# --- 4. Pasang paket Python ---
step "Paket aplikasi"
python -m ensurepip --upgrade >>"$INSTALL_LOG" 2>&1 || true
python -m pip install --upgrade --quiet pip setuptools wheel >>"$INSTALL_LOG" 2>&1 \
  || warn "pip/setuptools/wheel belum dapat diperbarui — lanjut."
# --no-deps: pyproject.toml memuat dependensi desktop yang tak ada di Android.
# Dependensi yang benar-benar dipakai dipasang dari requirements-termux.txt.
python -m pip install --no-deps --no-build-isolation "$SRC" >>"$INSTALL_LOG" 2>&1 \
  || die "Paket bagasai gagal dipasang. Log: $INSTALL_LOG"
python -m pip install -r "$SRC/requirements-termux.txt" >>"$INSTALL_LOG" 2>&1 \
  || die "Dependensi Termux gagal dipasang. Log: $INSTALL_LOG"
ok "Paket Python siap"

# --- 5. Perintah global ---
step "Perintah terminal"
# pip Termux memasang skrip konsol ke $PREFIX/bin (sudah ada di PATH).
if command -v bagas-ai >/dev/null 2>&1; then
  ok "Perintah 'bagas-ai' siap dipakai"
else
  SCRIPT="$(python - <<'PY'
import importlib.metadata as M, os, sysconfig
try:
    d = M.distribution("bagasai")
    for f in (d.files or []):
        n = f.name.lower()
        if n in ("bagas-ai", "bagas-ai.exe"):
            print(os.path.dirname(os.path.realpath(d.locate_file(f))))
            raise SystemExit
except Exception:
    pass
print(sysconfig.get_path("scripts"))
PY
)"
  if [ -n "$SCRIPT" ] && [ -d "$SCRIPT" ]; then
    case ":$PATH:" in
      *":$SCRIPT:"*) ok "Perintah 'bagas-ai' siap dipakai" ;;
      *)
        printf '\n# bagas-ai\nexport PATH="%s:$PATH"\n' "$SCRIPT" >> "$HOME/.bashrc"
        export PATH="$SCRIPT:$PATH"
        ok "Menambahkan $SCRIPT ke ~/.bashrc"
        ;;
    esac
  else
    warn "Lokasi skrip 'bagas-ai' tak terbaca — cek dengan: python -m agent --help"
  fi
fi

# --- 6. OCR lokal ---
step "OCR lokal"
if command -v tesseract >/dev/null 2>&1; then
  ok "Tesseract tersedia"
else
  warn "Tesseract belum terpasang — OCR lokal (/image) nonaktif."
  note "pasang: pkg install tesseract tesseract-data-ind"
fi

# --- 7. OpenCode Termux (runtime model AI) ---
step "OpenCode"
if command -v opencode >/dev/null 2>&1; then
  ok "opencode sudah terpasang ($(opencode --version 2>/dev/null | head -n1 || printf 'versi tak terbaca'))"
else
  ARCH="$(uname -m)"
  if [ "$ARCH" != "aarch64" ]; then
    warn "OpenCode Termux resmi hanya tersedia untuk aarch64 (perangkat ini: $ARCH)."
    note "Pasang manual bila ada build untuk arsitektur ini."
  else
    note "mengunduh OpenCode Termux (Android aarch64)…"
    API="https://api.github.com/repos/guysoft/opencode-termux/releases/latest"
    URL=""
    if command -v curl >/dev/null 2>&1; then
      URL="$(curl -fsSL "$API" 2>/dev/null \
        | grep -o '"browser_download_url": *"[^"]*"' \
        | sed 's/.*"\(https[^"]*\)"/\1/' \
        | grep 'android-aarch64\.zip' | grep -v 'opencode2' | head -n1 || true)"
    fi
    # Cadangan bila API berubah: rilis yang pernah diverifikasi.
    [ -n "$URL" ] || URL="https://github.com/guysoft/opencode-termux/releases/download/v0.2.1/opencode-1.17.9-android-aarch64.zip"
    TMP_ZIP="$INSTALL_HOME/downloads/opencode-termux.zip"
    TMP_DIR="$INSTALL_HOME/downloads/opencode-termux"
    mkdir -p "$INSTALL_HOME/downloads"
    rm -rf "$TMP_DIR"; mkdir -p "$TMP_DIR"
    if curl -fsSL -o "$TMP_ZIP" "$URL" >>"$INSTALL_LOG" 2>&1 \
       && unzip -oq "$TMP_ZIP" -d "$TMP_DIR" >>"$INSTALL_LOG" 2>&1; then
      ROOT="$TMP_DIR"
      N_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
      N_FILE="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type f | wc -l | tr -d ' ')"
      if [ "$N_DIR" = "1" ] && [ "$N_FILE" = "0" ]; then
        ROOT="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n1)"
      fi
      cp -a "$ROOT/." "$PREFIX/bin/"
      chmod +x "$PREFIX/bin/opencode" "$PREFIX/bin/opencode.bin" 2>/dev/null || true
      if command -v opencode >/dev/null 2>&1; then
        ok "opencode terpasang"
      else
        warn "arsip terunduh tetapi binary opencode tak ditemukan."
      fi
    else
      warn "pemasangan opencode gagal — bagas-ai tetap terpasang."
      note "unduh manual: $URL"
    fi
  fi
fi

# --- 8. Wizard login (opsional) ---
if [ "${BAGASAI_SKIP_LOGIN:-0}" = "1" ]; then
  printf '\n'
  note "Wizard dilewati (BAGASAI_SKIP_LOGIN=1) — jalankan 'bagas-ai login' kapan pun."
else
  printf '\n'; step "Setup — bot Telegram (opsional)"
  if command -v bagas-ai >/dev/null 2>&1; then
    if [ -t 0 ]; then
      BAGASAI_DISCLAIMER_ACCEPTED=1 bagas-ai login || true
    elif [ -r /dev/tty ]; then
      note "stdin bukan terminal — wizard membaca dari /dev/tty"
      BAGASAI_DISCLAIMER_ACCEPTED=1 bagas-ai login < /dev/tty || true
    else
      warn "Tidak ada terminal interaktif — wizard dilewati."
      note "Jalankan 'bagas-ai login' nanti untuk menghubungkan bot Telegram."
    fi
  else
    warn "Perintah bagas-ai belum dikenali di sesi ini."
    note "Buka terminal baru, lalu jalankan: bagas-ai login"
  fi
fi

printf '\n'
say "${GRN}${BOLD}Selesai!${RST} Ketik ${CYN}${BOLD}bagas-ai${RST} di terminal mana pun untuk mulai."
note "Log: $INSTALL_LOG"
printf '\n'

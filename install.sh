#!/usr/bin/env bash
# ============================================================================
# bagasAI — installer satu-perintah (Linux / macOS / Git-Bash di Windows).
#
# Pakai salah satu:
#   ./install.sh                      # dari dalam folder proyek
#   curl -fsSL <URL>/install.sh | bash   # dari mana saja (mengunduh repo)
#
# Skrip ini: cek Python, memasang bagasAI sebagai perintah global, memastikan
# PATH, lalu menjalankan wizard login untuk memasukkan API key NVIDIA.
# ============================================================================
set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; RED=$'\033[31m'
CYN=$'\033[36m'; MAG=$'\033[35m'; RST=$'\033[0m'
say()  { printf "%s\n" "$*"; }
step() { printf "${MAG}${BOLD}» %s${RST}\n" "$*"; }
ok()   { printf "  ${GRN}✓${RST} %s\n" "$*"; }
err()  { printf "  ${RED}✗ %s${RST}\n" "$*" >&2; }

REPO_URL="${BAGASAI_REPO:-https://github.com/ahmadadptr/bagasai}"

printf "\n${MAG}${BOLD}bagasAI${RST} ${DIM}· installer${RST}\n\n"

# --- 1. Python 3.10+ ---
step "Memeriksa Python"
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  err "Butuh Python 3.10+. Pasang dari https://www.python.org/downloads/ lalu ulangi."
  exit 1
fi
ok "Python: $($PY --version 2>&1)"

# --- 2. Dapatkan sumber kode ---
# Jika sudah di folder proyek (ada pyproject.toml), pasang dari sini.
# Kalau tidak, clone repo ke ~/.bagasai/src.
SRC=""
if [ -f "pyproject.toml" ] && grep -q "bagasai" pyproject.toml 2>/dev/null; then
  SRC="$(pwd)"
  ok "Sumber: folder saat ini"
else
  step "Mengunduh bagasAI"
  DEST="${HOME}/.bagasai/src"
  if command -v git >/dev/null 2>&1; then
    rm -rf "$DEST"; mkdir -p "$DEST"
    git clone --depth 1 "$REPO_URL" "$DEST"
    SRC="$DEST"
    ok "Diunduh ke $DEST"
  else
    err "git tidak ada. Pasang git, atau jalankan install.sh dari dalam folder proyek."
    exit 1
  fi
fi

# --- 3. Pasang sebagai perintah global ---
step "Memasang bagasAI (pip install)"
INSTALLER=""
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SRC" && INSTALLER="pipx" || INSTALLER=""
fi
if [ -z "$INSTALLER" ]; then
  "$PY" -m pip install --user --upgrade "$SRC"
  INSTALLER="pip --user"
fi
ok "Terpasang via $INSTALLER"

# --- 4. Pastikan direktori bin/Scripts ada di PATH ---
step "Memeriksa PATH"
BIN_DIR="$("$PY" -c 'import site,sys,os; base=site.getuserbase(); print(os.path.join(base,"Scripts") if os.name=="nt" else os.path.join(base,"bin"))')"
if ! command -v bagasAI >/dev/null 2>&1; then
  case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *)
      SHELL_RC="${HOME}/.bashrc"; [ -n "${ZSH_VERSION:-}" ] && SHELL_RC="${HOME}/.zshrc"
      printf '\n# bagasAI\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$SHELL_RC"
      export PATH="$BIN_DIR:$PATH"
      ok "Menambahkan $BIN_DIR ke $SHELL_RC (buka terminal baru bila 'bagasAI' belum dikenali)"
      ;;
  esac
else
  ok "Perintah 'bagasAI' siap dipakai"
fi

# --- 5. Wizard login (API key NVIDIA + Telegram opsional) ---
printf "\n"; step "Login — masukkan API key NVIDIA"
if command -v bagasAI >/dev/null 2>&1; then
  bagasAI login || true
else
  "$PY" -m agent login || true
fi

printf "\n${GRN}${BOLD}Selesai.${RST} Ketik ${CYN}${BOLD}bagasAI${RST} di terminal mana pun untuk mulai.\n\n"

#!/usr/bin/env bash
# ============================================================================
# bagas-ai — installer satu-perintah (Linux / macOS / Git-Bash di Windows).
#
# Pakai salah satu:
#   ./install.sh                      # dari dalam folder proyek
#   curl -fsSL <URL>/install.sh | bash   # dari mana saja (mengunduh repo)
#
# Skrip ini: cek Python, memasang bagas-ai sebagai perintah global, memastikan
# PATH, lalu menjalankan wizard setup (bot Telegram opsional; tanpa API key).
# ============================================================================
set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; RED=$'\033[31m'
CYN=$'\033[36m'; MAG=$'\033[35m'; YLW=$'\033[33m'; RST=$'\033[0m'
say()  { printf "%s\n" "$*"; }
step() { printf "${MAG}${BOLD}» %s${RST}\n" "$*"; }
ok()   { printf "  ${GRN}✓${RST} %s\n" "$*"; }
err()  { printf "  ${RED}✗ %s${RST}\n" "$*" >&2; }

REPO_URL="${BAGASAI_REPO:-https://github.com/ahmadadptr001/bagas-ai}"
REPO_BRANCH="${BAGASAI_BRANCH:-master}"

printf "\n${MAG}${BOLD}bagas-ai${RST} ${DIM}· installer${RST}\n\n"

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
  step "Mengunduh bagas-ai"
  DEST="${HOME}/.bagasai/src"
  if command -v git >/dev/null 2>&1; then
    rm -rf "$DEST"; mkdir -p "$DEST"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$DEST"
    SRC="$DEST"
    ok "Diunduh ke $DEST"
  else
    err "git tidak ada. Pasang git, atau jalankan install.sh dari dalam folder proyek."
    exit 1
  fi
fi

# --- 3. Pasang sebagai perintah global ---
step "Memasang bagas-ai (pip install)"
INSTALLER=""
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SRC" && INSTALLER="pipx" || INSTALLER=""
fi
if [ -z "$INSTALLER" ]; then
  # Di Linux/macOS modern (PEP 668) 'pip install --user' bisa ditolak karena
  # environment "externally-managed". Coba normal dulu, lalu fallback.
  if "$PY" -m pip install --user --upgrade "$SRC" 2>/dev/null; then
    INSTALLER="pip --user"
  else
    "$PY" -m pip install --user --break-system-packages --upgrade "$SRC"
    INSTALLER="pip --user"
  fi
fi
ok "Terpasang via $INSTALLER"

# --- 3b. Browser Chromium untuk Playwright ---
# WAJIB: seluruh model bagas-ai berjalan lewat browser. Paket pip `playwright`
# hanya membawa pustakanya; binari browsernya harus diunduh terpisah. Tanpa
# langkah ini, model pertama yang dipilih akan gagal dengan pesan teknis.
step "Mengunduh browser Chromium (sekali saja, ~120 MB)"
if "$PY" -m playwright install chromium; then
  ok "Browser siap"
else
  warn "Gagal mengunduh Chromium — jalankan nanti: $PY -m playwright install chromium"
fi

# --- 4. Pastikan direktori bin/Scripts ada di PATH ---
step "Memeriksa PATH"
# Cari lokasi executable yang BENAR-BENAR terpasang (lebih andal daripada
# menebak dari getuserbase, mis. pada Python Store di Windows).
BIN_DIR="$("$PY" - <<'PY'
import importlib.metadata as M, os, site
def find():
    try:
        d = M.distribution("bagasai")
        for f in (d.files or []):
            n = f.name.lower()
            if n.startswith("bagas") and ("." not in n or n.endswith(".exe")):
                return os.path.dirname(os.path.realpath(d.locate_file(f)))
    except Exception:
        pass
    b = site.getuserbase()
    return os.path.join(b, "Scripts" if os.name == "nt" else "bin")
print(find())
PY
)"
# Deteksi Windows (Git Bash / MSYS / Cygwin): PATH harus diperbarui di REGISTRY
# Windows (User PATH), BUKAN di ~/.bashrc — kalau ke .bashrc, perintah cuma
# muncul di Git Bash dan tidak di PowerShell/cmd (dan hanya sesi bash BARU).
IS_WIN=false
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=true ;; esac

if command -v bagas-ai >/dev/null 2>&1; then
  ok "Perintah 'bagas-ai' siap dipakai"
elif $IS_WIN; then
  # BIN_DIR sudah bentuk Windows (C:\...) dari locator. Tambahkan ke User PATH
  # registry via PowerShell -> berlaku di SEMUA terminal Windows (PowerShell,
  # cmd, dan sesi Git Bash BARU yang mewarisi PATH Windows).
  PS="powershell"; command -v powershell >/dev/null 2>&1 || PS="powershell.exe"
  if command -v "$PS" >/dev/null 2>&1; then
    "$PS" -NoProfile -Command "\$d='$BIN_DIR'; \$p=[Environment]::GetEnvironmentVariable('Path','User'); if(\$null -eq \$p){\$p=''}; if((\$p -split ';') -notcontains \$d){ [Environment]::SetEnvironmentVariable('Path', ((\$p.TrimEnd(';'))+';'+\$d).TrimStart(';'), 'User') }" 2>/dev/null \
      && ok "Ditambahkan ke PATH Windows (User) — berlaku di PowerShell, cmd, & Git Bash baru." \
      || err "Gagal memperbarui PATH Windows; tambahkan '$BIN_DIR' ke PATH manual."
  else
    err "PowerShell tak ditemukan; tambahkan '$BIN_DIR' ke PATH Windows manual."
  fi
  # Perbarui juga sesi Git Bash SAAT INI (bentuk POSIX).
  if command -v cygpath >/dev/null 2>&1; then
    export PATH="$(cygpath -u "$BIN_DIR"):$PATH"
  else
    export PATH="$BIN_DIR:$PATH"
  fi
  say "  ${DIM}Buka terminal BARU bila 'bagas-ai' belum dikenali.${RST}"
else
  # Linux/macOS: tambahkan ke rc shell.
  case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *)
      case "${SHELL:-}" in
        */zsh)  SHELL_RC="${HOME}/.zshrc" ;;
        */bash) SHELL_RC="${HOME}/.bashrc" ;;
        *)      SHELL_RC="${HOME}/.profile" ;;
      esac
      printf '\n# bagas-ai\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$SHELL_RC"
      export PATH="$BIN_DIR:$PATH"
      ok "Menambahkan $BIN_DIR ke $SHELL_RC (buka terminal baru bila belum dikenali)"
      ;;
  esac
fi

# --- 5. Wizard setup (bot Telegram opsional; TIDAK ada API key) ---
# bagas-ai tak punya kredensial wajib: model dipilih lewat /model lalu login
# dilakukan sekali di jendela browser.
printf "\n"; step "Setup — bot Telegram (opsional)"
if command -v bagas-ai >/dev/null 2>&1; then
  bagas-ai login || true
else
  "$PY" -m agent login || true
fi

printf "\n${GRN}${BOLD}Selesai.${RST} Ketik ${CYN}${BOLD}bagas-ai${RST} di terminal mana pun untuk mulai.\n\n"

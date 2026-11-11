#!/usr/bin/env bash
# ============================================================================
# bagas-ai — installer satu-perintah (Linux / macOS / Git-Bash di Windows).
#
# Pakai salah satu:
#   ./install.sh                          # dari dalam folder proyek
#   curl -fsSL <URL>/install.sh | bash    # dari mana saja (mengunduh repo)
#
# Langkah: cek Python 3.10+ → dapatkan sumber (folder ini / git / ZIP) →
# cek kecocokan sistem (sistem.py) → pasang sebagai perintah global →
# unduh Chromium untuk Playwright → cek Brave → cek Tesseract OCR (OCR
# lokal /image) → rapikan PATH → cek/pasang opencode CLI →
# wizard login (opsional; tanpa API key).
#
# Variabel lingkungan (opsional):
#   BAGASAI_REPO        URL repo alternatif
#   BAGASAI_BRANCH      cabang alternatif (default: master)
#   BAGASAI_SKIP_LOGIN  isi "1" untuk melewati wizard di akhir
# ============================================================================
set -euo pipefail
INSTALL_HOME="${BAGASAI_HOME:-${HOME}/.bagasai}"
mkdir -p "$INSTALL_HOME/logs"
INSTALL_HOME="$(cd "$INSTALL_HOME" && pwd -P)"
[ "$INSTALL_HOME" != "/" ] || { printf '%s\n' 'Direktori instalasi tidak valid.' >&2; exit 1; }
INSTALL_LOG="$INSTALL_HOME/logs/install-$(date +%Y%m%d-%H%M%S).log"
export BAGASAI_INSTALL_LOG="$INSTALL_LOG"
STEP_INDEX=0

# ---------- Util & warna ----------
# Warna hanya bila output-nya terminal sungguhan (bukan pipe/log/CI) dan
# NO_COLOR kosong. Ditulis kompatibel bash 3.2 (bawaan macOS): tanpa array,
# tanpa &>, printf dengan pesan sebagai argumen (aman dari karakter '%').
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

# --- Termux/Android: jalur pemasangan sendiri ------------------------------
# Di Termux, `sys.platform` tetap "linux" tetapi bionic bukan glibc: wheel
# manylinux tak bisa dipakai, Playwright/Chromium tak ada, dan Ollama/Whisper
# tidak berjalan di Android. install-termux.sh menangani semua itu (paket
# `pkg`, requirements-termux.txt, OpenCode Android aarch64).
IS_TERMUX=0
[ -n "${TERMUX_VERSION:-}" ] && IS_TERMUX=1
case "${PREFIX:-}" in *com.termux*) IS_TERMUX=1 ;; esac
if [ "$IS_TERMUX" = "1" ]; then
  # Saat dijalankan lewat `curl | bash`, BASH_SOURCE kosong/$0 = "bash" —
  # jangan sampai file install-termux.sh acak di folder aktif ikut dieksekusi.
  SELF_DIR=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/install-termux.sh" ]; then
    printf '\n  %sTermux terdeteksi%s — beralih ke installer Android.\n' "$CYN$BOLD" "$RST"
    exec bash "$SELF_DIR/install-termux.sh" "$@"
  fi
  TERMUX_INSTALLER="$INSTALL_HOME/install-termux.sh"
  if command -v curl >/dev/null 2>&1; then
    printf '\n  %sTermux terdeteksi%s — mengunduh installer Android.\n' "$CYN$BOLD" "$RST"
    curl -fsSL -o "$TERMUX_INSTALLER" \
      "${REPO_URL}/raw/${REPO_BRANCH}/install-termux.sh" \
      || die "install-termux.sh gagal diunduh — periksa koneksi internet."
    exec bash "$TERMUX_INSTALLER" "$@"
  fi
  die "Termux terdeteksi tetapi install-termux.sh tak ada. Jalankan dari folder proyek: bash install-termux.sh"
fi

printf '\n  %sbagas-ai%s\n  %sPemasangan | komponen yang tersedia dipakai kembali%s\n' "$CYN$BOLD" "$RST" "$DIM" "$RST"

# --- 1. Python 3.10+ ---
step "Python"
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      PY="$c"; break
    fi
  fi
done
[ -n "$PY" ] || die "Butuh Python 3.10+. Pasang dari https://www.python.org/downloads/ lalu ulangi."
ok "$("$PY" --version 2>&1)"

# --- 2. Dapatkan sumber kode ---
# Kalau sudah di folder proyek (ada pyproject.toml), pasang dari sini.
# Kalau tidak: clone via git; tanpa git, unduh ZIP cabang lalu ekstrak
# (python dijamin ada — dipakai untuk ekstraksi, tak butuh unzip/tar).
SRC=""
step "Kode sumber"
if [ -f "pyproject.toml" ] && grep -q "bagasai" pyproject.toml 2>/dev/null; then
  SRC="$(pwd)"
  ok "Sumber: folder saat ini"
else
  DEST="$INSTALL_HOME/src"
  SOURCE_ID="$REPO_URL#$REPO_BRANCH"
  for candidate in "$DEST" "$DEST"/*; do
    [ -f "$candidate/pyproject.toml" ] && [ -f "$candidate/src/agent/install_support.py" ] || continue
    if [ -f "$candidate/.bagasai-source-id" ]; then
      [ "$(cat "$candidate/.bagasai-source-id")" = "$SOURCE_ID" ] || continue
    elif [ -n "${BAGASAI_REPO:-}${BAGASAI_BRANCH:-}" ]; then
      continue
    fi
    SRC="$candidate"; break
  done
  if [ -n "$SRC" ]; then
    ok "Kode sumber sudah tersedia"
  else
    if [ -e "$DEST" ]; then
      mv "$DEST" "$INSTALL_HOME/src-lama-$(date +%Y%m%d-%H%M%S)-$$"
    fi
    if command -v git >/dev/null 2>&1; then
      git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$DEST" >>"$INSTALL_LOG" 2>&1 \
        || die "Kode sumber gagal diunduh. Log: $INSTALL_LOG"
      SRC="$DEST"
    else
    note "git tidak ada — memakai unduhan ZIP"
    ZIP_URL="${REPO_URL}/archive/${REPO_BRANCH}.zip"
    mkdir -p "$INSTALL_HOME/downloads"
    CACHE_KEY="$("$PY" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$SOURCE_ID")"
    ZIP_FILE="$INSTALL_HOME/downloads/source-$CACHE_KEY.zip"
    if ! "$PY" -c 'import sys,zipfile; raise SystemExit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$ZIP_FILE"; then
      if command -v curl >/dev/null 2>&1; then
      curl -fsSL -o "${ZIP_FILE}.part" "$ZIP_URL" \
        || die "Unduhan gagal — periksa koneksi internet."
      elif command -v wget >/dev/null 2>&1; then
      wget -qO "${ZIP_FILE}.part" "$ZIP_URL" \
        || die "Unduhan gagal — periksa koneksi internet."
      else
      if ! "$PY" - "$ZIP_URL" "${ZIP_FILE}.part" <<'PYEOF'
import sys, urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PYEOF
      then die "Unduhan gagal — periksa koneksi internet."; fi
      fi
      mv "${ZIP_FILE}.part" "$ZIP_FILE"
    fi
    "$PY" -m zipfile -e "$ZIP_FILE" "$DEST" || die "Ekstraksi ZIP gagal."
    # GitHub mengekstrak ke <repo>-<cabang>; ambil folder pertama apa adanya.
    SRC="$(find "$DEST" -mindepth 1 -maxdepth 1 -type d | head -n 1 || true)"
    [ -n "$SRC" ] || die "Isi ZIP tidak terduga — ekstraksi gagal."
    fi
    printf '%s\n' "$SOURCE_ID" > "$SRC/.bagasai-source-id"
    ok "Kode sumber siap"
  fi
fi
SUPPORT="$SRC/src/agent/install_support.py"

# --- 2b. Cek kecocokan sistem (sebelum apa pun dipasang) ---
# sistem.py: OS/arsitektur/RAM/disk/Python/internet + Ketentuan & Kebijakan,
# lalu konfirmasi lanjut/batal sebelum pip install. Keluar 1 = dibatalkan
# pengguna (bukan galat); 2 = sistem tak didukung. set -e akan menelan exit
# code-nya, jadi tangkap dulu.
step "Pemeriksaan perangkat"
SISTEM_RC=0
"$PY" "${SRC}/src/agent/sistem.py" || SISTEM_RC=$?
case "$SISTEM_RC" in
  0) ;;
  1) warn "Dibatalkan — tidak ada yang dipasang."; exit 0 ;;
  *) die "Sistem ini belum didukung — pemasangan dihentikan." ;;
esac
# --- 3. Pasang sebagai perintah global ---
step "Paket aplikasi"

# pip --user ditolak di dalam venv ("user site-packages are not visible"),
# jadi flag-nya dilepas bila installer dijalankan dari venv aktif.
USER_FLAG="--user"
if [ -n "${VIRTUAL_ENV:-}" ]; then USER_FLAG=""; fi

PW_PY="$PY"
INSTALLER="pip"
if command -v pipx >/dev/null 2>&1 && [ -z "${VIRTUAL_ENV:-}" ]; then
  PIPX_HOME_DIR="$(pipx environment --value PIPX_HOME 2>/dev/null || printf '%s' "${HOME}/.local/pipx")"
  for cand in "${PIPX_HOME_DIR}/venvs/bagasai/bin/python" \
              "${PIPX_HOME_DIR}/venvs/bagasai/Scripts/python.exe"; do
    if [ -x "$cand" ]; then PW_PY="$cand"; INSTALLER="pipx"; USER_FLAG=""; break; fi
  done
  # pipx yang sudah ada dipakai kembali; --force tidak pernah diperlukan.
  if [ "$INSTALLER" != "pipx" ] && ! "$PY" -c 'import importlib.metadata as m; m.distribution("bagasai")' >/dev/null 2>&1; then
    if pipx install "$SRC" >>"$INSTALL_LOG" 2>&1; then
      for cand in "${PIPX_HOME_DIR}/venvs/bagasai/bin/python" "${PIPX_HOME_DIR}/venvs/bagasai/Scripts/python.exe"; do
        if [ -x "$cand" ]; then PW_PY="$cand"; INSTALLER="pipx"; USER_FLAG=""; break; fi
      done
    fi
  fi
fi
if ! "$PW_PY" "$SUPPORT" python --source "$SRC" $USER_FLAG; then
  if [ "$INSTALLER" = "pipx" ] || ! "$PW_PY" "$SUPPORT" python --source "$SRC" $USER_FLAG --break-system-packages; then
    die "Paket aplikasi belum siap. Log: $INSTALL_LOG"
  fi
fi

# --- 3c. Pengenal suara lokal ---
step "Pengenal suara"
# Helper menjalankan --prepare-model dengan cache lokal terlebih dahulu.
"$PW_PY" "$SUPPORT" voice \
  || die "Whisper lokal gagal dipasang atau dimuat."

# --- 3d. Browser Chromium untuk Playwright ---
# WAJIB: seluruh model bagas-ai berjalan lewat browser. Paket pip `playwright`
# hanya membawa pustakanya; binari browsernya harus diunduh terpisah. Tanpa
# langkah ini, model pertama yang dipilih akan gagal dengan pesan teknis.
#
# Pakai interpreter yang SUDAH punya playwright: kalau dipasang via pipx,
# paketnya hidup di venv pipx, bukan di Python sistem.
step "Browser Chromium"
if "$PW_PY" "$SUPPORT" browser; then
  :
else
  warn "Gagal mengunduh Chromium — jalankan nanti:"
  note "$PW_PY -m playwright install chromium"
  if [ "$(uname -s 2>/dev/null)" = "Linux" ] && [ "$(id -u)" != "0" ]; then
    note "di Linux, bila Chromium mati karena pustaka sistem kurang:"
    note "sudo $PW_PY -m playwright install-deps chromium"
  fi
fi

# --- 3c. Brave: browser yang DIPAKAI sehari-hari ---
# Chromium di atas cuma jaring pengaman terakhir. Yang dijalankan connector
# adalah browser ASLI yang terpasang di mesin ini, dan bawaannya Brave
# (CONNECTOR_BROWSER_CHANNEL). Chromium bundel Playwright paling sering
# diblok situs, jadi ia tak boleh jadi pilihan sehari-hari.
#
# TIDAK dipasang diam-diam di sini. Di Linux, memasang browser berarti
# menambah repo APT/RPM pihak ketiga ke sistem — perubahan yang terlalu besar
# untuk dikerjakan tanpa ditanya. Yang dilakukan cuma memeriksa & menunjukkan
# perintahnya. bagas-ai tetap jalan tanpanya: ia memakai Chrome/Chromium yang
# sudah ada (lihat _pilih_exe).
step "Browser sistem"
if command -v brave-browser >/dev/null 2>&1 \
   || command -v brave >/dev/null 2>&1 \
   || [ -x "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" ]; then
  ok "Brave sudah ada"
else
  warn "Brave belum terpasang - bagas-ai akan memakai Chrome/Chromium."
  case "$(uname -s)" in
    Darwin) note "pasang: brew install --cask brave-browser" ;;
    *)      note "pasang: https://brave.com/linux/" ;;
  esac
fi

# --- 3d. Tesseract OCR (untuk OCR lokal /image & read_image_local) ---
# Opsional: tanpanya /image tetap jalan, hanya bagian "OCR lokal" kosong.
# Tidak dipasang diam-diam: butuh sudo (apt) atau Homebrew yang belum tentu
# ada — cukup periksa & tunjukkan perintahnya.
step "OCR lokal"
if command -v tesseract >/dev/null 2>&1; then
  ok "Tesseract sudah tersedia"
else
  warn "Tesseract belum terpasang - OCR lokal (/image) akan nonaktif."
  case "$(uname -s)" in
    Darwin) note "pasang: brew install tesseract" ;;
    *)      note "pasang: sudo apt install tesseract-ocr  (atau manajer paket distro-mu)" ;;
  esac
fi

# --- 3e. Ollama + Gemma 3 4B (WAJIB untuk vision lokal) ---
step "Model gambar lokal"
if ! command -v ollama >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then brew install --cask ollama >>"$INSTALL_LOG" 2>&1 || true; fi ;;
    Linux)
      curl -fsSL https://ollama.com/install.sh | sh >>"$INSTALL_LOG" 2>&1 || true ;;
  esac
fi
command -v ollama >/dev/null 2>&1 || die "Ollama wajib tetapi gagal dipasang. Pasang dari https://ollama.com/download lalu jalankan installer lagi."
"$PW_PY" "$SUPPORT" vision || die "Model gambar belum siap. Log: $INSTALL_LOG"

# --- 4. Pastikan direktori bin/Scripts ada di PATH ---
step "Perintah terminal"
# Cari lokasi executable yang BENAR-BENAR terpasang (lebih andal daripada
# menebak dari getuserbase, mis. pada Python Store di Windows).
BIN_DIR=""
if [ "$INSTALLER" = "pipx" ]; then
  BIN_DIR="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
fi
if [ -z "$BIN_DIR" ] || [ ! -d "$BIN_DIR" ]; then
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
fi
[ -n "$BIN_DIR" ] || die "Tak bisa menentukan folder bin hasil instalasi."

IS_WIN=false
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=true ;; esac

# Bentuk POSIX dari BIN_DIR untuk perbandingan/sesi Git Bash.
BIN_POSIX="$BIN_DIR"
if $IS_WIN && command -v cygpath >/dev/null 2>&1; then
  BIN_POSIX="$(cygpath -u "$BIN_DIR" 2>/dev/null || printf '%s' "$BIN_DIR")"
fi

path_has_bin() {
  case ":$PATH:" in
    *":$BIN_DIR:"*|*":$BIN_POSIX:"*) return 0 ;;
  esac
  return 1
}

if path_has_bin; then
  ok "Perintah 'bagas-ai' siap dipakai"
elif $IS_WIN; then
  # Windows (Git Bash): PATH harus diperbarui di REGISTRY (User PATH), bukan
  # di ~/.bashrc — kalau ke .bashrc, perintah cuma muncul di Git Bash dan
  # tidak di PowerShell/cmd. $BIN_DIR dikirim lewat variabel lingkungan agar
  # bebas masalah quoting, dan nilai registry ditulis mentah (DoNotExpand) supaya
  # entri ber-%VARIABEL% tidak ter-bake.
  PS="powershell.exe"
  command -v powershell.exe >/dev/null 2>&1 || PS="powershell"
  command -v "$PS" >/dev/null 2>&1 || PS="pwsh"
  if command -v "$PS" >/dev/null 2>&1; then
    if BAGASAI_BINDIR="$BIN_DIR" "$PS" -NoProfile -Command '
      $d = $env:BAGASAI_BINDIR
      $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
      $o = [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
      $v = [string]$k.GetValue("Path", $null, $o)
      $kind = if ($v) { $k.GetValueKind("Path") } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
      $parts = @($v -split ";" | Where-Object { $_ })
      $hit = @($parts | Where-Object { "$_".TrimEnd("\") -ieq $d.TrimEnd("\") })
      if ($hit.Count -eq 0) {
        $k.SetValue("Path", (($parts + $d) -join ";"), $kind)
        Add-Type -Namespace BagasAI -Name NM -MemberDefinition "[DllImport(`"user32.dll`",SetLastError=true,CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, UIntPtr w, string l, uint f, uint t, out UIntPtr r);"
        [UIntPtr]$r = [UIntPtr]::Zero
        [BagasAI.NM]::SendMessageTimeout([IntPtr]0xFFFF, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$r) | Out-Null
      }
      $k.Close()
    ' >/dev/null 2>&1; then
      ok "Ditambahkan ke PATH Windows (User) — berlaku di PowerShell, cmd, & Git Bash baru."
    else
      err "Gagal memperbarui PATH Windows; tambahkan ini secara manual:"
      note "$BIN_DIR"
    fi
  else
    err "PowerShell tak ditemukan; tambahkan '$BIN_DIR' ke PATH Windows manual."
  fi
  export PATH="$BIN_POSIX:$PATH"
  hash -r 2>/dev/null || true
  note "Buka terminal BARU bila 'bagas-ai' belum dikenali."
else
  # Linux/macOS: tambahkan ke rc shell bila belum ada.
  SHELL_RC="${HOME}/.profile"
  case "${SHELL:-}" in
    */zsh)  SHELL_RC="${HOME}/.zshrc" ;;
    */bash) SHELL_RC="${HOME}/.bashrc" ;;
  esac
  if grep -qsF "export PATH=\"$BIN_DIR" "$SHELL_RC" 2>/dev/null; then
    ok "Sudah ada di $SHELL_RC"
  else
    printf '\n# bagas-ai\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$SHELL_RC"
    ok "Menambahkan $BIN_DIR ke $SHELL_RC"
  fi
  export PATH="$BIN_DIR:$PATH"
  note "Buka terminal baru (atau: source $SHELL_RC) bila 'bagas-ai' belum dikenali."
fi

# --- 4b. OpenCode CLI: runtime model AI ---
step "OpenCode"
if command -v opencode >/dev/null 2>&1; then
  ok "opencode sudah terpasang"
else
  note "memasang opencode lewat skrip resminya..."
  if curl -fsSL https://opencode.ai/install | bash >>"$INSTALL_LOG" 2>&1; then
    ok "opencode terpasang"
  else
    warn "pemasangan opencode gagal — bagas-ai tetap terpasang."
    note "pasang manual nanti: curl -fsSL https://opencode.ai/install | bash"
  fi
fi

# --- 5. Wizard setup (API key NVIDIA / OpenRouter; bot Telegram opsional) ---
# bagas-ai tak punya kredensial wajib: model dipilih lewat /model lalu login
# dilakukan sekali di jendela browser.
if [ "${BAGASAI_SKIP_LOGIN:-0}" = "1" ]; then
  printf '\n'
  note "Wizard dilewati (BAGASAI_SKIP_LOGIN=1) — jalankan 'bagas-ai login' kapan pun."
else
  printf '\n'; step "Setup — bot Telegram (opsional)"
  if command -v bagas-ai >/dev/null 2>&1; then
    if [ -t 0 ]; then
      BAGASAI_DISCLAIMER_ACCEPTED=1 bagas-ai login || true
    elif [ -r /dev/tty ]; then
      # curl|bash: skrip dibaca dari stdin, jadi wizard TIDAK boleh membaca
      # stdin (langsung EOF) — arahkan ke terminal asli.
      note "stdin bukan terminal — wizard membaca dari /dev/tty"
      BAGASAI_DISCLAIMER_ACCEPTED=1 bagas-ai login < /dev/tty || true
    else
      warn "Tidak ada terminal interaktif — wizard dilewati."
      note "Jalankan 'bagas-ai login' nanti untuk menghubungkan bot Telegram."
    fi
  else
    if [ -t 0 ]; then
      BAGASAI_DISCLAIMER_ACCEPTED=1 "$PY" -m agent login || true
    elif [ -r /dev/tty ]; then
      BAGASAI_DISCLAIMER_ACCEPTED=1 "$PY" -m agent login < /dev/tty || true
    else
      warn "Tidak ada terminal interaktif — wizard dilewati."
      note "Jalankan 'bagas-ai login' nanti untuk menghubungkan bot Telegram."
    fi
  fi
fi

printf '\n'
say "${GRN}${BOLD}Selesai!${RST} Ketik ${CYN}${BOLD}bagas-ai${RST} di terminal mana pun untuk mulai."
note "Log: $INSTALL_LOG"
printf '\n'

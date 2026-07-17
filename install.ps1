# ============================================================================
# bagasAI - installer satu-perintah untuk Windows (PowerShell).
#
# Pakai salah satu:
#   .\install.ps1                     # dari dalam folder proyek
#   irm <URL>/install.ps1 | iex       # dari mana saja (mengunduh repo)
#
# Skrip ini: cek Python, memasang bagasAI sebagai perintah global, memastikan
# PATH, lalu menjalankan wizard login untuk memasukkan API key.
# ============================================================================
$ErrorActionPreference = "Stop"

function Step($m) { Write-Host "> $m" -ForegroundColor Magenta }
function Ok($m)   { Write-Host "  + $m" -ForegroundColor Green }
function Err($m)  { Write-Host "  x $m" -ForegroundColor Red }

$RepoUrl = if ($env:BAGASAI_REPO) { $env:BAGASAI_REPO } else { "https://github.com/ahmadadptr001/bagas-ai" }
$RepoBranch = if ($env:BAGASAI_BRANCH) { $env:BAGASAI_BRANCH } else { "master" }

Write-Host ""
Write-Host "bagasAI " -ForegroundColor Magenta -NoNewline
Write-Host "- installer" -ForegroundColor DarkGray
Write-Host ""

# --- 1. Python 3.10+ ---
Step "Memeriksa Python"
$Py = $null
foreach ($c in @("python", "py", "python3")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        & $c -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $Py = $c; break }
    }
}
if (-not $Py) {
    Err "Butuh Python 3.10+. Pasang dari https://www.python.org/downloads/ (centang 'Add to PATH') lalu ulangi."
    exit 1
}
Ok "Python: $(& $Py --version)"

# --- 2. Dapatkan sumber kode ---
$Src = $null
if ((Test-Path "pyproject.toml") -and (Select-String -Path "pyproject.toml" -Pattern "bagasai" -Quiet)) {
    $Src = (Get-Location).Path
    Ok "Sumber: folder saat ini"
} else {
    Step "Mengunduh bagasAI"
    $Dest = Join-Path $HOME ".bagasai\src"
    if (Get-Command git -ErrorAction SilentlyContinue) {
        if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
        git clone --depth 1 --branch $RepoBranch $RepoUrl $Dest
        $Src = $Dest
        Ok "Diunduh ke $Dest"
    } else {
        Err "git tidak ada. Pasang git, atau jalankan install.ps1 dari dalam folder proyek."
        exit 1
    }
}

# --- 3. Pasang sebagai perintah global ---
Step "Memasang bagasAI (pip install)"
# Pastikan pip ada dulu (sebagian Python Store/venv memicu 'No module named pip').
& $Py -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  pip belum ada - memasang via ensurepip..." -ForegroundColor DarkGray
    & $Py -m ensurepip --upgrade --default-pip 2>$null
}
& $Py -m pip install --user --upgrade $Src
if ($LASTEXITCODE -ne 0) {
    # Coba sekali lagi setelah memastikan pip (jaga-jaga 'No module named pip').
    & $Py -m ensurepip --upgrade --default-pip 2>$null
    & $Py -m pip install --user --upgrade $Src
    if ($LASTEXITCODE -ne 0) { Err "pip install gagal."; exit 1 }
}
Ok "Terpasang"

# --- 4. Pastikan folder Scripts ada di PATH (user) ---
# Cari lokasi .exe yang BENAR-BENAR terpasang (penting untuk Python Store yang
# menaruh script di folder tak terduga), bukan sekadar menebak dari getuserbase.
Step "Memeriksa PATH"
# Skrip locator ditulis ke file temporer lalu dijalankan. JANGAN dioper via
# `python -c "<multi-baris>"`: PowerShell 5.1 menghapus tanda kutip di dalam
# argumen multi-baris untuk program native -> Python jadi 'invalid syntax'.
$Locate = @'
import importlib.metadata as M, os, site, sysconfig, glob
def find():
    cands = []
    # 1) Lokasi .exe yang BENAR-BENAR tercatat saat install (paling andal).
    try:
        d = M.distribution("bagasai")
        for f in (d.files or []):
            n = f.name.lower()
            if n.startswith("bagas") and n.endswith(".exe"):
                cands.append(os.path.dirname(os.path.realpath(d.locate_file(f))))
    except Exception:
        pass
    # 2) Skema sysconfig (user & default).
    for sch in ("nt_user", "nt"):
        try:
            p = sysconfig.get_path("scripts", sch)
            if p:
                cands.append(p)
        except Exception:
            pass
    ub = site.getuserbase()
    # 3) Python Store menaruh script di local-packages\PythonXX\Scripts
    #    (BUKAN getuserbase\Scripts) -> cari lewat glob.
    cands += glob.glob(os.path.join(ub, "Python*", "Scripts"))
    cands.append(os.path.join(ub, "Scripts"))
    uniq = []
    for c in cands:
        if c and c not in uniq:
            uniq.append(c)
    # Utamakan folder yang MEMANG berisi bagas*.exe.
    for c in uniq:
        if glob.glob(os.path.join(c, "bagas*.exe")):
            return c
    for c in uniq:
        if os.path.isdir(c):
            return c
    return uniq[0] if uniq else ""
print(find())
'@
$LocateFile = Join-Path $env:TEMP ("bagasai_locate_" + [guid]::NewGuid().ToString("N") + ".py")
Set-Content -Path $LocateFile -Value $Locate -Encoding UTF8
$BinDir = ""
try {
    $out = & $Py $LocateFile 2>$null | Select-Object -Last 1
    if ($out) { $BinDir = "$out".Trim() }
} catch {
    # abaikan - akan pakai fallback di bawah
} finally {
    Remove-Item $LocateFile -ErrorAction SilentlyContinue
}
if (-not $BinDir) {
    $fb = & $Py -c "import site,os; print(os.path.join(site.getuserbase(),'Scripts'))" 2>$null | Select-Object -Last 1
    if ($fb) { $BinDir = "$fb".Trim() }
}
if (-not $BinDir) {
    Err "Tak bisa menentukan folder Scripts. Tambahkan folder Scripts Python ke PATH secara manual."
} elseif (-not (Get-Command bagasAI -ErrorAction SilentlyContinue)) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { $userPath = "" }
    if ($userPath.Split(';') -notcontains $BinDir) {
        $newPath = ($userPath.TrimEnd(';') + ";" + $BinDir).TrimStart(';')
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        $env:Path = "$env:Path;$BinDir"
        Ok "Menambahkan $BinDir ke PATH (user). Buka terminal baru bila 'bagasAI' belum dikenali."
    } else {
        $env:Path = "$env:Path;$BinDir"
        Ok "Perintah 'bagasAI' sudah ada di PATH. Buka terminal baru bila belum dikenali."
    }
} else {
    Ok "Perintah 'bagasAI' siap dipakai"
}

# --- 5. Wizard login (API key + Telegram opsional) ---
Write-Host ""
Step "Login - masukkan API key"
$bagas = Get-Command bagasAI -ErrorAction SilentlyContinue
if ($bagas) { & bagasAI login } else { & $Py -m agent login }

Write-Host ""
Write-Host "Selesai. " -ForegroundColor Green -NoNewline
Write-Host "Ketik " -NoNewline
Write-Host "bagasAI" -ForegroundColor Cyan -NoNewline
Write-Host " di terminal mana pun untuk mulai."
Write-Host ""

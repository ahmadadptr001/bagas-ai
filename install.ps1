# ============================================================================
# bagas-ai - installer satu-perintah untuk Windows (PowerShell 5.1+).
#
# Pakai salah satu:
#   .\install.ps1                     # dari dalam folder proyek
#   irm <URL>/install.ps1 | iex       # dari mana saja (mengunduh repo)
#
# Langkah: cek Python 3.10+ -> dapatkan sumber (folder ini / git / ZIP) ->
# cek kecocokan sistem (sistem.py) -> pip install -> unduh Chromium untuk
# Playwright -> cek Brave -> pasang Tesseract OCR (OCR lokal /image) ->
# rapikan PATH (registry) -> cek/pasang opencode CLI -> wizard
# login (opsional; tanpa API key).
#
# Variabel lingkungan (opsional):
#   BAGASAI_REPO        URL repo alternatif
#   BAGASAI_BRANCH      cabang alternatif (default: master)
#   BAGASAI_SKIP_LOGIN  =1 untuk melewati wizard di akhir
#
# Catatan: skrip ini sengaja ASCII murni - PowerShell 5.1 membaca berkas
# tanpa BOM sebagai ANSI, jadi karakter non-ASCII akan tampil mojibake.
# ============================================================================
$ErrorActionPreference = "Stop"
$InstallHome = if ($env:BAGASAI_HOME) { $env:BAGASAI_HOME } else { Join-Path $HOME ".bagasai" }
$InstallHome = [IO.Path]::GetFullPath($InstallHome)
$LogDir = Join-Path $InstallHome "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$InstallLog = Join-Path $LogDir ("install-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
$env:BAGASAI_INSTALL_LOG = $InstallLog
$StepIndex = 0

# PS 5.1: unduhan dari GitHub butuh TLS 1.2 (di PS lama belum jadi default).
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

function Step($m) {
    $script:StepIndex += 1
    Write-Host ""
    Write-Host ("  {0:00}  {1}" -f $script:StepIndex, $m) -ForegroundColor Cyan
}
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Note($m) { Write-Host "        $m" -ForegroundColor DarkGray }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Err($m)  { Write-Host "  [x]  $m" -ForegroundColor Red }

# Jalankan program native dengan stderr dibuang. WAJIB lewat helper ini,
# JANGAN tulis `& exe ... 2>$null` langsung: di PS 5.1, redirect stderr
# mengubah baris stderr program menjadi ErrorRecord, dan dengan
# $ErrorActionPreference = "Stop" baris PERTAMA (mis. warning pip) langsung
# mematikan installer. Di dalam helper, EAP diturunkan dulu ke Continue.
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Exe @Arguments >> $InstallLog 2>&1; $rc = $LASTEXITCODE }
    finally { $ErrorActionPreference = $prev }
    return $rc
}

# Sama seperti Invoke-Quiet, tapi mengembalikan stdout (bukan exit code).
function Invoke-Captured([string]$Exe, [string[]]$Arguments) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & $Exe @Arguments 2>$null } finally { $ErrorActionPreference = $prev }
    return $out
}

# Seperti Invoke-Quiet, tapi output stdoutnya DIBIARKAN tampil di konsol dan
# hanya exit code yang dikembalikan. WAJIB untuk program yang mencetak lalu
# meminta input (mis. sistem.py): pakai Invoke-Quiet malah menelan outputnya
# ke variabel, dan `switch` pada variabel-array itu terbaca salah.
function Invoke-Live([string]$Exe, [string[]]$Arguments) {
    # Start-Process mewarisi console secara langsung. Ini penting karena hasil
    # fungsi ditampung ke `$SistemRc`; pemanggilan `&` biasa membuat stdout
    # Python ikut tertangkap dan prompt baru terlihat sesudah proses selesai.
    try {
        $quoted = @($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ })
        $p = Start-Process -FilePath $Exe -ArgumentList $quoted `
            -NoNewWindow -Wait -PassThru
        return $p.ExitCode
    } catch {
        Err "Gagal menjalankan pemeriksaan sistem: $($_.Exception.Message)"
        return 2
    }
}

function ConvertTo-NativeArgument([string]$Value) {
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    $Value = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $Value = [regex]::Replace($Value, '(\\+)$', '$1$1')
    return '"' + $Value + '"'
}

function Assert-CachePath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $root = $InstallHome.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Lokasi cache di luar direktori instalasi: $resolved"
    }
    return $resolved
}

$RepoUrl = if ($env:BAGASAI_REPO) { $env:BAGASAI_REPO } else { "https://github.com/ahmadadptr001/bagas-ai" }
$RepoUrl = "$RepoUrl".TrimEnd('/')
$RepoBranch = if ($env:BAGASAI_BRANCH) { $env:BAGASAI_BRANCH } else { "master" }

Write-Host ""
Write-Host "  bagas-ai" -ForegroundColor Cyan
Write-Host "  Pemasangan | komponen yang tersedia dipakai kembali" -ForegroundColor DarkGray
Write-Host ""

# --- 1. Python 3.10+ ---
Step "Python"
$Py = $null
$PyCheck = "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)"
foreach ($c in @("python", "py", "python3")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        if ((Invoke-Quiet $c @("-c", $PyCheck)) -eq 0) { $Py = $c; break }
    }
}
if (-not $Py) {
    Err "Butuh Python 3.10+. Pasang dari https://www.python.org/downloads/"
    Err "(centang 'Add to PATH' saat memasang) lalu ulangi."
    exit 1
}
Ok "$(& $Py --version)"

# --- 2. Dapatkan sumber kode ---
$Src = $null
Step "Kode sumber"
if ((Test-Path "pyproject.toml") -and (Select-String -Path "pyproject.toml" -Pattern "bagasai" -Quiet)) {
    $Src = (Get-Location).Path
    Ok "Sumber: folder saat ini"
} else {
    $Dest = Assert-CachePath (Join-Path $InstallHome "src")
    $SourceId = "$RepoUrl#$RepoBranch"
    $Candidates = @($Dest)
    if (Test-Path -LiteralPath $Dest) {
        $Candidates += @(Get-ChildItem -LiteralPath $Dest -Directory | Select-Object -ExpandProperty FullName)
    }
    foreach ($candidate in $Candidates) {
        if (-not (Test-Path -LiteralPath (Join-Path $candidate "pyproject.toml"))) { continue }
        if (-not (Test-Path -LiteralPath (Join-Path $candidate "src\agent\install_support.py"))) { continue }
        $marker = Join-Path $candidate ".bagasai-source-id"
        if (Test-Path -LiteralPath $marker) {
            if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne $SourceId) { continue }
        } elseif ($env:BAGASAI_REPO -or $env:BAGASAI_BRANCH) { continue }
        $Src = $candidate
        break
    }
    if ($Src) {
        Ok "Kode sumber sudah tersedia"
    } else {
      if (Test-Path -LiteralPath $Dest) {
        # Pertahankan cache lama; jangan menghapus checkout/aset pengguna.
        $SavedSource = Assert-CachePath (Join-Path $InstallHome ("src-lama-" + [guid]::NewGuid().ToString("N")))
        Move-Item -LiteralPath $Dest -Destination $SavedSource
      }
      if (Get-Command git -ErrorAction SilentlyContinue) {
        $rc = Invoke-Quiet git @("clone", "--depth", "1", "--branch", $RepoBranch, $RepoUrl, $Dest)
        if ($rc -ne 0) { Err "Kode sumber gagal diunduh. Log: $InstallLog"; exit 1 }
        $Src = $Dest
      } else {
        # Tanpa git: unduh ZIP cabang dari GitHub lalu ekstrak.
        Note "git tidak ada - memakai unduhan ZIP"
        $ZipUrl = "$RepoUrl/archive/$RepoBranch.zip"
        $Downloads = Assert-CachePath (Join-Path $InstallHome "downloads")
        New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
        $sha = [Security.Cryptography.SHA256]::Create()
        $CacheKey = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($SourceId)))).Replace("-", "").Substring(0, 12)
        $sha.Dispose()
        $ZipFile = Join-Path $Downloads ("source-" + $CacheKey + ".zip")
        try {
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $ZipReady = $false
            if (Test-Path -LiteralPath $ZipFile) {
                try { $archive = [IO.Compression.ZipFile]::OpenRead($ZipFile); $archive.Dispose(); $ZipReady = $true } catch {}
            }
            if (-not $ZipReady) {
                Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl -OutFile ($ZipFile + ".part")
                Move-Item -LiteralPath ($ZipFile + ".part") -Destination $ZipFile -Force
            }
            Expand-Archive -LiteralPath $ZipFile -DestinationPath $Dest
        } catch {
            Err "Unduhan/ekstraksi gagal: $($_.Exception.Message)"
            Err "URL: $ZipUrl"
            exit 1
        }
        # GitHub mengekstrak ke <repo>-<cabang>; ambil folder pertama.
        $dir = Get-ChildItem -LiteralPath $Dest -Directory | Select-Object -First 1
        if (-not $dir) { Err "Isi ZIP tidak terduga."; exit 1 }
        $Src = $dir.FullName
      }
      Set-Content -LiteralPath (Join-Path $Src ".bagasai-source-id") -Value $SourceId -Encoding UTF8
      Ok "Kode sumber siap"
    }
}
$Support = Join-Path $Src "src\agent\install_support.py"

# --- 2b. Cek kecocokan sistem (sebelum apa pun dipasang) ---
# sistem.py: OS/arsitektur/RAM/disk/Python/internet + Ketentuan & Kebijakan,
# lalu konfirmasi lanjut/batal. Sengaja dijalankan sebelum pip install agar
# pengguna bisa membatalkan sebelum proses unduhan dimulai. Prompt Y/n-nya
# interaktif; Invoke-Live menjaga output dan prompt tetap tampil di console.
Step "Pemeriksaan perangkat"
$SistemRc = Invoke-Live $Py @((Join-Path $Src "src\agent\sistem.py"))
switch ($SistemRc) {
    0 { }
    1 { Warn "Dibatalkan - tidak ada yang dipasang."; exit 0 }
    default { Err "Sistem ini belum didukung - pemasangan dihentikan."; exit 1 }
}
# --- 3. Pasang sebagai perintah global ---
Step "Paket aplikasi"
# --user ditolak di dalam venv ("user site-packages are not visible").
$UserFlags = @("--user")
if ($env:VIRTUAL_ENV) { $UserFlags = @() }

$PackageRc = Invoke-Live $Py (@($Support, "python", "--source", $Src) + $UserFlags)
if ($PackageRc -ne 0) { Err "Paket aplikasi belum siap. Log: $InstallLog"; exit 1 }

# --- 3b. Pengenal suara lokal ---
Step "Pengenal suara"
# Helper tetap menjalankan --prepare-model; cache lengkap dimuat tanpa unduhan.
$VoiceRc = Invoke-Live $Py @($Support, "voice")
if ($VoiceRc -ne 0) {
    Err "Whisper lokal gagal dipasang atau dimuat. Instalasi dibatalkan."
    exit 1
}

# --- 3c. Browser Chromium untuk Playwright ---
# WAJIB: seluruh model bagas-ai berjalan lewat browser. Paket pip `playwright`
# hanya membawa pustakanya; binari browsernya harus diunduh terpisah. Tanpa
# langkah ini, model pertama yang dipilih akan gagal dengan pesan teknis.
Step "Browser Chromium"
$BrowserRc = Invoke-Live $Py @($Support, "browser")
if ($BrowserRc -ne 0) { Warn "Browser belum siap. Log: $InstallLog" }

# --- 3c. Brave: browser yang DIPAKAI sehari-hari ---
# Chromium di atas cuma jaring pengaman terakhir. Yang dijalankan connector
# adalah browser ASLI yang terpasang di mesin ini, dan bawaannya Brave
# (CONNECTOR_BROWSER_CHANNEL). Chromium bundel Playwright paling sering
# diblok situs, jadi ia tak boleh jadi pilihan sehari-hari.
Step "Browser sistem"
$BravePaths = @(
    (Join-Path $env:LOCALAPPDATA 'BraveSoftware\Brave-Browser\Application\brave.exe'),
    (Join-Path $env:ProgramFiles 'BraveSoftware\Brave-Browser\Application\brave.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'BraveSoftware\Brave-Browser\Application\brave.exe')
)
$Brave = $BravePaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($Brave) {
    Ok "Brave sudah ada"
} else {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
        Note "memasang Brave lewat winget..."
        Invoke-Quiet winget @("install", "--id", "Brave.Brave", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements") | Out-Null
        $Brave = $BravePaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        if ($Brave) { Ok "Brave terpasang" }
    }
    if (-not $Brave) {
        # TIDAK menggagalkan pemasangan. bagas-ai tetap jalan: kalau Brave tak
        # ada, ia memakai Chrome atau Edge yang terpasang (lihat _pilih_exe).
        $Fallback = @(
            (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
            (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')
        ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        if ($Fallback) {
            Warn "Brave belum terpasang - bagas-ai akan memakai $(Split-Path -Leaf $Fallback)."
        } else {
            Warn "Brave belum terpasang dan tak ada Chrome/Edge - pasang salah satunya."
        }
        Note "pasang Brave nanti: winget install --id Brave.Brave -e"
        Note "atau unduh di https://brave.com/download/"
    }
}

# --- 3d. Tesseract OCR (untuk OCR lokal /image & read_image_local) ---
# Opsional tapi kecil dan cakupan-pengguna: tanpanya /image tetap jalan,
# hanya bagian "OCR lokal" yang kosong. Dipasang otomatis bila winget ada.
# Jangan memanggil winget tanpa Invoke-Quiet: stderr-nya mematikan installer
# (lihat catatan di helper itu).
Step "OCR lokal"
$Winget = Get-Command winget -ErrorAction SilentlyContinue
$TessPaths = @(
    (Join-Path $env:ProgramFiles 'Tesseract-OCR\tesseract.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Tesseract-OCR\tesseract.exe')
)
if ($env:LOCALAPPDATA) {
    $TessPaths += (Join-Path $env:LOCALAPPDATA 'Programs\Tesseract-OCR\tesseract.exe')
}
$Tess = $null
$TessCmd = Get-Command tesseract -ErrorAction SilentlyContinue
if ($TessCmd) { $Tess = $TessCmd.Source }
if (-not $Tess) {
    $Tess = $TessPaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if ($Tess) {
    Ok "Tesseract sudah tersedia"
} elseif ($Winget) {
    Note "memasang Tesseract lewat winget..."
    # Satu percobaan; retry --scope user TIDAK berguna (diam2 gagal: paket ini
    # cuma mendukung cakupan mesin). Cek ulang path bawaan setelahnya.
    Invoke-Quiet winget @("install", "--id", "UB-Mannheim.TesseractOCR",
                          "-e", "--silent", "--accept-package-agreements",
                          "--accept-source-agreements") | Out-Null
    $TessCmd = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($TessCmd) { $Tess = $TessCmd.Source }
    if (-not $Tess) {
        $Tess = $TessPaths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if ($Tess) { Ok "Tesseract siap" }
}
if (-not $Tess) {
    # TIDAK menggagalkan pemasangan: /image tetap jalan, hanya teksnya kosong.
    Warn "Tesseract belum terpasang - OCR lokal /image akan nonaktif."
    Note "pasang nanti: winget install --id UB-Mannheim.TesseractOCR -e"
}

# --- 3e. Ollama + Gemma 3 4B (WAJIB untuk vision lokal) ---
Step "Model gambar lokal"
$Ollama = Invoke-Captured $Py @($Support, "find-ollama") | Select-Object -Last 1
if (-not $Ollama) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
        Note "memasang Ollama lewat winget..."
        Invoke-Quiet winget @("install", "--id", "Ollama.Ollama", "-e", "--silent",
                              "--accept-package-agreements", "--accept-source-agreements") | Out-Null
        $Ollama = Invoke-Captured $Py @($Support, "find-ollama") | Select-Object -Last 1
    }
}
if (-not $Ollama) {
    Err "Ollama wajib tetapi gagal dipasang. Pasang dari https://ollama.com/download/windows lalu jalankan installer lagi."
    exit 1
}
$rc = Invoke-Live $Py @($Support, "vision")
if ($rc -ne 0) { Err "Gemma 3 4B terpasang tetapi gagal merespons gambar. Instalasi dibatalkan."; exit 1 }

# --- 4. Pastikan folder Scripts ada di PATH (User) ---
# Cari lokasi .exe yang BENAR-BENAR terpasang (penting untuk Python Store yang
# menaruh script di folder tak terduga), bukan sekadar menebak dari getuserbase.
Step "Perintah terminal"
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
    $out = Invoke-Captured $Py @($LocateFile) | Select-Object -Last 1
    if ($out) { $BinDir = "$out".Trim() }
} catch {
    # abaikan - akan pakai fallback di bawah
} finally {
    Remove-Item $LocateFile -ErrorAction SilentlyContinue
}
if (-not $BinDir) {
    $fb = Invoke-Captured $Py @("-c", "import site,os; print(os.path.join(site.getuserbase(),'Scripts'))") | Select-Object -Last 1
    if ($fb) { $BinDir = "$fb".Trim() }
}
if (-not $BinDir) {
    Err "Tak bisa menentukan folder Scripts. Tambahkan folder Scripts Python ke PATH secara manual."
} else {
    # Tulis User PATH lewat registry LANGSUNG, bukan [Environment]::Set...:
    # cara itu membaca nilai yang SUDAH di-expand lalu menulis balik sebagai
    # REG_SZ, sehingga entri ber-%VARIABEL% ikut ter-bake dan jenis value
    # berubah. Di sini nilai mentah dipertahankan (REG_EXPAND_SZ tetap), entry
    # lama tak disentuh, dan penambahan hanya dilakukan bila benar-benar belum
    # ada (perbandingan tanpa peduli besar huruf & trailing backslash).
    try {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
        if (-not $key) { throw "tidak bisa membuka registry Environment" }
        $opts = [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
        # [string] cast, BUKAN "$(...)": string kosong "" sebagai argumen di
        # dalam interpolasi bikin tokenizer PS 5.1 menganggap string tak
        # tertutup (TerminatorExpectedAtEndOfString).
        $raw = [string]$key.GetValue("Path", "", $opts)
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        if ($raw) { $kind = $key.GetValueKind("Path") }
        $parts = @($raw -split ";" | Where-Object { $_ })
        $exists = @($parts | Where-Object { "$_".TrimEnd('\') -ieq $BinDir.TrimEnd('\') })
        if ($exists.Count -gt 0) {
            Ok "PATH (User) sudah memuat: $BinDir"
        } else {
            $key.SetValue("Path", (($parts + $BinDir) -join ";"), $kind)
            $key.Close()
            Ok "Ditambahkan ke PATH (User): $BinDir"
            # Umumkan ke Windows (WM_SETTINGCHANGE) supaya terminal BARU yang
            # dibuka dari Explorer/Start langsung mengenali tanpa perlu logoff.
            try {
                Add-Type -Namespace BagasAI -Name Native -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
                $res = [UIntPtr]::Zero
                [BagasAI.Native]::SendMessageTimeout([IntPtr]0xFFFF, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$res) | Out-Null
            } catch {}
        }
        if ($key) { $key.Close() }
    } catch {
        Err "Gagal memperbarui PATH: $($_.Exception.Message)"
        Err "Tambahkan manual: $BinDir"
    }
    # Perbarui sesi SAAT INI juga (agar login di bawah & terminal ini langsung bisa).
    $inSession = @($env:Path -split ';' | Where-Object { "$_".TrimEnd('\') -ieq $BinDir.TrimEnd('\') })
    if ($inSession.Count -eq 0) { $env:Path = "$($env:Path);$BinDir" }
    Note "Tutup lalu buka terminal BARU bila 'bagas-ai' belum dikenali."
}

# --- 4b. OpenCode CLI: runtime model AI ---
Step "OpenCode"
$OpenCode = Get-Command opencode -ErrorAction SilentlyContinue
if ($OpenCode) {
    Ok "OpenCode sudah tersedia"
} else {
    $Npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($Npm) {
        Note "memasang opencode lewat npm (butuh beberapa menit)..."
        $rc = Invoke-Quiet npm @("install", "-g", "opencode-ai")
        if ($rc -eq 0) {
            Ok "opencode terpasang"
        } else {
            Warn "npm install opencode gagal (exit $rc) - bagas-ai tetap terpasang."
            Note "pasang manual nanti: npm install -g opencode-ai"
        }
    } else {
        Warn "OpenCode belum tersedia untuk model AI."
        Note "Pasang dengan: npm install -g opencode-ai"
    }
}

# --- 5. Wizard setup (API key NVIDIA / OpenRouter; bot Telegram opsional) ---
# bagas-ai tak punya kredensial wajib: model dipilih lewat /model lalu login
# dilakukan sekali di jendela browser.
if ($env:BAGASAI_SKIP_LOGIN -eq "1") {
    Note "Wizard dilewati (BAGASAI_SKIP_LOGIN=1) - jalankan 'bagas-ai login' kapan pun."
} else {
    Step "Setup - bot Telegram (opsional)"
    $bagas = Get-Command bagas-ai -ErrorAction SilentlyContinue
    $PrevDisclaimerAccepted = $env:BAGASAI_DISCLAIMER_ACCEPTED
    $env:BAGASAI_DISCLAIMER_ACCEPTED = "1"
    try {
        if ($bagas) { & bagas-ai login } else { & $Py -m agent login }
    } finally {
        if ($null -eq $PrevDisclaimerAccepted) {
            Remove-Item Env:BAGASAI_DISCLAIMER_ACCEPTED -ErrorAction SilentlyContinue
        } else {
            $env:BAGASAI_DISCLAIMER_ACCEPTED = $PrevDisclaimerAccepted
        }
    }
}

Write-Host ""
Write-Host "Selesai. " -ForegroundColor Green -NoNewline
Write-Host "Ketik " -NoNewline
Write-Host "bagas-ai" -ForegroundColor Cyan -NoNewline
Write-Host " di terminal mana pun untuk mulai."
Write-Host "  Log: $InstallLog" -ForegroundColor DarkGray
Write-Host ""

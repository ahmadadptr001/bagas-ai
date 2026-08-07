"""Hub browser (Playwright) untuk fitur CONNECTOR web-AI.

Kenapa perlu satu thread khusus:
  Objek sinkron Playwright TERIKAT pada thread yang membuatnya — dipakai dari
  thread lain langsung error. Padahal tiap giliran CLI dijalankan di thread
  worker BARU (lihat interfaces/cli.py). Karena itu SELURUH aksi browser
  dijalankan di SATU thread daemon berumur panjang milik hub ini; pemanggil
  cukup menitipkan pekerjaan lewat submit() dan menunggu hasilnya. Efek samping
  bagus: akses browser otomatis ter-serialisasi (satu aksi pada satu waktu).

Profil login DISIMPAN permanen di ~/.bagasai/browser/<service>/ (persistent
context Chromium), jadi login cukup SEKALI — sesi berikutnya otomatis terpakai.

Playwright bersifat OPSIONAL: modul ini hanya mengimpornya saat benar-benar
dipakai, sehingga bagas-ai tetap jalan normal walau Playwright belum terpasang.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import config

_PROFILE_ROOT = config.CONFIG_HOME / "browser"


class _PlaywrightNoiseFilter(logging.Filter):
    """Sembunyikan galat INTERNAL Playwright yang tak berarti bagi pengguna.

    Saat sebuah panggilan Playwright ditinggalkan (mis. peluncuran pertama gagal
    lalu diulang, atau proses berakhir), loop internalnya mencetak traceback
    "SyncBase._sync ... 'NoneType' object has no attribute 'switch'". Itu murni
    derau: tak memengaruhi hasil, tapi terlihat menakutkan di terminal."""

    _NOISE = ("SyncBase._sync", "has no attribute 'switch'")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage() + str(getattr(record, "exc_text", "") or "")
            exc = getattr(record, "exc_info", None)
            if exc and exc[1] is not None:
                text += repr(exc[1])
        except Exception:  # noqa: BLE001
            return True
        return not any(n in text for n in self._NOISE)


logging.getLogger("asyncio").addFilter(_PlaywrightNoiseFilter())

# Logger modul. Dipakai untuk peristiwa yang perlu bisa ditelusuri belakangan
# tapi TIDAK layak mengganggu layar pengguna — mis. "terminal ini menumpang
# Chrome milik terminal lain", yang normal terjadi dan bukan masalah.
log = logging.getLogger(__name__)


def profile_dir(service: str) -> "Path":
    """Folder profil login persisten milik sebuah service."""
    return _PROFILE_ROOT / service


def _ps_profile_query(target: "Path") -> str:
    """Potongan PowerShell: proses Chrome yang memakai folder profil `target`.

    Dipakai bersama oleh pencarian PID & pembunuhan proses supaya aturan
    pencocokannya HANYA ada di satu tempat (dulu duplikat, dan bug backslash
    sempat membuat salah satunya tak pernah cocok)."""
    # -like memakai backslash secara LITERAL. Jangan meng-escape (menggandakan)
    # backslash — polanya jadi tak pernah cocok.
    marker = str(target).replace("'", "")
    # Nama prosesnya TAK BOLEH dipatok "chrome": browsernya bisa diganti lewat
    # CONNECTOR_BROWSER_CHANNEL, dan brave.exe maupun msedge.exe tak
    # mengandung "chrom" sama sekali. Saringan nama di sini cuma penyaring
    # murah; yang benar-benar menentukan tetap jalur profil di bawahnya, dan
    # jalur itu milik kita sendiri sehingga tak mungkin salah tangkap.
    return (
        "Get-CimInstance Win32_Process -Filter \"Name like '%chrom%' "
        "or Name like '%brave%' or Name like '%edge%'\" | "
        "Where-Object { $_.CommandLine -like '*" + marker + "*' }"
    )


def _chrome_pids(service: str) -> set[int]:
    """PID proses Chrome yang memakai profil connector `service` (Windows)."""
    if sys.platform != "win32":
        return set()
    try:
        ps = _ps_profile_query(profile_dir(service)) + \
            " | ForEach-Object { $_.ProcessId }"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=25,
        )
        return {int(x) for x in (out.stdout or "").split() if x.strip().isdigit()}
    except Exception:  # noqa: BLE001
        return set()


# user32 dengan argtypes LENGKAP, dibuat sekali. Tanpa argtypes, HWND yang
# dilewatkan sebagai int Python dimarshal jadi C int 32-bit dan bisa TERPOTONG
# di Windows 64-bit sehingga jendela salah/gagal disembunyikan.
_U32: dict[str, Any] = {}


def _user32() -> Any:
    """Kembalikan (dll, tipe HWND, tipe callback enum) atau None bila tak ada."""
    if sys.platform != "win32":
        return None
    if "dll" in _U32:
        return _U32["dll"]
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [proc, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        # Dipakai untuk MEMUNCULKAN kembali jendela yang tak kita sembunyikan
        # sendiri (lihat set_windows_visible). Kelas jendela jadi penyaringnya:
        # proses Chrome juga punya jendela bantu BERJUDUL ("MSCTFIME UI",
        # "Default IME") yang kalau ikut ditampilkan muncul sebagai kotak
        # kosong aneh di layar pengguna.
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        # Menampilkan jendela TIDAK sama dengan membuatnya terlihat: jendela
        # connector dilahirkan di luar layar (lihat posisi_sembunyi), jadi sesudah
        # ShowWindow ia tetap di luar setiap monitor. Tiga fungsi ini yang
        # menyeretnya kembali ke dalam layar.
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        u.SetWindowPos.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                            ctypes.c_void_p, wintypes.UINT]
        u.SystemParametersInfoW.restype = wintypes.BOOL
        _U32.update(dll=u, proc=proc, hwnd=wintypes.HWND,
                    dword=wintypes.DWORD, byref=ctypes.byref,
                    buf=ctypes.create_unicode_buffer)
        return u
    except Exception:  # noqa: BLE001
        _U32["dll"] = None
        return None


# service -> daftar HWND yang KITA sembunyikan. Dipakai agar saat ditampilkan
# lagi (mis. perlu login) hanya jendela itu yang kembali — bukan jendela bantu
# internal Chrome yang memang seharusnya tak terlihat.
_HIDDEN_WINDOWS: dict[str, list[int]] = {}


def _kelas_jendela(u: Any, hwnd: Any) -> str:
    """Nama kelas Win32 sebuah jendela ("" bila gagal dibaca).

    Jendela utama Chrome selalu berkelas `Chrome_WidgetWin_*`; jendela bantu
    yang ikut dimiliki prosesnya ("MSCTFIME UI", "Default IME") tidak. Judul
    saja tak cukup memilah — jendela bantu itu PUNYA judul."""
    try:
        buf = _U32["buf"](64)
        n = u.GetClassNameW(hwnd, buf, 64)
        return buf.value if n else ""
    except Exception:  # noqa: BLE001
        return ""


def posisi_sembunyi() -> tuple[int, int]:
    """Koordinat "di luar layar tapi MASUK AKAL" untuk melahirkan jendela.

    Dulu dipakai -32000,-32000: pasti tak terlihat, tapi juga pasti mustahil.
    Halaman membaca posisi jendela lewat window.screenX/screenY, dan tak ada
    manusia yang punya monitor di koordinat itu — bagi mesin penilai risiko
    (chat.z.ai memakai Aliyun Captcha) itu penanda yang menonjol. TERCATAT di
    repo ini: jendela mulai dilahirkan di -32000,-32000 pada 6 Agu 20:24, dan
    captcha pertama dilaporkan pengguna sekitar 22:27 hari yang sama. Korelasi,
    bukan bukti — tapi tak ada alasan mempertahankan angka yang mustahil itu.

    Yang dipakai sekarang: persis di sebelah KANAN seluruh layar, seukuran
    tepat satu jendela. Tetap tak pernah terlihat, tapi bentuknya sama seperti
    jendela yang dibuka di monitor kedua — hal yang sehari-hari terjadi."""
    u = _user32()
    if u is None:
        return -2000, 40
    try:
        vx, vw = u.GetSystemMetrics(76), u.GetSystemMetrics(78)
        if vw > 0:
            return vx + vw + 8, 40
    except Exception:  # noqa: BLE001
        pass
    return -2000, 40


def _di_luar_layar(u: Any, hwnd: Any) -> bool:
    """True bila jendela itu praktis tak ada di layar mana pun.

    Bukan sekadar "sebagian keluar tepi": yang dicari adalah jendela yang tak
    menyisakan bidang yang cukup untuk disentuh — termasuk jendela connector
    yang memang dilahirkan di luar layar (lihat posisi_sembunyi)."""
    try:
        import ctypes
        from ctypes import wintypes

        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        # Layar VIRTUAL: gabungan semua monitor, jadi jendela di monitor kedua
        # tak dikira hilang lalu diseret paksa ke monitor utama.
        vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if vw <= 0 or vh <= 0:
            return False
        lebar = min(r.right, vx + vw) - max(r.left, vx)
        tinggi = min(r.bottom, vy + vh) - max(r.top, vy)
        return lebar < 300 or tinggi < 200
    except Exception:  # noqa: BLE001
        return False


def _ke_layar(u: Any, hwnd: Any) -> bool:
    """Seret jendela KEMBALI KE DALAM LAYAR bila ia di luar. True bila dipindah.

    Menampilkan jendela tidak sama dengan membuatnya terlihat. Jendela
    connector sengaja dilahirkan jauh di luar layar supaya tak pernah
    berkelebat (lihat _launch), dan ShowWindow mengembalikannya persis ke
    koordinat mustahil itu. Akibatnya fatal justru di saat yang paling
    membutuhkan: captcha hanya bisa diselesaikan tangan manusia, pengguna
    disuruh menyelesaikannya "di jendela yang sudah kubuka" — dan jendela itu
    tak ada di layarnya. DILAPORKAN PENGGUNA: "jendelanya gada di layar,
    terlalu tergeser ke samping"."""
    if not _di_luar_layar(u, hwnd):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        lebar, tinggi = r.right - r.left, r.bottom - r.top
        kerja = wintypes.RECT()
        # Bidang KERJA layar utama (di luar taskbar), bukan seluruh layar:
        # jendela yang tepinya tertutup taskbar susah digeser lagi.
        if not u.SystemParametersInfoW(0x0030, 0, ctypes.byref(kerja), 0):
            kerja.left = kerja.top = 0
            kerja.right = u.GetSystemMetrics(0)
            kerja.bottom = u.GetSystemMetrics(1)
        kw, kh = kerja.right - kerja.left, kerja.bottom - kerja.top
        if lebar < 400 or lebar > kw:
            lebar = max(400, kw - 120)
        if tinggi < 300 or tinggi > kh:
            tinggi = max(300, kh - 120)
        x = kerja.left + max(0, (kw - lebar) // 2)
        y = kerja.top + max(0, (kh - tinggi) // 2)
        SWP_SHOWWINDOW = 0x0040
        return bool(u.SetWindowPos(hwnd, None, x, y, lebar, tinggi,
                                   SWP_SHOWWINDOW))
    except Exception:  # noqa: BLE001
        return False


def set_windows_visible(service: str, visible: bool) -> int:
    """Sembunyikan / tampilkan JENDELA browser milik `service` (Windows).

    Dipakai agar connector benar-benar berjalan DI LATAR: setelah login, jendela
    Chrome disembunyikan sepenuhnya (tak ada di taskbar) — bukan sekadar
    di-minimize — sementara prosesnya tetap hidup & merender normal. Jendela
    ditampilkan lagi hanya saat pengguna perlu login. Return jumlah jendela yang
    diubah (0 bila tak didukung)."""
    u = _user32()
    if u is None:
        return 0
    HWND, DWORD, byref = _U32["hwnd"], _U32["dword"], _U32["byref"]
    SW_HIDE, SW_SHOWNOACTIVATE, SW_RESTORE = 0, 4, 9
    try:
        if visible:
            # Kembalikan HANYA jendela yang tadi kita sembunyikan, dan HANYA
            # yang handle-nya masih hidup. Handle basi (browser sudah diluncurkan
            # ulang) TIDAK boleh dihitung: kalau dihitung, pemanggil mengira
            # jendela sudah tampil lalu melewati cadangan CDP — pengguna disuruh
            # login ke jendela yang sebenarnya masih tersembunyi.
            shown = 0
            for h in _HIDDEN_WINDOWS.pop(service, []):
                hw = HWND(h)
                if u.IsWindow(hw):
                    u.ShowWindow(hw, SW_SHOWNOACTIVATE)
                    # Kalau ia harus diseret kembali ke layar, artinya ia
                    # memang sedang benar-benar tersembunyi — dan yang
                    # memanggil pasti butuh pengguna MENYENTUHNYA (login atau
                    # captcha). Baru di situ fokus direbut; kalau jendelanya
                    # sudah di layar, jangan ganggu terminal yang sedang
                    # diketik.
                    if _ke_layar(u, hw):
                        u.SetForegroundWindow(hw)
                    shown += 1
            if shown:
                return shown
            # TAK ADA CATATAN, tapi belum tentu tak ada yang perlu dimunculkan.
            #
            # Catatan itu hidup di MEMORI PROSES INI saja, sedangkan jendela yang
            # tersembunyi/terminimalkan sering kali warisan proses SEBELUMNYA:
            # sesi yang ditutup mendadak meninggalkan Chrome-nya hidup, lalu
            # proses berikutnya MENUMPANG di jendela itu (lihat _sambung_cdp) dan
            # cuma menambah tab. Karena catatannya kosong, dulu fungsi ini
            # menjawab 0 dan pemanggil jatuh ke cadangan CDP — padahal
            # Browser.setWindowBounds TIDAK bisa membatalkan ShowWindow(SW_HIDE)
            # milik proses lain. Hasilnya jendelanya tak pernah muncul lagi, dan
            # makin sering terjadi seiring menumpuknya sesi yang tak tertutup
            # rapi.
            pids = _chrome_pids(service)
            if not pids:
                return 0

            def _munculkan(hwnd, _lparam):
                nonlocal shown
                pid = DWORD()
                u.GetWindowThreadProcessId(hwnd, byref(pid))
                if pid.value not in pids or u.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                if not _kelas_jendela(u, hwnd).startswith("Chrome_WidgetWin"):
                    return True
                # HANYA yang memang sedang tak terlihat. Jendela yang sudah
                # tampil sengaja tak disentuh: _foreground dipanggil di SETIAP
                # pengambilan halaman saat CONNECTOR_SHOW aktif, dan mengangkat
                # jendela tiap kali pesan dikirim berarti merebut fokus dari
                # terminal yang sedang diketik pengguna.
                # "Terlihat" menurut Windows TIDAK berarti terlihat oleh mata:
                # jendela di luar layar tetap berstatus visible. Tanpa syarat
                # terakhir ini, jendela yang sudah pernah di-ShowWindow proses
                # lain akan dilewati di sini dan pengguna dikirimi kabar
                # "jendelanya sudah terbuka" untuk jendela yang tak ada di
                # layarnya.
                if (u.IsWindowVisible(hwnd) and not u.IsIconic(hwnd)
                        and not _di_luar_layar(u, hwnd)):
                    return True
                u.ShowWindow(hwnd, SW_RESTORE)
                _ke_layar(u, hwnd)
                u.SetForegroundWindow(hwnd)
                shown += 1
                return True

            u.EnumWindows(_U32["proc"](_munculkan), 0)
            return shown

        # Sudah tersembunyi dari panggilan sebelumnya & jendelanya masih itu-itu
        # juga? Tak ada yang perlu dikerjakan — hindari spawn PowerShell yang
        # mahal (~0,7 dtk) pada SETIAP pengiriman pesan.
        prev = _HIDDEN_WINDOWS.get(service) or []
        if prev and all(u.IsWindow(HWND(h)) and not u.IsWindowVisible(HWND(h))
                        for h in prev):
            return len(prev)

        pids = _chrome_pids(service)
        if not pids:
            return 0
        hidden: list[int] = []

        def _cb(hwnd, _lparam):
            pid = DWORD()
            u.GetWindowThreadProcessId(hwnd, byref(pid))
            # Hanya jendela NYATA yang sedang terlihat (punya judul) — jendela
            # bantu internal Chrome dibiarkan apa adanya.
            if (pid.value in pids and u.IsWindowVisible(hwnd)
                    and u.GetWindowTextLengthW(hwnd) > 0):
                u.ShowWindow(hwnd, SW_HIDE)
                hidden.append(int(hwnd))
            return True

        u.EnumWindows(_U32["proc"](_cb), 0)
        if hidden:
            _HIDDEN_WINDOWS[service] = hidden
        return len(hidden)
    except Exception:  # noqa: BLE001
        return 0


def _mark_profile_clean(service: str) -> None:
    """Tandai profil Chrome sebagai 'ditutup normal'.

    Chrome menampilkan dialog "Restore pages?" bila sesi sebelumnya TIDAK
    berakhir bersih — dan itu yang terjadi setiap kali prosesnya kita hentikan
    paksa atau proses bagas-ai berakhir tanpa menutup browser. Menyetel ulang
    penanda di Preferences membuat peluncuran berikutnya bersih tanpa dialog."""
    prefs = profile_dir(service) / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    prof = data.get("profile")
    if not isinstance(prof, dict):
        prof = {}
        data["profile"] = prof
    prof["exit_type"] = "Normal"
    prof["exited_cleanly"] = True
    try:
        prefs.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def _shutdown_on_hub(hub: "BrowserHub") -> None:
    """Tutup context lalu hentikan driver Playwright (di thread hub)."""
    hub.close_all()
    try:
        if hub._pw is not None:
            hub._pw.stop()
            hub._pw = None
    except Exception:  # noqa: BLE001
        pass


def shutdown(timeout: float = 8.0) -> None:
    """Tutup SEMUA browser connector dengan RAPI (dipanggil saat bagas-ai keluar).

    Penutupan rapi = Chrome menulis status 'keluar normal', sehingga tidak lagi
    menawarkan "Restore pages?" saat dipakai lagi. Driver Playwright ikut
    dihentikan supaya tak ada callback menggantung saat proses berakhir."""
    global _HUB
    with _HUB_LOCK:
        h = _HUB
        _HUB = None
    if h is None or not h._started:
        return
    closed = h.dispose(timeout=timeout, paksa=True)
    if not closed:
        # Penutupan rapi gagal. Chrome yang masih hidup TIDAK boleh ditinggalkan
        # dalam keadaan tersembunyi: tanpa jendela & tanpa entri taskbar,
        # pengguna hanya bisa menutupnya lewat Task Manager. Tampilkan lagi,
        # lalu hentikan prosesnya.
        for svc in list(_HIDDEN_WINDOWS):
            set_windows_visible(svc, True)
        _kill_profile_browsers()


def _shutdown_atexit() -> None:
    """Jaring pengaman bila proses berakhir tanpa sempat memanggil shutdown().
    Sengaja SENYAP: saat interpreter membongkar diri, Playwright bisa melempar
    error yang tak berguna bagi pengguna."""
    try:
        shutdown(timeout=5.0)
    except BaseException:  # noqa: BLE001
        pass


atexit.register(_shutdown_atexit)


def forget_profile(service: str) -> bool:
    """LOGOUT total: tutup browser service ini lalu HAPUS folder profilnya
    (cookie & sesi login ikut terhapus). True bila folder benar-benar hilang."""
    try:
        reset_hub()  # buang hub + bunuh Chrome yang memegang profil
    except Exception:  # noqa: BLE001
        pass
    _kill_profile_browsers(service)
    time.sleep(1.0)  # beri OS waktu melepas kunci file
    prof = profile_dir(service)
    shutil.rmtree(prof, ignore_errors=True)
    return not prof.exists()


# Service yang browsernya BUKAN milik proses ini — kita cuma menumpang di
# Chrome yang diluncurkan terminal lain. Disimpan di tingkat modul (bukan di
# dalam hub) karena penjaganya dibutuhkan justru saat hub sudah dibubarkan:
# jalur penutupan darurat di shutdown() berjalan sesudah itu.
_MENUMPANG: set[str] = set()


# Channel yang dikenal Playwright sendiri. Di luar ini, browsernya harus
# ditunjuk lewat executable_path (lihat _launch._try).
_CHANNEL_PLAYWRIGHT = {
    "chrome", "chrome-beta", "chrome-dev", "chrome-canary",
    "msedge", "msedge-beta", "msedge-dev", "msedge-canary", "chromium",
}


def _exe_browser(channel: str) -> str | None:
    """Jalur chrome.exe / msedge.exe di mesin ini, atau None bila tak ketemu.

    Dibutuhkan sejak browsernya dijalankan SENDIRI (lihat _luncur_sendiri):
    Playwright yang biasanya tahu di mana tiap channel dipasang, dan
    pengetahuan itu tak diekspos ke API Python.

    Folder pemasangan dicari LEBIH DULU, registry cuma cadangan. Urutan
    sebaliknya salah, dan salahnya diam-diam: App Paths dikunci per NAMA EXE,
    sedangkan chrome dan chrome-beta sama-sama bernama chrome.exe — jadi
    channel beta akan menjawab jalur Chrome stabil dengan yakin. Folder yang
    membedakan keduanya, bukan nama berkasnya."""
    if sys.platform != "win32" or not channel:
        return None
    # channel -> (nama exe, folder pemasangan di bawah Program Files/LocalAppData)
    kenal = {
        "brave": ("brave.exe", r"BraveSoftware\Brave-Browser"),
        "chrome": ("chrome.exe", r"Google\Chrome"),
        "chrome-beta": ("chrome.exe", r"Google\Chrome Beta"),
        "msedge": ("msedge.exe", r"Microsoft\Edge"),
    }
    if channel not in kenal:
        return None
    nama, merek = kenal[channel]
    # Brave lazim dipasang PER PENGGUNA (di LocalAppData) dan tak menulis
    # App Paths sama sekali, jadi LocalAppData di daftar ini bukan sekadar
    # kelengkapan — bagi Brave, di situlah ia biasanya satu-satunya ketemu.
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        akar = os.environ.get(env)
        if not akar:
            continue
        p = Path(akar) / merek / "Application" / nama
        if p.exists():
            return str(p)
    # Tak ada di tempat bakunya — mungkin dipasang di lokasi tak lazim. Di
    # sinilah registry berguna; risiko salah-channel tak berlaku lagi karena
    # jalur bakunya sudah dicoba dan meleset.
    try:
        import winreg
        for akar_reg in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                        akar_reg,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                        rf"\App Paths\{nama}") as k:
                    jalur = winreg.QueryValue(k, None)
                # Jawabannya WAJIB berada di bawah folder merek yang diminta.
                # Tanpa syarat ini, meminta chrome-beta yang tak terpasang
                # dijawab jalur Chrome STABIL dengan penuh percaya diri —
                # keduanya bernama chrome.exe, dan App Paths cuma tahu nama.
                if (jalur and Path(jalur).exists()
                        and merek.split("\\")[-1].lower() in jalur.lower()):
                    return jalur
            except OSError:
                continue
    except Exception:  # noqa: BLE001 - registry tak terbaca: menyerah dengan None
        pass
    return None


def _pilih_exe(channel: str) -> tuple[str | None, str]:
    """(jalur exe, channel yang benar-benar dipakai) — atau (None, "").

    Bila browser yang diminta tak terpasang, browser ASLI lain dicoba dulu.
    Menyerah ke Chromium bundel Playwright adalah pilihan TERBURUK — ia yang
    paling sering diblok — jadi ia tak boleh kebagian giliran hanya karena
    Brave belum sempat dipasang di mesin ini."""
    if not channel:
        return None, ""
    urutan = [channel] + [c for c in ("brave", "chrome", "msedge")
                          if c != channel]
    for c in urutan:
        exe = _exe_browser(c)
        if exe:
            if c != channel:
                log.info("browser %s tak terpasang — memakai %s", channel, c)
            return exe, c
    return None, ""


def _kill_profile_browsers(service: str | None = None) -> None:
    """Bunuh proses Chrome/Chromium yang memakai folder profil connector.

    Chrome yang tertinggal MENGUNCI folder profil (Chrome menolak profil yang
    sedang dipakai proses lain), sehingga peluncuran ulang IKUT MENGGANTUNG —
    inilah 'pembukaan sesi browser nyangkut' setelah Ctrl+C/crash. Dengan
    `service`, hanya Chrome untuk profil itu yang dibunuh (sesi lain aman);
    tanpa `service`, seluruh profil connector. Best-effort; hanya Windows.

    TIDAK PERNAH membunuh browser yang cuma kita TUMPANGI. Jendelanya milik
    terminal lain yang mungkin sedang menunggu jawaban; membunuhnya berarti
    menghancurkan sesi orang lain demi membereskan sesi kita sendiri. Penjaga
    ini penting justru di jalur penutupan darurat: kalau penutupan rapi gagal,
    dulu proses ini menyapu SEMUA Chrome profil connector — termasuk yang bukan
    miliknya."""
    if sys.platform != "win32":
        return
    if service and service in _MENUMPANG:
        log.info("lewati pembunuhan Chrome %s — jendelanya milik terminal lain",
                 service)
        return
    if service is None and _MENUMPANG:
        # Penyapuan menyeluruh tak bisa memilah per profil, jadi ia dibatalkan
        # seluruhnya bila ada satu saja yang ditumpangi. Kerugiannya cuma
        # kunci profil yang telat lepas; kerugian sebaliknya jauh lebih besar.
        log.info("lewati pembunuhan menyeluruh — masih menumpang: %s",
                 ", ".join(sorted(_MENUMPANG)))
        return
    try:
        target = _PROFILE_ROOT / service if service else _PROFILE_ROOT
        # Jendela yang tercatat tersembunyi ikut dilupakan — prosesnya mati,
        # handle-nya tak berlaku lagi.
        if service:
            _HIDDEN_WINDOWS.pop(service, None)
        else:
            _HIDDEN_WINDOWS.clear()
        # MENUNGGU prosesnya benar-benar mati, bukan sekadar mengirim sinyal.
        # Stop-Process kembali seketika, sementara Chrome baru melepas kunci
        # profilnya beberapa ratus milidetik kemudian — dan peluncuran ulang
        # yang datang di sela itu GAGAL karena profilnya masih terkunci. Itulah
        # 'kadang lancar, kadang nyangkut' sesudah pembatalan. Menunggunya di
        # dalam SATU panggilan PowerShell jauh lebih murah daripada polling
        # dari Python (tiap spawn PowerShell ~0,7 detik).
        ps = (
            "$ids = @(" + _ps_profile_query(target) +
            " | ForEach-Object { $_.ProcessId }); "
            "if ($ids.Count) { "
            "Stop-Process -Id $ids -Force -ErrorAction SilentlyContinue; "
            "Wait-Process -Id $ids -Timeout 8 -ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=30,
        )
        # Sisa file kunci Chrome bisa menghalangi peluncuran berikutnya.
        for name in ("lockfile", "SingletonLock", "SingletonCookie",
                     "SingletonSocket"):
            try:
                (target / name).unlink()
            except OSError:
                pass
        # Proses tadi dimatikan PAKSA -> tanpa ini Chrome berikutnya menawarkan
        # "Restore pages?".
        if service:
            _mark_profile_clean(service)
    except Exception:  # noqa: BLE001
        pass


class BrowserError(RuntimeError):
    """Kegagalan terkait browser/connector (login gagal, timeout, dsb)."""


class WebLimitError(BrowserError):
    """Layanan AI web sedang MEMBATASI pemakaian (kuota/limit pesan habis).

    Dibedakan dari kegagalan lain supaya bagas-ai bisa memberi tahu pengguna
    dengan jelas (termasuk kapan bisa dipakai lagi) alih-alih menunggu jawaban
    yang memang tak akan datang."""


class WebChatRusakError(BrowserError):
    """Percakapan di situsnya RUSAK — chat-nya tak bisa dilanjutkan lagi.

    Terlihat di chat.qwen.ai sebagai spanduk merah:
        "Oops! There was an issue connecting to Qwen3.8-Max.
         Invalid input chat parent_id … is not exist."
    Artinya rangkaian pesan yang dipegang situs tak lagi utuh (chat-nya terhapus
    atau ditulis ulang di sisi server), jadi tiap pesan berikutnya ke chat yang
    sama akan gagal dengan cara yang sama.

    BEDA TEGAS dari WebLimitError: kuota kita baik-baik saja. Karena itu
    jawabannya bukan menunggu maupun ganti model, melainkan MULAI CHAT BARU lalu
    kirim ulang — dan itu bisa dikerjakan sendiri tanpa merepotkan pengguna."""


class WebKonteksPenuhError(BrowserError):
    """Percakapan di situsnya sudah KEPANJANGAN untuk dilanjutkan.

    Terlihat di kimi.com sebagai:
        "Your conversation with Kimi is getting too long.
         Try starting a new session."

    BEDA TEGAS dari WebLimitError maupun WebChatRusakError: kuota kita aman dan
    chat-nya tidak rusak — ia cuma sudah terlalu panjang. Jalan keluarnya juga
    berbeda: bukan menunggu, bukan sekadar membuka chat kosong, melainkan
    MERINGKAS dulu apa yang sudah dikerjakan lalu membawa ringkasan itu ke chat
    baru. Chat baru tanpa ringkasan berarti pekerjaan yang sedang berjalan
    (berkas yang setengah diedit, keputusan yang sudah diambil) hilang seluruhnya
    dan harus ditemukan ulang dari nol."""


class WebLampiranPenuhError(BrowserError):
    """Percakapan sudah memuat lampiran SEBANYAK BATAS situs.

    Terlihat di chat.z.ai sebagai toast:
        "You can only chat with a maximum of 10 file(s) at a time."

    Ini yang menjelaskan kegagalan kirim yang membingungkan di sesi panjang:
    tiap langkah web_preview menambah satu screenshot, dan begitu batasnya
    kena, berkas berikutnya DITOLAK diam-diam — pratinjaunya tak pernah muncul,
    penantian unggahan habis waktu, lalu gagalnya dilaporkan seolah komposer
    yang rusak.

    BUKAN kegagalan giliran: pesannya sendiri masih bisa dikirim, hanya tanpa
    gambar. Karena itu jenisnya sendiri — penanganannya mematikan lampiran
    untuk percakapan ini lalu mengirim ulang teksnya berikut petunjuk cara
    kerja pengganti (lihat Agent._TANPA_GAMBAR)."""


class WebBusyError(BrowserError):
    """Layanan sedang KEWALAHAN sesaat ("System is currently busy…").

    BEDA TEGAS dari WebLimitError: kuota kita baik-baik saja, servernya yang
    penuh — biasanya pulih dalam hitungan detik. Karena itu jawabannya bukan
    "ganti model" melainkan TUNGGU LALU ULANGI otomatis.

    Dulu pemberitahuan begini terbaca sebagai isi balasan biasa, sehingga
    tampil sebagai "jawaban" model — giliran dianggap sukses padahal model tak
    pernah menjawab, dan alur agentic lanjut di atas teks yang bukan jawaban."""


def playwright_available() -> bool:
    """True bila Playwright + modul sync-nya bisa diimpor."""
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


class _Job:
    __slots__ = ("fn", "result", "error", "done", "started")

    def __init__(self, fn: Callable[["BrowserHub"], Any]) -> None:
        self.fn = fn
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        # Dibedakan dari `done`: menandai job sudah MULAI dijalankan thread hub.
        # Tanpa ini, "job berat yang sah" dan "job yang mengantre di belakang
        # job MACET" terlihat sama persis dari sisi pemanggil — dan yang kedua
        # itulah yang membuat terminal diam tanpa penjelasan.
        self.started = threading.Event()


class BrowserHub:
    """Pemilik tunggal instance Playwright; menjalankan semua aksi di 1 thread."""

    def __init__(self) -> None:
        self._q: "queue.Queue[_Job | None]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="bagasai-browser"
        )
        self._started = False
        self._start_lock = threading.Lock()
        self._pw: Any = None
        # service -> (context, page)
        self._ctx: dict[str, tuple[Any, Any]] = {}
        # Service yang context-nya BUKAN milik kita — kita cuma menumpang di
        # Chrome yang sudah diluncurkan terminal LAIN (lihat _sambung_cdp).
        # Bedanya penting saat menutup: yang menumpang hanya boleh menutup TAB
        # miliknya sendiri, tak boleh menutup jendela milik terminal sebelah.
        self._dipinjam: set[str] = set()
        # service -> proses Chrome yang KITA luncurkan sendiri (lihat
        # _luncur_sendiri). Playwright tak mengenalnya, jadi kitalah yang
        # bertanggung jawab menutupnya.
        self._proses: dict[str, Any] = {}
        # True bila sebuah job MACET melewati timeout -> hub ini tak bisa
        # dipercaya lagi (thread-nya mungkin menggantung); hub() akan
        # menggantinya dengan hub baru + membunuh Chrome profil yang tersisa.
        self.poisoned = False
        # True selama thread hub menjalankan sebuah job (lihat busy()).
        self._sedang_jalan = False
        # True bila hub ini sudah DIBUANG (dispose): thread-nya diakhiri dan
        # driver Playwright-nya dihentikan. Job baru harus ditolak SEKETIKA —
        # kalau tidak, pemanggil menunggu hasil yang takkan pernah datang
        # karena tak ada lagi yang mengambil job dari antrean.
        self._mati = False

    # --- sisi pemanggil (thread mana pun) ---
    def _ensure_thread(self) -> None:
        with self._start_lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def busy(self) -> bool:
        """True bila thread hub sedang sibuk.

        Antrean saja TIDAK cukup: job yang sedang BERJALAN sudah diambil dari
        antrean, sehingga `_q.empty()` bernilai True justru pada saat hub paling
        sibuk — kebalikan dari yang dimaksud."""
        return self._sedang_jalan or not self._q.empty()

    def submit(
        self,
        fn: Callable[["BrowserHub"], Any],
        timeout: float | None = None,
        *,
        queue_timeout: float = 120.0,
        on_wait: Callable[[], None] | None = None,
    ) -> Any:
        """Jalankan fn(hub) DI thread hub, kembalikan hasilnya (blocking).

        MENUNGGU ANTREAN dipisahkan dari MENUNGGU HASIL, dan itu penting:
        thread hub cuma SATU, jadi job yang macet membuat semua giliran
        berikutnya mengantre di belakangnya. Dulu keduanya dihitung dalam satu
        `timeout` yang untuk pengiriman pesan bernilai 12 menit — sehingga
        pengguna melihat terminal DIAM belasan menit tanpa penjelasan, tanpa
        browser terbuka, seolah programnya rusak.

        Sekarang: bila job belum MULAI dalam 3 detik, `on_wait` dipanggil supaya
        pemanggil bisa memberi tahu pengguna bahwa giliran sebelumnya belum
        lepas. Bila belum mulai juga sampai `queue_timeout`, hub ditandai
        POISONED dan galatnya menyebut sebabnya — jauh lebih berguna daripada
        menggantung.

        `timeout` tetap berlaku untuk job yang SUDAH berjalan."""
        if self._mati:
            raise BrowserError(
                "sesi browser sudah direset — kirim ulang permintaanmu.")
        self._ensure_thread()
        job = _Job(fn)
        self._q.put(job)

        if not job.started.wait(3.0):
            if on_wait is not None:
                try:
                    on_wait()
                except Exception:  # noqa: BLE001 - sekadar pemberitahuan
                    pass
            sisa = max(0.0, queue_timeout - 3.0)
            if not job.started.wait(sisa):
                self.poisoned = True
                raise BrowserError(
                    "giliran browser sebelumnya belum lepas setelah "
                    f"{queue_timeout:.0f} detik — sesi direset. Coba kirim lagi."
                )

        if not job.done.wait(timeout):
            self.poisoned = True
            raise BrowserError(
                "aksi browser melebihi batas waktu — sesi direset, coba lagi."
            )
        if job.error is not None:
            raise job.error
        return job.result

    # --- berjalan DI thread hub ---
    def _loop(self) -> None:
        from playwright.sync_api import sync_playwright

        # Driver Playwright kadang GAGAL start pada percobaan pertama: pipe ke
        # proses node driver tertutup sebelum handshake selesai, memunculkan
        # "'PlaywrightContextManager' object has no attribute '_playwright'"
        # (sering menyusul "Connection closed while reading from the driver").
        # Dulu galat itu menembus keluar _loop dan MEMBUNUH thread hub diam-diam:
        # tak ada lagi yang mengambil job dari antrean, sehingga SETIAP kirim
        # berikutnya menggantung tanpa penjelasan (persis gejala "cuma berpikir").
        # Sekarang: coba beberapa kali dengan jeda; bila tetap gagal, JANGAN
        # biarkan thread mati — layani tiap job dengan galat yang jelas dan tandai
        # hub POISONED agar hub() menggantinya dengan hub baru di pemakaian
        # berikutnya (peluncuran driver yang segar sering berhasil).
        pw = None
        galat: BaseException | None = None
        for percobaan in range(3):
            try:
                pw = sync_playwright().start()
                break
            except BaseException as exc:  # noqa: BLE001
                galat = exc
                time.sleep(0.8 * (percobaan + 1))

        if pw is None:
            self.poisoned = True
            pesan = BrowserError(
                "gagal memulai mesin browser — driver Playwright tak mau start "
                f"({type(galat).__name__ if galat else '?'}). Coba kirim lagi; "
                "bila terus terjadi jalankan 'playwright install'."
            )
            while True:
                job = self._q.get()
                if job is None:
                    break
                # Gagal-cepat: tak ada job yang boleh menggantung menunggu driver
                # yang takkan pernah siap.
                job.started.set()
                job.error = pesan
                job.done.set()
            return

        self._pw = pw
        while True:
            job = self._q.get()
            if job is None:
                break
            job.started.set()
            self._sedang_jalan = True
            try:
                job.result = job.fn(self)
            except BaseException as exc:  # noqa: BLE001 - diteruskan ke pemanggil
                job.error = exc
            finally:
                self._sedang_jalan = False
                job.done.set()
        # Thread berakhir: SISA ANTREAN wajib dilayani dengan galat yang jelas.
        # Tanpa ini, job yang sempat masuk tepat sebelum hub dibuang tak pernah
        # disentuh siapa pun — `done` tak pernah diset, dan pemanggilnya diam
        # menunggu sampai batas waktu kirim (belasan menit) tanpa satu pun tanda.
        self._mati = True
        self._drain()

    def _drain(self) -> None:
        """Gagalkan seluruh job tersisa di antrean (dipanggil saat thread usai)."""
        galat = BrowserError(
            "sesi browser direset di tengah jalan — kirim ulang permintaanmu.")
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                return
            if job is None:
                continue
            job.started.set()
            job.error = galat
            job.done.set()

    def page_for(self, service: str, headless: bool) -> Any:
        """Kembalikan page persisten untuk sebuah service (buat bila belum ada).

        HARUS dipanggil dari thread hub (lewat submit)."""
        entry = self._ctx.get(service)
        if entry is not None:
            ctx, page = entry
            if self._alive(page):
                return page
            # page/context mati (mis. jendela ditutup / crash). Buang, lalu
            # PASTIKAN tak ada Chrome sisa yang masih mengunci profil ini —
            # kalau ada, launch berikutnya akan menggantung.
            #
            # KECUALI kalau kita cuma menumpang: yang mati berarti TAB kita,
            # sementara jendelanya milik terminal lain yang mungkin sedang
            # bekerja. Membunuhnya di sini sama saja merusak sesi tetangga.
            menumpang = service in self._dipinjam
            self.drop(service)
            if not menumpang:
                _kill_profile_browsers(service)

        prof = _PROFILE_ROOT / service
        prof.mkdir(parents=True, exist_ok=True)
        # Profil sedang DIPEGANG proses lain. Dua kemungkinan yang dulu tak
        # dibedakan sama sekali:
        #   a. terminal LAIN sedang memakai model ini — harus ditumpangi;
        #   b. bangkai Chrome sesi sebelumnya — harus dibereskan.
        # Cara membedakannya: coba sambung dulu. Yang hidup akan menjawab.
        if any((prof / n).exists()
               for n in ("lockfile", "SingletonLock", "SingletonSocket")):
            ctx = self._sambung_cdp(service)
            if ctx is not None:
                self._dipinjam.add(service)
                _MENUMPANG.add(service)
                # TAB BARU, bukan tab yang sudah ada: tab pertama milik
                # terminal sebelah dan mungkin sedang menunggu jawaban.
                page = ctx.new_page()
                self._ctx[service] = (ctx, page)
                return page
            # Ada kunci profil, tapi TAK ADA yang menjawab: itu bangkai, bukan
            # tetangga. Kalau service ini masih bertanda "ditumpangi", tandanya
            # sudah BASI — jendela yang dulu ditumpangi sudah mati. Tanda basi
            # itu berbahaya, sebab _kill_profile_browsers menghormatinya dan
            # melewati pembunuhan; bangkainya tetap memegang kunci profil, lalu
            # peluncuran di bawah menggantung. Persis 'nyangkut saat membuka
            # jendela baru' sesudah sesi sebelumnya dihentikan paksa.
            self._lupakan_tumpangan(service)
            _kill_profile_browsers(service)
        # Bersihkan penanda crash sisa sesi sebelumnya sebelum meluncurkan,
        # supaya Chrome tak menampilkan tawaran "Restore pages?".
        _mark_profile_clean(service)
        ctx = self._launch(str(prof), headless, service)
        if service in self._dipinjam:
            # _launch gagal meluncur lalu berhasil menumpang (lomba dua terminal
            # yang start berbarengan) -> tetap wajib tab sendiri.
            page = ctx.new_page()
        else:
            # TAB BARU, lalu tab bawaan DIBUANG — bukan sekadar kerapian.
            #
            # Chrome memasang pita kuning "You are using an unsupported
            # command-line flag: ..." pada tab yang ADA SAAT ia start, dan
            # hanya di situ. Tab yang dibuka sesudahnya bersih. Karena
            # connector toh selalu menavigasi sendiri ke halaman chat, tab
            # bawaan itu tak ada gunanya — dan membuangnya menghapus pitanya
            # tanpa perlu melepas satu pun bendera.
            #
            # Urutannya WAJIB begini: buka dulu, baru tutup yang lama. Menutup
            # halaman terakhir sebuah persistent context ikut menutup
            # browsernya.
            lama = list(ctx.pages)
            page = ctx.new_page()
            for p in lama:
                try:
                    p.close()
                except Exception:  # noqa: BLE001 - sudah tertutup sendiri
                    pass
        self._ctx[service] = (ctx, page)
        return page

    def dispose(self, timeout: float = 6.0, paksa: bool = False) -> bool:
        """Bubarkan hub ini SEUTUHNYA: context ditutup, driver Playwright
        dihentikan, thread-nya diakhiri. True bila semuanya berhasil rapi.

        WAJIB dipakai setiap kali sebuah hub ditinggalkan. Dulu hub yang direset
        (mis. sesudah Ctrl+C) hanya dilepas acuannya — thread-nya tetap hidup
        dan proses driver Playwright-nya TETAP BERJALAN. TERUKUR: tiga kali
        reset meninggalkan tiga proses node.exe menganggur sekaligus tiga
        thread; setelah beberapa pembatalan, seluruh aplikasi terasa berat.

        Penghentian driver HARUS terjadi di thread hub (objek sinkron Playwright
        terikat pada thread pembuatnya), jadi dikerjakan lewat submit. Bila job
        yang sedang berjalan macet, submit-nya gagal — di situlah `paksa`
        dipakai: thread tetap diakhiri (sentinel None) supaya ia berhenti begitu
        panggilan yang menggantung itu lepas."""
        if not self._started or self._mati:
            self._mati = True
            return True
        rapi = True
        try:
            self.submit(_shutdown_on_hub, timeout=timeout, queue_timeout=timeout)
        except Exception:  # noqa: BLE001 - job lain sedang macet
            rapi = False
        if rapi or paksa:
            self._mati = True
            try:
                self._q.put(None)      # akhiri loop thread hub
            except Exception:  # noqa: BLE001
                pass
        return rapi

    def _lupakan_tumpangan(self, service: str) -> None:
        """Hapus tanda 'menumpang' untuk sebuah service.

        Tanda itu melindungi jendela milik terminal lain dari pembunuhan. Begitu
        jendelanya terbukti sudah mati, tandanya justru berbalik jadi masalah:
        ia melindungi BANGKAI yang memegang kunci profil. Karena itu ia harus
        bisa dicabut, bukan cuma dipasang."""
        self._dipinjam.discard(service)
        _MENUMPANG.discard(service)

    def drop(self, service: str) -> None:
        """Tutup & lupakan context sebuah service (HARUS di thread hub)."""
        # Jendela context ini akan lenyap -> jangan simpan handle basi yang bisa
        # menipu set_windows_visible pada peluncuran berikutnya.
        _HIDDEN_WINDOWS.pop(service, None)
        entry = self._ctx.pop(service, None)
        if entry is None:
            return
        ctx, page = entry
        if service in self._dipinjam:
            # MENUMPANG: jendelanya milik terminal lain yang mungkin sedang
            # bekerja. Yang boleh kita tutup cuma TAB kita sendiri; menutup
            # context berarti menutup seluruh tab tetangga sekaligus.
            self._lupakan_tumpangan(service)
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            # Putuskan sambungan CDP-nya saja. Pada browser yang DISAMBUNGI
            # (bukan diluncurkan), close() memutus koneksi tanpa mematikan
            # prosesnya — itulah yang kita mau.
            try:
                br = ctx.browser
                if br is not None:
                    br.close()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        # Chrome yang KITA jalankan sendiri tak dimiliki Playwright: menutup
        # context-nya cuma memutus sambungan CDP, prosesnya tetap hidup dan
        # tetap memegang kunci profil. Ditutup lewat perintah dulu (supaya
        # Chrome berakhir bersih & tak menawarkan "Restore pages?"), baru
        # prosesnya dipastikan benar-benar berakhir.
        if service in self._proses:
            try:
                br = ctx.browser
                if br is not None:
                    br.close()
            except Exception:  # noqa: BLE001
                pass
            self._matikan_proses(service)

    def close_all(self) -> None:
        """Tutup RAPI semua context (HARUS di thread hub). Dipakai saat keluar
        agar Chrome berakhir normal & tak menawarkan 'Restore pages?'."""
        for svc in list(self._ctx):
            self.drop(svc)
            _mark_profile_clean(svc)

    @staticmethod
    def _alive(page: Any) -> bool:
        try:
            if page.is_closed():
                return False
            _ = page.url
            return True
        except Exception:  # noqa: BLE001
            return False

    # --- berbagi satu Chrome antar-terminal ---------------------------------
    #
    # Masalah yang diperbaiki: menjalankan bagas-ai di terminal KEDUA dengan
    # model yang sama, sementara terminal pertama masih memakainya. Satu folder
    # profil Chrome hanya boleh dipegang satu proses, jadi peluncuran kedua
    # gagal — dan penanganan lamanya MEMBUNUH Chrome milik terminal pertama
    # lalu meluncurkan ulang. Terminal pertama kehilangan sesi & percakapan
    # yang sedang berjalan, tanpa penjelasan apa pun di layarnya.
    #
    # Sekarang: yang datang belakangan MENUMPANG. Peluncur pertama membuka
    # porta remote-debugging dan mencatat nomornya di dalam folder profil;
    # proses berikutnya membaca catatan itu, menyambung lewat CDP, lalu membuka
    # TAB BARU di jendela yang sudah ada. Tak ada yang dibunuh, tak ada profil
    # yang diperebutkan.
    #
    # Portanya DIPILIH ACAK (bukan angka tetap seperti 9222) supaya tak pernah
    # bentrok dengan Chrome lain milik pengguna yang kebetulan juga membuka
    # porta debug — dan karena nomornya dicatat di dalam folder profil, tak ada
    # yang perlu ditebak.
    @staticmethod
    def _berkas_porta(service: str) -> Path:
        return _PROFILE_ROOT / service / ".bagasai-cdp-port"

    @staticmethod
    def _porta_bebas() -> int:
        """Nomor porta yang sedang bebas di localhost."""
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _sambung_cdp(self, service: str) -> Any:
        """Sambung ke Chrome yang SUDAH jalan untuk service ini, atau None.

        None berarti "tak ada yang bisa ditumpangi" — entah memang belum ada
        yang jalan, entah catatannya basi karena prosesnya sudah mati."""
        f = self._berkas_porta(service)
        try:
            porta = int(f.read_text(encoding="utf-8").strip())
        except Exception:  # noqa: BLE001 - belum ada / rusak: anggap tak ada
            return None
        try:
            # Timeout pendek: kalau tak ada yang mendengarkan, jangan menahan
            # peluncuran normal berlama-lama.
            browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{porta}", timeout=4000)
        except Exception:  # noqa: BLE001 - catatan basi / porta sudah mati
            try:
                f.unlink()
            except OSError:
                pass
            return None
        try:
            ctx = browser.contexts[0] if browser.contexts else None
        except Exception:  # noqa: BLE001
            ctx = None
        if ctx is None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
            return None
        log.info("menumpang Chrome yang sudah jalan untuk %s (porta %s)",
                 service, porta)
        return ctx

    def _launch(self, user_data_dir: str, headless: bool,
               service: str | None = None) -> Any:
        """Buka persistent context. Utamakan CHROME asli (channel="chrome") agar
        lebih jarang di-blok anti-bot; fallback ke Chromium bawaan bila Chrome
        tak terpasang. Tak meng-override user-agent -> pakai UA asli browser.

        Bila peluncuran GAGAL karena profil masih dikunci Chrome sisa (proses
        lama belum mati -> 'Target ... has been closed'), Chrome profil itu
        dibunuh lalu peluncuran DIULANG sekali."""
        # JENDELA TAK BOLEH BERKELEBAT SAAT SUDAH LOGIN.
        #
        # Selama ini jendelanya MUNCUL dulu lalu disembunyikan (ShowWindow),
        # jadi tiap kali mulai mengetik ada kotak Chrome yang berkedip di layar
        # — mengganggu, dan mencuri fokus dari terminal. Dilaporkan pengguna.
        #
        # Yang dipakai di sini: jendelanya DILAHIRKAN JAUH DI LUAR LAYAR. Ia
        # tetap jendela sungguhan (Cloudflare menolak sesi headless, jadi
        # headless bukan pilihan) dan tetap merender normal, cuma tak pernah
        # terlihat. Penyembunyian ShowWindow tetap jalan sesudahnya; ini
        # menutup celah beberapa ratus milidetik sebelum ia sempat bekerja.
        #
        # Hanya bila profilnya SUDAH ADA — artinya pernah login. Pemasangan
        # pertama justru harus terlihat: di situ pengguna memang perlu jendela
        # untuk memasukkan akunnya.
        sembunyi = False
        try:
            prof = Path(user_data_dir)
            sembunyi = (not headless and prof.exists()
                        and any(prof.iterdir())
                        and not config.CONNECTOR_SHOW)
        except Exception:  # noqa: BLE001
            sembunyi = False
        # Bendera yang WAJAR untuk Chrome mana pun. Sengaja sesedikit mungkin:
        # tiap bendera tambahan adalah satu hal lagi yang membedakan jendela ini
        # dari Chrome yang dibuka manusia.
        bendera = [
            "--start-maximized",
            # Jangan pernah menawarkan/memulihkan tab sesi sebelumnya —
            # connector selalu membuka halaman chat sendiri.
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--no-first-run",
            "--no-default-browser-check",
            # Jendela connector disembunyikan setelah login; flag ini mencegah
            # Chrome menahan/throttle render saat jendela tak terlihat, agar
            # token jawaban tetap masuk ke DOM & terbaca realtime.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        porta = self._porta_bebas()
        if sembunyi:
            # Ditaruh di koordinat yang tak terlihat di monitor mana pun, tapi
            # ukurannya tetap wajar supaya situs merender seperti biasa (layout
            # responsif situs chat berubah di jendela sempit).
            #
            # --start-maximized MEMBATALKAN posisi itu: Chrome memaksimalkan
            # jendelanya ke layar utama, jadi ia muncul persis di tengah.
            sx, sy = posisi_sembunyi()
            bendera = [b for b in bendera if b != "--start-maximized"]
            bendera += [f"--window-position={sx},{sy}", "--window-size=1280,900"]
        channel = config.CONNECTOR_BROWSER_CHANNEL

        def _catat_porta(ctx: Any) -> Any:
            # Porta remote-debugging: inilah yang membuat terminal BERIKUTNYA
            # bisa menumpang alih-alih membunuh jendela ini. Diumumkan selalu,
            # bukan hanya saat dibutuhkan — pemakainya datang belakangan dan tak
            # bisa meminta jendela yang sudah terlanjur jalan untuk membukanya.
            if service:
                try:
                    f = self._berkas_porta(service)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(str(porta), encoding="utf-8")
                except OSError:
                    pass          # gagal mencatat = cuma kehilangan berbagi
            return ctx

        # JALUR UTAMA: Chrome dijalankan SENDIRI, lalu disambung lewat porta.
        #
        # Bukan soal selera. TERUKUR — 42 argumen identik, yang berbeda hanya
        # cara menyambung:
        #     Playwright + --remote-debugging-pipe -> navigator.webdriver True
        #     argumen sama, lewat porta            -> navigator.webdriver False
        #
        # Jadi PIPA itulah yang menyalakan penanda otomasi, bukan
        # --enable-automation, bukan Chromium, bukan headless. Selama ini kita
        # menutupinya dengan --disable-blink-features=AutomationControlled —
        # tambal sulam yang justru membuat Chrome memasang pita kuning "unsupported
        # command-line flag", sehingga butuh akal-akalan lagi untuk
        # menyembunyikannya. Lewat porta, penandanya mati dengan sendirinya dan
        # tak ada satu pun bendera anti-deteksi yang perlu dibawa.
        ctx = self._luncur_sendiri(user_data_dir, headless, porta, bendera,
                                   channel, service)
        if ctx is not None:
            return _catat_porta(ctx)

        # CADANGAN: cara lama. Dipakai bila chrome.exe tak ketemu (channel
        # kosong / browsernya tak terpasang) atau peluncuran sendiri gagal.
        # Di sini pipa tak terhindarkan, jadi penandanya harus ditutup lagi.
        log.info("peluncuran mandiri tak tersedia — kembali ke Playwright")
        opts = dict(
            user_data_dir=user_data_dir,
            headless=headless,
            no_viewport=True,  # ikuti ukuran jendela asli (lebih natural)
            # SATU-SATUNYA cara membuang "--enable-automation".
            #
            # Bendera itu TIDAK ditulis di sini — Playwright sendiri yang
            # menyelipkannya ke setiap peluncuran chromium (lihat
            # chromiumSwitches.js: `assistantMode ? "" : "--enable-automation"`),
            # jadi menghapusnya dari daftar `args` di bawah tak ada gunanya;
            # ia harus dibuang lewat ignore_default_args.
            #
            # Dia yang memasang pita kuning "Chrome is being controlled by
            # automated test software" di atas halaman, dan dia pula yang
            # menyalakan penanda otomasi di dalam browser — yang dibaca mesin
            # penilai risiko seperti Aliyun Captcha milik chat.z.ai. Playwright
            # membuangnya persis begini untuk mode "assistant"-nya, yaitu mode
            # yang memang dipakai menyetir situs konsumen sungguhan.
            #
            # `--disable-blink-features=AutomationControlled` di bawah tetap
            # dipertahankan: itu saklar LAIN, yang mematikan navigator.webdriver.
            ignore_default_args=["--enable-automation"],
            # Playwright mematikan sandbox Chrome kecuali diminta tegas
            # (`if (options.chromiumSandbox !== true) push("--no-sandbox")`).
            # Chrome asli milik pengguna TIDAK pernah jalan begitu, dan
            # --no-sandbox termasuk bendera yang diprotes Chrome sendiri.
            # Dinyalakan: satu bendera ganjil berkurang, dan pengamanan
            # prosesnya kembali seperti Chrome biasa.
            chromium_sandbox=True,
            args=bendera + ["--disable-blink-features=AutomationControlled",
                            f"--remote-debugging-port={porta}"],
        )

        def _try() -> Any:
            # Playwright cuma mengenal channel bawaannya. Brave bukan salah
            # satunya — kalau namanya diteruskan begitu saja, peluncurannya
            # gagal lalu diam-diam jatuh ke Chromium bundel, yaitu browser yang
            # TIDAK diminta pengguna. Karena itu browser di luar daftar itu
            # ditunjuk lewat jalur exe-nya.
            if channel in _CHANNEL_PLAYWRIGHT:
                try:
                    return self._pw.chromium.launch_persistent_context(
                        channel=channel, **opts
                    )
                except Exception:  # noqa: BLE001 - tak terpasang -> Chromium bawaan
                    pass
            elif channel:
                exe, _ = _pilih_exe(channel)
                if exe:
                    try:
                        return self._pw.chromium.launch_persistent_context(
                            executable_path=exe, **opts
                        )
                    except Exception:  # noqa: BLE001
                        pass
            return self._pw.chromium.launch_persistent_context(**opts)

        try:
            return _catat_porta(_try())
        except Exception:  # noqa: BLE001 - profil dipegang proses lain?
            # SEBELUM membunuh apa pun: mungkin yang memegang profil ini adalah
            # terminal LAIN yang sedang bekerja, bukan bangkai sesi lama. Coba
            # tumpangi dulu. Dulu langkah ini tak ada, sehingga terminal kedua
            # selalu mengeksekusi pembunuhan dan menghancurkan sesi tetangganya.
            if service:
                ctx = self._sambung_cdp(service)
                if ctx is not None:
                    self._dipinjam.add(service)
                    _MENUMPANG.add(service)
                    return ctx
            _kill_profile_browsers(service)
            import time as _t
            _t.sleep(1.0)  # beri OS waktu melepas kunci profil
            return _catat_porta(_try())

    def _luncur_sendiri(self, user_data_dir: str, headless: bool, porta: int,
                        bendera: list[str], channel: str,
                        service: str | None) -> Any:
        """Jalankan Chrome sendiri lalu sambung lewat porta debug.

        Kembali None bila tak bisa — pemanggil lalu memakai cara lama. Setiap
        kegagalan di sini HARUS berujung None, bukan pengecualian: ini jalur
        yang boleh tak tersedia, bukan jalur yang boleh menggagalkan sesi."""
        exe, _ = _pilih_exe(channel)
        if not exe:
            return None
        args = [exe, f"--remote-debugging-port={porta}",
                f"--user-data-dir={user_data_dir}"] + bendera
        if headless:
            args.append("--headless=new")
        args.append("about:blank")
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)
        except OSError as e:
            log.info("gagal menjalankan %s sendiri: %s", exe, e)
            return None
        ctx = self._tunggu_porta(porta, proc)
        if ctx is None:
            # Chrome menyala tapi porta tak pernah menjawab (atau ia langsung
            # mati karena profilnya terkunci). Jangan tinggalkan prosesnya
            # menggantung: pemanggil akan mencoba cara lain pada profil yang
            # sama, dan dua Chrome atas satu profil pasti bentrok.
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            return None
        if service:
            self._proses[service] = proc
        log.info("Chrome dijalankan sendiri untuk %s (porta %s)",
                 service or "?", porta)
        return ctx

    def _tunggu_porta(self, porta: int, proc: Any,
                      batas: float = 30.0) -> Any:
        """Tunggu porta debug siap, lalu sambung. None bila tak kunjung siap.

        Kesiapan ditanyakan lewat /json/version, bukan sekadar "porta terbuka":
        Chrome membuka soketnya sebelum benar-benar siap melayani, dan
        connect_over_cdp yang datang terlalu cepat gagal dengan galat yang
        menyesatkan."""
        import urllib.request
        tenggat = time.time() + batas
        siap = False
        while time.time() < tenggat:
            if proc.poll() is not None:
                return None       # Chrome mati sebelum sempat siap
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{porta}/json/version", timeout=1):
                    siap = True
                    break
            except Exception:  # noqa: BLE001 - belum siap: coba lagi
                time.sleep(0.2)
        if not siap:
            return None
        try:
            browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{porta}", timeout=15000)
        except Exception as e:  # noqa: BLE001
            log.info("porta %s siap tapi tak bisa disambung: %s", porta, e)
            return None
        try:
            return browser.contexts[0] if browser.contexts else None
        except Exception:  # noqa: BLE001
            return None

    def _matikan_proses(self, service: str) -> None:
        """Pastikan Chrome yang KITA jalankan untuk service ini benar-benar
        berakhir. Aman dipanggil untuk service yang tak punya proses sendiri."""
        proc = self._proses.pop(service, None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                for _ in range(30):        # ~3 detik, cukup untuk keluar rapi
                    if proc.poll() is not None:
                        return
                    time.sleep(0.1)
                proc.kill()
        except Exception:  # noqa: BLE001 - sudah mati / tak bisa disentuh
            pass


_HUB: BrowserHub | None = None
_HUB_LOCK = threading.Lock()
# Kunci PEMBERSIHAN. Beda tugas dengan _HUB_LOCK yang cuma menjaga penggantian
# acuan singleton: yang ini menjaga agar hub baru TIDAK lahir selagi hub lama
# masih dibereskan.
#
# Tanpa ini ada lubang yang gejalanya persis "sesudah Ctrl+C dua kali, membuka
# jendela berikutnya sering nyangkut": reset_hub melepas acuan hub lebih dulu
# lalu mengerjakan bagian lambatnya (dispose, membunuh Chrome, dispose paksa) di
# luar kunci. Pesan berikutnya yang datang di sela itu melihat _HUB kosong,
# meluncurkan Chrome baru — lalu pembersihan yang MASIH BERJALAN membunuh Chrome
# profil itu, termasuk browser yang baru saja lahir. Terbukti lewat reproduksi:
# "hub BARU dibuat" muncul sebelum "BUNUH chrome profil".
#
# Re-entrant karena pembersihan bisa memanggil jalur yang mengambil kunci ini
# lagi. Urutannya WAJIB tetap: _BERSIH_LOCK dulu, baru _HUB_LOCK — jangan pernah
# dibalik, atau dua thread bisa saling menunggu selamanya.
_BERSIH_LOCK = threading.RLock()


def hub() -> BrowserHub:
    """Singleton hub browser (dibuat saat pertama dipakai).

    Bila hub sebelumnya POISONED (ada job yang macet melewati timeout — mis.
    setelah Ctrl+C di tengah pembukaan sesi), buat hub BARU dan bunuh Chrome
    profil yang mungkin tertinggal & mengunci profil. Ini menyembuhkan gejala
    'tiap Ctrl+C lalu chat baru, pembukaan sesi browser nyangkut tak selesai'.

    MENUNGGU pembersihan yang mungkin sedang berjalan (lihat _BERSIH_LOCK).
    Menunggu di sini justru yang membuat pembukaan cepat: browser yang lahir di
    tengah pembersihan akan ikut terbunuh, lalu percobaan berikutnya terhalang
    profil yang masih terkunci — persis 'nyangkut' yang mau dihindari."""
    global _HUB
    with _BERSIH_LOCK:
        with _HUB_LOCK:
            if _HUB is not None and _HUB.poisoned:
                _kill_profile_browsers()  # lepaskan kunci profil sebelum hub baru
                _HUB = None
            if _HUB is None:
                _HUB = BrowserHub()
            return _HUB


def reset_hub(service: str | None = None) -> None:
    """Buang hub saat ini; hub baru dibuat otomatis pada pemakaian berikutnya.

    Dipakai saat pemulihan error — terutama sesudah pembatalan (Ctrl+C) yang
    meninggalkan giliran browser menggantung.

    Dua hal yang dulu keliru di sini, dan keduanya terasa langsung oleh
    pengguna:

      1. Hub lama cuma DILEPAS acuannya, sehingga proses driver Playwright-nya
         menumpuk tiap kali reset (lihat dispose).
      2. Chrome SELALU dibunuh — untuk SEMUA profil, bukan cuma yang dipakai.
         Padahal sebagian besar "macet" sesudah Ctrl+C sebenarnya cuma
         panggilan browser yang belum sempat lepas. Akibatnya jendela browser
         lenyap pada pembatalan yang sehat, lalu pesan berikutnya harus
         meluncurkan Chrome dari nol.

    Sekarang: bubarkan hub dengan RAPI dulu (context ditutup, driver berhenti,
    sesi login tetap utuh di profil). Chrome hanya dibunuh bila pembubaran rapi
    GAGAL — pertanda job memang benar-benar macet di dalam browser, dan
    mematikan browsernya justru satu-satunya cara melepaskannya.

    `service` membatasi pembunuhan itu ke satu profil saja; None = semua.

    SELURUH pembersihan berada di dalam _BERSIH_LOCK, bukan cuma penggantian
    acuan hubnya. Dulu hanya baris pertama yang terkunci, sehingga hub baru bisa
    lahir di tengah jalan lalu ikut terbunuh oleh pembersihan ini sendiri.
    """
    global _HUB
    with _BERSIH_LOCK:
        with _HUB_LOCK:
            h, _HUB = _HUB, None
        if h is None:
            return
        if h.dispose(timeout=6.0):
            return
        _kill_profile_browsers(service)
        # Browsernya mati -> panggilan Playwright yang menggantung kini melempar,
        # jadi thread hub bisa menyelesaikan job-nya. Beri satu kesempatan lagi
        # untuk berhenti rapi; kalau tetap tidak bisa, akhiri paksa.
        h.dispose(timeout=4.0, paksa=True)

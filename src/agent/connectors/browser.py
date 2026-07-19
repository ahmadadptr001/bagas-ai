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


def profile_dir(service: str) -> "Path":
    """Folder profil login persisten milik sebuah service."""
    return _PROFILE_ROOT / service


def _chrome_pids(service: str) -> set[int]:
    """PID proses Chrome yang memakai profil connector `service` (Windows)."""
    if sys.platform != "win32":
        return set()
    try:
        marker = str(profile_dir(service)).replace("'", "")
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name like '%chrom%'\" | "
            "Where-Object { $_.CommandLine -like '*" + marker + "*' } | "
            "ForEach-Object { $_.ProcessId }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=25,
        )
        return {int(x) for x in (out.stdout or "").split() if x.strip().isdigit()}
    except Exception:  # noqa: BLE001
        return set()


# service -> daftar HWND yang KITA sembunyikan. Dipakai agar saat ditampilkan
# lagi (mis. perlu login) hanya jendela itu yang kembali — bukan jendela bantu
# internal Chrome yang memang seharusnya tak terlihat.
_HIDDEN_WINDOWS: dict[str, list[int]] = {}


def set_windows_visible(service: str, visible: bool) -> int:
    """Sembunyikan / tampilkan JENDELA browser milik `service` (Windows).

    Dipakai agar connector benar-benar berjalan DI LATAR: setelah login, jendela
    Chrome disembunyikan sepenuhnya (tak ada di taskbar) — bukan sekadar
    di-minimize — sementara prosesnya tetap hidup & merender normal. Jendela
    ditampilkan lagi hanya saat pengguna perlu login. Return jumlah jendela yang
    diubah (0 bila tak didukung)."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        SW_HIDE, SW_SHOWNOACTIVATE = 0, 4

        if visible:
            # Kembalikan HANYA jendela yang tadi kita sembunyikan.
            hwnds = _HIDDEN_WINDOWS.pop(service, [])
            for hwnd in hwnds:
                try:
                    user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)
                except Exception:  # noqa: BLE001
                    pass
            return len(hwnds)

        pids = _chrome_pids(service)
        if not pids:
            return 0
        hidden: list[int] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # Hanya jendela NYATA yang sedang terlihat (punya judul) — jendela
            # bantu internal Chrome dibiarkan apa adanya.
            if (pid.value in pids and user32.IsWindowVisible(hwnd)
                    and user32.GetWindowTextLengthW(hwnd) > 0):
                user32.ShowWindow(hwnd, SW_HIDE)
                hidden.append(int(hwnd))
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
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
    try:
        h.submit(_shutdown_on_hub, timeout=timeout)
    except Exception:  # noqa: BLE001 - keluar tetap harus mulus
        pass
    try:
        h._q.put(None)  # akhiri loop thread hub
    except Exception:  # noqa: BLE001
        pass


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


def _kill_profile_browsers(service: str | None = None) -> None:
    """Bunuh proses Chrome/Chromium yang memakai folder profil connector.

    Chrome yang tertinggal MENGUNCI folder profil (Chrome menolak profil yang
    sedang dipakai proses lain), sehingga peluncuran ulang IKUT MENGGANTUNG —
    inilah 'pembukaan sesi browser nyangkut' setelah Ctrl+C/crash. Dengan
    `service`, hanya Chrome untuk profil itu yang dibunuh (sesi lain aman);
    tanpa `service`, seluruh profil connector. Best-effort; hanya Windows."""
    if sys.platform != "win32":
        return
    try:
        target = _PROFILE_ROOT / service if service else _PROFILE_ROOT
        # PENTING: -like memakai backslash secara LITERAL. Jangan meng-escape
        # (menggandakan) backslash — polanya jadi tak pernah cocok & proses
        # Chrome yang mengunci profil tak pernah terbunuh.
        marker = str(target).replace("'", "")
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name like '%chrom%'\" | "
            "Where-Object { $_.CommandLine -like '*" + marker + "*' } | "
            "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction Stop } catch {} }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=25,
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


def playwright_available() -> bool:
    """True bila Playwright + modul sync-nya bisa diimpor."""
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


class _Job:
    __slots__ = ("fn", "result", "error", "done")

    def __init__(self, fn: Callable[["BrowserHub"], Any]) -> None:
        self.fn = fn
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = threading.Event()


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
        # True bila sebuah job MACET melewati timeout -> hub ini tak bisa
        # dipercaya lagi (thread-nya mungkin menggantung); hub() akan
        # menggantinya dengan hub baru + membunuh Chrome profil yang tersisa.
        self.poisoned = False

    # --- sisi pemanggil (thread mana pun) ---
    def _ensure_thread(self) -> None:
        with self._start_lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def submit(
        self, fn: Callable[["BrowserHub"], Any], timeout: float | None = None
    ) -> Any:
        """Jalankan fn(hub) DI thread hub, kembalikan hasilnya (blocking).

        Bila melewati `timeout`, hub ini ditandai POISONED: job yang macet masih
        menduduki thread hub, jadi hub berikutnya harus dibuat baru (lihat hub())
        agar giliran-giliran selanjutnya tak ikut mengantre di belakang job macet
        itu selamanya."""
        self._ensure_thread()
        job = _Job(fn)
        self._q.put(job)
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

        self._pw = sync_playwright().start()
        while True:
            job = self._q.get()
            if job is None:
                break
            try:
                job.result = job.fn(self)
            except BaseException as exc:  # noqa: BLE001 - diteruskan ke pemanggil
                job.error = exc
            finally:
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
            self.drop(service)
            _kill_profile_browsers(service)

        prof = _PROFILE_ROOT / service
        prof.mkdir(parents=True, exist_ok=True)
        # Sisa Chrome dari proses sebelumnya masih MENGUNCI profil -> peluncuran
        # pertama gagal lalu diulang (lambat + memunculkan galat Playwright yang
        # membingungkan). Adanya file kunci = pertanda; bereskan lebih dulu.
        if any((prof / n).exists()
               for n in ("lockfile", "SingletonLock", "SingletonSocket")):
            _kill_profile_browsers(service)
        # Bersihkan penanda crash sisa sesi sebelumnya sebelum meluncurkan,
        # supaya Chrome tak menampilkan tawaran "Restore pages?".
        _mark_profile_clean(service)
        ctx = self._launch(str(prof), headless, service)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._ctx[service] = (ctx, page)
        return page

    def drop(self, service: str) -> None:
        """Tutup & lupakan context sebuah service (HARUS di thread hub)."""
        entry = self._ctx.pop(service, None)
        if entry is not None:
            ctx, _ = entry
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

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

    def _launch(self, user_data_dir: str, headless: bool,
               service: str | None = None) -> Any:
        """Buka persistent context. Utamakan CHROME asli (channel="chrome") agar
        lebih jarang di-blok anti-bot; fallback ke Chromium bawaan bila Chrome
        tak terpasang. Tak meng-override user-agent -> pakai UA asli browser.

        Bila peluncuran GAGAL karena profil masih dikunci Chrome sisa (proses
        lama belum mati -> 'Target ... has been closed'), Chrome profil itu
        dibunuh lalu peluncuran DIULANG sekali."""
        opts = dict(
            user_data_dir=user_data_dir,
            headless=headless,
            no_viewport=True,  # ikuti ukuran jendela asli (lebih natural)
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                # Jangan pernah menawarkan/memulihkan tab sesi sebelumnya —
                # connector selalu membuka halaman chat sendiri.
                "--hide-crash-restore-bubble",
                "--disable-session-crashed-bubble",
                "--no-first-run",
                "--no-default-browser-check",
                # Jendela connector di-MINIMIZE setelah login; flag ini mencegah
                # Chrome menahan/throttle render saat jendela tersembunyi, agar
                # token jawaban tetap masuk ke DOM & terbaca realtime.
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        channel = config.CONNECTOR_BROWSER_CHANNEL

        def _try() -> Any:
            if channel:
                try:
                    return self._pw.chromium.launch_persistent_context(
                        channel=channel, **opts
                    )
                except Exception:  # noqa: BLE001 - Chrome tak ada -> Chromium bawaan
                    pass
            return self._pw.chromium.launch_persistent_context(**opts)

        try:
            return _try()
        except Exception:  # noqa: BLE001 - profil terkunci Chrome sisa?
            _kill_profile_browsers(service)
            import time as _t
            _t.sleep(1.0)  # beri OS waktu melepas kunci profil
            return _try()


_HUB: BrowserHub | None = None
_HUB_LOCK = threading.Lock()


def hub() -> BrowserHub:
    """Singleton hub browser (dibuat saat pertama dipakai).

    Bila hub sebelumnya POISONED (ada job yang macet melewati timeout — mis.
    setelah Ctrl+C di tengah pembukaan sesi), buat hub BARU dan bunuh Chrome
    profil yang mungkin tertinggal & mengunci profil. Ini menyembuhkan gejala
    'tiap Ctrl+C lalu chat baru, pembukaan sesi browser nyangkut tak selesai'."""
    global _HUB
    with _HUB_LOCK:
        if _HUB is not None and _HUB.poisoned:
            _kill_profile_browsers()  # lepaskan kunci profil sebelum hub baru
            _HUB = None
        if _HUB is None:
            _HUB = BrowserHub()
        return _HUB


def reset_hub() -> None:
    """Paksa hub dibuang & Chrome profil dibunuh (dipakai saat pemulihan error).
    Hub baru dibuat otomatis pada pemakaian berikutnya lewat hub()."""
    global _HUB
    with _HUB_LOCK:
        _HUB = None
    _kill_profile_browsers()

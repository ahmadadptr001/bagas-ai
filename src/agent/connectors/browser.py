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

import queue
import subprocess
import sys
import threading
from typing import Any, Callable

from .. import config

_PROFILE_ROOT = config.CONFIG_HOME / "browser"


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
        marker = str(target).replace("'", "").replace("\\", "\\\\")
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
    except Exception:  # noqa: BLE001
        pass


class BrowserError(RuntimeError):
    """Kegagalan terkait browser/connector (login gagal, timeout, dsb)."""


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
        ctx = self._launch(str(prof), headless)
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

    @staticmethod
    def _alive(page: Any) -> bool:
        try:
            if page.is_closed():
                return False
            _ = page.url
            return True
        except Exception:  # noqa: BLE001
            return False

    def _launch(self, user_data_dir: str, headless: bool) -> Any:
        """Buka persistent context. Utamakan CHROME asli (channel="chrome") agar
        lebih jarang di-blok anti-bot; fallback ke Chromium bawaan bila Chrome
        tak terpasang. Tak meng-override user-agent -> pakai UA asli browser."""
        opts = dict(
            user_data_dir=user_data_dir,
            headless=headless,
            no_viewport=True,  # ikuti ukuran jendela asli (lebih natural)
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                # Jendela connector di-MINIMIZE setelah login; flag ini mencegah
                # Chrome menahan/throttle render saat jendela tersembunyi, agar
                # token jawaban tetap masuk ke DOM & terbaca realtime.
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        channel = config.CONNECTOR_BROWSER_CHANNEL
        if channel:
            try:
                return self._pw.chromium.launch_persistent_context(
                    channel=channel, **opts
                )
            except Exception:  # noqa: BLE001 - Chrome tak ada -> Chromium bawaan
                pass
        return self._pw.chromium.launch_persistent_context(**opts)


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

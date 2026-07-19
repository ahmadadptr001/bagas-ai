"""Kerangka connector web-AI: buka halaman chat, ketik prompt, tunggu jawaban.

Satu WebConnector = satu situs (Claude, Qwen, dst). Tiap subclass cukup mengisi
SELECTOR & URL situsnya; algoritma kirim + tunggu-jawaban ada di sini dan dibuat
TAHAN-BANTING: alih-alih bergantung pada sinyal "selesai mengetik" yang berbeda
tiap situs & sering berubah, kita memantau TEKS balasan terakhir sampai BERHENTI
bertambah (stabil beberapa kali cek). Cara ini bertahan walau layout situs
berubah — yang perlu dijaga hanyalah selektor kotak input & wadah pesan.

Login: pertama kali dipakai, browser TAMPIL (headed) dan pengguna login manual
(termasuk CAPTCHA/2FA). Sesi disimpan permanen (persistent context), jadi
berikutnya otomatis. Semua aksi Playwright dijalankan di thread hub (browser.py).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .. import config
from .browser import BrowserError, hub

StatusCb = Callable[[str], None]
TokenCb = Callable[[str], None]


class WebConnector:
    """Basis connector. Subclass mengisi atribut kelas di bawah."""

    service: str = ""          # kunci internal & nama folder profil (mis. "claude")
    label: str = ""            # nama tampilan (mis. "Claude (web)")
    chat_url: str = ""         # halaman chat / sesi baru
    input_selector: str = ""   # kotak input (textarea / contenteditable)
    message_selector: str = "" # wadah pesan JAWABAN (diambil yang terakhir)
    input_is_contenteditable: bool = False
    submit_key: str = "Enter"  # tombol kirim
    # Penanda URL halaman LOGIN/AUTH: selama URL page mengandung salah satu ini,
    # user pasti BELUM login — jangan pernah dicap "siap" walau ada elemen input
    # yang kebetulan cocok selector (inilah sumber salah-deteksi sebelumnya).
    login_url_markers: tuple[str, ...] = (
        "login", "signin", "sign-in", "sign_in", "oauth", "/auth", "sso",
    )

    # Batas waktu (detik).
    login_timeout: float = 300.0     # tunggu pengguna menyelesaikan login
    answer_timeout: float = 300.0    # tunggu jawaban selesai
    # Berapa kali cek berturut-turut teks tak berubah -> dianggap selesai.
    _stable_needed: int = 5
    _poll_ms: int = 400

    # ---- API publik ----
    def connect(
        self,
        *,
        on_status: StatusCb | None = None,
        cancel_event: Any = None,
    ) -> bool:
        """Hubungkan ke situs — dipanggil SAAT MODEL DIPILIH (/model), bukan saat
        pesan pertama. Belum pernah login -> diarahkan ke Chrome untuk login
        SEKALI; sudah pernah -> langsung tersambung ke sesi chat.

        Return True bila proses login baru saja dilakukan (False = sesi lama)."""
        return hub().submit(
            lambda h: self._connect_on_hub(h, on_status, cancel_event)
        )

    def send(
        self,
        prompt: str,
        *,
        on_status: StatusCb | None = None,
        on_token: TokenCb | None = None,
        cancel_event: Any = None,
    ) -> str:
        """Kirim prompt ke situs & kembalikan teks jawaban (lewat thread hub)."""
        return hub().submit(
            lambda h: self._send_on_hub(h, prompt, on_status, on_token, cancel_event)
        )

    # ---- hook opsional untuk subclass ----
    def _is_done(self, page: Any) -> bool:
        """Petunjuk KHUSUS-situs bahwa balasan sudah tuntas (mis. tombol stop
        hilang). Default True -> murni andalkan kestabilan teks."""
        return True

    # ---- internal (berjalan DI thread hub) ----
    def _send_on_hub(
        self,
        h: Any,
        prompt: str,
        on_status: StatusCb | None,
        on_token: TokenCb | None,
        cancel_event: Any,
    ) -> str:
        from .. import llm  # untuk llm.Cancelled (impor tunda: hindari siklus)

        def status(msg: str) -> None:
            if on_status:
                on_status(msg)

        def check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

        status("menyiapkan sesi browser…")
        page, _ = self._acquire_ready_page(h, status, check_cancel)

        # --- kirim prompt ---
        check_cancel()
        status(f"mengetik pesan ke {self.label}…")
        try:
            box = page.wait_for_selector(
                self.input_selector, state="visible", timeout=8000
            )
        except Exception:  # noqa: BLE001
            box = None
        if box is None:
            raise BrowserError(
                f"kotak input tak ditemukan ({self.input_selector}). "
                "Situs mungkin berubah layout."
            )
        box.click()
        before = len(page.query_selector_all(self.message_selector))
        if self.input_is_contenteditable:
            page.keyboard.insert_text(prompt)
        else:
            box.fill(prompt)
        page.keyboard.press(self.submit_key)

        # --- tunggu balasan baru muncul ---
        status(f"{self.label} sedang menjawab…")
        t0 = time.time()
        while len(page.query_selector_all(self.message_selector)) <= before:
            check_cancel()
            if time.time() - t0 > 60:
                break  # mungkin situs memakai ulang wadah yang sama
            page.wait_for_timeout(300)

        # --- pantau teks balasan terakhir sampai stabil ---
        last = ""
        emitted = 0
        stable = 0
        deadline = time.time() + self.answer_timeout
        while time.time() < deadline:
            check_cancel()
            els = page.query_selector_all(self.message_selector)
            if not els:
                page.wait_for_timeout(self._poll_ms)
                continue
            try:
                cur = (els[-1].inner_text() or "").strip()
            except Exception:  # noqa: BLE001 - DOM sempat berganti saat dibaca
                page.wait_for_timeout(self._poll_ms)
                continue
            if on_token and len(cur) > emitted:
                on_token(cur[emitted:])
                emitted = len(cur)
            if cur and cur == last:
                stable += 1
                if stable >= self._stable_needed and self._is_done(page):
                    break
            else:
                stable = 0
                last = cur
            page.wait_for_timeout(self._poll_ms)

        if not last:
            raise BrowserError(
                f"tidak ada jawaban terbaca dari {self.label}. Coba periksa "
                "selektor pesan, atau kirim ulang."
            )
        return last

    def _connect_on_hub(
        self, h: Any, on_status: StatusCb | None, cancel_event: Any
    ) -> bool:
        from .. import llm  # impor tunda: hindari siklus impor

        def status(msg: str) -> None:
            if on_status:
                on_status(msg)

        def check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

        status(f"menghubungkan ke {self.label}…")
        _, did_login = self._acquire_ready_page(h, status, check_cancel)
        return did_login

    def _acquire_ready_page(
        self, h: Any, status: StatusCb, check_cancel: Callable[[], None]
    ) -> tuple[Any, bool]:
        """Kembalikan (page siap-pakai yang SUDAH login, apakah login BARU terjadi).

        Konsep connector: browser MUNCUL sekali untuk LOGIN, lalu MINGGIR
        (di-minimize) — seluruh proses & jawaban tampil di TERMINAL, pengguna tak
        menyentuh browser lagi. Kenapa bukan headless: situs seperti claude.ai
        pakai Cloudflare, dan clearance-nya terikat fingerprint browser TAMPIL —
        di headless ditolak. Jadi jendela tetap ada tapi disembunyikan (minimize).

        CONNECTOR_HEADLESS=true = paksa headless sejati (tak tampil sama sekali)
        untuk situs yang memang lolos tanpa Cloudflare (mis. sebagian akun Qwen).
        """
        # Opt-in: headless sejati (mungkin diblok anti-bot di sebagian situs).
        if config.CONNECTOR_HEADLESS:
            page = h.page_for(self.service, headless=True)
            if self._chat_ready(page, 1500):
                return page, False
            self._goto(page)
            if not self._chat_ready(page, 10000):
                raise BrowserError(
                    "mode headless belum siap (kemungkinan diblok anti-bot / "
                    "belum login). Hapus CONNECTOR_HEADLESS agar login via jendela."
                )
            return page, False

        # Default: jendela TAMPIL (lolos Cloudflare) lalu di-minimize.
        page = h.page_for(self.service, headless=False)
        # Sudah di percakapan aktif & login? Lanjutkan (jangan buka chat baru).
        if self._chat_ready(page, 1500):
            self._minimize(page)
            return page, False

        self._goto(page)
        did_login = False
        if not self._chat_ready(page, 8000):
            # BELUM login (masih di halaman login/auth) -> user HARUS sign-in
            # sungguhan di jendela; kita menunggu sampai benar-benar masuk chat.
            status(
                "🔐 Silakan SIGN-IN di jendela Chrome yang terbuka "
                "(email/Google + kode/CAPTCHA). Aku tunggu sampai selesai…"
            )
            self._wait_login(page, check_cancel)
            status("login berhasil ✓ — jendela diminimalkan, lanjut di terminal")
            did_login = True
        self._minimize(page)
        return page, did_login

    def _minimize(self, page: Any) -> None:
        """Sembunyikan jendela browser (minimize) via CDP — pengguna cukup pakai
        terminal. Diam-diam gagal bila tak didukung."""
        try:
            cdp = page.context.new_cdp_session(page)
            info = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": info["windowId"],
                "bounds": {"windowState": "minimized"},
            })
        except Exception:  # noqa: BLE001
            pass

    def _goto(self, page: Any) -> None:
        try:
            page.goto(self.chat_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"gagal membuka {self.chat_url}: {exc}") from exc

    def _wait_login(self, page: Any, check_cancel: Callable[[], None]) -> None:
        """Tunggu pengguna BENAR-BENAR menyelesaikan sign-in di jendela Chrome."""
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            check_cancel()
            try:
                if page.is_closed():
                    raise BrowserError(
                        "jendela Chrome ditutup sebelum login selesai. "
                        "Pilih ulang modelnya untuk mencoba lagi."
                    )
            except BrowserError:
                raise
            except Exception:  # noqa: BLE001
                pass
            if self._chat_ready(page, 2000):
                return
            try:
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001 - page mati saat menunggu
                raise BrowserError(
                    "jendela Chrome tertutup saat menunggu login. Coba lagi."
                )
        raise BrowserError(
            "login tidak selesai dalam waktu yang ditentukan. Coba lagi."
        )

    def _on_login_page(self, page: Any) -> bool:
        """True bila page sedang di halaman login/auth (claude.ai/login, Google
        sign-in, dsb) — dipastikan lewat URL, bukan tebakan elemen."""
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(m in url for m in self.login_url_markers)

    def _chat_ready(self, page: Any, timeout_ms: int) -> bool:
        """Deteksi KETAT bahwa halaman chat siap & user SUDAH login:
        (1) URL BUKAN halaman login/auth, dan (2) kotak input chat terlihat.
        Halaman login yang kebetulan punya elemen mirip input tak akan lolos."""
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            if not self._on_login_page(page):
                try:
                    el = page.query_selector(self.input_selector)
                    if el is not None and el.is_visible():
                        return True
                except Exception:  # noqa: BLE001 - DOM/page sedang transisi
                    pass
            if time.time() >= deadline:
                return False
            try:
                page.wait_for_timeout(250)
            except Exception:  # noqa: BLE001
                return False

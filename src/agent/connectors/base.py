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

    # Batas waktu (detik).
    login_timeout: float = 300.0     # tunggu pengguna menyelesaikan login
    answer_timeout: float = 300.0    # tunggu jawaban selesai
    # Berapa kali cek berturut-turut teks tak berubah -> dianggap selesai.
    _stable_needed: int = 5
    _poll_ms: int = 400

    # ---- API publik ----
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
        page = self._acquire_ready_page(h, status, check_cancel)

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

    def _acquire_ready_page(
        self, h: Any, status: StatusCb, check_cancel: Callable[[], None]
    ) -> Any:
        """Kembalikan page siap-pakai yang SUDAH login.

        Alur sesuai konsep connector: chat berjalan di LATAR (headless) sehingga
        seluruh proses & jawaban tampil di TERMINAL, bukan browser. Browser hanya
        MUNCUL sekali saat perlu LOGIN, lalu ditutup & dilanjutkan di latar.
        (CONNECTOR_HEADLESS=false memaksa jendela selalu tampil — jalan keluar
        bila mode latar diblokir anti-bot.)
        """
        chat_headless = config.CONNECTOR_HEADLESS

        page = h.page_for(self.service, headless=chat_headless)
        self._goto(page)
        if self._input_ready(page, 8000):
            return page  # sudah login -> langsung jalan (di latar)

        # Belum login -> pastikan jendela TAMPIL untuk login manual.
        if chat_headless:
            status("belum login → membuka Chrome untuk login (sekali saja)…")
            h.drop(self.service)
            page = h.page_for(self.service, headless=False)
            self._goto(page)

        status(
            "🔐 Silakan LOGIN di jendela Chrome yang terbuka "
            "(termasuk CAPTCHA/2FA). Menunggu…"
        )
        self._wait_login(page, check_cancel)

        if chat_headless:
            # Sesi login tersimpan -> tutup jendela & lanjut di LATAR (terminal).
            status("login berhasil ✓ — lanjut di terminal (browser di latar)")
            h.drop(self.service)
            page = h.page_for(self.service, headless=True)
            self._goto(page)
            if not self._input_ready(page, 15000):
                raise BrowserError(
                    "login OK tapi sesi mode-latar belum siap (mungkin diblok "
                    "anti-bot). Coba lagi, atau set CONNECTOR_HEADLESS=false."
                )
        else:
            status("login berhasil ✓")
        return page

    def _goto(self, page: Any) -> None:
        try:
            page.goto(self.chat_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"gagal membuka {self.chat_url}: {exc}") from exc

    def _wait_login(self, page: Any, check_cancel: Callable[[], None]) -> None:
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            check_cancel()
            if self._input_ready(page, 2000):
                return
            page.wait_for_timeout(1500)
        raise BrowserError(
            "login tidak selesai dalam waktu yang ditentukan. Coba lagi."
        )

    def _input_ready(self, page: Any, timeout_ms: int) -> bool:
        """True bila kotak input terlihat (indikator halaman chat siap/login OK)."""
        try:
            page.wait_for_selector(
                self.input_selector, timeout=timeout_ms, state="visible"
            )
            return True
        except Exception:  # noqa: BLE001
            return False

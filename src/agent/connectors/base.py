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
    headless: bool = False     # headed lebih andal melawan anti-bot; login butuh ini

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

        status("membuka browser…")
        page = h.page_for(self.service, self.headless)
        self._ensure_ready(page, status, check_cancel)

        # --- kirim prompt ---
        check_cancel()
        status(f"mengetik pesan ke {self.label}…")
        box = page.query_selector(self.input_selector)
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

    def _ensure_ready(
        self, page: Any, status: StatusCb, check_cancel: Callable[[], None]
    ) -> None:
        """Pastikan halaman chat siap & sudah login (bila belum, tunggu login)."""
        try:
            page.goto(self.chat_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"gagal membuka {self.chat_url}: {exc}") from exc

        if self._input_ready(page, 6000):
            return

        # Belum login / halaman login tampil -> minta pengguna login manual.
        status(
            "🔐 Silakan LOGIN di jendela browser yang terbuka "
            "(termasuk CAPTCHA/2FA). Menunggu…"
        )
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            check_cancel()
            if self._input_ready(page, 2000):
                status("login terdeteksi ✓")
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

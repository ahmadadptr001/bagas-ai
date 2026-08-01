"""Tool pratinjau halaman web — menutup loop "tulis kode UI → LIHAT hasilnya".

web_preview membuka sebuah URL (paling sering dev server localhost) di browser
Chromium HEADLESS milik Playwright — dependensi yang memang sudah dibawa
bagas-ai untuk connector — lalu mengembalikan status HTTP, judul, error
console/JS/permintaan-gagal, dan SCREENSHOT halamannya. Penanda [GAMBAR] pada
hasil membuat core melampirkan PNG-nya ke pesan berikutnya sehingga model
benar-benar MELIHAT tampilan yang ia bangun, bukan menebak dari kode.

Berbeda dari take_screenshot (memotret layar/desktop pengguna): ini merender
URL-nya sendiri, jadi bekerja tanpa perlu browser user terbuka di halaman itu.
Instance Playwright-nya terpisah dari hub connector (dibuat & ditutup per
panggilan) — tak mengganggu sesi chat web yang sedang berjalan.
"""
from __future__ import annotations

import re
import time

from .base import tool
from .files import _display, _safe_path
from .screen import IMAGE_MARK

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SC_DIR = "screenshots"
# Batas jumlah error per kategori yang dilaporkan (halaman rusak bisa memuntahkan
# ratusan error identik — sisanya cuma bising).
_MAX_ERR = 8


@tool
def web_preview(url: str, wait_seconds: float = 2.0, full_page: bool = False) -> str:
    """LIHAT SENDIRI hasil halaman web/dev server: buka URL (mis. localhost:3000) di browser headless, lalu dapatkan status HTTP, judul, error console/JS, dan screenshot halamannya (otomatis terlampir ke pesan berikutnya). WAJIB dipakai sesudah mengubah kode UI — jangan mengaku tampilannya benar tanpa melihatnya.

    Server harus sudah berjalan (nyalakan lewat run_command_bg, mis.
    'npm run dev', tunggu siap via bg_output, baru panggil ini).

    url: alamat halaman (mis. 'http://localhost:3000' — 'localhost:3000' juga
        diterima, otomatis diberi http://).
    wait_seconds: jeda ekstra setelah halaman load (0-10 dtk, default 2) —
        beri waktu SPA/animasi selesai merender.
    full_page: true = screenshot seluruh panjang halaman, bukan cuma viewport.
    """
    u = (url or "").strip()
    if not u:
        return "[error] url kosong — contoh: web_preview('http://localhost:3000')"
    if not _URL_RE.match(u):
        u = "http://" + u
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("[GAGAL] Playwright belum terpasang — jalankan: "
                "pip install playwright && python -m playwright install chromium")

    try:
        wait = max(0.0, min(float(wait_seconds), 10.0))
    except (TypeError, ValueError):
        wait = 2.0
    # Milidetik ikut dipakai: dua preview dalam detik yang sama (halaman
    # sebelum/sesudah perbaikan) tak boleh saling menimpa.
    rel = (f"{_SC_DIR}/preview-{time.strftime('%Y%m%d-%H%M%S')}"
           f"-{int(time.time() * 1000) % 1000:03d}.png")
    try:
        target = _safe_path(rel)
    except ValueError as e:
        return f"[GAGAL] {e}"
    target.parent.mkdir(parents=True, exist_ok=True)

    console_err: list[str] = []
    js_err: list[str] = []
    req_gagal: list[str] = []
    status = None
    title = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, timeout=60000)
            try:
                page = browser.new_page(
                    viewport={"width": 1280, "height": 800})
                page.on("console", lambda m: console_err.append(
                    m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: js_err.append(str(e)))
                page.on("requestfailed", lambda r: req_gagal.append(
                    f"{r.url} ({r.failure or 'gagal'})"))
                resp = page.goto(u, wait_until="load", timeout=20000)
                if wait:
                    page.wait_for_timeout(int(wait * 1000))
                status = resp.status if resp is not None else None
                title = page.title() or ""
                page.screenshot(path=str(target), full_page=bool(full_page))
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - galat Playwright beragam & mentah
        pesan = str(exc).splitlines()[0] if str(exc) else repr(exc)
        if "ERR_CONNECTION_REFUSED" in pesan or "net::ERR" in pesan:
            return (f"[GAGAL] tak bisa membuka {u}: {pesan}\n"
                    "Kemungkinan servernya BELUM berjalan. Nyalakan dulu "
                    "(run_command_bg, mis. 'npm run dev'), pastikan siap lewat "
                    "bg_output, lalu panggil web_preview lagi.")
        if "Executable doesn't exist" in pesan or "playwright install" in pesan:
            return ("[GAGAL] browser Chromium Playwright belum terunduh — "
                    "jalankan: python -m playwright install chromium")
        if "Timeout" in pesan or "timeout" in pesan:
            return (f"[GAGAL] {u} tidak selesai dimuat dalam 20 detik: {pesan}\n"
                    "Server lambat/menggantung? Cek lognya via bg_output.")
        return f"[GAGAL] web_preview {u}: {pesan}"

    baris = [
        f"web_preview {u} — HTTP {status if status is not None else '?'} · "
        f"judul: {title or '(tanpa judul)'}"
    ]
    if status is not None and status >= 400:
        baris.append(f"⚠ Server membalas ERROR {status} — halaman ini bukan "
                     "tampilan yang diinginkan. Periksa log servernya.")

    def _bagian(nama: str, items: list[str]) -> None:
        if not items:
            return
        tampil = items[:_MAX_ERR]
        lebih = f"\n  … {len(items) - _MAX_ERR} lagi" if len(items) > _MAX_ERR else ""
        baris.append(f"{nama} ({len(items)}):\n  " + "\n  ".join(
            t.replace("\n", " ")[:300] for t in tampil) + lebih)

    _bagian("✗ Error console", console_err)
    _bagian("✗ Error JavaScript (uncaught)", js_err)
    _bagian("⚠ Permintaan gagal", req_gagal)
    if not (console_err or js_err):
        baris.append("✓ Tidak ada error console/JS.")
    baris.append(f"Screenshot: {_display(target)} — lihat gambarnya untuk "
                 "menilai tampilan (layout, warna, konten).")
    baris.append(f"{IMAGE_MARK} {target}")
    return "\n".join(baris)

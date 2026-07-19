"""Connector claude.ai (web).

Kalau claude.ai mengubah layout & jawaban tak lagi terbaca, cukup sesuaikan
SELECTOR di bawah — sisanya (kirim, tunggu, streaming) ditangani base.py.
"""
from __future__ import annotations

from typing import Any

from .base import WebConnector
from .browser import BrowserError, hub

# Kelola percakapan lewat API internal claude.ai, dijalankan DI DALAM halaman
# yang sudah login (cookie ikut otomatis). Jauh lebih andal daripada mengeklik
# menu satu per satu, dan cukup satu permintaan per percakapan.
_JS_LIST_CHATS = r"""
async () => {
  const r = await fetch('/api/organizations', {credentials: 'include'});
  if (!r.ok) return {error: 'organizations HTTP ' + r.status};
  const orgs = await r.json();
  if (!Array.isArray(orgs) || !orgs.length) return {error: 'organisasi tak ditemukan'};
  const org = orgs[0].uuid;
  const r2 = await fetch(`/api/organizations/${org}/chat_conversations`,
                         {credentials: 'include'});
  if (!r2.ok) return {error: 'chat_conversations HTTP ' + r2.status};
  const chats = await r2.json();
  return {org, chats: (Array.isArray(chats) ? chats : []).map(c => ({
    id: c.uuid, title: c.name || '(tanpa judul)',
    created: c.created_at, updated: c.updated_at,
  }))};
}
"""

_JS_DELETE_CHATS = r"""
async (ids) => {
  const r = await fetch('/api/organizations', {credentials: 'include'});
  if (!r.ok) return {deleted: 0, error: 'organizations HTTP ' + r.status};
  const orgs = await r.json();
  if (!Array.isArray(orgs) || !orgs.length) return {deleted: 0, error: 'no org'};
  const org = orgs[0].uuid;
  let deleted = 0; const failed = [];
  for (const id of ids) {
    try {
      const res = await fetch(
        `/api/organizations/${org}/chat_conversations/${id}`,
        {method: 'DELETE', credentials: 'include'});
      if (res.ok || res.status === 404) deleted++; else failed.push(res.status);
    } catch (e) { failed.push(String(e)); }
  }
  return {deleted, failed};
}
"""


class ClaudeConnector(WebConnector):
    service = "claude"
    label = "Claude (web)"
    chat_url = "https://claude.ai/new"
    chat_url_template = "https://claude.ai/chat/{id}"
    # Kotak input Claude = editor ProseMirror (contenteditable), bukan textarea.
    input_selector = 'div[contenteditable="true"]'
    input_is_contenteditable = True
    # Wadah balasan — DIVERIFIKASI LANGSUNG ke claude.ai (2026-07-19):
    #   .standard-markdown   = teks jawaban BERSIH (tanpa jejak "Thought for..",
    #                          tanpa prefix sr-only "Claude responded:").
    #   div[data-is-streaming] = wadah pesan asisten (cadangan; teks agak kotor).
    #   .font-claude-response  = wrapper respons (cadangan terakhir).
    # (Selektor lama .font-claude-message SUDAH TIDAK ADA -> dulu jawaban tak
    #  pernah terbaca; inilah akar bug "browser jawab, terminal kosong".)
    message_selector = (
        ".standard-markdown",
        "div[data-is-streaming]",
        ".font-claude-response",
    )
    submit_key = "Enter"
    read_as_markdown = True  # jawaban Claude penuh markdown (list/tabel/kode)
    # Penanda "sedang mengetik": atribut data-is-streaming="true" (verified).
    streaming_selector = '[data-is-streaming="true"]'
    # Saat Claude masih BERPIKIR, satu-satunya teks yang terbaca adalah indikator
    # "Thought for 2s" (kadang berulang). Itu BUKAN jawaban — kalau dianggap
    # jawaban, giliran berhenti dini & usulan tool tak pernah terbaca.
    noise_pattern = r"(?:Thought for[^\n]*\s*)+"

    # Tombol/opsi UI yang bisa diklik program lewat /effort (DIVERIFIKASI live):
    #   tombol pemilih model: data-testid="model-selector-dropdown"
    #   varian model (role=menuitemradio): Sonnet 5 / Haiku 4.5.
    #   submenu Effort (role=menuitem "Effort") -> Low/Medium/High/Extra/Max.
    #   ("Medium" ambigu dg pembuka submenu -> cocokkan "Default" utk level Medium.)
    # Model berlabel "Pro" (Opus 4.8, Fable 5) SENGAJA TIDAK ditawarkan: memilihnya
    # hanya memunculkan ajakan "Upgrade" dan tak mengganti model apa pun.
    web_model_button = 'button[data-testid="model-selector-dropdown"]'
    web_actions = (
        ("Sonnet 5", ("Sonnet 5",), "model cepat & efisien"),
        ("Haiku 4.5", ("Haiku 4.5",), "model tercepat"),
        ("Effort: Low", ("Effort", "Low"), "usaha berpikir minimal"),
        ("Effort: Medium", ("Effort", "Default"), "usaha berpikir sedang (default)"),
        ("Effort: High", ("Effort", "High"), "usaha berpikir tinggi"),
        ("Effort: Extra", ("Effort", "Extra"), "usaha berpikir ekstra"),
        ("Effort: Max", ("Effort", "Max"), "usaha berpikir maksimum"),
    )

    # Tombol "stop" hanya ada SELAMA Claude membalas — sinyal paling andal bahwa
    # respons masih berjalan (atribut data-is-streaming sempat hilang saat fase
    # berpikir, sehingga tak cukup diandalkan sendirian).
    _STOP_SELECTORS = (
        '[data-testid="stop-button"]',
        'button[aria-label*="Stop"]',
        'button[aria-label*="stop"]',
    )

    def _is_done(self, page: Any) -> bool:
        try:
            if page.query_selector('[data-is-streaming="true"]') is not None:
                return False
            for sel in self._STOP_SELECTORS:
                if page.query_selector(sel) is not None:
                    return False
            return True
        except Exception:  # noqa: BLE001
            return True

    # ---- pengelolaan percakapan (agar chat tak menumpuk di akun) ----
    def supports_chat_admin(self) -> bool:
        return True

    def list_chats(self) -> list[dict]:
        def job(h):
            page, _ = self._acquire_ready_page(h, lambda m: None, lambda: None)
            return page.evaluate(_JS_LIST_CHATS)

        res = hub().submit(job, timeout=self.login_timeout + 60) or {}
        if res.get("error"):
            raise BrowserError(f"gagal membaca daftar chat: {res['error']}")
        return res.get("chats", [])

    def delete_chats(self, ids: list[str]) -> int:
        ids = [i for i in (ids or []) if i]
        if not ids:
            return 0

        def job(h):
            page, _ = self._acquire_ready_page(h, lambda m: None, lambda: None)
            return page.evaluate(_JS_DELETE_CHATS, ids)

        res = hub().submit(job, timeout=self.login_timeout + 30 + len(ids) * 3) or {}
        if res.get("error"):
            raise BrowserError(f"gagal menghapus chat: {res['error']}")
        return int(res.get("deleted", 0))

"""Kerangka connector web-AI: buka halaman chat, ketik prompt, tunggu jawaban.

Satu WebConnector = satu situs (Claude, Qwen, dst). Tiap subclass cukup mengisi
SELECTOR & URL situsnya; algoritma kirim + tunggu-jawaban ada di sini dan dibuat
TAHAN-BANTING:
  - Deteksi login KETAT: URL bukan halaman login/auth DAN kotak input terlihat
    (halaman login yang kebetulan punya elemen mirip input tak akan lolos).
  - Pembacaan jawaban memakai BEBERAPA kandidat selector + pemantauan teks
    sampai STABIL (berhenti bertambah) — bertahan walau layout situs berubah.
  - SEMUA operasi dibatasi waktu & bisa dibatalkan (cancel_event) — tak ada
    yang boleh menggantung selamanya (lihat juga timeout submit di browser.py).

Login: pertama kali dipakai, jendela Chrome TAMPIL dan pengguna sign-in manual
(termasuk CAPTCHA/2FA). Sesi disimpan permanen (persistent context), jadi
berikutnya otomatis. Semua aksi Playwright dijalankan di thread hub (browser.py).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .. import config
from .browser import BrowserError, hub, profile_dir

StatusCb = Callable[[str], None]
TokenCb = Callable[[str], None]


# Serializer DOM -> Markdown (dijalankan DI HALAMAN). inner_text() membuang
# struktur (bullet, tabel, heading, blok kode) sehingga jawaban tampil polos di
# terminal; ini merekonstruksi markdown dari HTML yang sudah dirender situs agar
# rich bisa menampilkannya rapi (list, tabel, kode, bold, tautan).
JS_TO_MARKDOWN = r"""
(selectors) => {
  let el = null;
  for (const s of selectors) {
    const nodes = document.querySelectorAll(s);
    if (nodes.length) { el = nodes[nodes.length - 1]; break; }
  }
  if (!el) return "";

  function listItems(listEl, ordered) {
    let out = ""; let i = 1;
    for (const li of listEl.children) {
      if (li.tagName.toLowerCase() !== "li") continue;
      const marker = ordered ? (i + ".") : "-";
      let content = ser(li).trim().replace(/\n{2,}/g, "\n").replace(/\n/g, "\n  ");
      out += marker + " " + content + "\n";
      i++;
    }
    return out;
  }
  function table(t) {
    const rows = [];
    for (const tr of t.querySelectorAll("tr")) {
      const cells = [];
      for (const c of tr.querySelectorAll("th,td"))
        cells.push((c.innerText || "").trim().replace(/\|/g, "\\|").replace(/\n/g, " "));
      if (cells.length) rows.push(cells);
    }
    if (!rows.length) return "";
    let out = "| " + rows[0].join(" | ") + " |\n";
    out += "| " + rows[0].map(() => "---").join(" | ") + " |\n";
    for (let r = 1; r < rows.length; r++) out += "| " + rows[r].join(" | ") + " |\n";
    return out + "\n";
  }
  function codeFence(pre) {
    const codeEl = pre.querySelector("code");
    let lang = "";
    if (codeEl && codeEl.className) {
      const m = codeEl.className.match(/language-([\w+-]+)/);
      if (m) lang = m[1];
    }
    const code = (codeEl ? codeEl.textContent : pre.textContent).replace(/\n$/, "");
    return "\n```" + lang + "\n" + code + "\n```\n\n";
  }
  function ser(node) {
    let out = "";
    for (const ch of node.childNodes) {
      if (ch.nodeType === 3) { out += ch.textContent; continue; }
      if (ch.nodeType !== 1) continue;
      const tag = ch.tagName.toLowerCase();
      // Buang chrome UI (tombol salin/svg) yang bukan isi jawaban.
      if (tag === "button" || tag === "svg") continue;
      if (/^h[1-6]$/.test(tag)) {
        out += "\n" + "#".repeat(+tag[1]) + " " + (ch.innerText || "").trim() + "\n\n";
      } else if (tag === "p") {
        out += ser(ch).trim() + "\n\n";
      } else if (tag === "br") {
        out += "\n";
      } else if (tag === "strong" || tag === "b") {
        out += "**" + ser(ch).trim() + "**";
      } else if (tag === "em" || tag === "i") {
        out += "*" + ser(ch).trim() + "*";
      } else if (tag === "del" || tag === "s") {
        out += "~~" + ser(ch).trim() + "~~";
      } else if (tag === "pre") {
        out += codeFence(ch);
      } else if (tag === "code") {
        out += "`" + ch.textContent + "`";
      } else if (tag === "ul") {
        out += "\n" + listItems(ch, false) + "\n";
      } else if (tag === "ol") {
        out += "\n" + listItems(ch, true) + "\n";
      } else if (tag === "blockquote") {
        const inner = ser(ch).trim();
        out += inner.split("\n").map(l => "> " + l).join("\n") + "\n\n";
      } else if (tag === "a") {
        const href = ch.getAttribute("href") || "";
        const txt = ser(ch).trim();
        out += href ? ("[" + txt + "](" + href + ")") : txt;
      } else if (tag === "table") {
        out += "\n" + table(ch);
      } else if (tag === "hr") {
        out += "\n---\n\n";
      } else if (tag === "li") {
        out += ser(ch);
      } else {
        // Wadah code-block (div pembungkus dg header bahasa + tombol salin):
        // kalau elemen ini memuat <pre> dan sisa teksnya PENDEK (cuma label
        // bahasa/salin), emit kode-nya saja supaya label tak bocor jadi teks.
        const pre = ch.querySelector ? ch.querySelector("pre") : null;
        if (pre) {
          const extra = (ch.innerText || "").length - (pre.innerText || "").length;
          if (extra < 40) { out += codeFence(pre); continue; }
        }
        out += ser(ch);
      }
    }
    return out;
  }
  return ser(el).replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}
"""


class WebConnector:
    """Basis connector. Subclass mengisi atribut kelas di bawah."""

    service: str = ""          # kunci internal & nama folder profil (mis. "claude")
    label: str = ""            # nama tampilan (mis. "Claude (web)")
    chat_url: str = ""         # halaman chat / sesi baru
    input_selector: str = ""   # kotak input (textarea / contenteditable)
    # Wadah pesan JAWABAN — boleh SATU selector (str) atau BEBERAPA kandidat
    # (tuple); dicoba berurutan, yang pertama menghasilkan teks dipakai.
    message_selector: str | tuple[str, ...] = ""
    input_is_contenteditable: bool = False
    submit_key: str = "Enter"  # tombol kirim
    # True = jawaban FINAL direkonstruksi jadi Markdown dari HTML (agar list,
    # tabel, heading, blok kode tampil rapi di terminal), bukan teks polos.
    read_as_markdown: bool = False
    # Penanda URL halaman LOGIN/AUTH: selama URL page mengandung salah satu ini,
    # user pasti BELUM login — jangan pernah dicap "siap" walau ada elemen input
    # yang kebetulan cocok selector (inilah sumber salah-deteksi sebelumnya).
    login_url_markers: tuple[str, ...] = (
        "login", "signin", "sign-in", "sign_in", "oauth", "/auth", "sso",
    )
    # Selector penanda "sedang mengetik/streaming" (bila situs punya).
    streaming_selector: str = ""
    # Teks yang BUKAN jawaban (chrome UI situs), mis. indikator berpikir
    # "Thought for 2s". Bila SELURUH teks yang terbaca hanya ini, artinya jawaban
    # BELUM muncul — jangan dianggap sebagai balasan (akar bug: giliran berhenti
    # lebih awal & mengembalikan "Thought for 2s" alih-alih jawaban asli).
    noise_pattern: str = ""

    # --- Aksi UI yang bisa DIKLIK program di situs (permintaan pengguna: ganti
    #     varian model & mode berpikir dari terminal via /effort). Tiap aksi =
    #     (label tampil, urutan teks yang diklik berurutan, deskripsi). Urutan
    #     >1 elemen dipakai untuk menu bertingkat (mis. buka "Effort" lalu "High").
    web_model_button: str = ""   # tombol pembuka menu (diklik dulu bila ada)
    web_actions: tuple[tuple[str, tuple[str, ...], str], ...] = ()

    # Batas waktu (detik).
    login_timeout: float = 300.0     # tunggu pengguna menyelesaikan login
    answer_timeout: float = 300.0    # tunggu jawaban selesai
    start_timeout: float = 90.0      # tunggu jawaban MULAI muncul
    # Berapa kali cek berturut-turut teks tak berubah -> dianggap selesai.
    _stable_needed: int = 5
    _poll_ms: int = 400

    # Pola menangkap ID percakapan dari URL (mis. claude.ai/chat/<uuid>).
    chat_id_pattern: str = r"/chat/([0-9a-fA-F-]{16,})"
    # ID percakapan yang terakhir dipakai (diisi tiap kali send selesai).
    last_chat_id: str = ""

    # ---- catatan chat yang DIBUAT bagas-ai (agar penghapusan aman) ----
    def _registry_path(self):
        """File catatan chat buatan bagas-ai untuk service ini."""
        return config.CONFIG_HOME / "browser" / f"{self.service}_chats.json"

    def own_chats(self) -> list[dict]:
        """Daftar chat yang DIBUAT bagas-ai (terbaru dulu). Dipakai agar fitur
        bersih-bersih tak pernah menyentuh percakapan pribadi pengguna."""
        try:
            data = json.loads(self._registry_path().read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_own_chats(self, rows: list[dict]) -> None:
        p = self._registry_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rows[:500], ensure_ascii=False),
                         encoding="utf-8")
        except OSError:
            pass

    def record_chat(self, chat_id: str, title: str = "") -> None:
        """Catat satu chat baru buatan bagas-ai."""
        if not chat_id:
            return
        rows = [r for r in self.own_chats() if r.get("id") != chat_id]
        rows.insert(0, {"id": chat_id, "title": (title or "")[:80],
                        "ts": time.time()})
        self._save_own_chats(rows)

    def forget_chats(self, ids: set[str]) -> None:
        """Buang beberapa chat dari catatan (setelah dihapus di situs)."""
        self._save_own_chats([r for r in self.own_chats()
                              if r.get("id") not in ids])

    def current_chat_id(self, page: Any) -> str:
        """ID percakapan yang sedang terbuka (dari URL), "" bila tak dikenali."""
        try:
            m = re.search(self.chat_id_pattern, page.url or "")
            return m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---- API publik ----
    def supports_chat_admin(self) -> bool:
        """True bila connector ini bisa mendaftar & menghapus chat di situs."""
        return False

    def list_chats(self) -> list[dict]:
        """Semua percakapan di akun situs: [{id, title, created, updated}]."""
        raise BrowserError(
            f"{self.label} belum mendukung pengelolaan chat dari bagas-ai.")

    def delete_chats(self, ids: list[str]) -> int:
        """Hapus percakapan berdasarkan ID; kembalikan jumlah yang terhapus."""
        raise BrowserError(
            f"{self.label} belum mendukung penghapusan chat dari bagas-ai.")

    def prune_own_chats(self, keep: int) -> int:
        """Hapus chat LAMA buatan bagas-ai, sisakan `keep` yang terbaru.
        Hanya menyentuh chat yang tercatat dibuat bagas-ai."""
        if keep < 0 or not self.supports_chat_admin():
            return 0
        rows = self.own_chats()
        extra = rows[keep:]
        if not extra:
            return 0
        ids = [r["id"] for r in extra if r.get("id")]
        try:
            n = self.delete_chats(ids)
        except BrowserError:
            return 0
        self.forget_chats(set(ids))
        return n

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
            lambda h: self._connect_on_hub(h, on_status, cancel_event),
            timeout=self.login_timeout + 90,
        )

    def send(
        self,
        prompt: str,
        *,
        on_status: StatusCb | None = None,
        on_token: TokenCb | None = None,
        cancel_event: Any = None,
        new_chat: bool = False,
        complete_when: Callable[[str], bool] | None = None,
    ) -> str:
        """Kirim prompt ke situs & kembalikan teks jawaban (lewat thread hub).

        `new_chat=True` memulai PERCAKAPAN BARU di situs (buang konteks chat lama)
        — dipakai pada pesan pertama tiap sesi bagas-ai supaya AI web tak terbawa
        konteks percakapan sebelumnya.

        `complete_when(teks)` (opsional) = syarat TAMBAHAN bahwa balasan sudah
        utuh. Dipakai pemanggil untuk menahan kesimpulan 'selesai' saat balasan
        masih setengah dirender (mis. blok usulan tool belum tertutup)."""
        return hub().submit(
            lambda h: self._send_on_hub(
                h, prompt, on_status, on_token, cancel_event, new_chat,
                complete_when),
            timeout=self.login_timeout + self.answer_timeout + 120,
        )

    def set_web_option(self, label: str) -> str:
        """Klik OPSI di UI web (varian model / mode berpikir) — dipakai /effort.
        `label` = label aksi dari web_options() (mis. "Sonnet 5", "Effort: High")."""
        path = next((p for lbl, p, _ in self.web_actions if lbl == label), None)
        if path is None:
            raise BrowserError(f"opsi '{label}' tak dikenal untuk {self.label}")
        return hub().submit(
            lambda h: self._set_action_on_hub(h, label, path),
            timeout=self.login_timeout + 60,
        )

    def web_options(self) -> list[tuple[str, str]]:
        """Daftar (label, deskripsi) opsi web yang bisa dikendalikan program."""
        return [(lbl, desc) for lbl, _path, desc in self.web_actions]

    # ---- hook opsional untuk subclass ----
    def _is_done(self, page: Any) -> bool:
        """Petunjuk KHUSUS-situs bahwa balasan sudah tuntas (mis. indikator
        streaming hilang). Default True -> murni andalkan kestabilan teks."""
        return True

    # ---- internal (berjalan DI thread hub) ----
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

    def _send_on_hub(
        self,
        h: Any,
        prompt: str,
        on_status: StatusCb | None,
        on_token: TokenCb | None,
        cancel_event: Any,
        new_chat: bool = False,
        complete_when: Callable[[str], bool] | None = None,
    ) -> str:
        from .. import llm  # untuk llm.Cancelled (impor tunda: hindari siklus)

        def status(msg: str) -> None:
            if on_status:
                on_status(msg)

        def check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

        status("menyiapkan sesi browser…")
        page, _ = self._acquire_ready_page(
            h, status, check_cancel, force_new_chat=new_chat)

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
        counts_before = self._msg_counts(page)
        if self.input_is_contenteditable:
            page.keyboard.insert_text(prompt)
        else:
            box.fill(prompt)
        page.keyboard.press(self.submit_key)

        # --- tunggu jawaban MULAI (streaming muncul / jumlah pesan bertambah) ---
        status(f"{self.label} sedang berpikir…")
        t0 = time.time()
        started = False
        while time.time() - t0 < self.start_timeout:
            check_cancel()
            if self._answer_started(page, counts_before):
                started = True
                break
            page.wait_for_timeout(300)
        if not started and not self._read_last_message(page):
            raise BrowserError(
                f"balasan tak terdeteksi dari {self.label} — kemungkinan selector "
                "pesan usang untuk layout situs sekarang. Laporkan/perbarui "
                "message_selector di connectors/"
                f"{self.service}.py."
            )

        # Jawaban sudah MULAI mengalir -> ubah fase jadi "menjawab" (bukan diam
        # di "berpikir"), supaya terminal mencerminkan keadaan sebenarnya.
        status(f"{self.label} sedang menjawab…")

        # --- pantau teks balasan terakhir sampai stabil ---
        last = ""
        emitted = 0
        stable = 0
        deadline = time.time() + self.answer_timeout
        while time.time() < deadline:
            check_cancel()
            cur = self._read_last_message(page)
            if not cur:
                page.wait_for_timeout(self._poll_ms)
                continue
            if on_token and len(cur) > emitted:
                on_token(cur[emitted:])
                emitted = len(cur)
            if cur == last:
                stable += 1
                # Selesai bila: teks berhenti berubah, situs tak lagi menandai
                # "sedang mengetik", DAN (bila diminta) balasan sudah utuh
                # menurut pemanggil — mencegah berhenti saat blok usulan tool
                # baru separuh dirender.
                if (stable >= self._stable_needed and self._is_done(page)
                        and (complete_when is None or complete_when(cur))):
                    break
            else:
                stable = 0
                last = cur
            page.wait_for_timeout(self._poll_ms)

        # Catat ID percakapan yang sedang dipakai (untuk fitur bersih-bersih).
        self.last_chat_id = self.current_chat_id(page)

        if not last:
            raise BrowserError(
                f"tidak ada jawaban terbaca dari {self.label}. Coba periksa "
                "selektor pesan, atau kirim ulang."
            )
        # Jawaban final: rekonstruksi Markdown dari HTML (list/tabel/heading/kode
        # utuh) bila diaktifkan; kalau gagal, pakai teks polos yang sudah stabil.
        if self.read_as_markdown:
            md = self._read_last_markdown(page)
            if md:
                return md
        return last

    def _set_action_on_hub(self, h: Any, label: str, path: tuple[str, ...]) -> str:
        """Klik aksi UI (varian model / effort). Buka tombol menu bila ada, lalu
        klik tiap teks di `path` berurutan (dukungan menu bertingkat)."""
        page, _ = self._acquire_ready_page(h, lambda m: None, lambda: None)

        opened = False
        if self.web_model_button:
            try:
                btn = page.query_selector(self.web_model_button)
                if btn is not None and btn.is_visible():
                    btn.click()
                    opened = True
                    page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001
                opened = False

        try:
            for i, text in enumerate(path):
                self._click_menu_text(page, text)
                # Jeda antar-tingkat agar submenu sempat muncul.
                page.wait_for_timeout(500 if i < len(path) - 1 else 250)
        except Exception as exc:
            if opened:
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            raise BrowserError(
                f"opsi '{label}' tak bisa diklik di UI {self.label} — "
                "situs mungkin berubah layout / teksnya beda."
            ) from exc
        try:  # tutup menu bila masih terbuka
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        return f"'{label}' dipilih di {self.label}"

    def _click_menu_text(self, page: Any, text: str) -> None:
        """Klik item menu (menuitem/menuitemradio/option) yang memuat `text`.
        Diutamakan item menu agar tak salah klik elemen lain berteks sama."""
        esc = text.replace('"', '\\"')
        loc = page.locator(
            f'[role="menuitemradio"]:has-text("{esc}"), '
            f'[role="menuitem"]:has-text("{esc}"), '
            f'[role="option"]:has-text("{esc}")'
        ).first
        try:
            loc.scroll_into_view_if_needed(timeout=1500)
        except Exception:  # noqa: BLE001
            pass
        loc.click(timeout=4000)

    # ---- pembacaan pesan (multi-kandidat, tahan perubahan layout) ----
    def _msg_selectors(self) -> tuple[str, ...]:
        sel = self.message_selector
        return (sel,) if isinstance(sel, str) else tuple(sel)

    def _msg_counts(self, page: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for sel in self._msg_selectors():
            try:
                out[sel] = len(page.query_selector_all(sel))
            except Exception:  # noqa: BLE001
                out[sel] = 0
        return out

    def _answer_started(self, page: Any, counts_before: dict[str, int]) -> bool:
        if self.streaming_selector:
            try:
                if page.query_selector(self.streaming_selector) is not None:
                    return True
            except Exception:  # noqa: BLE001
                pass
        now = self._msg_counts(page)
        return any(now.get(s, 0) > n for s, n in counts_before.items())

    def _is_noise(self, text: str) -> bool:
        """True bila teks HANYA chrome UI situs (mis. 'Thought for 2s'), bukan
        jawaban sesungguhnya."""
        if not self.noise_pattern:
            return False
        return bool(re.fullmatch(self.noise_pattern, (text or "").strip(),
                                 re.DOTALL))

    def _read_last_message(self, page: Any) -> str:
        """Teks pesan jawaban TERAKHIR — kandidat selector dicoba berurutan.
        Dipakai untuk deteksi kestabilan (poll), jadi sengaja teks polos & cepat.
        Teks yang cuma indikator berpikir dilewati (bukan jawaban)."""
        for sel in self._msg_selectors():
            try:
                els = page.query_selector_all(sel)
                if els:
                    txt = (els[-1].inner_text() or "").strip()
                    if txt and not self._is_noise(txt):
                        return txt
            except Exception:  # noqa: BLE001 - DOM sedang transisi
                continue
        return ""

    def _read_last_markdown(self, page: Any) -> str:
        """Jawaban TERAKHIR sebagai Markdown (list/tabel/heading/kode utuh),
        direkonstruksi dari HTML yang dirender situs."""
        try:
            md = page.evaluate(JS_TO_MARKDOWN, list(self._msg_selectors()))
            return (md or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    # ---- kesiapan halaman & login ----
    def _acquire_ready_page(
        self, h: Any, status: StatusCb, check_cancel: Callable[[], None],
        force_new_chat: bool = False,
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
            if self._chat_ready(page, 1500, check_cancel):
                return page, False
            self._goto(page)
            if not self._chat_ready(page, 10000, check_cancel):
                raise BrowserError(
                    "mode headless belum siap (kemungkinan diblok anti-bot / "
                    "belum login). Hapus CONNECTOR_HEADLESS agar login via jendela."
                )
            return page, False

        # Default: jendela TAMPIL (lolos Cloudflare) lalu di-minimize.
        page = h.page_for(self.service, headless=False)
        # Sudah di percakapan aktif & login? Lanjutkan (jangan buka chat baru) —
        # KECUALI diminta memulai percakapan BARU (pesan pertama sesi bagas-ai),
        # supaya AI web tak terbawa konteks chat sebelumnya.
        if self._chat_ready(page, 1500, check_cancel):
            if force_new_chat:
                self._goto(page)          # buka chat baru (chat_url)
                self._chat_ready(page, 8000, check_cancel)
            self._minimize(page)
            return page, False

        self._goto(page)
        did_login = False
        if not self._chat_ready(page, 8000, check_cancel):
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
            if self._chat_ready(page, 2000, check_cancel):
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

    def _chat_ready(
        self,
        page: Any,
        timeout_ms: int,
        check_cancel: Callable[[], None] | None = None,
    ) -> bool:
        """Deteksi KETAT bahwa halaman chat siap & user SUDAH login:
        (1) URL BUKAN halaman login/auth, dan (2) kotak input chat terlihat.
        Halaman login yang kebetulan punya elemen mirip input tak akan lolos."""
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            if check_cancel is not None:
                check_cancel()
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

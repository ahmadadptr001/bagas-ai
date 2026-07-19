"""Agent inti: loop chat + tool-calling (streaming). Dipakai semua antarmuka.

Menangani: system prompt dinamis, streaming (token realtime + pembatalan),
pemilihan model & effort yang tersimpan, dan penyimpanan sesi.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from . import config, llm, models, prefs, prompts
from .memory import Memory
from .session import Session
from .tools import base as tools

# --- Protokol tool untuk CONNECTOR web (Claude/Qwen web) ---
# AI web tak punya function-calling API, jadi kita ajari ia "memanggil" tool
# dengan menuliskan blok teks bertanda kurung siku ganda yang mudah di-parse,
# lalu bagas-ai mengeksekusi tool itu SUNGGUHAN di laptop (mesin tool yang sama
# dengan model NVIDIA) dan mengirim balik hasilnya — berulang sampai selesai.
_WEB_TOOL_RE = re.compile(r"\[\[TOOL\]\](.*?)\[\[/TOOL\]\]", re.DOTALL)
# Pagar blok kode markdown (```json / ```): AI web sering merender usulan tool
# sebagai blok kode, jadi pagarnya harus dibuang sebelum JSON di-parse.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*")
# Batas langkah tool per giliran web (jaring anti-loop-liar).
_WEB_MAX_STEPS = 24
# Batas panjang hasil tool yang dikirim balik ke AI web (hemat & fokus).
_WEB_RESULT_CAP = 6000


def _web_tool_protocol() -> str:
    """Instruksi + katalog tool untuk AI web agar bisa BERTINDAK (edit file,
    jalankan perintah, cari web, dll) seperti model NVIDIA."""
    lines = []
    for sc in tools.get_schemas():
        fn = sc.get("function", sc)
        name = fn.get("name", "")
        desc = (fn.get("description", "") or "").strip().split("\n")[0]
        params = fn.get("parameters", {}).get("properties", {}) or {}
        req = set(fn.get("parameters", {}).get("required", []) or [])
        pieces = []
        for pn, pinfo in params.items():
            tag = pn + ("*" if pn in req else "")
            pieces.append(f"{tag}:{pinfo.get('type', 'any')}")
        lines.append(f"- {name}({', '.join(pieces)}) — {desc}")
    tools_text = "\n".join(lines)
    return (
        "Konteks jujur soal percakapan ini, supaya tidak salah paham:\n\n"
        "Pesan-pesan ini dikirim & dibaca oleh **bagas-ai**, sebuah program agent "
        "open-source yang berjalan di laptop saya (saya pemiliknya, dan saya yang "
        "meminta ini). Kamu TIDAK mengeksekusi apa pun sendiri — kamu tetap model "
        "bahasa yang hanya menghasilkan teks, dan kamu memang tidak punya akses "
        "langsung ke laptop saya. Perannya begini:\n"
        "  1. Kamu MENGUSULKAN satu langkah dalam format teks di bawah.\n"
        "  2. Program bagas-ai di laptop saya membaca usulan itu, MENJALANKANNYA "
        "di sana (dengan izin & tanggung jawab saya sebagai pemilik), lalu\n"
        "  3. mengirimkan hasil nyatanya kembali ke percakapan ini sebagai teks.\n\n"
        "Jadi kamu berperan sebagai PERENCANA/otak, dan program lokal itu yang "
        "jadi tangannya. Kamu tidak perlu mengklaim punya akses apa pun — cukup "
        "usulkan langkahnya, dan hasil eksekusi akan kulaporkan balik apa adanya. "
        "Kalau sebuah usulan gagal dijalankan, kamu akan menerima pesan errornya.\n\n"
        "FORMAT USULAN LANGKAH — JSON WAJIB di dalam blok kode ```json (supaya "
        "isinya tidak berubah saat dirender; teks biasa merusak karakter seperti "
        "__nama__ menjadi tebal):\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "<nama_tool>", "args": {"<param>": "<nilai>"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "Contoh mengusulkan pembuatan file:\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "write_file", "args": {"path": "contoh.py", '
        '"content": "def halo():\\n    print(\'hai\')\\n"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "Aturan praktis:\n"
        "1. JSON harus valid (escape newline sebagai \\n, kutip sebagai \\\") dan "
        "SELALU dibungkus ```json ... ``` di dalam penanda [[TOOL]].\n"
        "2. Boleh beberapa blok sekaligus bila langkahnya independen.\n"
        "3. Setelah kukirim balik hasilnya (ditandai [[HASIL <nama_tool>]]), "
        "lanjutkan berdasarkan hasil itu.\n"
        "4. Kalau tugas sudah selesai, balas biasa TANPA blok [[TOOL]] — itu "
        "kuanggap jawaban akhir.\n"
        "5. Untuk membuat/mengubah file, usulkan write_file (bukan menampilkan "
        "kode untuk saya salin manual), karena tujuan saya memang agar bagas-ai "
        "yang menuliskannya langsung ke proyek.\n"
        "6. Path file relatif terhadap folder proyek yang disebut di konteks.\n\n"
        f"LANGKAH yang bisa diusulkan (tanda * = wajib):\n{tools_text}"
    )


def _parse_web_tool_calls(text: str) -> list[dict]:
    """Ambil daftar {'name','arguments'} dari blok [[TOOL]] pada balasan AI web.

    Toleran terhadap cara AI web merender blok: pagar markdown (```json), spasi/
    baris kosong, dan teks tambahan sesudah objek JSON."""
    calls = []
    for m in _WEB_TOOL_RE.finditer(text or ""):
        body = _FENCE_RE.sub("", m.group(1) or "").strip()
        start = body.find("{")
        if start < 0:
            continue
        try:
            # raw_decode: berhenti di akhir objek JSON pertama, sisanya diabaikan.
            obj, _ = json.JSONDecoder().raw_decode(body[start:])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("name")
        args = obj.get("args") or obj.get("arguments") or {}
        if name and isinstance(args, dict):
            calls.append({"name": str(name), "arguments": args})
    return calls


def _web_reply_complete(text: str) -> bool:
    """False bila balasan AI web tampak MASIH ditulis: ada [[TOOL]] yang belum
    ditutup [[/TOOL]]. Dipakai agar bagas-ai tidak menganggap balasan selesai
    padahal blok usulan tool baru separuh dirender (akar bug 'AI cuma menjawab
    iya tanpa melakukan apa pun')."""
    t = text or ""
    return t.count("[[TOOL]]") == t.count("[[/TOOL]]")


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _est_messages(messages: list[dict[str, Any]]) -> int:
    total = sum(len(str(m.get("content", "") or "")) for m in messages)
    return total // 4


class Usage:
    """Akumulator token (energi AI)."""

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, usage: Any) -> None:
        if not usage:
            return
        self.prompt += getattr(usage, "prompt_tokens", 0) or 0
        self.completion += getattr(usage, "completion_tokens", 0) or 0

    def add_raw(self, prompt: int, completion: int) -> None:
        self.prompt += prompt
        self.completion += completion


class Agent:
    """Agent percakapan dengan kemampuan memanggil tools (streaming)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        tool_names: list[str] | None = None,
        max_iterations: int | None = None,
        session: Session | None = None,
    ) -> None:
        model_id = model or prefs.get_model() or config.CHAT_MODEL
        if "deepseek" in model_id.lower():
            # DeepSeek dihapus dari katalog (sering gagal dipakai) -> alihkan
            # preferensi lama ke model pengganti ACAK (tanpa pola, beban
            # menyebar antar model) & simpan agar migrasi sekali saja.
            picked = models.random_fallback()
            model_id = picked.id if picked else config.CHAT_MODEL
            prefs.save(model=model_id)
        self.model_spec = models.spec_for_id(model_id)
        self._init_effort()

        self.memory = Memory(system_prompt=prompts.build_system_prompt())
        self.tool_names = tool_names
        self.max_iterations = max_iterations or config.MAX_TOOL_ITERATIONS
        self.session = session

        self.tokens_session = Usage()
        self.tokens_last = Usage()
        self.tokens_live = 0  # nilai token realtime untuk tampilan

        # Auto-fallback: model yang sudah dicoba & berapa kali naik-kelas (per giliran).
        self._tried_models: set[str] = set()
        self._escalations = 0
        # Connector web: apakah konteks laptop/proyek sudah dikirim ke sesi web
        # ini (dikirim SEKALI sbg preamble pesan pertama; AI web ingat sepanjang chat).
        self._web_ctx_sent = False

        # Token SESI bersifat persisten: saat --resume, lanjutkan hitungan
        # sesi sebelumnya (bukan mulai dari nol).
        if session and getattr(session, "tokens", None):
            self.tokens_session.prompt = int(session.tokens.get("prompt", 0) or 0)
            self.tokens_session.completion = int(
                session.tokens.get("completion", 0) or 0
            )

        if session and session.messages:
            self.memory.load(session.messages)

    # --- model & effort ---
    def _init_effort(self) -> None:
        if self.model_spec.supports_effort():
            saved = prefs.get_effort()
            self.effort = (
                saved if saved in self.model_spec.effort_options()
                else self.model_spec.default_effort()
            )
        else:
            self.effort = None

    @property
    def model(self) -> str:
        return self.model_spec.id

    def set_model(self, name: str) -> str:
        self.model_spec = models.resolve(name)
        self.effort = self.model_spec.default_effort()
        prefs.save(model=self.model_spec.id, effort=self.effort)
        return self.model_spec.label

    def set_effort(self, name: str) -> str | None:
        if not self.model_spec.supports_effort():
            return None
        if name not in self.model_spec.effort_options():
            raise ValueError(f"Effort '{name}' tidak dikenal untuk model ini.")
        self.effort = name
        prefs.save(effort=name)
        return name

    # --- auto-fallback saat AI ngeloop / performanya turun -------------------
    def _escalate(self, reason: str) -> str | None:
        """Naik kelas ketika AI ngelantur: NAIKKAN effort dulu (murah), lalu GANTI
        model. Memory/konteks TIDAK disentuh -> percakapan tetap nyambung, agent
        lanjut dari titik yang sama dengan otak yang lebih kuat/berbeda.

        Return deskripsi perubahan, atau None bila tak ada yang bisa dinaikkan.
        """
        if not config.AUTO_FALLBACK:
            return None
        if self._escalations >= config.MAX_ESCALATIONS:
            return None
        # 1) Naikkan effort (paling murah & cepat, model tetap sama).
        if self.model_spec.supports_effort() and self.effort:
            opts = list(self.model_spec.effort_options().keys())
            if self.effort in opts:
                i = opts.index(self.effort)
                if i < len(opts) - 1:
                    old, self.effort = self.effort, opts[i + 1]
                    self._escalations += 1
                    return f"effort {old} → {self.effort} ({reason})"
        # 2) Ganti ke model lain yang belum dicoba di giliran ini — dipilih
        #    ACAK (bukan urutan katalog) agar pengalihan tak berpola: kegagalan
        #    tidak selalu jatuh ke model yang itu-itu saja.
        self._tried_models.add(self.model_spec.id)
        spec = models.random_fallback(self._tried_models)
        if spec is None:
            return None
        old = self.model_spec.label
        self.model_spec = spec
        self._tried_models.add(spec.id)
        self._init_effort()
        self._escalations += 1
        return f"model {old} → {spec.label} ({reason})"

    def refresh_system_prompt(self) -> None:
        """Bangun ulang system prompt (mis. setelah add-dir) & pasang ke memory."""
        self.memory.set_system(prompts.build_system_prompt())

    # --- sesi ---
    def reset(self) -> None:
        self.memory.reset()
        self._persist()

    def _persist(self) -> None:
        if self.session:
            try:
                self.session.save(
                    self.memory.messages,
                    tokens={
                        "prompt": self.tokens_session.prompt,
                        "completion": self.tokens_session.completion,
                    },
                )
            except OSError:
                pass

    # --- inti ---
    def run(
        self,
        user_input: Any,
        *,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        cancel_event: Any = None,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        """Proses satu giliran (streaming). Kembalikan teks jawaban final.

        `on_notice(teks)` dipanggil saat bagas-ai OTOMATIS naik-kelas (ganti
        effort/model) karena terdeteksi ngeloop / performanya menurun.

        `on_message(teks)` dipanggil untuk narasi antar-langkah (ketika agent
        menjelaskan apa yang akan dilakukan sebelum memakai tool).
        `on_tool_result(nama, hasil)` dipanggil SETELAH sebuah tool selesai —
        dipakai UI untuk menampilkan hasil (mis. output perintah) secara ringkas.
        `on_retry(percobaan, tunggu, exc)` dipanggil saat NVIDIA rate-limit dan
        bagas-ai menunggu lalu MELANJUTKAN langkah yang sama.
        Bila `cancel_event` diset di tengah jalan, melempar llm.Cancelled.

        Bila model aktif adalah CONNECTOR web (mis. Claude/Qwen web), giliran ini
        TIDAK memakai API NVIDIA maupun tool-calling — melainkan diteruskan ke
        situs webnya lewat browser (`on_status`/`on_token` untuk progres & teks).
        """
        if self.model_spec.is_web:
            return self._run_connector(
                user_input, cancel_event=cancel_event,
                on_status=on_status, on_token=on_token,
                on_tool=on_tool, on_message=on_message,
                on_tool_result=on_tool_result, on_notice=on_notice,
            )
        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0
        # Jatah naik-kelas dihitung ulang tiap giliran (masalah bisa spesifik tugas).
        self._escalations = 0
        self._tried_models = {self.model_spec.id}
        try:
            return self._run_loop(
                on_tool, on_message, cancel_event, on_retry, on_tool_result,
                on_notice,
            )
        except BaseException:
            # Apa pun yang gagal di tengah giliran (rate limit, error tool,
            # pembatalan), rapikan state tool yang menggantung supaya instruksi
            # pengguna & konteks tetap tersimpan dan panggilan API berikutnya
            # tetap valid. Berlaku untuk SEMUA antarmuka (CLI, API, Telegram),
            # bukan hanya CLI. Lalu lempar ulang agar pemanggil bisa menangani.
            self.memory.repair_dangling_tools()
            self._persist()
            raise

    def _run_connector(
        self,
        user_input: Any,
        *,
        cancel_event: Any = None,
        on_status: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> str:
        """Jalankan giliran lewat AI web (browser) sebagai AGENT penuh.

        AI web tak punya function-calling, jadi kita ajari ia memakai TOOLS lewat
        protokol teks (_web_tool_protocol): ia menuliskan blok [[TOOL]]{...}[[/TOOL]],
        bagas-ai MENGEKSEKUSI tool itu sungguhan di laptop (mesin tool yang sama
        dengan model NVIDIA), lalu mengirim balik hasilnya — berulang sampai AI web
        menjawab tanpa blok tool (jawaban akhir). Dengan begitu Claude/Qwen web bisa
        mengedit file, menjalankan perintah, mencari web, dll — setara model NVIDIA.

        Web-AI menyimpan konteks percakapannya SENDIRI di sesi browser; memory
        bagas-ai tetap mencatat transkrip agar tampil di UI & tersimpan.
        """
        from . import connectors  # impor tunda: Playwright opsional

        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0
        user_text = str(user_input)

        if not connectors.playwright_available():
            answer = (
                "Fitur connector web butuh Playwright + browser Chromium yang "
                "belum terpasang. Jalankan:\n\n"
                "    pip install playwright\n"
                "    playwright install chromium\n\n"
                "lalu coba lagi. Atau kembali ke model NVIDIA dengan /model."
            )
            self.memory.add_assistant_text(answer)
            self._persist()
            return answer

        # Pesan PERTAMA sesi web memuat: protokol tool + konteks laptop/proyek
        # (keduanya SEKALI saja — AI web mengingatnya sepanjang chat).
        include_ctx = not self._web_ctx_sent
        first_msg = user_text
        if include_ctx:
            preamble = _web_tool_protocol()
            try:
                ctx = prompts.build_web_context()
            except Exception:  # noqa: BLE001
                ctx = ""
            if ctx:
                preamble += "\n\n" + ctx
            first_msg = preamble + "\n\n==========\nPERMINTAAN SAYA:\n" + user_text

        conn = connectors.get_connector(self.model_spec.connector)
        prompt_chars = 0
        reply_chars = 0
        answer = ""

        def _sync_tokens() -> None:
            """Perbarui hitungan token (estimasi) agar penghitung di UI hidup
            selama giliran berjalan, bukan melompat di akhir."""
            self.tokens_live = (prompt_chars + reply_chars) // 4

        def _send(msg: str, new_chat: bool = False) -> str:
            nonlocal prompt_chars, reply_chars
            prompt_chars += len(msg)
            _sync_tokens()
            out = conn.send(
                msg, on_status=on_status, on_token=on_token,
                cancel_event=cancel_event, new_chat=new_chat,
                complete_when=_web_reply_complete,
            )
            reply_chars += len(out or "")
            _sync_tokens()
            return out

        try:
            # Pesan pertama sesi: mulai CHAT BARU di situs supaya AI web tidak
            # terbawa konteks percakapan sebelumnya (mis. proyek lain).
            reply = _send(first_msg, new_chat=include_ctx)
            if include_ctx:
                self._web_ctx_sent = True
                # Catat percakapan baru ini + rapikan chat lama buatan bagas-ai
                # supaya tak menumpuk di akun (chat pribadi tak tersentuh).
                try:
                    chat_id = getattr(conn, "last_chat_id", "")
                    if chat_id:
                        conn.record_chat(chat_id, user_text[:80])
                        if config.CONNECTOR_KEEP_CHATS > 0:
                            conn.prune_own_chats(config.CONNECTOR_KEEP_CHATS)
                except Exception:  # noqa: BLE001 - bersih-bersih tak boleh menggagalkan giliran
                    pass

            steps = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise llm.Cancelled()
                calls = _parse_web_tool_calls(reply)
                if not calls or steps >= _WEB_MAX_STEPS:
                    # Tak ada tool -> ini jawaban AKHIR. Bersihkan sisa penanda.
                    answer = _WEB_TOOL_RE.sub("", reply).strip() or reply.strip()
                    if steps >= _WEB_MAX_STEPS and calls:
                        answer += ("\n\n_(batas langkah tool tercapai — sebagian "
                                   "aksi mungkin belum tuntas.)_")
                    break

                # Narasi sebelum tool (teks di luar blok [[TOOL]]).
                narration = _WEB_TOOL_RE.sub("", reply).strip()
                if narration and on_message:
                    on_message(narration)

                # Eksekusi tiap tool & kumpulkan hasil untuk dikirim balik.
                result_blocks = []
                for c in calls:
                    if cancel_event is not None and cancel_event.is_set():
                        raise llm.Cancelled()
                    name, args = c["name"], c["arguments"]
                    if on_tool:
                        on_tool(name, args)
                    result = tools.execute(name, args)
                    if on_tool_result:
                        on_tool_result(name, result)
                    steps += 1
                    clipped = result if len(result) <= _WEB_RESULT_CAP else (
                        result[:_WEB_RESULT_CAP] + "\n…[hasil dipotong]")
                    result_blocks.append(
                        f"[[HASIL {name}]]\n{clipped}\n[[/HASIL]]")

                follow = (
                    "\n\n".join(result_blocks)
                    + "\n\nLanjutkan tugas berdasarkan hasil di atas. Kalau perlu "
                    "tool lagi, keluarkan blok [[TOOL]] berikutnya; kalau sudah "
                    "SELESAI, beri jawaban akhir biasa (tanpa blok tool)."
                )
                reply = _send(follow)
        except llm.Cancelled:
            self.memory.repair_dangling_tools()
            self._persist()
            raise
        except connectors.BrowserError as exc:
            answer = f"[Connector {self.model_spec.label}] {exc}"
        except Exception as exc:  # noqa: BLE001 - laporkan apa adanya, jangan crash REPL
            answer = f"[Connector {self.model_spec.label}] gagal: {exc}"

        self.memory.add_assistant_text(answer)
        # Web-AI tak melaporkan token; pakai estimasi ~4 karakter per token dari
        # TOTAL lalu-lintas giliran ini (semua pesan terkirim + semua balasan),
        # bukan hanya jawaban akhir, supaya angkanya konsisten dgn model NVIDIA.
        self.tokens_last.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_session.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_live = self.tokens_last.total
        self._persist()
        return answer

    def _run_loop(
        self,
        on_tool: Callable[[str, dict[str, Any]], None] | None,
        on_message: Callable[[str], None] | None,
        cancel_event: Any,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
    ) -> str:
        schemas = tools.get_schemas(self.tool_names)

        # Agent boleh memakai tool sampai tugas selesai, TAPI dengan jaring
        # pengaman anti-loop-liar agar tidak mengulang-ulang / ngelantur:
        #  - `seen_tools`  : cache hasil per (nama+argumen) -> panggilan PERSIS
        #                    SAMA tak dieksekusi ulang, cukup dikembalikan + ditegur.
        #  - `dup_hits`    : berapa kali pengulangan terjadi; bila melewati batas,
        #                    tool DIMATIKAN dan agent dipaksa menyimpulkan.
        #  - `total_calls` : total panggilan tool; ada anggaran maksimum.
        guard = 0
        safety = max(self.max_iterations, 60)
        seen_tools: dict[str, str] = {}
        dup_hits = 0
        total_calls = 0
        force_final = False
        # Sinyal "performa menurun": model menuliskan tool call sebagai TEKS/XML
        # (weak_hits) atau membalas kosong berulang (empty_hits).
        weak_hits = 0
        empty_hits = 0
        # Berapa kali giliran ini kena MACET total (StreamStalled) — dibatasi
        # agar tak berputar selamanya bila jaringan/model benar-benar mati.
        stall_rounds = 0
        while True:
            guard += 1
            if guard > safety:
                break
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

            # Dihitung ULANG tiap putaran: model/effort bisa BERUBAH di tengah
            # giliran akibat auto-fallback.
            extra = self.model_spec.extra_body_for(self.effort)
            prompt_est = _est_messages(self.memory.messages)
            state = {"completion_est": 0}
            self.tokens_live = self.tokens_last.total + prompt_est

            def on_content(piece: str) -> None:
                state["completion_est"] += _est_tokens(piece)
                self.tokens_live = (
                    self.tokens_last.total + prompt_est + state["completion_est"]
                )

            def _on_retry(attempt: int, wait: float, exc: Exception) -> None:
                # Panggilan diulang dari awal -> reset estimasi token parsial
                # agar tidak dobel-hitung, lalu teruskan info ke UI.
                state["completion_est"] = 0
                self.tokens_live = self.tokens_last.total + prompt_est
                if on_retry:
                    on_retry(attempt, wait, exc)

            # Saat stagnasi terdeteksi, matikan tool -> model TERPAKSA menjawab
            # dengan teks (menyimpulkan), tidak bisa mengulang tool lagi.
            active_tools = None if force_final else (schemas or None)
            try:
                content, tool_calls, usage = llm.stream_completion(
                    self.memory.messages,
                    tools=active_tools,
                    model=self.model_spec.id,
                    extra_body=extra,
                    on_content=on_content,
                    cancel_event=cancel_event,
                    on_retry=_on_retry,
                )
            except llm.StreamStalled:
                # ANTI-MACET: stream berhenti mengirim data berulang kali di model
                # ini. Batalkan sendiri, NAIK KELAS (effort/model), lalu ULANGI —
                # memory belum disentuh di putaran ini, jadi konteks tetap utuh.
                stall_rounds += 1
                if stall_rounds > 3:
                    final = (
                        "Maaf, model terus macet (tidak mengirim respons) meski "
                        "sudah kubatalkan & kuulang otomatis beberapa kali. "
                        "Kemungkinan jaringan/server NVIDIA sedang bermasalah — "
                        "coba lagi sebentar lagi, atau ganti model dengan /model."
                    )
                    self.memory.add_assistant_text(final)
                    self._persist()
                    return final
                changed = self._escalate("respons macet — stream diam terlalu lama")
                if on_notice:
                    on_notice(changed or
                              "respons macet — dibatalkan & diulang otomatis")
                continue

            # Konfirmasi token: pakai usage asli bila ada, jika tidak estimasi.
            if usage:
                self.tokens_last.add(usage)
                self.tokens_session.add(usage)
            else:
                self.tokens_last.add_raw(prompt_est, state["completion_est"])
                self.tokens_session.add_raw(prompt_est, state["completion_est"])
            self.tokens_live = self.tokens_last.total

            # --- Deteksi "performa menurun" ---
            # Model menuliskan tool call sebagai TEKS/XML (diselamatkan llm.py &
            # diberi id 'txt_') = tanda model ini lemah di function-calling.
            if tool_calls and any(
                str(tc.get("id", "")).startswith("txt_") for tc in tool_calls
            ):
                weak_hits += 1
            if not tool_calls and not (content and content.strip()):
                empty_hits += 1

            if not tool_calls:
                # Balasan kosong berulang -> coba naik-kelas dulu sebelum menyerah.
                if empty_hits >= 2 and not force_final:
                    changed = self._escalate("respons kosong berulang")
                    if changed:
                        empty_hits = 0
                        if on_notice:
                            on_notice(changed)
                        self.memory.add({
                            "role": "user",
                            "content": ("[SISTEM] Balasanmu kosong. Lanjutkan tugas "
                                        "dari konteks di atas dan berikan jawaban."),
                        })
                        continue
                # Jaring pengaman: jangan pernah "berhenti diam". Bila model tak
                # menghasilkan teks apa pun (mis. berhenti tanpa menjawab), beri
                # pesan cadangan yang jelas alih-alih layar kosong.
                final = content if (content and content.strip()) else (
                    "Hmm, aku berhenti tanpa sempat menyusun jawaban. Coba ulangi "
                    "atau perjelas permintaanmu. Kalau modelnya bertipe reasoning, "
                    "turunkan /effort agar tidak kehabisan anggaran berpikir."
                )
                self.memory.add_assistant_text(final)
                self._persist()
                return final

            # Narasi sebelum aksi tool (mis. "Baik, saya akan membuat file X").
            if content and content.strip() and on_message:
                on_message(content)

            # Ada tool call: catat lalu eksekusi.
            self.memory.add(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [
                        {
                            "id": tc["id"] or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"] or "{}",
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                }
            )
            for i, tc in enumerate(tool_calls):
                if cancel_event is not None and cancel_event.is_set():
                    raise llm.Cancelled()
                name = tc["name"]
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if on_tool:
                    on_tool(name, args)
                # Dedup: panggilan PERSIS SAMA (nama+argumen) tak dieksekusi ulang.
                key = name + "::" + json.dumps(
                    args, sort_keys=True, ensure_ascii=False, default=str
                )
                if key in seen_tools:
                    dup_hits += 1
                    result = (
                        "[SISTEM] Kamu SUDAH memanggil tool ini dengan argumen yang "
                        "sama persis; hasilnya identik dengan di bawah. JANGAN "
                        "mengulanginya — gunakan hasil ini lalu lanjut ke langkah "
                        "berikutnya atau berikan jawaban akhir.\n\n" + seen_tools[key]
                    )
                else:
                    result = tools.execute(name, args)
                    seen_tools[key] = result
                    # Tampilkan hasil (mis. output perintah) HANYA saat benar-benar
                    # dieksekusi — bukan saat dedup mengembalikan cache + teguran.
                    if on_tool_result:
                        on_tool_result(name, result)
                total_calls += 1
                self.memory.add(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"] or f"call_{i}",
                        "content": result,
                    }
                )

            # --- Stagnasi / performa menurun ---
            # NGELOOP (mengulang tool sama) atau model lemah di function-calling:
            # COBA NAIK-KELAS DULU (effort ↑, lalu ganti model) dan LANJUTKAN dengan
            # KONTEKS YANG SAMA — memory tak direset, jadi progres tak hilang.
            looping = dup_hits >= config.MAX_DUPLICATE_TOOL_CALLS
            weak = weak_hits >= 2
            if not force_final and (looping or weak):
                reason = "terdeteksi mengulang langkah" if looping else \
                         "model lemah memanggil tool"
                changed = self._escalate(reason)
                if changed:
                    if on_notice:
                        on_notice(changed)
                    # Reset penghitung loop supaya otak baru dapat kesempatan bersih.
                    dup_hits = 0
                    weak_hits = 0
                    seen_tools.clear()
                    self.memory.add(
                        {
                            "role": "user",
                            "content": (
                                f"[SISTEM] Kamu tampak {reason}. Aku sudah menaikkan "
                                f"kemampuanmu ({changed}). Konteks & progres di atas "
                                f"TETAP berlaku — JANGAN ulangi dari nol. Lihat apa "
                                f"yang SUDAH selesai, lalu lanjutkan langkah "
                                f"BERIKUTNYA sampai tuntas."
                            ),
                        }
                    )
                    continue
            # Sudah tak bisa naik-kelas lagi (atau anggaran tool habis) -> minta
            # menyimpulkan dengan jujur.
            if not force_final and (
                looping or weak or total_calls >= config.MAX_TOOL_CALLS
            ):
                force_final = True
                self.memory.add(
                    {
                        "role": "user",
                        "content": (
                            "[SISTEM] Kamu tampak mengulang langkah / terlalu banyak "
                            "memakai tool. STOP memakai tool dan berikan jawaban akhir "
                            "dalam teks biasa. JUJUR: jelaskan apa yang SUDAH selesai "
                            "dan apa yang BELUM. JANGAN mengaku tuntas kalau memang "
                            "belum — sebutkan langkah tersisa yang perlu dilakukan."
                        ),
                    }
                )

        fallback = (
            "Maaf, proses berhenti karena mencapai batas iterasi tool. "
            "Coba persempit permintaanmu."
        )
        self.memory.add_assistant_text(fallback)
        self._persist()
        return fallback

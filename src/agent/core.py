"""Agent inti: loop chat + tool-calling (streaming). Dipakai semua antarmuka.

Menangani: system prompt dinamis, streaming (token realtime + pembatalan),
pemilihan model & effort yang tersimpan, dan penyimpanan sesi.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from . import config, llm, models, prefs, prompts
from .memory import Memory
from .session import Session
from .tools import base as tools


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
    ) -> str:
        """Teruskan giliran ke AI web (browser) alih-alih ke API NVIDIA.

        Web-AI menyimpan konteks percakapannya SENDIRI di dalam sesi browser;
        memory bagas-ai tetap mencatat transkrip agar tampil di UI, tersimpan di
        sesi, dan tetap nyambung bila nanti pengguna berganti ke model NVIDIA.
        """
        from . import connectors  # impor tunda: Playwright opsional

        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0
        prompt_text = str(user_input)

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

        try:
            answer = connectors.get_connector(self.model_spec.connector).send(
                prompt_text,
                on_status=on_status,
                on_token=on_token,
                cancel_event=cancel_event,
            )
        except llm.Cancelled:
            self.memory.repair_dangling_tools()
            self._persist()
            raise
        except connectors.BrowserError as exc:
            answer = f"[Connector {self.model_spec.label}] {exc}"
        except Exception as exc:  # noqa: BLE001 - laporkan apa adanya, jangan crash REPL
            answer = f"[Connector {self.model_spec.label}] gagal: {exc}"

        self.memory.add_assistant_text(answer)
        # Web-AI tak melaporkan token; estimasi kasar agar tampilan tetap masuk akal.
        self.tokens_last.add_raw(_est_tokens(prompt_text), _est_tokens(answer))
        self.tokens_session.add_raw(_est_tokens(prompt_text), _est_tokens(answer))
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

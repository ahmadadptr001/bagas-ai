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
        self.model_spec = models.spec_for_id(model_id)
        self._init_effort()

        self.memory = Memory(system_prompt=prompts.build_system_prompt())
        self.tool_names = tool_names
        self.max_iterations = max_iterations or config.MAX_TOOL_ITERATIONS
        self.session = session

        self.tokens_session = Usage()
        self.tokens_last = Usage()
        self.tokens_live = 0  # nilai token realtime untuk tampilan

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
    ) -> str:
        """Proses satu giliran (streaming). Kembalikan teks jawaban final.

        `on_message(teks)` dipanggil untuk narasi antar-langkah (ketika agent
        menjelaskan apa yang akan dilakukan sebelum memakai tool).
        `on_tool_result(nama, hasil)` dipanggil SETELAH sebuah tool selesai —
        dipakai UI untuk menampilkan hasil (mis. output perintah) secara ringkas.
        `on_retry(percobaan, tunggu, exc)` dipanggil saat NVIDIA rate-limit dan
        bagas-ai menunggu lalu MELANJUTKAN langkah yang sama.
        Bila `cancel_event` diset di tengah jalan, melempar llm.Cancelled.
        """
        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0
        try:
            return self._run_loop(
                on_tool, on_message, cancel_event, on_retry, on_tool_result
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

    def _run_loop(
        self,
        on_tool: Callable[[str, dict[str, Any]], None] | None,
        on_message: Callable[[str], None] | None,
        cancel_event: Any,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
    ) -> str:
        schemas = tools.get_schemas(self.tool_names)
        extra = self.model_spec.extra_body_for(self.effort)

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
        while True:
            guard += 1
            if guard > safety:
                break
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

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
            content, tool_calls, usage = llm.stream_completion(
                self.memory.messages,
                tools=active_tools,
                model=self.model_spec.id,
                extra_body=extra,
                on_content=on_content,
                cancel_event=cancel_event,
                on_retry=_on_retry,
            )

            # Konfirmasi token: pakai usage asli bila ada, jika tidak estimasi.
            if usage:
                self.tokens_last.add(usage)
                self.tokens_session.add(usage)
            else:
                self.tokens_last.add_raw(prompt_est, state["completion_est"])
                self.tokens_session.add_raw(prompt_est, state["completion_est"])
            self.tokens_live = self.tokens_last.total

            if not tool_calls:
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

            # Deteksi stagnasi -> paksa menyimpulkan pada iterasi berikutnya.
            if not force_final and (
                dup_hits >= config.MAX_DUPLICATE_TOOL_CALLS
                or total_calls >= config.MAX_TOOL_CALLS
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

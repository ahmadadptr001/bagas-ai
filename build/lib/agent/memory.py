"""Manajemen riwayat percakapan per sesi."""
from __future__ import annotations

from typing import Any

from .prompts import SYSTEM_PROMPT


class Memory:
    """Menyimpan daftar pesan (format OpenAI) dan menjaganya tetap ringkas.

    Pesan `system` selalu dipertahankan di indeks 0. Ketika jumlah pesan
    melebihi batas, pesan terlama (setelah system) dibuang agar hemat token
    dan kuota.
    """

    def __init__(
        self, system_prompt: str = SYSTEM_PROMPT, max_messages: int = 40
    ) -> None:
        self.max_messages = max_messages
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    def add(self, message: dict[str, Any]) -> None:
        self._messages.append(message)
        self._trim()

    def add_user(self, content: Any) -> None:
        self.add({"role": "user", "content": content})

    def add_assistant_text(self, content: str) -> None:
        self.add({"role": "assistant", "content": content})

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    def set_system(self, system_prompt: str) -> None:
        """Perbarui system prompt (indeks 0) tanpa mengubah riwayat lain.

        Dipakai saat konteks berubah di tengah sesi (mis. add-dir folder baru).
        """
        msg = {"role": "system", "content": system_prompt}
        if self._messages and self._messages[0].get("role") == "system":
            self._messages[0] = msg
        else:
            self._messages.insert(0, msg)

    def reset(self) -> None:
        self._messages = self._messages[:1]  # sisakan hanya system prompt

    def repair_dangling_tools(self) -> None:
        """Rapikan riwayat setelah error/pembatalan di tengah giliran.

        Instruksi pengguna & konteks TETAP disimpan (supaya bagas-ai ingat
        percakapan sebelumnya meski barusan terjadi error). Yang diperbaiki:
        setiap `assistant.tool_calls` harus diikuti respons tool untuk SEMUA
        panggilannya, dalam URUTAN yang sama dengan tool_calls, tanpa disela
        pesan lain — sesuai syarat API OpenAI/NVIDIA. Panggilan yang belum
        dijawab diberi respons sintetis; respons asli yang tercecer/terbalik
        ditata ulang ke posisi & urutan yang benar.
        """
        # Peta tool_call_id -> pesan respons tool aslinya (jika ada).
        responses: dict[str, dict[str, Any]] = {
            m.get("tool_call_id"): m
            for m in self._messages
            if m.get("role") == "tool" and m.get("tool_call_id")
        }
        emitted: set[str] = set()
        repaired: list[dict[str, Any]] = []
        for msg in self._messages:
            # Respons tool ditata ulang lewat blok assistant di bawah, jadi di
            # sini dilewati (respons yatim tanpa assistant induk dibuang).
            if msg.get("role") == "tool":
                continue
            repaired.append(msg)
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tcid = tc.get("id")
                    if not tcid or tcid in emitted:
                        continue
                    repaired.append(
                        responses.get(tcid)
                        or {
                            "role": "tool",
                            "tool_call_id": tcid,
                            "content": "[dibatalkan — giliran terputus]",
                        }
                    )
                    emitted.add(tcid)
        self._messages = repaired

    def load(self, saved_messages: list[dict[str, Any]]) -> None:
        """Muat riwayat dari sesi tersimpan (untuk --resume).

        System prompt saat ini dipertahankan; pesan `system` lama dibuang agar
        konteks (root project, memory) selalu yang terbaru.
        """
        body = [m for m in saved_messages if m.get("role") != "system"]
        self._messages = self._messages[:1] + body
        self._trim()

    def _trim(self) -> None:
        if len(self._messages) <= self.max_messages:
            return
        system = self._messages[0]
        overflow = len(self._messages) - self.max_messages
        remaining = self._messages[1 + overflow:]
        # Jangan biarkan pesan pertama yang tersisa berupa 'tool' (harus
        # mengikuti panggilan tool sebelumnya) — buang sampai aman.
        while remaining and remaining[0].get("role") == "tool":
            remaining = remaining[1:]
        self._messages = [system] + remaining

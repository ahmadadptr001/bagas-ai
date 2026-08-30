"""Manajemen riwayat percakapan per sesi."""
from __future__ import annotations

import json
from typing import Any

from .prompts import SYSTEM_PROMPT


class Memory:
    """Menyimpan daftar pesan (format OpenAI) dan menjaganya tetap ringkas.

    Pesan `system` selalu dipertahankan di indeks 0. Ketika jumlah pesan
    melebihi batas, pesan terlama (setelah system) dibuang agar hemat token
    dan kuota.
    """

    def __init__(
        self, system_prompt: str = SYSTEM_PROMPT, max_messages: int = 40,
        max_chars: int = 48_000,
    ) -> None:
        self.max_messages = max_messages
        # Batas pesan saja tidak cukup: satu hasil tool 20 KB dulu dihitung
        # sama dengan satu jawaban pendek. Batas karakter adalah pendekatan
        # tokenizer-independen (~12k token pada estimator aplikasi).
        self.max_chars = max_chars
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

    def add_diff(self, path: str, unified: str, is_new: bool,
                 deleted: bool = False) -> None:
        """Catat PRATINJAU DIFF langkah tulis/ubah/hapus untuk replay --resume.

        Diff hanya tampil di layar sesaat langkah berjalan; tanpa record ini
        transkrip --resume kehilangan seluruh potongan kodenya. Disimpan
        sebagai record ber-role 'diff' (bukan pesan), jadi otomatis AMAN dari
        semua konsumen riwayat: digest & berkas konteks menyaring role
        user/assistant saja (lihat prompts.transcript_rows), dan demikian
        pula protokol web. `unified` sudah terpangkas di pemanggilnya."""
        self.add({"role": "diff", "path": path, "diff": unified,
                  "is_new": bool(is_new), "deleted": bool(deleted)})

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
        self._trim()

    def reset(self) -> None:
        self._messages = self._messages[:1]  # sisakan hanya system prompt

    def repair_dangling_tools(self) -> None:
        """Rapikan riwayat setelah error/pembatalan di tengah giliran.

        Instruksi pengguna & konteks TETAP disimpan (supaya bagas-ai ingat
        percakapan sebelumnya meski barusan terjadi error). Yang diperbaiki:
        setiap `assistant.tool_calls` harus diikuti respons tool untuk SEMUA
        panggilannya, dalam URUTAN yang sama dengan tool_calls, tanpa disela
        pesan lain — warisan bentuk pesan gaya OpenAI. Panggilan yang belum
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

    def potong_awal(self, simpan_terakhir: int = 14) -> int:
        """Buang pesan TERLAMA saat konteks model penuh. Return jumlah sisa.

        Sistem prompt (indeks 0) selalu selamat; sisanya menyimpan N entri
        TERAKHIR — yang paling menentukan kelanjutan kerja. Pasangan tool
        yang terpotong setengahnya langsung dirapikan repair_dangling_tools()
        di akhir: endpoint menolak riwayat yang memuat respons `tool` tanpa
        induk `assistant.tool_calls`, dan sebaliknya.
        """
        if len(self._messages) <= 2:
            return len(self._messages)
        system = self._messages[0]
        ekor = self._messages[-(simpan_terakhir):]
        # Jangan sampai ekornya DIBUKA oleh respons 'tool' yatim.
        while ekor and ekor[0].get("role") == "tool":
            ekor = ekor[1:]
        if not ekor:
            return 0
        self._messages = [system] + ekor
        self.repair_dangling_tools()
        return len(self._messages)

    def _trim(self) -> None:
        def ukuran(message: dict[str, Any]) -> int:
            try:
                return len(json.dumps(message, ensure_ascii=False,
                                      default=str))
            except (TypeError, ValueError):
                return len(str(message))

        total = sum(ukuran(m) for m in self._messages)
        if (len(self._messages) <= self.max_messages
                and (self.max_chars <= 0 or total <= self.max_chars)):
            return
        system = self._messages[0]
        remaining = self._messages[1:]
        total = ukuran(system) + sum(ukuran(m) for m in remaining)
        while len(remaining) > 1 and (
            len(remaining) + 1 > self.max_messages
            or (self.max_chars > 0 and total > self.max_chars)
        ):
            total -= ukuran(remaining.pop(0))
            # Bila induk assistant.tool_calls ikut terpangkas, seluruh respons
            # tool yang langsung mengikutinya juga harus dibuang.
            while remaining and remaining[0].get("role") == "tool":
                total -= ukuran(remaining.pop(0))
        # Jangan biarkan pesan pertama yang tersisa berupa 'tool' (harus
        # mengikuti panggilan tool sebelumnya) — buang sampai aman.
        while remaining and remaining[0].get("role") == "tool":
            remaining = remaining[1:]
        self._messages = [system] + remaining

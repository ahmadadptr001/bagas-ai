# -*- coding: utf-8 -*-
"""Uji: transkrip --resume tampil di UI Textual + galat ID sesi jelas.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_resume_teks.py

Yang dicek:
1. Sesi lanjutan (agent.memory berisi pesan) → transkrip TERECHO di
   MessageList: pesan user, jawaban assistant, DAN diff kode (role
   'diff') — potongan kode tak lenyap saat sesi dibuka kembali.
2. Record internal (system, tool) TIDAK ikut di-echo.
3. Sesi baru (memory hanya system prompt) → tanpa header replay.
"""
import asyncio
import os
import sys
import tempfile
from unittest.mock import MagicMock

_TMP = tempfile.mkdtemp(prefix="uji_resume_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import MessageList


class MemoryPalsu:
    def __init__(self, messages):
        self.messages = messages


def fake_agent(messages):
    ag = MagicMock()
    ag.memory = MemoryPalsu(messages)
    ag.model_spec = MagicMock(label="uji", is_web=False)
    return ag


def _teks_items(msgs) -> str:
    return "\n".join(str(getattr(it, "plain", it)) for it in msgs._items)


async def tunggu(pilot, kondisi, maks=60, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


async def main():
    riwayat = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "tolong ubah halo.py"},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "diff", "path": "halo.py", "is_new": False,
         "diff": "@@ -1,1 +1,2 @@\n-print('halo')\n+print('hai')"},
        {"role": "assistant", "content": "Sudah saya ubah."},
    ]

    # ── 1-2. sesi lanjutan: transkrip terecho, record internal tidak ──
    ag = fake_agent(riwayat)
    app = BagasAIApp(agent=ag, resume=True)
    # Jawaban AI dirender Markdown (tak punya .plain) — rekam lewat patch
    # level-kelas SEBELUM run_test; on_mount/_show_welcome jalan di dalamnya.
    rekam_jawaban: list[str] = []
    _asli_ai = MessageList.append_ai_message

    def _catat_ai(self, text):
        rekam_jawaban.append(text)
        _asli_ai(self, text)

    MessageList.append_ai_message = _catat_ai
    try:
        async with app.run_test(size=(100, 40)) as pilot:
            msgs = app.query_one("#messages", MessageList)
            await pilot.pause(0.2)
            teks = _teks_items(msgs) + "\n" + "\n".join(rekam_jawaban)
        assert "tolong ubah halo.py" in teks, "pesan user harus terecho"
        assert "Sudah saya ubah." in teks, "jawaban assistant harus terecho"
        assert "halo.py" in teks and "print('hai')" in teks, \
            "diff kode harus direplay, bukan lenyap"
        assert "percakapan sebelumnya" in teks
        assert "system prompt" not in teks, \
            "system prompt tak boleh ikut di-echo"
        assert "[tidak interaktif]" not in teks
    finally:
        MessageList.append_ai_message = _asli_ai

    # ── 3. sesi BARU: tanpa replay ──
    # Patch kelas dipasang lagi dengan penanda indeks: sesi baru tak
    # boleh menambah jawaban AI apa pun (belum ada giliran).
    _batas = len(rekam_jawaban)

    def _catat_ai2(self, text):
        rekam_jawaban.append(text)
        _asli_ai(self, text)

    MessageList.append_ai_message = _catat_ai2
    try:
        ag2 = fake_agent([{"role": "system", "content": "system prompt"}])
        app2 = BagasAIApp(agent=ag2)
        async with app2.run_test(size=(100, 40)) as pilot:
            msgs2 = app2.query_one("#messages", MessageList)
            await pilot.pause(0.2)
            teks2 = _teks_items(msgs2)
            assert "percakapan sebelumnya" not in teks2, \
                "sesi baru tak boleh menampilkan header replay"
            assert "Selamat datang" in teks2
        assert len(rekam_jawaban) == _batas, \
            "sesi baru tak boleh mereplay jawaban AI apa pun"
    finally:
        MessageList.append_ai_message = _asli_ai

    print("OK - transkrip --resume terecho (pesan+diff), record internal "
          "disaring, sesi baru tanpa replay")


asyncio.run(main())

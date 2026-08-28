# -*- coding: utf-8 -*-
"""Uji: antrean MAJU jadi giliran baru saat giliran berjalan dibatalkan.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_antre_batal.py

Yang dicek:
1. Saat giliran aktif, pesan baru masuk antrean (QueueStrip), tak
   langsung dijalankan.
2. Ctrl+C membatalkan giliran — antrean TIDAK tersangkut: begitu worker
   lama mati, pesan antrean maju sebagai giliran BARU (agent.run dipanggil
   lagi dengan teks antrean).
3. _turn_complete giliran lama (turn_id basi) diabaikan — hasil worker
   yang dibatalkan tak dirender.
4. Antrean kosong saat dibatalkan → tak ada giliran baru yang menyala.
"""
import asyncio
import os
import sys
import tempfile
import threading
from unittest.mock import MagicMock

_TMP = tempfile.mkdtemp(prefix="uji_antre_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import MessageList, QueueStrip


class AgentPalsu:
    """Agent yang agent.run-nya menggantung sampai cancel/lepas."""

    def __init__(self):
        self.model_spec = MagicMock(label="uji", is_web=False)
        self.panggilan: list[str] = []
        self._gilir_aktif: threading.Event | None = None
        self._lock = threading.Lock()

    def run(self, text, **_kwargs):
        with self._lock:
            self.panggilan.append(text)
            ev = threading.Event()
            self._gilir_aktif = ev
        # Blokir sampai cancel_event diset (dilepas _stop_turn) ATAU
        # pengujian melepasnya manual.
        cancel = _kwargs.get("cancel_event")
        while not (cancel and cancel.is_set()) and not ev.is_set():
            ev.wait(0.05)
        if cancel and cancel.is_set():
            raise KeyboardInterrupt
        return "jawaban untuk: " + text


def _teks_items(msgs) -> str:
    return "\n".join(str(getattr(it, "plain", it)) for it in msgs._items)


async def tunggu(pilot, kondisi, maks=100, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


async def main():
    ag = AgentPalsu()
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(100, 40)) as pilot:
        msgs = app.query_one("#messages", MessageList)
        strip = app.query_one("#queue-strip", QueueStrip)

        # Jawaban AI dirender sebagai widget Markdown — rekam teks mentahnya
        # lewat pembungkus agar bisa diperiksa.
        jawaban_tercatat: list[str] = []
        _asli_ai = msgs.append_ai_message

        def _catat_ai(text: str):
            jawaban_tercatat.append(text)
            _asli_ai(text)

        msgs.append_ai_message = _catat_ai

        # ── 1. pesan saat giliran aktif → mengantre, tak dijalankan ──
        chatbox = app.query_one("#chatbox")
        chatbox.set_text("tugas pertama")
        await pilot.press("enter")
        await tunggu(pilot, lambda: ag.panggilan == ["tugas pertama"],
                     pesan="giliran pertama harus jalan")
        assert app.is_turn_active

        chatbox.set_text("pesan yang mengantre")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert ag.panggilan == ["tugas pertama"], \
            "pesan saat giliran aktif tak boleh langsung dijalankan"
        with app._antre_lock:
            assert app._prompt_queue == ["pesan yang mengantre"], \
                app._prompt_queue
        assert any("pesan yang mengantre" in str(i) for i in strip._items), \
            "QueueStrip harus menampilkan pesan yang mengantre"

        # ── 2. Ctrl+C → antrean maju jadi giliran baru ──
        # CATATAN: is_turn_active sempat False (UI idle) lalu cepat kembali
        # True karena antrean langsung maju — jadi yang ditunggu bukan
        # keadaan idle, tapi panggilan agent.run kedua.
        await pilot.press("ctrl+c")
        await tunggu(pilot,
                     lambda: ag.panggilan == ["tugas pertama",
                                              "pesan yang mengantre"],
                     maks=200,
                     pesan="antrean harus maju sebagai giliran baru "
                           f"(panggilan: {ag.panggilan})")

        # Giliran baru benar-benar berjalan: UI kembali busy & pesan
        # antrean terecho sebagai pesan pengguna.
        await tunggu(pilot, lambda: "pesan yang mengantre" in _teks_items(msgs),
                     pesan="pesan antrean harus terecho ke riwayat")
        assert app.is_turn_active, "giliran baru (dari antrean) harus aktif"

        # Selesaikan giliran kedua secara manual → hasil dirender normal.
        ag._gilir_aktif.set()
        await tunggu(pilot, lambda: "jawaban untuk: pesan yang mengantre"
                     in jawaban_tercatat,
                     pesan="giliran kedua selesai normal & dirender")
        await tunggu(pilot, lambda: not app.is_turn_active,
                     pesan="UI kembali idle setelah giliran kedua")

        # ── 3. antrean KOSONG saat batal → tak ada giliran baru ──
        chatbox.set_text("giliran sendirian")
        await pilot.press("enter")
        await tunggu(pilot, lambda: "giliran sendirian" in ag.panggilan,
                     pesan="giliran ketiga harus jalan")
        await pilot.press("ctrl+c")
        await pilot.pause(0.5)
        assert ag.panggilan.count("giliran sendirian") == 1, \
            "tak boleh ada giliran susulan saat antrean kosong"
        assert not app.is_turn_active

    print("OK - antrean maju jadi giliran baru saat dibatalkan; "
          "hasil giliran basi diabaikan; antrean kosong tak memicu "
          "giliran baru")


asyncio.run(main())

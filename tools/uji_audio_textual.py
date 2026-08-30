# -*- coding: utf-8 -*-
"""Uji /mic dan /voice di Textual tanpa memakai perangkat audio nyata."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["BAGASAI_PROJECT_ROOT"] = tempfile.mkdtemp(prefix="uji_audio_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import models
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import StatusBar
from agent.interfaces.textual_widgets.chat_box import _SLASH_COMMANDS


class PendengarPalsu:
    def __init__(self, on_perintah, on_kabar, jangkauan=None):
        self.on_perintah = on_perintah
        self.on_kabar = on_kabar
        self.jangkauan = jangkauan or "jauh"
        self.aktif = False
        self.merekam = False
        self.galat = ""

    def mulai(self):
        self.aktif = True
        return ""

    def berhenti(self):
        self.aktif = False


async def tunggu(pilot, kondisi, maks=100, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


async def main() -> None:
    perintah = {c for c, _, _ in _SLASH_COMMANDS}
    assert "/mic" in perintah and "/voice" in perintah

    spec = models.ModelSpec(id="uji/text", label="Model Uji",
                            multimodal=False)
    agent = MagicMock()
    agent.model_spec = spec
    agent.run.return_value = "jawaban audio"
    pref = {"suara": True}
    dikte_mulai = threading.Event()
    stop_saat_tutup = None

    def dikte_palsu(*, berhenti, on_status, **_kwargs):
        on_status("merekam")
        dikte_mulai.set()
        berhenti.wait(timeout=2.0)
        on_status("menganalisis")
        return "ubah berkas utama", {"engine": "whisper", "puncak": 200}

    def load_pref():
        return dict(pref)

    def save_pref(**kwargs):
        pref.update(kwargs)

    app = BagasAIApp(agent=agent)
    with (patch("agent.interfaces.textual_app.prefs.load",
                side_effect=load_pref),
          patch("agent.interfaces.textual_app.prefs.save",
                side_effect=save_pref),
          patch("agent.suara.mesin_tersedia", return_value=["edge"]),
          patch("agent.suara.ucap") as ucap,
          patch("agent.suara.getar") as getar,
          patch("agent.suara.diam") as diam,
          patch("agent.suara.tutup"),
          patch("agent.dengar.Pendengar", PendengarPalsu),
          patch("agent.dengar.nama_mikrofon", return_value="Mic Uji"),
          patch("agent.dengar.bunyi"),
          patch("agent.dengar.siap", return_value=(True, "")),
          patch("agent.dengar.dengar_sekali",
                return_value=("tes mikrofon", 321.0)),
          patch("agent.dengar.dengar_dikte", side_effect=dikte_palsu)):
        async with app.run_test(size=(100, 40)) as pilot:
            # /mic benar-benar mengubah preferensi dan tes memanggil TTS.
            app._handle_command("/mic off")
            assert pref["suara"] is False and diam.called
            app._handle_command("/mic on")
            assert pref["suara"] is True
            app._handle_command("/mic tes")
            await tunggu(pilot,
                         lambda: not app._voice_state["task_active"],
                         pesan="tes /mic harus selesai di worker")
            assert ucap.called

            # /voice on menyalakan listener dan indikator privasi.
            app._handle_command("/voice on")
            await tunggu(pilot,
                         lambda: app._voice_state["pendengar"] is not None,
                         pesan="listener /voice harus aktif")
            p = app._voice_state["pendengar"]
            app._refresh_status()
            assert app.query_one("#statusbar", StatusBar).voice_state == "dengar"

            p.merekam = True
            app._refresh_status()
            assert app.query_one("#statusbar", StatusBar).voice_state == "merekam"

            # Callback mikrofon menjadi prompt biasa dan dijalankan Agent.
            p.merekam = False
            p.on_perintah("tolong cek proyek")
            await tunggu(pilot, lambda: agent.run.called,
                         pesan="perintah suara harus masuk Agent.run")
            await tunggu(pilot, lambda: not app.is_turn_active,
                         pesan="giliran voice harus selesai")
            assert agent.run.call_args.args[0] == "tolong cek proyek"
            assert getar.called
            assert any(c.kwargs.get("penuh") is True
                       for c in ucap.call_args_list)

            app._handle_command("/voice off")
            await pilot.pause(0.1)
            assert app._voice_state["pendengar"] is None
            app._refresh_status()
            assert app.query_one("#statusbar", StatusBar).voice_state == ""

            # Tes pengenalan berjalan tanpa memblokir UI setelah listener mati.
            app._handle_command("/voice tes")
            await tunggu(pilot,
                         lambda: not app._voice_state["task_active"],
                         pesan="tes /voice harus selesai")

            # F4 memulai dikte TANPA wake word; tombol yang sama menghentikan.
            agent.run.reset_mock()
            await pilot.press("f4")
            await tunggu(pilot, lambda: dikte_mulai.is_set(),
                         pesan="F4 harus membuka dikte langsung")
            assert app._voice_state["dictation_active"] is True
            app._refresh_status()
            assert app.query_one("#statusbar", StatusBar).voice_state == "merekam"
            tombol = app.query_one("#chat-voice-button")
            assert str(tombol.label) == "■"
            await pilot.click("#chat-voice-button")
            await tunggu(pilot, lambda: not app._voice_state["dictation_active"],
                         pesan="klik kedua harus menyelesaikan dikte")
            await tunggu(pilot, lambda: agent.run.called,
                         pesan="transkrip dikte harus masuk Agent.run")
            assert agent.run.call_args.args[0] == "ubah berkas utama"
            assert str(tombol.label) == "🎙"

            # Menutup aplikasi ketika mic masih aktif wajib memberi sinyal
            # stop ke worker, bukan membiarkannya merekam di belakang layar.
            dikte_mulai.clear()
            await pilot.press("f4")
            await tunggu(pilot, lambda: dikte_mulai.is_set(),
                         pesan="dikte kedua harus mulai sebelum app ditutup")
            stop_saat_tutup = app._voice_state["dictation_stop"]

        assert stop_saat_tutup is not None and stop_saat_tutup.is_set()

    print("OK - /mic dan /voice tampil di menu dan berfungsi di Textual "
          "tanpa perangkat audio nyata")


asyncio.run(main())

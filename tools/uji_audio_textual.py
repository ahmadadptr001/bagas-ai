# -*- coding: utf-8 -*-
"""Uji /mic dan /voice di Textual tanpa memakai perangkat audio nyata."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["BAGASAI_PROJECT_ROOT"] = tempfile.mkdtemp(prefix="uji_audio_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import models
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import StatusBar, VoiceScreen, VoiceOrb
from agent.interfaces.textual_widgets.chat_box import _SLASH_COMMANDS


class PendengarPalsu:
    gagal = False

    def __init__(self, on_perintah, on_kabar, jangkauan=None,
                 langsung=False, on_level=None, **_kwargs):
        self.on_perintah = on_perintah
        self.on_kabar = on_kabar
        self.on_level = on_level or (lambda _level, _hearing: None)
        self.langsung = langsung
        self.jangkauan = jangkauan or "jauh"
        self.aktif = False
        self.merekam = False
        self.galat = ""

    def mulai(self):
        if type(self).gagal:
            return "mikrofon uji gagal"
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


async def tunggu_async(kondisi, maks=100, jeda=0.05,
                       pesan="kondisi tak terpenuhi"):
    """Tunggu tanpa Pilot._wait_for_screen untuk layar beranimasi kontinu."""
    for _ in range(maks):
        await asyncio.sleep(jeda)
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
    tts_busy = {"value": False}
    pendengar_saat_tutup = None

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
          patch("agent.suara.sibuk",
                side_effect=lambda: tts_busy["value"]),
          patch("agent.suara.getar") as getar,
          patch("agent.suara.diam") as diam,
          patch("agent.suara.tutup"),
          patch("agent.dengar.Pendengar", PendengarPalsu),
          patch("agent.dengar.nama_mikrofon", return_value="Mic Uji"),
          patch("agent.dengar.bunyi"),
          patch("agent.dengar.siap", return_value=(True, "")),
          patch("agent.dengar.dengar_sekali",
                return_value=("tes mikrofon", 321.0))):
        async with app.run_test(size=(100, 40)) as pilot:
            print("  app mounted", flush=True)
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
            print("  /mic selesai", flush=True)

            # /voice on membuka layar orb BESAR dan listener langsung.
            app._handle_command("/voice")
            await tunggu(pilot,
                         lambda: app._voice_state["pendengar"] is not None,
                         pesan="listener /voice harus aktif")
            p = app._voice_state["pendengar"]
            print("  /voice listener aktif", flush=True)
            assert p.langsung is True
            assert isinstance(app.screen, VoiceScreen)
            orb = app.screen.query_one("#voice-orb", VoiceOrb)
            assert orb.size.width >= 70 and orb.size.height >= 20, (
                f"orb harus dominan, ukuran aktual {orb.size}")
            app._refresh_status()
            assert app.query_one("#statusbar", StatusBar).voice_state == "dengar"

            p.on_level(0.9, True)
            await tunggu(pilot, lambda: app._voice_state["hearing"] is True)
            assert app.query_one("#statusbar", StatusBar).voice_state == "merekam"
            assert app._voice_visual_state()[0] == "menangkap"
            tts_busy["value"] = True
            assert app._voice_visual_state()[0] == "berbicara"
            tts_busy["value"] = False
            print("  orb dan level aktif", flush=True)

            # Callback mikrofon menjadi prompt biasa di chat utama dan jawaban
            # akhir tetap dibacakan meski preferensi /mic sedang mati.
            pref["suara"] = False
            p.on_perintah("tolong cek proyek")
            await tunggu(pilot, lambda: agent.run.called,
                         pesan="perintah suara harus masuk Agent.run")
            await tunggu(pilot, lambda: not app.is_turn_active,
                         pesan="giliran voice harus selesai")
            assert agent.run.call_args.args[0] == "tolong cek proyek"
            assert getar.called
            assert any(c.kwargs.get("penuh") is True
                       for c in ucap.call_args_list)
            assert app._voice_state["session_active"] is True
            print("  prompt dan TTS selesai", flush=True)

            # Esc menutup layar khusus sekaligus listener, tanpa menutup app.
            layar_pertama = app.screen
            await pilot.press("escape")
            await tunggu_async(
                lambda: not app._voice_state["session_active"])
            assert app._voice_state["pendengar"] is None
            await tunggu_async(lambda: p.aktif is False)
            assert not isinstance(app.screen, VoiceScreen)
            app._refresh_status()
            assert app.query_one("#statusbar", StatusBar).voice_state == ""
            print("  esc selesai", flush=True)
            await tunggu_async(lambda: layar_pertama.parent is None,
                               pesan="screen voice lama harus ter-unmount")

            # Tes pengenalan berjalan tanpa memblokir UI setelah listener mati.
            app._handle_command("/voice tes")
            await tunggu_async(
                lambda: not app._voice_state["task_active"],
                pesan="tes /voice harus selesai")
            print("  tes voice selesai", flush=True)

            # F4 membuka layar yang sama; tombol ⌵ menutupnya.
            await pilot.press("f4")
            await tunggu_async(lambda: isinstance(app.screen, VoiceScreen),
                               pesan="F4 harus membuka layar voice")
            await tunggu_async(
                lambda: app._voice_state["pendengar"] is not None)
            assert await pilot.click("#voice-close")
            await tunggu_async(
                lambda: not app._voice_state["session_active"],
                pesan="tombol ⌵ harus menutup sesi")
            print("  tombol close selesai", flush=True)

            # Gagal startup harus kembali ke chat utama, bukan menyisakan
            # overlay kosong yang tak bisa dijelaskan kepada pengguna.
            PendengarPalsu.gagal = True
            await pilot.press("f4")
            await tunggu_async(
                lambda: not app._voice_state["session_active"])
            assert not isinstance(app.screen, VoiceScreen)
            PendengarPalsu.gagal = False

            # Menutup aplikasi ketika mic masih aktif wajib memberi sinyal
            # stop ke listener, bukan membiarkannya merekam di belakang layar.
            await pilot.press("f4")
            await tunggu_async(
                lambda: app._voice_state["pendengar"] is not None)
            pendengar_saat_tutup = app._voice_state["pendengar"]
            print("  siap unmount", flush=True)

        assert pendengar_saat_tutup is not None
        assert pendengar_saat_tutup.aktif is False

    print("OK - /mic dan /voice tampil di menu dan berfungsi di Textual "
          "tanpa perangkat audio nyata")


asyncio.run(main())

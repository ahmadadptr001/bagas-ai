# -*- coding: utf-8 -*-
"""Uji: menu /model — label (rekomendasi) + pemisah kategori.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_menu_model.py

Yang dicek:
1. pilihan_model_grup() mengembalikan kelompok berkategori: OpenCode Zen,
   AI Web, API — urutan sesuai katalog.
2. Semua model opencode KECUALI nemotron-3.5-lightning-free berlabel
   " (rekomendasi)" pada tampilan; nilai aliasnya tetap murni.
3. Menu /model di UI Textual: pemisah kategori dirender, opsi rekomendasi
   bold, memilih opsi mengirim ALIAS MURNI (tanpa label) ke set_model.
4. Enter di baris pemisah tak memilih apa pun.
"""
import asyncio
import os
import sys
import tempfile
from unittest.mock import MagicMock

_TMP = tempfile.mkdtemp(prefix="uji_model_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from agent import models
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import MessageList
from agent.interfaces.textual_widgets.modal_screens import SelectScreen, _SEP


async def tunggu(pilot, kondisi, maks=100, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


def cek_grup():
    grup = models.pilihan_model_grup()
    kategori = [k for k, _ in grup]
    assert kategori[0].startswith("OpenCode Zen"), kategori
    assert any(k.startswith("AI Web") for k in kategori), kategori
    assert any(k.startswith("API") for k in kategori), kategori

    zen = dict(grup)[kategori[0]]
    tampil = {t: n for t, n in zen}  # tampilan -> nilai
    assert tampil.get("big-pickle (rekomendasi)") == "big-pickle"
    assert tampil.get("hy3-free (rekomendasi)") == "hy3-free"
    assert tampil.get("nemotron-3-ultra-free (rekomendasi)") == \
        "nemotron-3-ultra-free"
    # nemotron-3.5-lightning-free TANPA label (upstream masih 404)
    assert tampil.get("nemotron-3.5-lightning-free") == \
        "nemotron-3.5-lightning-free"
    print("  grup kategori + label rekomendasi: OK")


class AgentPalsu:
    """Agent minimal: set_model merekam argumennya."""

    def __init__(self, spec):
        self.model_spec = spec
        self.pilihan: list[str] = []

    def set_model(self, name):
        self.pilihan.append(name)
        return name


async def cek_menu():
    spec = next(iter(models.MODELS.values()))
    ag = AgentPalsu(spec)
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(100, 40)) as pilot:
        msgs = app.query_one("#messages", MessageList)
        msgs.append_user_message("/model")
        app._handle_command("/model")
        await tunggu(pilot,
                     lambda: any(isinstance(s, SelectScreen)
                                 for s in app.screen_stack),
                     pesan="SelectScreen harus terbuka")

        layar = next(s for s in app.screen_stack
                     if isinstance(s, SelectScreen))
        assert layar.options[0] == (_SEP, layar.options[0][1]), \
            "opsi pertama harus pemisah kategori"
        ada_sep = sum(1 for o in layar.options if o[0] == _SEP)
        assert ada_sep >= 3, f"butuh >=3 pemisah kategori, ada {ada_sep}"

        # Render pemisah: nama kategori bold.
        tampil_sep = layar._tampilan(layar.options[0])
        assert "OpenCode Zen" in str(tampil_sep)
        # Render label rekomendasi: "(rekomendasi)" terpisah & bold.
        idx_rec = next(i for i, o in enumerate(layar.options)
                       if isinstance(o, tuple) and o[0] != _SEP
                       and o[1] == "big-pickle")
        tampil_rec = layar._tampilan(layar.options[idx_rec])
        assert "(rekomendasi)" in tampil_rec.plain
        spans = [s.style for s in tampil_rec.spans]
        assert "bold" in spans, f"label rekomendasi harus bold: {spans}"

        # Sorot baris pemisah lalu Enter → tak ada yang dipilih.
        opt_list = layar.query_one("#select-options")
        opt_list.highlighted = 0
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert ag.pilihan == [], \
            "Enter di pemisah tak boleh memilih apa pun"
        # layar masih terbuka
        assert any(isinstance(s, SelectScreen) for s in app.screen_stack)

        # Pilih opsi rekomendasi → nilai ALIAS MURNI yang dikirim.
        opt_list.highlighted = idx_rec
        await pilot.press("enter")
        await tunggu(pilot, lambda: ag.pilihan == ["big-pickle"],
                     pesan=f"set_model harus menerima alias murni, "
                           f"terekam: {ag.pilihan}")
    print("  menu UI: pemisah, bold, nilai murni: OK")


async def main():
    cek_grup()
    await cek_menu()
    print("OK - menu /model: kategori terpisah, label (rekomendasi) "
          "bold pada model opencode kecuali nemotron-3.5-lightning-free")


asyncio.run(main())

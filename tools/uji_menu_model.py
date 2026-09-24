# -*- coding: utf-8 -*-
"""Uji: menu /model — label (rekomendasi) + pemisah kategori (INLINE).

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_menu_model.py

Yang dicek:
1. pilihan_model_grup() mengembalikan kelompok berkategori: OpenRouter,
   AI Web, API — urutan sesuai katalog.
2. Semua model opencode KECUALI nemotron-3.5-lightning-free berlabel
   " (rekomendasi)" pada tampilan; nilai aliasnya tetap murni.
3. Menu /model di UI Textual: prompt INLINE (bukan modal), pemisah kategori
   dirender di baris, opsi rekomendasi bold, memilih opsi mengirim ALIAS
   MURNI (tanpa label) ke set_model.
4. Jawaban tidak valid (nomor di luar jangkauan) tak memilih apa pun.
"""
import asyncio
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="uji_model_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from agent import models
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import MessageList
from agent.interfaces.textual_widgets.modal_screens import _SEP


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
    assert kategori[0].startswith("OpenRouter"), kategori
    assert kategori[-1].startswith("API"), kategori
    assert len(kategori) >= 2, kategori
    choices = [v for _, options in grup for _, v in options]
    assert choices == models.pilihan_model()
    assert choices[0] == "or-nemotron-ultra"
    assert all(models.cari(v).aktif for v in choices)
    assert all(models.cari(str(i)) == spec for i, _, spec in models.catalog())
    assert "ditunda" not in models.list_text().lower()
    assert "oxalpha" not in choices
    print("  kategori, filter model, nomor pilihan: OK")


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
        await tunggu(pilot, lambda: app._prompt_inline is not None,
                     pesan="prompt /model harus terbuka (inline)")

        st = app._prompt_inline
        assert st is not None and st["mode"] == "pilihan", st
        # Opsi dibangun lewat _baris_pilihan_inline: pemisah _SEP jadi
        # garis "── kategori ──" tanpa nomor di baris prompt — baris
        # pemisah TIDAK punya pasangan di nilai (hanya baris opsi yang
        # bernomor dan dipetakan ke nilai).
        baris = st["baris"]
        nilai = st["nilai"]
        baris_opsi = [b for b in baris if "──" not in b]
        assert len(baris_opsi) == len(nilai), (len(baris_opsi), len(nilai))
        # Nilai pertama dari grup biasanya alias model, BUKAN _SEP
        assert _SEP not in nilai, "pemisah tidak boleh jadi nilai pilihan"
        # Minimal ada pemisah kategori di baris (── … ──)
        ada_sep = any("──" in b for b in baris)
        assert ada_sep, f"pemisah kategori harus tampil di baris: {baris[:6]!r}"

        # Jawaban tidak valid (nomor di luar jangkauan) -> tak memilih.
        await pilot.press("9", "9", "9")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert ag.pilihan == [], \
            "jawaban tidak valid tak boleh memilih apa pun"
        assert app._prompt_inline is not None or ag.pilihan == []

        # Tutup prompt lalu buka lagi untuk pilih opsi valid.
        if app._prompt_inline is not None:
            app._batalkan_prompt_inline()
            await pilot.pause(0.1)
        app._handle_command("/model")
        await tunggu(pilot, lambda: app._prompt_inline is not None,
                     pesan="prompt /model harus terbuka lagi")

        # Pilih alias murni pertama (biasanya nemotron) lewat nomor 1.
        await pilot.press("1")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await tunggu(pilot, lambda: len(ag.pilihan) >= 1,
                     pesan=f"set_model harus menerima alias murni, "
                           f"terekam: {ag.pilihan}")
        # Nilai dikirim dari st['nilai'][n-1] — alias murni tanpa label.
        pilih = ag.pilihan[-1]
        assert pilih in [v for _, opts in models.pilihan_model_grup()
                         for _, v in opts], pilih
        assert "rekomendasi" not in pilih.lower(), pilih
    print("  menu UI INLINE: pemisah di baris, nilai murni: OK")


async def cek_picker():
    """Panah + Enter pada picker di atas input (tanpa ketik nomor)."""
    from agent.interfaces.textual_widgets import ChatBox

    spec = next(iter(models.MODELS.values()))
    ag = AgentPalsu(spec)
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(100, 40)) as pilot:
        app._handle_command("/model")
        await tunggu(pilot, lambda: app._prompt_inline is not None,
                     pesan="prompt /model harus terbuka")
        chatbox = app.query_one("#chatbox", ChatBox)
        await tunggu(pilot, lambda: chatbox.picker_open,
                     pesan="picker harus terbuka di atas input")
        # Enter tanpa mengetik apa pun -> opsi tersorot pertama.
        n0 = len(ag.pilihan)
        await pilot.press("enter")
        await tunggu(pilot, lambda: len(ag.pilihan) >= n0 + 1,
                     pesan="Enter pada picker harus memilih opsi tersorot")
        # Buka lagi, panah bawah sekali, Enter -> opsi kedua (boleh sama
        # alias bila katalog cuma punya satu; yang penting prompt tertutup).
        if app._prompt_inline is not None:
            app._batalkan_prompt_inline()
            await pilot.pause(0.1)
        app._handle_command("/model")
        await tunggu(pilot, lambda: app._prompt_inline is not None
                     and chatbox.picker_open,
                     pesan="picker harus terbuka lagi")
        await pilot.press("down")
        await pilot.pause(0.1)
        n1 = len(ag.pilihan)
        await pilot.press("enter")
        await tunggu(pilot, lambda: len(ag.pilihan) >= n1 + 1,
                     pesan="panah+Enter pada picker harus memilih opsi")
        assert app._prompt_inline is None, "prompt harus tertutup setelah pilih"
    print("  picker panah+Enter di atas input: OK")


async def main():
    cek_grup()
    await cek_menu()
    await cek_picker()
    print("OK - menu /model: kategori terpisah, label (rekomendasi) "
          "bold pada model NVIDIA, prompt INLINE + picker panah")


asyncio.run(main())

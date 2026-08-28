# -*- coding: utf-8 -*-
"""Uji PlanSidebar (layout lebar/sempit) + handler ask_* di UI Textual.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_plan_sidebar.py

Yang dicek:
1. plan() dipanggil di layar LEBAR (>= _LEBAR_MIN) -> sidebar kanan tampil,
   PlanPanel footer disembunyikan (jangan dobel).
2. plan_step() memindahkan penanda done/active.
3. Terminal disempitkan -> rencana pindah ke PlanPanel footer, sidebar hilang;
   dilebarkan lagi -> kembali ke sidebar.
4. core.run() mereset rencana (plan_tool.reset) -> kedua panel kosong.
5. Handler ask_choice TERPASANG (ask_user tak lagi "[tidak interaktif]"):
   - pilih satu (Enter),
   - pilih banyak (spasi tandai, Enter),
   - isian bebas "✎ Tulis jawaban sendiri…".
"""
import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from agent.interfaces.textual_app import BagasAIApp, _OPSI_TULIS
from agent.interfaces.textual_widgets import (PlanPanel, PlanSidebar,
                                               SelectScreen, MultiSelectScreen,
                                               TextPromptScreen)
from agent.tools import plan_tool


def fake_agent(pintu: threading.Event):
    ag = MagicMock()
    ag.dijalankan = []

    def run(text, **cb):
        ag.dijalankan.append(text)
        pintu.wait(timeout=10)
        return "hasil"

    ag.model_spec = SimpleNamespace(label="uji", is_web=False)
    ag.run = run
    return ag


async def tunggu(pilot, kondisi, maks=80, jeda=0.05, pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


async def uji_plan(app, pilot):
    plan = app.query_one("#plan", PlanPanel)
    sidebar = app.query_one("#plan-sidebar", PlanSidebar)

    # Layar lebar: sidebar muncul, footer tidak (jangan dobel)
    plan_tool.plan(["langkah pertama", "langkah kedua", "langkah ketiga"], 1)
    await tunggu(pilot, lambda: sidebar.display,
                 pesan="sidebar harus tampil di layar lebar")
    await pilot.pause()
    assert not plan.display, "PlanPanel footer harus sembunyi di layar lebar"
    isi = sidebar.terakhir
    assert "langkah pertama" in isi, isi
    assert "Rencana 0/3" in isi, isi

    # plan_step: langkah 1 selesai, langkah 2 aktif
    plan_tool.plan_step(2)
    await tunggu(pilot, lambda: "Rencana 1/3" in sidebar.terakhir,
                 pesan="hitungan selesai harus maju ke 1/3")
    isi = sidebar.terakhir
    assert "▸ langkah kedua" in isi, isi
    assert "✓ langkah pertama" in isi, isi

    # Sempitkan -> pindah ke footer; sidebar hilang
    await pilot.resize_terminal(80, 40)
    await tunggu(pilot, lambda: plan.display and not sidebar.display,
                 pesan="sempit: rencana harus pindah ke PlanPanel footer")
    isi_plan = plan.terakhir if hasattr(plan, "terakhir") else str(plan._content.render())
    assert "langkah kedua" in isi_plan, isi_plan

    # Lebarkan lagi -> kembali ke sidebar
    await pilot.resize_terminal(120, 40)
    await tunggu(pilot, lambda: sidebar.display and not plan.display,
                 pesan="lebar kembali: rencana harus kembali ke sidebar")

    # reset (dipanggil core.run tiap giliran baru) -> kedua panel kosong
    plan_tool.reset()
    await tunggu(pilot, lambda: not sidebar.display and not plan.display,
                 pesan="reset: kedua panel rencana harus kosong")


async def uji_ask(app, pilot):
    from agent import interaction
    assert interaction._default_handler is app._handler_pilihan_ref, \
        "handler pilihan harus terpasang sebagai default saat on_mount"

    hasil = {}

    def panggil(question, options, multiple):
        hasil["jawab"] = app._handler_pilihan_ref(question, options, multiple)

    # ── pilih satu: Enter memilih baris pertama ──
    t = threading.Thread(
        target=panggil,
        args=("Pilih warna", ["merah", "hijau"], False),
        daemon=True)
    t.start()
    await tunggu(pilot, lambda: isinstance(app.screen, SelectScreen),
                 pesan="SelectScreen harus muncul")
    assert app.screen.title_text == "Pilih warna"
    assert _OPSI_TULIS in app.screen.options, "isian bebas harus ada"
    await pilot.press("enter")
    t.join(timeout=5)
    assert not t.is_alive(), "handler harus kembali setelah Enter"
    assert hasil["jawab"] == "merah", hasil["jawab"]

    # ── pilih banyak: spasi menandai dua item, Enter mengirim ──
    t = threading.Thread(
        target=panggil,
        args=("Pilih buah", ["apel", "jeruk", "pisang"], True),
        daemon=True)
    t.start()
    await tunggu(pilot, lambda: isinstance(app.screen, MultiSelectScreen),
                 pesan="MultiSelectScreen harus muncul untuk multiple=True")
    await pilot.press("space")          # tandai apel
    await pilot.press("down")
    await pilot.press("space")          # tandai jeruk
    await pilot.press("enter")
    t.join(timeout=5)
    assert not t.is_alive()
    assert hasil["jawab"] == "(1) apel; (2) jeruk", hasil["jawab"]

    # ── isian bebas: pilih "✎ Tulis jawaban sendiri…" lalu ketik ──
    t = threading.Thread(
        target=panggil,
        args=("Nama siapa", ["andi", "budi"], False),
        daemon=True)
    t.start()
    await tunggu(pilot, lambda: isinstance(app.screen, SelectScreen),
                 pesan="SelectScreen harus muncul (isian bebas)")
    # sorot entri isian bebas (terakhir) lalu Enter
    for _ in range(len(app.screen.options) - 1):
        await pilot.press("down")
    await pilot.press("enter")
    await tunggu(pilot, lambda: isinstance(app.screen, TextPromptScreen),
                 pesan="TextPromptScreen harus muncul untuk isian bebas")
    await pilot.press(*"bagas")
    await pilot.press("enter")
    t.join(timeout=5)
    assert not t.is_alive()
    assert hasil["jawab"] == "bagas", hasil["jawab"]


async def uji_recall(app, pilot, pintu):
    """Panah-atas: tarik teks antrean -> riwayat; panah-bawah: turun riwayat."""
    from agent.interfaces.textual_widgets import ChatBox, QueueStrip
    chatbox = app.query_one("#chatbox", ChatBox)
    inputw = app.query_one("#chat-input")
    strip = app.query_one("#queue-strip", QueueStrip)

    # Giliran berjalan (tertahan pintu), lalu dua prompt mengantre.
    pintu.clear()
    inputw.value = "tugas A"
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause(0.15)
    assert app.is_turn_active, "giliran harus berjalan"
    for teks in ("tugas B", "tugas C"):
        inputw.value = teks
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.05)
    assert strip._items == ["tugas B", "tugas C"], strip._items

    # Panah-atas: teks antrean TERAKHIR (C) ditarik ke box chat, antrean
    # tinggal B.
    await pilot.press("up")
    await pilot.pause(0.1)
    assert inputw.value == "tugas C", f"antrean tak tertarik: {inputw.value!r}"
    assert strip._items == ["tugas B"], strip._items

    # Panah-atas lagi (antrean masih ada B): B ikut tertarik.
    await pilot.press("up")
    await pilot.pause(0.1)
    assert inputw.value == "tugas B", inputw.value
    assert strip._items == [], strip._items

    # Antrean habis -> panah-atas memulai riwayat (teks terkirim terakhir
    # dicatat saat dikirim, termasuk yang sempat mengantre).
    await pilot.press("up")
    await pilot.pause(0.1)
    assert inputw.value == "tugas C", inputw.value
    await pilot.press("up")
    await pilot.pause(0.1)
    assert inputw.value == "tugas B", inputw.value
    await pilot.press("up")
    await pilot.pause(0.1)
    assert inputw.value == "tugas A", inputw.value

    # Panah-bawah: berjalan kembali ke bawah, ujungnya kosong.
    await pilot.press("down")
    await pilot.pause(0.1)
    assert inputw.value == "tugas B", inputw.value
    await pilot.press("down")
    await pilot.pause(0.1)
    assert inputw.value == "tugas C", inputw.value
    await pilot.press("down")
    await pilot.pause(0.1)
    assert inputw.value == "", repr(inputw.value)

    # Bersihkan: batalkan giliran yang tertahan.
    pintu.set()
    chatbox.set_text("")
    for _ in range(200):
        await pilot.pause(0.05)
        if not app.is_turn_active:
            break
    await pilot.pause()


async def main():
    pintu = threading.Event()
    pintu.set()  # giliran palsu langsung selesai
    ag = fake_agent(pintu)
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        await uji_plan(app, pilot)
        await uji_ask(app, pilot)
        await uji_recall(app, pilot, pintu)
    print("OK - sidebar rencana (lebar/sempit/reset) + ask_* (satu/banyak/"
          "isian bebas) + panah-atas antrean/riwayat semuanya berfungsi")


asyncio.run(main())

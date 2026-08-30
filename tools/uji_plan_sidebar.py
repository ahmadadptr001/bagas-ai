# -*- coding: utf-8 -*-
"""Uji InfoSidebar (sistem + rencana, layout lebar/sempit) + ask_* di UI Textual.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_plan_sidebar.py

Yang dicek:
1. Layar LEBAR (>= _LEBAR_MIN): sidebar kanan dan seksi Planning tampil
   SEJAK AWAL walau belum ada rencana; Sistem juga selalu ada.
2. plan() -> seksi Rencana muncul di sidebar, PlanPanel footer sembunyi
   (jangan dobel). plan_step() memindahkan penanda done/active.
3. Terminal disempitkan -> rencana pindah ke PlanPanel footer, sidebar hilang;
   dilebarkan lagi -> kembali ke sidebar.
4. Rencana TUNTAS: tampil ±8 dtk lalu kembali ke empty-state tanpa berkedip.
   Rencana BARU dari model mengisi seksi yang sama kembali.
5. core.run() mereset rencana (plan_tool.reset) -> panel kosong.
6. Handler ask_choice TERPASANG (ask_user tak lagi "[tidak interaktif]"):
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
                                               InfoSidebar, SystemPanel,
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
    seksi = app.query_one("#plan-side", PlanSidebar)
    sidebar = app.query_one("#sidebar", InfoSidebar)
    sistem = app.query_one("#system-panel", SystemPanel)

    # Layar lebar: sidebar dan seksi Planning tampil SEJAK AWAL walau belum
    # ada rencana; seksi Sistem juga selalu ada di ukuran dashboard.
    await tunggu(pilot, lambda: sidebar.display,
                 pesan="sidebar harus tampil di layar lebar walau tanpa rencana")
    assert seksi.display, "seksi planning wajib tampil di sidebar"
    assert "Belum ada rencana aktif" in seksi.terakhir, seksi.terakhir
    assert "CPU" in sistem.terakhir, sistem.terakhir
    assert "RAM" in sistem.terakhir, sistem.terakhir

    # Lebar minimum tidak boleh membuat divider 28 karakter membungkus di
    # area isi yang hanya 25 kolom.
    sidebar.terapkan_lebar(28, simpan=False)
    await pilot.pause(0.1)
    assert max(map(len, seksi.terakhir.splitlines())) <= sidebar.lebar_isi, \
        (sidebar.lebar_isi, seksi.terakhir)
    sidebar.terapkan_lebar(34, simpan=False)
    await pilot.pause(0.1)

    # plan() -> seksi Rencana muncul di sidebar, footer sembunyi (jangan dobel)
    plan_tool.plan(["langkah pertama", "langkah kedua", "langkah ketiga"], 1)
    await tunggu(pilot, lambda: "langkah pertama" in seksi.terakhir,
                 pesan="langkah plan() harus tampil di sidebar")
    await pilot.pause()
    assert not plan.display, "PlanPanel footer harus sembunyi di layar lebar"
    isi = seksi.terakhir
    assert "langkah pertama" in isi, isi
    assert "Rencana 0/3" in isi, isi

    # plan_step: langkah 1 selesai, langkah 2 aktif
    plan_tool.plan_step(2)
    await tunggu(pilot, lambda: "Rencana 1/3" in seksi.terakhir,
                 pesan="hitungan selesai harus maju ke 1/3")
    isi = seksi.terakhir
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

    # reset (dipanggil core.run tiap giliran baru) -> kembali ke empty-state,
    # dan SIDEBAR tetap tampil.
    plan_tool.reset()
    await tunggu(pilot, lambda: (seksi.display and not plan.display and
                                 "Belum ada rencana aktif" in seksi.terakhir),
                 pesan="reset: planning sidebar harus kembali ke empty-state")
    assert sidebar.display, "sidebar sistem harus tetap tampil setelah reset"


async def uji_semput_tuntas(app, pilot):
    """Rencana tuntas: tampil ±8 dtk lalu hilang sendiri, tanpa berkedip."""
    plan = app.query_one("#plan", PlanPanel)
    seksi = app.query_one("#plan-side", PlanSidebar)
    sidebar = app.query_one("#sidebar", InfoSidebar)

    plan_tool.plan(["tugas satu", "tugas dua"], 1)
    await tunggu(pilot, lambda: "tugas satu" in seksi.terakhir,
                 pesan="langkah rencana harus tampil")
    # Tandai semua selesai (current > jumlah langkah).
    plan_tool.plan_step(3)
    await tunggu(pilot, lambda: "Rencana 2/2" in seksi.terakhir,
                 pesan="semua langkah harus tercentang")
    assert seksi.display, "rencana tuntas harus TAMPIL dulu ±8 dtk"

    # Setelah ±8 dtk langkah selesai kembali menjadi empty-state, TAPI seksi
    # Planning dan sidebar tetap ada. maks 250 x 0.05 dtk = 12.5 dtk.
    await tunggu(pilot,
                 lambda: (seksi.display and
                          "Belum ada rencana aktif" in seksi.terakhir),
                 maks=260,
                 pesan="rencana tuntas harus kembali ke empty-state")
    assert sidebar.display, "sidebar sistem harus TETAP tampil"
    assert not plan.display, "footer juga harus kosong"
    # Anti-kedip: 3 dtk berikut seksi tetap tersembunyi (bug lama: cache
    # dikosongkan -> poll menganggap rencana baru -> muncul lagi tiap 8 dtk).
    for _ in range(60):
        await pilot.pause(0.05)
    assert "Belum ada rencana aktif" in seksi.terakhir, \
        "rencana tuntas tak boleh berkedip muncul lagi"
    # State plan_tool tak dilupakan: masih utuh sampai giliran baru.
    snap = plan_tool.get_state()
    assert snap["steps"] == ["tugas satu", "tugas dua"], snap

    # Rencana BARU dari model -> seksi muncul lagi (flag disembunyikan lepas).
    plan_tool.plan(["tugas lain A", "tugas lain B"], 1)
    await tunggu(pilot, lambda: "tugas lain A" in seksi.terakhir,
                 pesan="rencana baru harus muncul lagi setelah auto-hide")
    plan_tool.reset()
    await tunggu(pilot,
                 lambda: (seksi.display and
                          "Belum ada rencana aktif" in seksi.terakhir),
                 pesan="reset akhir: planning harus kembali ke empty-state")


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


async def uji_batal_antre(app, pilot, pintu, ag):
    """Ctrl+C saat giliran berjalan -> pesan antrean MAJU sebagai giliran
    baru begitu worker yang dibatalkan benar-benar mati (dulu: tersangkut
    sampai pengguna mengirim pesan lagi)."""
    from agent.interfaces.textual_widgets import ChatBox, QueueStrip
    chatbox = app.query_one("#chatbox", ChatBox)
    inputw = app.query_one("#chat-input")
    strip = app.query_one("#queue-strip", QueueStrip)
    ag.dijalankan.clear()

    # Giliran A berjalan (tertahan pintu), lalu dua prompt mengantre.
    pintu.clear()
    inputw.value = "tugas A"
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause(0.15)
    assert app.is_turn_active
    for teks in ("tugas B", "tugas C"):
        inputw.value = teks
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.05)
    assert strip._items == ["tugas B", "tugas C"]

    # Batalkan: UI langsung idle, antrean menunggu worker lama mati.
    app._cancel_event.set()
    app._stop_turn()
    await pilot.pause(0.2)
    assert not app.is_turn_active, "UI harus idle segera setelah batal"

    # Worker lama selesai (pintu dibuka) -> antrean MAJU sendiri sebagai
    # giliran baru. Giliran lanjutan bisa selesai sangat cepat (pintu
    # terbuka), jadi yang ditunggu BUKTI EKSEKUSINYA — agent.run dipanggil
    # dengan batch B+C — bukan momen is_turn_active yang transien.
    pintu.set()
    await tunggu(pilot, lambda: len(ag.dijalankan) >= 2,
                 maks=120, pesan="antrean harus maju sebagai giliran baru "
                                 "setelah pembatalan")
    await tunggu(pilot, lambda: not app.is_turn_active and not strip._items,
                 maks=120, pesan="giliran lanjutan (B+C) harus selesai "
                                 "dan strip antrean kosong")
    assert ag.dijalankan == ["tugas A", "tugas B\ntugas C"], ag.dijalankan


async def main():
    pintu = threading.Event()
    pintu.set()  # giliran palsu langsung selesai
    ag = fake_agent(pintu)
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        await uji_plan(app, pilot)
        await uji_semput_tuntas(app, pilot)
        await uji_ask(app, pilot)
        await uji_recall(app, pilot, pintu)
        await uji_batal_antre(app, pilot, pintu, ag)
    print("OK - sidebar sistem+rencana (lebar/sempit/reset/auto-hide tuntas) "
          "+ ask_* (satu/banyak/isian bebas) + panah-atas antrean/riwayat "
          "+ antrean maju setelah dibatalkan semuanya berfungsi")


asyncio.run(main())

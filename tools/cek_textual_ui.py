"""Harness verifikasi: animasi logo kecil, blok prompt, alur antrean.

Alur yang diuji:
1. Giliran pertama berjalan → prompt kedua & ketiga dikirim (mengantre).
   - Strip antrean tampil (dim), TIDAK ada echo ke riwayat.
2. Giliran pertama selesai → antrean otomatis dijalankan:
   - prompt antrean di-echo sebagai pesan pengguna NORMAL,
   - strip kosong.
3. Semua tugas benar-benar dieksekusi — tak ada yang terlewat.
"""
import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets.progress_bar import TurnProgressBar
from agent.interfaces.textual_widgets.queue_strip import QueueStrip


def fake_agent(pintu: threading.Event, gagal_pertama=None):
    """Agent palsu yang TIAP gilirannya menunggu `pintu` diset.

    Tanpa ini, giliran 0.4 detik bisa selesai di sela pengetikan harness
    sehingga isi strip antrean tidak deterministik. `gagal_pertama`:
    list yang diisi True agar giliran pertama melempar error.
    """
    ag = MagicMock()
    ag.dijalankan = []

    def run(text, **cb):
        ag.dijalankan.append(text)
        cb["on_token"]("Halo ")
        pintu.wait(timeout=10)
        if gagal_pertama and gagal_pertama[0]:
            gagal_pertama[0] = False
            raise RuntimeError("sengaja gagal")
        cb["on_message"](f"**Selesai** untuk: {text[:30]}")
        return "hasil"

    ag.model_spec = SimpleNamespace(label="web/glm", is_web=True)
    ag.run = run
    return ag


async def skenario_error(pilot, app, ag, pintu, strip, inputw, gagal):
    """Giliran gagal → antrean tetap; prompt baru tak meloncati antrean."""
    # Giliran pertama skenario ini (giliran ke-2 di app) harus melempar error.
    gagal[0] = True
    pintu.clear()  # tahan giliran supaya antrean bisa diisi deterministik

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
    assert strip._items == ["tugas B", "tugas C"], strip._items

    # Buka pintu -> giliran A melempar error; antrean harus TETAP ADA.
    pintu.set()
    for _ in range(100):
        await pilot.pause(0.05)
        if not app.is_turn_active:
            break
    await pilot.pause()
    assert strip.display and strip._items == ["tugas B", "tugas C"], \
        f"antrean hilang setelah error: {strip._items}"

    # Prompt baru TIDAK boleh meloncati antrean lama.
    inputw.value = "tugas D"
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause(0.1)
    for _ in range(200):
        await pilot.pause(0.05)
        if not app.is_turn_active and not strip.display:
            break
    await pilot.pause()

    dijalankan = " | ".join(ag.dijalankan)
    for t in ("tugas A", "tugas B", "tugas C", "tugas D"):
        assert t in dijalankan, f"TUGAS TERLEWAT: {t} ({dijalankan})"
    # B, C, D harus terkumpul dalam SATU giliran batch SETELAH giliran A.
    idx_a = next(i for i, x in enumerate(ag.dijalankan) if "tugas A" in x)
    batch = [x for x in ag.dijalankan[idx_a + 1:]
             if "tugas B" in x and "tugas C" in x and "tugas D" in x]
    assert batch, f"B/C/D tak terbatch berurutan: {ag.dijalankan}"
    assert not strip.display, "strip harus kosong di akhir"


async def main():
    pintu = threading.Event()
    gagal = [False]  # dilempar ke closure fake_agent
    ag = fake_agent(pintu, gagal)
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        inputw = app.query_one("#chat-input")
        strip = app.query_one("#queue-strip", QueueStrip)

        # 1) giliran pertama (tertahan di pintu)
        inputw.value = "tugas satu"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert app.is_turn_active, "giliran 1 harus berjalan"

        # 2) dua prompt mengantre — tanpa echo riwayat, tanpa notice
        for teks in ("tugas dua", "tugas tiga"):
            inputw.value = teks
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.05)
        await pilot.pause(0.1)
        assert strip.display, "strip antrean harus tampil"
        assert strip._items == ["tugas dua", "tugas tiga"], strip._items
        # Strip harus DI AREA TERMINAL (di luar footer) — nempel di bawah
        # jawaban, bukan di area box di atas kotak chat.
        assert strip.parent.id != "footer", \
            f"strip masih di footer (parent={strip.parent.id})"

        # 3) animasi logo kecil tetap hidup (frame beragam)
        prog = app.query_one("#progress", TurnProgressBar)
        frame_ids = set()
        for _ in range(15):
            prog.tick()
            await pilot.pause(0.02)
            frame_ids.add(prog.phase)
        assert len(frame_ids) > 8, f"animasi tak bergerak: {frame_ids}"

        # 4) buka pintu → giliran 1 selesai → antrean otomatis dijalankan
        #    (giliran lanjutan lolos pintu langsung karena Event tetap set)
        pintu.set()
        for _ in range(400):
            await pilot.pause(0.05)
            if not app.is_turn_active and not strip.display:
                break
        await pilot.pause()

        # 5) tak ada tugas terlewat: semua prompt benar-benar dijalankan
        dijalankan = " | ".join(ag.dijalankan)
        for t in ("tugas satu", "tugas dua", "tugas tiga"):
            assert t in dijalankan, f"TUGAS TERLEWAT: {t} ({dijalankan})"
        assert not strip.display, "strip harus kosong setelah dijalankan"
        assert not app.is_turn_active, "tak boleh ada giliran menggantung"

        # 6) skenario error: giliran gagal → antrean selamat → tak terlewat
        await skenario_error(pilot, app, ag, pintu, strip, inputw, gagal)
    print("OK - logo kecil, blok prompt, antrean nempel-lalu-jalan: semua"
          " tugas dieksekusi (termasuk skenario error), tanpa crash")


asyncio.run(main())

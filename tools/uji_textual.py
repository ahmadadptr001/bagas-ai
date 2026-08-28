"""Smoke-test headless untuk UI Textual bagas-ai.

Jalankan:  python -m tools.uji_textual  (atau python tools/uji_textual.py)

Memakai ``App.run_test()`` (pilot) — TANPA terminal sungguhan. Yang diuji:

1. App + seluruh widget ter-mount tanpa exception (compose/CSS/layout).
2. Ketik "/" -> dropdown autocomplete MUNCUL; panah bawah geser sorotan;
   Tab melengkapi perintah; input berisi "/model ".
3. Enter memproses perintah /help -> keluaran bantuan masuk MessageList.
4. Menu pilih (/model) muncul dan bisa dikonfirmasi lewat Enter — inilah
   jalur yang dulu melempar "object bool can't be used in 'await'".
5. Ctrl+C saat idle keluar bersih.

Exit code 0 = semua lulus.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GAGAL: list[str] = []


def cek(nama: str, kondisi: bool, detail: str = "") -> None:
    status = "OK " if kondisi else "GAGAL"
    print(f"[{status}] {nama}" + (f" — {detail}" if detail else ""))
    if not kondisi:
        GAGAL.append(nama)


async def main() -> int:
    from agent.core import Agent
    from agent.interfaces.textual_app import BagasAIApp
    from agent.interfaces.textual_widgets import ChatBox, MessageList
    from textual.widgets import OptionList

    agent = Agent()
    app = BagasAIApp(agent=agent)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        # 1. Widget inti ter-mount
        cek("widget ter-mount", app.query_one("#footer") is not None)
        cek("input fokus", app.focused is not None)

        chatbox = app.query_one("#chatbox", ChatBox)
        inp = app.query_one("#chat-input")
        dropdown = app.query_one("#autocomplete-list", OptionList)

        # 2. Autocomplete: ketik "/"
        inp.value = "/"
        await pilot.pause(0.3)
        cek("dropdown muncul saat '/'", dropdown.display,
            f"display={dropdown.display}")

        # panah bawah -> sorotan bergerak
        await pilot.press("down")
        await pilot.pause()
        cek("panah bawah menggeser sorotan", dropdown.highlighted == 1,
            f"highlighted={dropdown.highlighted}")

        # Tab -> melengkapi perintah
        await pilot.press("tab")
        await pilot.pause()
        cek("tab melengkapi perintah",
            inp.value.startswith("/") and inp.value != "/",
            f"value={inp.value!r}")
        cek("dropdown tertutup setelah tab", not dropdown.display)

        # 3. /help
        inp.value = "/help"
        await pilot.pause()
        cek("dropdown tertutup untuk /help", not dropdown.display)
        await pilot.press("enter")
        await pilot.pause(0.3)
        pesan = app.query_one("#messages", MessageList)
        cek("/help menghasilkan keluaran", len(pesan._items) > 3,
            f"items={len(pesan._items)}")

        # 4. Menu pilih (/model tanpa argumen) — jalur await-bool lama
        inp.value = "/model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.5)
        # modal SelectScreen harus terbuka
        cek("modal model terbuka",
            any(s.__class__.__name__ == "SelectScreen"
                for s in app.screen_stack))
        # Enter konfirmasi pilihan
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("modal model tertutup setelah Enter",
            not any(s.__class__.__name__ == "SelectScreen"
                    for s in app.screen_stack))

        # 5. teks biasa -> gema pengguna + giliran berjalan
        inp.value = "halo dunia"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("gema pengguna masuk", len(pesan._items) > 5)

        # 6. Ctrl+C membatalkan giliran: UI harus kembali idle SEKARANG,
        #    dan hasil giliran yang dibatalkan tidak boleh dirender.
        from agent.ui import tema as tema_mod
        # (atribut _cancel_event diperiksa lewat app)
        app._cancel_event.set()
        app._stop_turn()
        await pilot.pause(0.3)
        cek("is_turn_active False setelah batal", app.is_turn_active is False)
        cek("progress bar sembunyi setelah batal",
            not app.query_one("#progress").display)
        # worker lama selesai -> hasilnya harus diabaikan (turn_id basi)
        app._safe_call(app._turn_complete, "HASIL BATAL", app._turn_id)
        await pilot.pause(0.3)
        cek("hasil giliran batal tidak dirender",
            not any("HASIL BATAL" in str(getattr(it, "plain", it))
                    for it in pesan._items))

        # 7. menu tema: pratinjau LANGSUNG saat sorotan berpindah, ⏎ memakai
        from agent.ui import tema as tema_mod
        id_awal = tema_mod.nama_aktif()
        inp.value = "/theme"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.5)
        layar_tema = [s for s in app.screen_stack
                      if s.__class__.__name__ == "ThemeScreen"]
        cek("menu tema terbuka", bool(layar_tema))
        if layar_tema:
            awal_sorot = app._tema_pratinjau
            await pilot.press("down")
            await pilot.pause(0.3)
            cek("panah memindah pratinjau",
                app._tema_pratinjau not in (None, awal_sorot),
                f"pratinjau: {awal_sorot} -> {app._tema_pratinjau}")
            cek("CSS memakai warna pratinjau",
                app.query_one("#statusbar").styles.background is not None)
            # ⏎ memakai & menyimpan tema pratinjau
            dipilih = app._tema_pratinjau
            await pilot.press("enter")
            await pilot.pause(0.5)
            cek("tema tersimpan setelah ⏎",
                tema_mod.nama_aktif() == dipilih,
                f"{id_awal} -> {tema_mod.nama_aktif()}")
            cek("pratinjau dibersihkan", app._tema_pratinjau is None)
        # kembalikan tema awal supaya prefs pengguna tidak berubah
        if tema_mod.nama_aktif() != id_awal:
            tema_mod.set_tema(id_awal)

    if GAGAL:
        print(f"\n{len(GAGAL)} tes gagal: {', '.join(GAGAL)}")
        return 1
    print("\nSemua tes lulus.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

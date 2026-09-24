"""Smoke-test headless untuk UI Textual bagas-ai.

Jalankan:  python -m tools.uji_textual  (atau python tools/uji_textual.py)

Memakai ``App.run_test()`` (pilot) — TANPA terminal sungguhan. Yang diuji:

1. App + seluruh widget ter-mount tanpa exception (compose/CSS/layout).
2. Ketik "/" -> dropdown autocomplete MUNCUL; panah bawah geser sorotan;
   Tab melengkapi perintah; input berisi "/model ".
3. Enter memproses perintah /help -> keluaran bantuan masuk MessageList.
4. Menu pilih (/model) muncul INLINE di riwayat (bukan modal): picker
   di atas input — panah + Enter memilih, ketik nomor + Enter tetap jalan.
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
    msg = f"[{status}] {nama}" + (f" - {detail}" if detail else "")
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(msg.encode(enc, "replace").decode(enc, "replace"))
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
        cek("/send-compact terlihat di menu '/'",
            any(c == "/send-compact" for c, _, _ in chatbox._matches),
            f"matches={[c for c, _, _ in chatbox._matches]!r}")
        cek("/dirs tidak tergeser dari menu '/'",
            any(c == "/dirs" for c, _, _ in chatbox._matches),
            f"matches={[c for c, _, _ in chatbox._matches]!r}")

        # Regresi: handler /send-compact sudah ada, tetapi dulu perintahnya
        # terlupa dari registry autocomplete Textual.
        inp.value = "/send"
        await pilot.pause(0.1)
        cek("/send-compact muncul di autocomplete",
            any(c == "/send-compact" for c, _, _ in chatbox._matches),
            f"matches={[c for c, _, _ in chatbox._matches]!r}")
        inp.value = "/"
        await pilot.pause(0.1)

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

        # 4. Menu pilih (/model tanpa argumen) — INLINE, bukan modal
        inp.value = "/model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("prompt model INLINE terbuka", app._prompt_inline is not None)
        cek("mode prompt model = pilihan",
            app._prompt_inline is not None
            and app._prompt_inline.get("mode") == "pilihan",
            f"st={app._prompt_inline!r}")
        cek("picker terbuka di atas input", chatbox.picker_open)
        # Panah bawah + Enter: pilih opsi tersorot (tanpa ketik nomor)
        await pilot.press("down")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("prompt model tertutup setelah panah+Enter",
            app._prompt_inline is None)

        # Jalur nomor lama tetap hidup: buka lagi, ketik 1 + Enter
        inp.value = "/model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("prompt model terbuka lagi (jalur nomor)",
            app._prompt_inline is not None and chatbox.picker_open)
        await pilot.press("1")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("prompt model tertutup setelah jawab nomor",
            app._prompt_inline is None)

        # 5. teks biasa -> gema pengguna + giliran berjalan
        inp.value = "halo dunia"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("gema pengguna masuk", len(pesan._items) > 5)

        # 6. Ctrl+C membatalkan giliran: UI harus kembali idle SEKARANG,
        #    dan hasil giliran yang dibatalkan tidak boleh dirender.
        from agent.ui import tema as tema_mod  # noqa: F401 — dipakai di #7
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

        # 7. menu tema: INLINE, ⏎ / panah memakai tema (tanpa pratinjau live)
        id_awal = tema_mod.nama_aktif()
        inp.value = "/theme"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.5)
        cek("menu tema INLINE terbuka", app._prompt_inline is not None)
        if app._prompt_inline is not None:
            # Enter memakai opsi tersorot pertama (picker)
            await pilot.press("enter")
            await pilot.pause(0.5)
            cek("prompt tema tertutup setelah pilih picker",
                app._prompt_inline is None)
            cek("tema aktif berubah atau prompt tertutup",
                tema_mod.nama_aktif() != id_awal
                or app._prompt_inline is None,
                f"{id_awal} -> {tema_mod.nama_aktif()}")
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

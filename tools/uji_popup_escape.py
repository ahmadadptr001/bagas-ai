"""Regresi Esc: menu inline, fokus berpindah, modal bertumpuk, dan cleanup."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock

root = Path(tempfile.mkdtemp(prefix="uji_popup_escape_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from agent import models, prefs
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import ChatBox, FileEditorScreen, VoiceScreen
from agent.interfaces.textual_widgets.modal_screens import (
    SelectScreen, MultiSelectScreen, ThemeScreen, ConfirmScreen,
    TextPromptScreen, BtwScreen,
)
from agent.ui import tema
from textual.widgets import OptionList, TextArea


async def main():
    prefs.save(tema="macos", suara=False)
    agent = MagicMock()
    agent.model_spec = models.ModelSpec(
        id="opencode/fixture", label="Model uji", provider="opencode",
        effort_cli=True, effort_levels=("default", "low", "high"),
        effort_default="default")
    agent.effort = "default"
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    async with app.run_test(size=(120, 36)) as pilot:
        base = app.screen
        chat = app.query_one("#chatbox", ChatBox)
        # Menu effort harus bisa ditutup walaupun fokus berada pada tombol.
        chat.set_text("Draf pesan tetap ada")
        await pilot.click("#input-effort")
        await pilot.pause(0.15)
        assert app._prompt_inline is not None and chat.picker_open
        app.query_one("#input-send").focus()
        await pilot.press("escape")
        assert app._prompt_inline is None and not chat.picker_open
        assert not chat._prompt_mode and chat.current_text == "Draf pesan tetap ada"
        assert app.focused is app.query_one("#chat-input")
        agent.set_effort.assert_not_called()
        assert app.is_running

        # Tema/konfirmasi/multi/isian bebas sama-sama batal dengan Esc.
        app._show_theme_menu()
        await pilot.press("down", "escape")
        assert app._prompt_inline is None and tema.nama_aktif() == "macos"
        app.is_turn_active = True
        for mode in ("pilihan", "confirm", "multi", "teks", "tulis"):
            results = []
            app._mulai_prompt_inline(judul="Uji popup", mode=mode,
                callback=results.append, baris=["Satu", "Dua"], nilai=["satu", "dua"])
            chat.set_text("filter atau isian yang dibatalkan")
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert app._prompt_inline is None and not chat.picker_open
            assert results == [False if mode == "confirm" else None], (mode, results)
            assert app.is_turn_active and not app._cancel_event.is_set()
            assert chat.current_text == "Draf pesan tetap ada"

        # Autocomplete dan picker tanpa prompt tidak boleh menghentikan agen.
        chat.set_text("/mo")
        await pilot.pause(0.1)
        assert chat.autocomplete_open
        chat.query_one(OptionList).focus()
        await pilot.press("escape")
        assert not chat.autocomplete_open and chat.current_text == "/mo"
        chat.open_picker([("Satu", "satu")], title="Pilihan")
        await pilot.press("escape")
        assert not chat.picker_open and app.is_turn_active
        # Jalur pesan dari ChatBox pun tidak boleh jatuh ke action_cancel.
        chat.open_picker([("Satu", "satu")], title="Pilihan")
        assert chat.proses_tombol("escape")
        await pilot.pause(0.1)
        assert app.is_running and app.is_turn_active and not app._cancel_event.is_set()
        chat.set_text("Draf aman saat tidak ada popup")
        await pilot.press("escape", "escape")
        assert chat.current_text == "Draf aman saat tidak ada popup"
        assert app.is_running and app.is_turn_active
        app._bersihkan_turn_ui()

        # Setiap modal mempertahankan kontrak hasil pembatalan dan cleanup-nya.
        for screen, expected in (
            (SelectScreen(options=["Satu"]), None),
            (MultiSelectScreen(options=["Satu"]), None),
            (ThemeScreen(themes=tema.daftar()), None),
            (ConfirmScreen(), False),
            (TextPromptScreen(default="Draf"), None),
            (BtwScreen(answer=lambda _: "Jawaban"), None),
        ):
            results = []
            app.push_screen(screen, callback=results.append)
            await pilot.pause(0.1)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app.screen is base and results == [expected], type(screen).__name__
            assert app.is_running

        parent = SelectScreen(options=["Satu"])
        app.push_screen(parent)
        await pilot.pause(0.1)
        child = TextPromptScreen(default="Draf")
        app.push_screen(child)
        await pilot.pause(0.1)
        await pilot.press("escape")
        assert app.screen is parent
        await pilot.press("escape")
        assert app.screen is base

        closed = []
        voice = VoiceScreen(lambda: ("Mendengarkan", 0.0), lambda: closed.append(True))
        app.push_screen(voice)
        await pilot.pause(0.1)
        await pilot.press("escape")
        assert app.screen is base and closed == [True]

        saved = []
        editor = FileEditorScreen(root / "demo.py", "print('awal')\n",
            lambda old, new: saved.append(new))
        app.push_screen(editor)
        await pilot.pause(0.1)
        editor.query_one("#file-editor-area", TextArea).load_text("print('baru')\n")
        await pilot.pause(0.1)
        await pilot.press("escape")
        assert app.screen is editor, "Esc pertama menjaga perubahan yang belum disimpan"
        await pilot.press("escape")
        assert app.screen is base and not saved
        agent.run.assert_not_called()
    print("OK: Esc menutup menu dan modal teratas, draf aman, agen tidak ikut dibatalkan")


asyncio.run(main())

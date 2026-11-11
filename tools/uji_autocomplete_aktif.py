"""Regresi menu slash: perintah aktif, seluruh opsi terjangkau, dan effort."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock

root = Path(tempfile.mkdtemp(prefix="uji_autocomplete_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from agent import models, prefs
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import ChatBox, BtwScreen
from agent.interfaces.cli import SLASH_COMMANDS
from agent.ui.commands import QUICK_COMMANDS
from agent.ui.help import HelpContent


async def main():
    prefs.save(suara=False, tema="macos")
    agent = MagicMock()
    with_effort = models.ModelSpec(id="opencode/uji", label="Model uji", provider="opencode",
        effort_cli=True, effort_levels=("default", "high"), effort_default="default")
    agent.model_spec = with_effort
    agent.effort = "default"
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    names = [name for name, _, _ in QUICK_COMMANDS]
    assert len(names) == len(set(names))
    assert ["/" + name for name, _ in SLASH_COMMANDS] == names
    help_names = {row[0] for _, rows in HelpContent(full=True).groups for row in rows}
    assert set(names) - {"/help", "/exit"} <= help_names
    assert not ({"/tim", "/video", "/web", "/bot", "/permissions-bot", "/delete"} & help_names)
    assert {"/scan", "/rm-dir", "/dirs", "/version"} <= help_names
    async with app.run_test(size=(120, 36)) as pilot:
        chat = app.query_one("#chatbox", ChatBox)
        for width, height in ((120, 36), (80, 24), (40, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause(0.15)
            chat.set_text("/")
            await pilot.pause(0.15)
            assert [item[0] for item in chat._matches] == names
            await pilot.press(*(["down"] * (len(names) - 1)))
            assert chat._autocomplete.highlighted == len(names) - 1
            assert chat._autocomplete.scroll_y > 0
            assert chat._autocomplete.region.height <= 9
            await pilot.press("tab")
            assert chat.current_text == "/exit" and app.is_running
            if "--preview" in sys.argv:
                chat.set_text("/")
                await pilot.pause(0.1)
                out = repo / "screenshots/ui-reference"
                out.mkdir(parents=True, exist_ok=True)
                app.save_screenshot(f"autocomplete-{width}.svg", path=str(out))

        for removed in ("/tim", "/video", "/web", "/bot", "/reset", "/send-compact", "/version"):
            chat.set_text(removed)
            await pilot.pause(0.05)
            assert not chat.autocomplete_open
        chat.set_text("/ef")
        await pilot.pause(0.1)
        assert chat.autocomplete_open and chat._matches[0][0] == "/effort"
        agent.model_spec = models.ModelSpec(id="opencode/plain", label="Tanpa effort", provider="opencode")
        app._refresh_status()
        assert not chat.autocomplete_open and chat.query_one("#input-effort").disabled
        agent.model_spec = with_effort
        app._refresh_status()
        assert chat.autocomplete_open
        await pilot.press("tab")
        assert chat.current_text == "/effort "
        await pilot.press("enter")
        assert chat.picker_open and app._prompt_inline is not None
        await pilot.press("escape")
        assert not chat.picker_open and app.is_running
        chat.set_text("/bt")
        await pilot.pause(0.1)
        await pilot.press("tab", "enter")
        assert isinstance(app.screen, BtwScreen)
        await pilot.press("escape")
        chat.set_text("/help")
        await pilot.press("enter")
        assert isinstance(app.query_one("#messages")._items[-1], HelpContent)
        agent.run.assert_not_called()
    print("OK: katalog aktif, scroll seluruh opsi, filter effort, Tab/Enter/Esc dan bantuan sinkron")


asyncio.run(main())

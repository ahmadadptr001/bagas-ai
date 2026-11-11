"""Cek tata letak dan kontrol title bar. --preview menyimpan render SVG."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock, patch

temp = Path(tempfile.mkdtemp(prefix="uji_macbook_"))
os.environ["BAGASAI_HOME"] = str(temp / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(temp)
os.environ.pop("NO_COLOR", None)
os.environ["TERM"] = "xterm-256color"
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import ChatBox, InfoSidebar
from textual.events import MouseMove


async def main():
    agent = MagicMock()
    agent.model_spec = MagicMock(label="Big Pickle (CLI)", is_web=False)
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    with patch.object(InfoSidebar, "_baca_lebar_tersimpan", return_value=34):
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            header = app.query_one("#logo")
            status = app.query_one("#statusbar")
            sidebar = app.query_one("#sidebar")
            footer = app.query_one("#footer")
            hint = app.query_one("#empty-state")
            history = app.query_one("#history")
            chat = app.query_one("#chatbox", ChatBox)
            for width, height in ((140, 40), (100, 30), (80, 24), (40, 20)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert header.region.width == status.region.width == width
                assert header.display and header.region.bottom <= history.region.y
                assert footer.region.bottom <= status.region.y
                assert app.query_one("#input-send").region.right <= width
                assert app.query_one("#chat-input").region.bottom <= status.region.y
                assert abs((hint.region.y + hint.region.height / 2)
                           - (history.region.y + history.region.height / 2)) <= 1
                if width >= 100:
                    assert sidebar.display and footer.region.right <= sidebar.region.x
                    assert sidebar.region.y == header.region.bottom
                    assert sidebar.region.bottom <= status.region.y
                else:
                    assert not sidebar.display

            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            log = app.query_one("#messages")
            log.clear_messages()
            log.append_notice("alpha beta gamma")
            await pilot.pause(0.35)  # tunggu reflow/debounce resize terakhir
            x, y = log.gutter.left + 2, log.gutter.top
            await pilot.mouse_down(log, offset=(x, y), button=1)
            log.post_message(MouseMove(log, x + 1, y, 0, 1, 0, 0, False, False, False))
            await pilot.mouse_up(log, offset=(x + 1, y))
            await pilot.pause()
            assert app.clipboard == "a", repr(app.clipboard)
            await pilot.press("ctrl+c")
            assert app.is_running and app.clipboard == "a"
            if "--preview" in sys.argv:
                from agent import config
                output = repo / "screenshots" / "ui-reference"
                output.mkdir(parents=True, exist_ok=True)
                with patch.object(config, "PROJECT_ROOT", repo):
                    sidebar._gambar_path()
                    await pilot.pause()
                    app.save_screenshot("macbook.svg", path=str(output))
                print(f"Preview: {output / 'macbook.svg'}")

            await pilot.click("#input-more")
            await pilot.pause()
            assert chat.current_text == "/" and chat.autocomplete_open
            await pilot.press("escape")
            chat.set_text("draft tetap ada")
            await pilot.click("#input-more")
            assert chat.current_text == "draft tetap ada"
            chat.clear()

            await pilot.click("#titlebar-help")
            await pilot.pause()
            assert header.display and not hint.display
            agent.run.assert_not_called()
            await pilot.click("#titlebar-theme")
            await pilot.pause()
            assert app._prompt_inline is not None
            # Klik menu lain tidak boleh menjawab prompt tema yang sedang aktif.
            state = app._prompt_inline
            await pilot.click("#titlebar-help")
            assert app._prompt_inline is state
            assert app.query_one("#input-more").disabled
            await pilot.press("escape")
    print("OK: header/status penuh, sidebar/input terpisah, responsive, menu dan draft aman")


asyncio.run(main())

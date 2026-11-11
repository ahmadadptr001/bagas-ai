"""Regresi popup /btw, kontrol kecil, draf saat sibuk, dan isolasi chat utama."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import MagicMock

root = Path(tempfile.mkdtemp(prefix="uji_btw_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import BtwScreen, MessageList
from textual.widgets import Input
from agent import prefs


async def wait_reply(pilot, screen):
    for _ in range(100):
        await pilot.pause(0.02)
        if not screen._busy:
            return
    raise AssertionError("jawaban /btw tidak selesai")


async def main():
    prefs.save(suara=False, tema="macos")
    agent = MagicMock()
    agent.model_spec = MagicMock(label="Big Pickle (CLI)", is_web=False)
    agent.memory.messages = []
    agent.session = None
    agent.btw.return_value = "**Efek kaca** memberi kesan berlapis. Gunakan kontras yang cukup agar teks tetap terbaca."
    app = BagasAIApp(agent=agent)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        main_log = app.query_one("#messages", MessageList)
        original = list(main_log._items)
        for width, height in ((140, 44), (80, 24), (40, 20)):
            await pilot.resize_terminal(width, height)
            app._cmd_btw("/btw Apa kegunaan efek kaca pada UI?")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, BtwScreen)
            await wait_reply(pilot, screen)
            inp = screen.query_one("#btw-input", Input)
            close = screen.query_one("#btw-close")
            send = screen.query_one("#btw-send")
            assert close.content_region.height == send.content_region.height == 1
            assert close.region.right <= width and send.region.right <= width
            assert inp.region.bottom < height
            log = screen.query_one("#btw-log", MessageList)
            assert log.max_scroll_x == 0
            text = "\n".join(line.text for line in log.lines)
            assert "Efek kaca" in text and "**Efek kaca**" not in text
            assert list(main_log._items) == original and not app.is_turn_active
            await pilot.press("ctrl+c")
            assert app.is_running and app.screen is screen
            if "--preview" in sys.argv:
                out = repo / "screenshots/ui-reference"
                out.mkdir(parents=True, exist_ok=True)
                app.save_screenshot(f"btw-{width}.svg", path=str(out))
            await pilot.click("#btw-close")
            await pilot.pause()
            assert app.screen is not screen

        release = threading.Event()
        agent.btw.side_effect = lambda question, **kwargs: (release.wait(5), "Jawaban selesai")[1]
        app._cmd_btw("/btw Pertanyaan pertama")
        await pilot.pause()
        screen = app.screen
        inp = screen.query_one("#btw-input", Input)
        inp.value = "draf berikutnya"
        await pilot.press("enter")
        assert inp.value == "draf berikutnya" and screen._busy
        assert screen.query_one("#btw-send").disabled
        loading = screen.query_one("#btw-loading")
        assert loading.display and loading.parent.id == "btw-container"
        assert loading.region.y >= screen.query_one("#btw-log").region.bottom
        assert loading.region.bottom <= screen.query_one("#btw-compose").region.y
        await pilot.press("escape")
        release.set()
        await pilot.pause(0.2)
        assert app.screen is not screen and list(main_log._items) == original
        agent.run.assert_not_called()
        assert agent.memory.messages == []
    print("OK: popup /btw tematik, 3 ukuran, Markdown, tombol utuh, draf dan tugas utama aman")


asyncio.run(main())

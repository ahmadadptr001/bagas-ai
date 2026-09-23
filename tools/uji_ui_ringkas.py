"""Uji layout chat dan voice di terminal lebar maupun sempit."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock

root = Path(tempfile.mkdtemp(prefix="uji_ui_ringkas_"))
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import VoiceScreen


async def main():
    for width, height in ((120, 40), (80, 24), (40, 20)):
        agent = MagicMock()
        agent.model_spec = MagicMock(label="Model uji", is_web=False)
        app = BagasAIApp(agent=agent)
        async with app.run_test(size=(width, height)) as pilot:
            await pilot.pause()
            logo = app.query_one("#logo")
            assert logo.size.height <= 4
            chat = app.query_one("#chat-input")
            assert chat.region.bottom <= height and chat.region.right <= width
            if width == 120:
                app.save_screenshot("chat.svg", path=str(root))
            screen = VoiceScreen(lambda: ("menangkap", 0.5), lambda: None)
            app.push_screen(screen)
            await pilot.pause()
            button = screen.query_one("#voice-close")
            status = screen.query_one("#voice-status")
            assert 0 <= button.region.y < button.region.bottom <= height
            assert button.region.right <= width
            assert status.region.bottom <= button.region.y
            if width == 120:
                app.save_screenshot("voice.svg", path=str(root))
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen
    print(f"OK: chat/voice muat di 120x40, 80x24, 40x20; screenshot: {root}")


asyncio.run(main())

"""Regresi hapus penanda, multi-lampiran, kirim cepat, dan preview ringkas."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock

root = Path(tempfile.mkdtemp(prefix="uji_lampiran_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from PIL import Image
from agent import models, prefs
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import ChatBox, ImagePreview


async def wait_done(pilot, app, agent):
    for _ in range(100):
        await pilot.pause(0.05)
        if agent.run.called and not app.is_turn_active:
            return
    raise AssertionError("Pesan belum terkirim")


async def main():
    prefs.save(suara=False, tema="macos")
    paths = [root / "tampilan aplikasi.png", root / "referensi kedua.png"]
    for index, path in enumerate(paths):
        Image.new("RGB", (180, 120), (30 + index * 60, 90, 180)).save(path)
    agent = MagicMock()
    agent.model_spec = models.ModelSpec(id="opencode/uji", label="Model uji", provider="opencode")
    agent.memory.messages = []
    agent.run.return_value = "Gambar sudah diterima."
    app = BagasAIApp(agent=agent)
    async with app.run_test(size=(140, 40)) as pilot:
        chat = app.query_one("#chatbox", ChatBox)
        preview = app.query_one("#image-preview", ImagePreview)
        for width, height in ((140, 40), (80, 24), (40, 20)):
            await pilot.resize_terminal(width, height)
            chat.set_text("Periksa gambar ini")
            chat.handle_paste(str(paths[0]))
            await pilot.pause(0.2)
            assert preview.display and preview.region.height == 1
            assert preview.title == paths[0].name
            toggle = preview.query_one("#image-toggle")
            assert toggle.label.cell_length <= toggle.content_size.width
            if width == 40:
                assert toggle.label.plain.endswith("…")
            remove = preview.query_one("#image-remove")
            assert remove.content_region.height == 1
            assert remove.region.right <= preview.region.right <= width
            await pilot.click("#image-toggle")
            await pilot.pause(0.15)
            assert preview._expanded and preview.region.height <= 6
            body = preview.query_one("#image-content")
            assert all(line.cell_len <= body.content_size.width
                       for line in body.content.split("\n"))
            assert app.query_one("#chat-input").region.bottom <= app.query_one("#statusbar").region.y
            if "--preview" in sys.argv:
                out = repo / "screenshots/ui-reference"
                out.mkdir(parents=True, exist_ok=True)
                app.save_screenshot(f"attachment-open-{width}.svg", path=str(out))
            # Hapus satu karakter penanda melalui tombol keyboard asli.
            end = chat.current_text.index("[foto]") + len("[foto]")
            chat._input.move_cursor((0, end))
            chat.focus()
            await pilot.press("backspace")
            await pilot.pause(0.2)
            assert not preview.display and not app._pending_gambar
            assert "Periksa gambar ini" in chat.current_text and paths[0].is_file()

        chat.set_text("Bandingkan kedua gambar")
        chat.handle_paste(f'"{paths[0]}" "{paths[1]}"')
        await pilot.pause(0.2)
        assert len(app._pending_gambar["items"]) == 2
        chat.set_text(chat.current_text.replace("[foto]", ""))
        await pilot.pause(0.15)
        assert preview.display and list(app._pending_gambar["items"]) == ["[foto 2]"]
        await pilot.click("#image-remove")
        await pilot.pause(0.15)
        assert not preview.display and not app._pending_gambar
        assert "Bandingkan kedua gambar" in chat.current_text

        # Menu sementara tidak menghapus lampiran draf; Esc memulihkannya.
        chat.set_text("Draf dengan foto")
        chat.handle_paste(str(paths[0]))
        await pilot.pause(0.15)
        app._show_theme_menu()
        await pilot.pause(0.15)
        assert preview.display and app._pending_gambar
        await pilot.press("escape")
        assert "[foto]" in chat.current_text and app._pending_gambar
        chat.clear()
        await pilot.pause(0.15)
        assert not preview.display and not app._pending_gambar

        # Drop dan kirim dalam giliran event yang sama, sebelum Pasted diproses.
        chat.set_text("Kirim cepat")
        chat.handle_paste(str(paths[0]))
        chat.kirim()
        await wait_done(pilot, app, agent)
        prompt = agent.run.call_args.args[0]
        assert "[GAMBAR]" in prompt and str(paths[0]) in prompt
        assert not preview.display and not app._pending_gambar
        agent.run.reset_mock()
        chat.set_text("Bandingkan")
        chat.handle_paste(f'"{paths[0]}" "{paths[1]}"')
        await pilot.pause(0.15)
        await pilot.press("enter")
        await wait_done(pilot, app, agent)
        prompt = agent.run.call_args.args[0]
        assert prompt.count("[GAMBAR]") == 2 and all(str(path) in prompt for path in paths)
        assert not preview.display and not app._pending_gambar
    print("OK: hapus penanda sinkron, multi-lampiran, kirim cepat aman, preview 1/6 baris pada 3 ukuran")


asyncio.run(main())

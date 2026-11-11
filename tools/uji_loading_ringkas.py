"""Regresi loading: timer, reflow Unicode, dan pergantian tahap respons."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock, patch

root = Path(tempfile.mkdtemp(prefix="uji_loading_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from agent import prefs
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import MessageList, TurnProgressBar


async def main():
    prefs.save(tema="macos", suara=False)
    agent = MagicMock()
    agent.model_spec = MagicMock(label="Model uji", is_web=False)
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    async with app.run_test(size=(140, 40)) as pilot:
        progress = app.query_one("#progress", TurnProgressBar)
        content = progress.query_one("#progress-content")
        assert not progress.display

        # Perintah di luar chat tetap beranimasi, tanpa timer milik app.
        progress.show(1.0, "memindai proyek...")
        await pilot.pause(0.2)
        assert progress.label == "Memindai proyek" and not app.is_turn_active
        first = progress.phase
        await pilot.pause(0.2)
        assert progress.phase != first, "fraksi 1.0 tidak boleh membekukan animasi"
        started = progress._started_at
        with patch.object(content, "update", wraps=content.update) as update:
            for _ in range(100):
                progress.show(1.0, "memindai proyek...")
            assert update.call_count == 0, "status sama tidak perlu digambar tiap token"
        assert progress._started_at == started

        # Label berganti tanpa reset jam; newline dan Unicode tidak meluber.
        progress.show(0.0, "menunggu\nrespons...")
        assert progress._started_at == started
        assert progress.label == "Menunggu respons"
        for width, height in ((140, 40), (80, 24), (40, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause(0.25)
            progress._started_at -= 65
            label = "Membaca file · 漢字 📄 " + "nama_file_panjang_" * 20
            progress.show(0.99, label)
            progress.tick()
            await pilot.pause(0.2)
            text = content.content
            assert progress.region.height == content.region.height == 1
            assert "\n" not in text.plain
            assert text.cell_len <= progress.content_size.width, (text, progress.region)
            if progress.content_size.width < 48:
                assert text.plain.endswith("…")
            else:
                assert text.plain.rsplit(" ", 1)[-1].count(":") == 1
            assert progress.tooltip == label
            assert app.query_one("#history").region.height >= height // 3
            assert app.query_one("#chat-input").region.bottom <= app.query_one("#statusbar").region.y
            if "--preview" in sys.argv:
                progress.show(0.0, "Menunggu respons")
                out = repo / "screenshots/ui-reference"
                out.mkdir(parents=True, exist_ok=True)
                await pilot.pause(0.2)
                app.save_screenshot(f"loading-{width}.svg", path=str(out))

        progress.hide()
        phase = progress.phase
        await pilot.pause(0.4)
        assert not progress.display and progress.phase == phase
        assert progress._started_at is None

        # Penalaran dan loading tidak menumpuk; respons hanya tampil sekali.
        app.is_turn_active = True
        app._turn_id += 1
        app.query_one("#empty-state").display = False
        messages = app.query_one("#messages", MessageList)
        messages.clear_messages()
        messages.begin_stream()
        progress.show(0.0, "Menyiapkan respons")
        app._forward_reasoning("Memeriksa perubahan yang dibutuhkan.")
        await pilot.pause(0.2)
        assert not progress.display and app.query_one("#thinking-block").display
        app._show_status("menunggu model")
        assert not progress.display
        app._show_tool_start("read_file", {"path": "app.py"})
        assert progress.display and progress.label == "Baca file · app.py"
        assert not app.query_one("#thinking-block").display
        app._forward_token("Jawaban tetap utuh.")
        assert progress.label == "Menjawab"
        app._turn_complete("Jawaban tetap utuh.", app._turn_id)
        await pilot.pause(0.3)
        assert not progress.display and not app.is_turn_active
        assert "\n".join(line.text for line in messages.lines).count("Jawaban tetap utuh.") == 1

        # Pembatalan menghentikan indikator dengan jalur cleanup yang sama.
        app.is_turn_active = True
        progress.show(0.0, "Menunggu respons")
        app._turn_error(KeyboardInterrupt(), app._turn_id)
        await pilot.pause(0.3)
        assert not progress.display and not app.is_turn_active
    assert progress._timer is None, "timer harus dibersihkan ketika widget dilepas"
    print("OK: loading satu baris, timer mandiri, Unicode aman, transisi dan cleanup lulus")


asyncio.run(main())

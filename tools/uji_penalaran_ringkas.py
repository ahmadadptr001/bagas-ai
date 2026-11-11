"""Regresi viewport penalaran, gulir, dan satu arsip untuk beberapa tahap."""
import asyncio
import io
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock, patch

root = Path(tempfile.mkdtemp(prefix="uji_penalaran_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from rich.console import Console, Group
from rich.cells import cell_len
from textual.events import MouseScrollDown, MouseScrollUp
from agent import prefs
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import ThinkingBlock, MessageList
from agent.interfaces import cli


def wheel(widget, kind, x, y):
    widget.post_message(kind(widget, x, y, 0, 0, 0, False, False, False))


def check_classic(text):
    for width, height in ((140, 40), (80, 24), (40, 20)):
        out = io.StringIO()
        console = Console(file=out, width=width, height=height, color_system=None)
        with patch.object(cli, "console", console):
            console.print(Group(*cli._blok_pikir(text, True)))
        lines = out.getvalue().splitlines()
        assert len(lines) <= 1 + min(3, height // 8)
        assert all(cell_len(line) <= width for line in lines)


async def main():
    prefs.save(tema="macos", suara=False)
    agent = MagicMock()
    agent.model_spec = MagicMock(label="Model uji", is_web=False)
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    text = ("Saya memeriksa struktur proyek dan perubahan yang dibutuhkan. " * 70
            + "Periksa karakter lebar 漢字 agar pembungkusan tetap rapi. " * 10
            + "AKHIR_PENALARAN")
    check_classic(text)
    async with app.run_test(size=(140, 40)) as pilot:
        for width, height in ((140, 40), (80, 24), (40, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause(0.3)
            app.query_one("#empty-state").display = False
            messages = app.query_one("#messages", MessageList)
            messages.clear_messages()
            thinking = app.query_one("#thinking-block", ThinkingBlock)
            thinking.clear()
            app._reasoning_item = None
            app._pikiran_sudah_simpan = False
            app.is_turn_active = True
            app._turn_id += 1
            app.query_one("#progress").show(0.0, "menyiapkan")
            app._forward_reasoning(text)
            await pilot.pause(0.3)
            assert thinking.region.height == 1, thinking.region
            assert not app.query_one("#progress").display
            if "--preview" in sys.argv:
                out = repo / "screenshots/ui-reference"
                out.mkdir(parents=True, exist_ok=True)
                app.save_screenshot(f"reasoning-closed-{width}.svg", path=str(out))
            await pilot.click("#thinking-content")
            await pilot.pause(0.3)
            body = thinking.query_one("#thinking-body")
            assert not thinking.collapsed and body.display
            assert 1 < thinking.region.height <= 1 + min(3, height // 8), thinking.region
            assert body.max_scroll_y > 0 and body.max_scroll_x == 0, (
                body.region, body.content_region, body.scrollable_content_region,
                len(body.lines), body.max_scroll_y, body.max_scroll_x, body.virtual_size)
            assert app.query_one("#chat-input").region.bottom <= app.query_one("#statusbar").region.y
            assert app.query_one("#history").region.height >= height // 3
            assert "AKHIR_PENALARAN" in "\n".join(line.text for line in body.lines)
            wheel(body, MouseScrollUp, 2, 1)
            await pilot.pause(0.15)
            assert body.scroll_y < body.max_scroll_y and not thinking.collapsed
            position = body.scroll_y
            thinking.append_thinking(" TAMBAHAN_LANGKAH")
            await pilot.pause(0.3)
            assert abs(body.scroll_y - position) <= 1
            if "--preview" in sys.argv:
                app.save_screenshot(f"reasoning-open-{width}.svg", path=str(out))

            app._show_tool_start("read_file", {"path": "app.py"})
            app._show_tool_result("read_file", "isi file", {"path": "app.py"})
            await pilot.pause()
            assert not thinking.display and app.query_one("#progress").display
            item = app._reasoning_item
            assert item is not None and not item.terbuka
            app._forward_reasoning(" TAHAP_KEDUA")
            assert not thinking.display
            assert "TAHAP_KEDUA" in item.body.plain
            app._forward_token("Hasil sudah diperiksa.")
            app._turn_complete("Hasil sudah diperiksa.", app._turn_id)
            await pilot.pause()
            assert sum(entry is item for entry in messages._items) == 1
            assert item.body.plain == text + " TAMBAHAN_LANGKAH TAHAP_KEDUA"
            assert "\n".join(line.text for line in messages.lines).count("Hasil sudah diperiksa.") == 1

            messages.scroll_home(animate=False)
            await pilot.pause()
            await pilot.click(messages, offset=(messages.gutter.left + 2, messages.gutter.top))
            await pilot.pause()
            assert item.terbuka and item.max_expanded_lines == min(3, height // 8)
            assert len([value for value in messages._blok_klik.values() if value is item]) <= 4
            before = item.line_offset
            wheel(messages, MouseScrollDown, messages.gutter.left + 8, messages.gutter.top + 1)
            await pilot.pause()
            assert item.line_offset == before + 1
            assert messages.scroll_y == 0
            if "--preview" in sys.argv:
                app.save_screenshot(f"reasoning-history-{width}.svg", path=str(out))
            await pilot.click(messages, offset=(messages.gutter.left + 2, messages.gutter.top))
            await pilot.pause()
            assert not item.terbuka
        # Jawaban langsung, lalu penalaran tambahan ketika arsip sedang dibuka.
        messages.clear_messages()
        thinking.clear()
        app._reasoning_item = None
        app.is_turn_active = True
        app._turn_id += 1
        app._forward_reasoning("Penalaran awal untuk jawaban langsung.")
        app._forward_token("Jawaban langsung")
        item = app._reasoning_item
        assert item is not None and not thinking.display
        messages._toggle_blok(item)
        app._forward_reasoning(" Penalaran lanjutan tanpa duplikasi.")
        app._forward_token(" tetap utuh.")
        app._turn_complete("Jawaban langsung tetap utuh.", app._turn_id)
        await pilot.pause()
        assert "\n".join(line.text for line in messages.lines).count("Jawaban langsung tetap utuh.") == 1
        assert sum(entry is item for entry in messages._items) == 1
        assert "Penalaran lanjutan" in item.body.plain
        # Resize tinggi saja juga mengecilkan viewport arsip yang sedang dibuka.
        await pilot.resize_terminal(40, 32)
        await pilot.pause(0.3)
        assert item.max_expanded_lines == 3
        await pilot.resize_terminal(40, 20)
        await pilot.pause(0.3)
        assert item.max_expanded_lines == 2
    print("OK: header 1 baris, viewport 2-3 baris, gulir tanpa duplikasi, penalaran lengkap dan jawaban tunggal")


asyncio.run(main())

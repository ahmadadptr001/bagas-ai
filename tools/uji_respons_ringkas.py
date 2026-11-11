"""Regresi narasi ganda, detail tool, dan diff OpenCode yang sudah dieksekusi."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock, patch

root = Path(tempfile.mkdtemp(prefix="uji_respons_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from agent import llm, prefs
from agent.core import Agent
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import ChatBox
from agent.ui.diff_view import DiffView
from agent.ui.help import HelpContent
from agent.interfaces.textual_widgets.message_list import _BlokDetail


def check_transport():
    first = "Saya periksa file."
    second = "Saya sesuaikan tombol."
    final = "Tombol terhubung. Pengujian lulus."
    read = {"type": "tool_use", "part": {"id": "tool-read", "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "test.html"}, "output": "isi"}}}
    events = [
        {"type": "text", "part": {"id": "a", "text": first[:10]}},
        {"type": "text", "part": {"id": "a", "text": first}},
        read, read,
        {"type": "text", "part": {"id": "b", "text": second}},
        {"type": "tool_use", "part": {"id": "tool-write", "tool": "write",
            "state": {"status": "completed", "input": {}, "output": "selesai"}}},
        {"type": "text", "part": {"id": "c", "text": final}},
    ]
    process = MagicMock()
    process.stdout = io.StringIO("\n".join(json.dumps(ev) for ev in events) + "\n")
    process.stderr = io.StringIO("")
    process.returncode = 0
    tokens, tools = [], []
    with (patch.object(llm.shutil, "which", return_value="opencode"),
          patch.object(llm, "_opencode_variant_args", return_value=["-m", "fixture"]),
          patch.object(llm.subprocess, "Popen", return_value=process)):
        text, calls, _usage = llm._stream_opencode_cli(
            [{"role": "user", "content": "uji"}],
            on_content=tokens.append, on_tool_pending=lambda *args: tools.append(args))
    assert text == final and not calls
    assert "".join(tokens) == first + second + final
    assert len(tools) == 2, tools
    print("OK: final OpenCode tidak mengulang narasi; event tool ulang diabaikan")


async def check_ui():
    prefs.save(suara=False, tema="macos")
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    app = BagasAIApp(agent=agent)
    file = root / "test.html"
    before = '<button id="send">Kirim</button>\n<script>\n// belum terhubung\n</script>\n'
    after = '<button id="send">Kirim</button>\n<script>\nsend.onclick = submit;\n</script>\n'
    first, second, final = "Saya periksa file.", "Saya hubungkan tombol.", "Tombol terhubung dan siap diuji."
    file.write_text(before, encoding="utf-8")

    def stream(_messages, **kwargs):
        kwargs["on_content"](first)
        kwargs["on_tool_pending"]("read", {"status": "completed", "input": {"filePath": str(file)},
            "output": before, "metadata": {}})
        kwargs["on_content"](second)
        # Native OpenCode menulis lebih dulu, lalu mengirim event completed.
        file.write_text(after, encoding="utf-8")
        kwargs["on_tool_pending"]("write", {"status": "completed", "input": {
            "filePath": str(file), "content": after}, "output": "Wrote file successfully",
            "metadata": {"exists": True}})
        kwargs["on_content"](final)
        return final, [], None

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        log = app.query_one("#messages")
        log.clear_messages()
        with patch.object(llm, "stream_completion", side_effect=stream):
            app.on_chatbox_submitted(ChatBox.Submitted("Hubungkan tombol"))
            for _ in range(100):
                await pilot.pause(0.05)
                if not app.is_turn_active:
                    break
        assert not app.is_turn_active
        content = "\n".join(line.text for line in log.lines)
        assert all(content.count(text) == 1 for text in (first, second, final)), content
        diffs = [item for item in log._items if isinstance(item, DiffView)]
        assert len(diffs) == 1 and diffs[0].added == diffs[0].removed == 1
        assert "-// belum terhubung" in diffs[0].diff and "+send.onclick = submit;" in diffs[0].diff
        assert "send.onclick = submit;" in content, "diff harus langsung terlihat"
        assert not any("read_file" in line.text for line in log.lines)
        assert "? Baca file" not in content and "✓ Baca file" in content
        assert agent.memory.messages[-1]["content"] == final
        if "--preview" in sys.argv:
            out = repo / "screenshots/ui-reference"
            out.mkdir(parents=True, exist_ok=True)
            app.save_screenshot("tools.svg", path=str(out))

        # API mengirim on_token lalu on_message untuk narasi yang sama.
        log.clear_messages()
        log.begin_stream()
        app._forward_token(first)
        app._show_narration(first)
        await pilot.pause()
        assert "\n".join(line.text for line in log.lines).count(first) == 1

        log.clear_messages()
        log.append_tool_step("read_file", {"path": str(file)}, "baris awal\nbaris kedua\ndetail unik")
        await pilot.pause()
        assert "baris awal" in "\n".join(line.text for line in log.lines)
        assert "baris kedua" in "\n".join(line.text for line in log.lines)
        assert "detail unik" not in "\n".join(line.text for line in log.lines)
        detail = next(item for item in log._items if isinstance(item, _BlokDetail))
        await pilot.click(log, offset=(log.gutter.left + 2, log.gutter.top + detail.baris_judul))
        await pilot.pause()
        assert "detail unik" in "\n".join(line.text for line in log.lines)
        await pilot.click(log, offset=(log.gutter.left + 2, log.gutter.top + detail.baris_judul))
        await pilot.pause()
        assert not detail.terbuka
        assert "detail unik" not in "\n".join(line.text for line in log.lines)
        log.clear_messages()
        old = "\n".join(f"old line {n}" for n in range(40))
        new = "\n".join(f"new line {n}" for n in range(40))
        log.append_diff("long.txt", old, new)
        await pilot.pause()
        log.scroll_home(animate=False)
        await pilot.pause()
        diff = next(item for item in log._items if isinstance(item, DiffView))
        await pilot.click(log, offset=(log.gutter.left + 8, log.gutter.top))
        await pilot.pause()
        assert diff.terbuka and log.scroll_y == 0
        assert "new line 39" in "\n".join(line.text for line in log.lines)

        # Diff normal, metadata replay, dan penghapusan memakai kartu yang sama.
        for width, height in ((140, 44), (80, 24), (40, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause(0.3)
            log.clear_messages()
            log.append_diff("test.html", before, after)
            await pilot.pause()
            assert log.max_scroll_x == 0
            assert "send.onclick" in "\n".join(line.text for line in log.lines)
            assert app.query_one("#input-send").content_region.height >= 1
            if "--preview" in sys.argv:
                out = repo / "screenshots/ui-reference"
                out.mkdir(parents=True, exist_ok=True)
                app.save_screenshot(f"diff-{width}.svg", path=str(out))
        log.append_delete("test.html", before)
        assert any(isinstance(item, DiffView) and item.deleted for item in log._items)
        log.clear_messages()
        app.on_chatbox_submitted(ChatBox.Submitted("/help"))
        assert len(log._items) == 2
        app.on_chatbox_submitted(ChatBox.Submitted("/help semua"))
        assert isinstance(log._items[-1], HelpContent) and log._items[-1].full
        assert "/rm-dir" in log._items[-1].plain and not app.is_turn_active
    print("OK: narasi tunggal, detail dapat dibuka, write menjadi diff, 3 ukuran aman")


check_transport()
asyncio.run(check_ui())

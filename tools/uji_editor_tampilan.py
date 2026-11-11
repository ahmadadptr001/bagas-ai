"""Regresi editor tematik, ukuran kecil, dan lifecycle simpan/batal."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock

root = Path(tempfile.mkdtemp(prefix="uji_editor_ui_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from textual.widgets import TextArea
from textual.events import MouseMove
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import FileEditorScreen, VoiceScreen


async def main():
    agent = MagicMock()
    agent.model_spec = MagicMock(label="Model Uji", is_web=False)
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    saved = []
    content = 'from pathlib import Path\n\ndef read_project(root: Path) -> list[str]:\n    # Daftar berkas project\n    return [p.name for p in root.iterdir() if p.is_file()]\n'

    def save(old, new):
        saved.append((old, new))
        return True, "Perubahan disimpan"

    async with app.run_test(size=(120, 36)) as pilot:
        for size in ((120, 36), (80, 24), (40, 20)):
            await pilot.resize_terminal(*size)
            editor = FileEditorScreen(root / "[demo].ts", content, save)
            app.push_screen(editor)
            await pilot.pause()
            area = editor.query_one("#file-editor-area", TextArea)
            assert area.size.height >= 5
            assert area.theme == "bagas-editor"
            for control in ("#file-editor-btn-save", "#file-editor-btn-close", "#file-editor-btn-diff"):
                region = editor.query_one(control).region
                assert 0 <= region.x < region.right <= size[0], (control, region)
                assert region.bottom <= size[1]
                assert editor.query_one(control).content_region.height >= 1, control
            close = editor.query_one("#file-editor-head-close")
            assert close.content_region.height == 1
            await pilot.press("escape")
            assert app.screen is not editor

        await pilot.resize_terminal(120, 36)
        editor = FileEditorScreen(repo / "src/agent/workspace.py", content, save)
        app.push_screen(editor)
        await pilot.pause()
        output = repo / "screenshots/ui-reference"
        if "--preview" in sys.argv:
            output.mkdir(parents=True, exist_ok=True)
            app.save_screenshot("editor.svg", path=str(output))
        area = editor.query_one("#file-editor-area", TextArea)
        # Seleksi satu huruf dengan mouse, bukan seleksi semua teks Widget.
        x, y = area.gutter_width + area.gutter.left, area.gutter.top
        await pilot.mouse_down(area, offset=(x, y), button=1)
        area.post_message(MouseMove(area, x + 1, y, 0, 1, 0, 0, False, False, False))
        await pilot.mouse_up(area, offset=(x + 1, y))
        await pilot.pause()
        assert area.selected_text == app.clipboard == "f"
        await pilot.press("ctrl+c")
        assert app.screen is editor and app.clipboard == "f"
        await pilot.pause(0.6)
        await pilot.click(area, offset=(x + 7, y), times=2)
        assert area.selected_text == app.clipboard == "pathlib"
        await pilot.click(area, offset=(x + 7, y), times=3)
        assert area.selected_text.strip() == content.splitlines()[0]
        await pilot.press("f6", "ctrl+c")
        assert app.clipboard.strip() == content.splitlines()[0]
        await pilot.press("home", "shift+right", "ctrl+c")
        assert app.clipboard == "f"
        await pilot.press("ctrl+v")
        assert area.text == content
        area.load_text(content.replace("p.name", "str(p)"))
        await pilot.pause()
        await pilot.press("ctrl+s")
        assert editor._preview and not saved
        if "--preview" in sys.argv:
            app.save_screenshot("editor-diff.svg", path=str(output))
        await pilot.click("#file-editor-btn-code")
        assert not editor._preview and area.has_focus
        await pilot.press("ctrl+s", "ctrl+s")
        assert len(saved) == 1 and not editor.dirty
        assert "belum" not in str(editor.query_one("#file-editor-title").render())
        area.insert("\n# Belum disimpan")
        await pilot.press("escape")
        assert app.screen is editor
        await pilot.press("escape")
        assert app.screen is not editor
        screen = VoiceScreen(lambda: ("menangkap", 0.55), lambda: None)
        app.push_screen(screen)
        await pilot.pause()
        if "--preview" in sys.argv:
            app.save_screenshot("voice.svg", path=str(output))
        await pilot.press("escape")
        assert app.screen is not screen
    print("OK: editor/voice tematik, 3 ukuran, TypeScript, tab, simpan dua tahap, batal tanpa kehilangan")


asyncio.run(main())

"""Regresi galeri sumber: preview asli, lazy loading, pilihan, refresh dan Esc."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import MagicMock, patch

ROOT = Path(tempfile.mkdtemp(prefix="uji_sumber_visual_"))
REPO = Path(__file__).resolve().parents[1]
os.environ["BAGASAI_HOME"] = str(ROOT / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(ROOT)
os.environ.pop("NO_COLOR", None)
sys.path.insert(0, str(REPO / "src"))

from PIL import Image
from agent import desktop_capture as capture
from agent.desktop_capture import WindowTarget
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import SourcePickerScreen


def backend():
    target = WindowTarget(123, 20, "Jendela fixture")
    state = (capture._target, capture._generation, capture._last_info, capture._camera)
    with (patch.object(capture, "_window", return_value=target),
          patch.object(capture, "_print_window", return_value=Image.new("RGB", (1600, 900), "blue")) as render):
        thumbnail = capture.capture_thumbnail(target)
        assert thumbnail.size == (240, 135) and thumbnail.mode == "RGB"
        render.assert_called_once_with(123, timeout=.8, preview=True)
        thumbnail.close()
    assert state == (capture._target, capture._generation, capture._last_info, capture._camera)
    with (patch.object(capture, "_window", return_value=WindowTarget(123, 21, "Proses lain")),
          patch.object(capture, "_print_window") as render):
        try:
            capture.capture_thumbnail(target)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Window dengan PID berubah tidak boleh dipratinjau")
        render.assert_not_called()
    gate, started = threading.Event(), threading.Event()

    def render(hwnd):
        if hwnd == 123:
            started.set()
            gate.wait(2)
        return Image.new("RGB", (100, 60), "blue")

    with patch.object(capture, "_render_window", side_effect=render):
        try:
            try:
                capture._print_window(123, timeout=.02, preview=True)
            except RuntimeError:
                pass
            assert started.wait(1)
            # Job thumbnail yang tertunda tidak mengambil slot capture utama.
            image = capture._print_window(456, timeout=1)
            assert image.size == (100, 60)
            image.close()
        finally:
            gate.set()
    print("OK: thumbnail dibatasi ukurannya, sumber tidak berubah, identitas dicek, slot capture utama tetap tersedia")


async def wait(pilot, condition):
    for _ in range(120):
        await pilot.pause(.02)
        if condition():
            return
    raise AssertionError("Galeri tidak selesai memuat")


async def ui():
    agent = MagicMock()
    agent.model_spec = MagicMock(label="Model Uji", is_web=False)
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    windows = [WindowTarget(i + 1, 100 + i, f"Aplikasi {i + 1} · [halaman] yang sedang dibuka") for i in range(20)]
    calls = []

    def thumbnail(target):
        calls.append(target.hwnd if target else 0)
        return Image.new("RGB", (160, 90), "#528bff" if target else "#7cbe88")

    with patch("agent.interfaces.textual_widgets.source_picker.capture_thumbnail", side_effect=thumbnail):
        async with app.run_test(size=(120, 36)) as pilot:
            results = []
            screen = SourcePickerScreen(windows)
            app.push_screen(screen, results.append)
            await wait(pilot, lambda: sum(card.loaded for card in screen._cards) >= 3)
            before = len(calls)
            assert before < len(screen._cards), "Sumber di luar viewport tidak perlu dipotret"
            await pilot.pause(.15)
            assert len(calls) == before, "Thumbnail tidak boleh menjadi loop video"
            await pilot.click("#source-card-1")
            assert screen._source_index == 1
            await pilot.press("end")
            await wait(pilot, lambda: screen._cards[-1].loaded)
            assert screen._source_index == len(screen._cards) - 1
            assert 0 in calls and len(calls) < len(screen._cards), (calls, len(screen._cards))
            await pilot.press("enter")
            await pilot.pause()
            assert results == ["0"]

            calls.clear()
            screen = SourcePickerScreen(windows, current=0)
            app.push_screen(screen)
            await wait(pilot, lambda: screen._cards[-1].loaded)
            assert screen._source_index == len(screen._cards) - 1
            assert len(calls) < len(screen._cards), ("Sumber aktif di ujung daftar", calls)
            await pilot.press("up")
            assert screen._source_index == len(screen._cards) - 4
            await pilot.press("escape")

            for size, columns in (((120, 36), 3), ((80, 24), 2), ((40, 20), 1)):
                await pilot.resize_terminal(*size)
                results = []
                screen = SourcePickerScreen(windows[:4], current=2)
                app.push_screen(screen, results.append)
                await pilot.pause(.1)
                assert screen._source_index == 1 and screen._column_count == columns
                for selector in ("#source-picker-refresh", "#source-picker-close", "#source-picker-cancel", "#source-picker-confirm"):
                    button = screen.query_one(selector)
                    assert 0 <= button.region.x < button.region.right <= size[0], (size, selector, button.region)
                    assert button.region.bottom <= size[1]
                await pilot.press("right")
                await pilot.click("#source-picker-confirm")
                await pilot.pause()
                assert results == ["3"], (size, results)

            await pilot.resize_terminal(120, 36)
            screen = SourcePickerScreen(windows[:4], current=2)
            app.push_screen(screen)
            await wait(pilot, lambda: screen._cards[1].loaded)
            with patch("agent.interfaces.textual_widgets.source_picker.list_windows", return_value=windows[1:4]):
                await pilot.press("f5")
                await wait(pilot, lambda: len(screen._cards) == 4 and screen._cards[0].loaded)
                assert screen._sources[screen._source_index].hwnd == 2
            await pilot.press("ctrl+l")
            assert app.screen is screen
            await pilot.press("ctrl+c")
            assert app.screen is not screen and app.is_running

            # Hasil capture yang tiba setelah popup ditutup harus dibuang.
            gate = threading.Event()
            started = threading.Event()
            created = []

            def delayed(target):
                started.set()
                gate.wait(5)
                image = Image.new("RGB", (100, 60), "blue")
                created.append(image)
                return image

            with patch("agent.interfaces.textual_widgets.source_picker.capture_thumbnail", side_effect=delayed):
                results = []
                screen = SourcePickerScreen(windows[:1])
                app.push_screen(screen, results.append)
                await wait(pilot, started.is_set)
                await pilot.press("escape")
                gate.set()
                await wait(pilot, lambda: bool(created))
                await pilot.pause(.1)
                assert results == [None] and app.screen is not screen
                assert screen._preview_stop.is_set()
                try:
                    created[0].getpixel((0, 0))
                except ValueError:
                    pass
                else:
                    raise AssertionError("Thumbnail terlambat masih disimpan")
    print("OK: lazy loading, scroll, 3 ukuran, pilihan keyboard/mouse, refresh, Ctrl+C dan Esc tanpa hasil terlambat")


if __name__ == "__main__":
    backend()
    asyncio.run(ui())

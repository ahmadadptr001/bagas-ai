# -*- coding: utf-8 -*-
"""Uji pembaca gambar lokal dan jalur paste Textual tanpa upload/provider."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_UJI = Path(tempfile.mkdtemp(prefix="uji_image_local_"))
os.environ["BAGASAI_PROJECT_ROOT"] = str(ROOT_UJI)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image


def buat_gambar() -> Path:
    path = ROOT_UJI / "contoh lokal.png"
    img = Image.new("RGBA", (12, 8), (220, 40, 30, 255))
    img.putpixel((0, 0), (0, 0, 255, 0))
    img.save(path)
    return path


def cek_tool(path: Path) -> None:
    from agent.tools import REGISTRY, execute

    assert "read_image_local" in REGISTRY
    hasil = execute("read_image_local", {"path": str(path), "ocr": False})
    assert "PEMBACAAN GAMBAR LOKAL" in hasil
    assert "12×8 px" in hasil
    assert "PNG" in hasil and "Transparansi: ya" in hasil
    assert "OCR lokal: dilewati" in hasil
    # Bukti kontrak privasi: hasil tool hanya laporan teks, bukan marker
    # attachment maupun salinan data-URL/base64.
    assert "[GAMBAR]" not in hasil
    assert "[LAMPIR-MEDIA]" not in hasil
    assert "data:image" not in hasil
    assert "base64" not in hasil.lower()
    print("  tool read_image_local: Python lokal, tanpa attachment: OK")


async def cek_textual(path: Path) -> None:
    from agent import models
    from agent.interfaces.textual_app import BagasAIApp
    from agent.interfaces.textual_widgets import ChatBox
    from agent.interfaces.textual_widgets.chat_box import _SLASH_COMMANDS

    assert "/image" in {nama for nama, _, _ in _SLASH_COMMANDS}
    agent = MagicMock()
    agent.model_spec = models.ModelSpec(
        id="uji/vision", label="Vision Uji", multimodal=True)
    agent.supports_vision.return_value = True
    agent.run.return_value = "selesai"
    app = BagasAIApp(agent=agent)

    async with app.run_test(size=(100, 38)) as pilot:
        chatbox = app.query_one("#chatbox", ChatBox)
        chatbox.handle_paste(str(path))
        await pilot.pause(0.2)
        assert app._pending_gambar.get("path") == str(path.resolve())
        assert app.query_one("#chat-input").text == "[foto]"

        await pilot.press("enter")
        for _ in range(80):
            await pilot.pause(0.05)
            if agent.run.called and not app.is_turn_active:
                break
        assert agent.run.called
        prompt = agent.run.call_args.args[0]
        assert "[GAMBAR]" in prompt and str(path.resolve()) in prompt
        assert not app._pending_gambar

        with patch("agent.tools.image_local.read_image_local",
                   return_value="LAPORAN LOKAL UJI") as baca:
            app._handle_command(f'/image "{path}"')
            for _ in range(80):
                await pilot.pause(0.05)
                if not app._image_task_active:
                    break
            assert baca.call_args.args[0] == str(path)
            assert not app._image_task_active
    print("  /image + paste Textual menyimpan path dan mengirim marker: OK")


async def main() -> None:
    path = buat_gambar()
    cek_tool(path)
    await cek_textual(path)
    print("OK - pembacaan gambar lokal terimplementasi tanpa upload file")


asyncio.run(main())

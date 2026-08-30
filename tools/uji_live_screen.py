# -*- coding: utf-8 -*-
"""Uji /live dan /video tanpa mengambil screenshot layar sungguhan.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_live_screen.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="uji_live_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import models
from agent.core import Agent
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import StatusBar
from agent.tools import screen


async def tunggu(pilot, kondisi, maks=100, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


def cek_kapabilitas_model() -> None:
    ag = Agent.__new__(Agent)

    # Model API teks ditolak tanpa menyentuh connector.
    ag.model_spec = models.ModelSpec(
        id="uji/text", label="Teks", multimodal=False)
    assert ag.supports_vision() is False

    # Model API multimodal diterima langsung.
    ag.model_spec = models.ModelSpec(
        id="uji/vision", label="Vision", multimodal=True)
    assert ag.supports_vision() is True

    # Model web baru dianggap vision bila jalur unggah connector tersedia.
    ag.model_spec = models.ModelSpec(
        id="web/uji", label="Web", connector="uji", multimodal=True)
    conn = MagicMock()
    with patch("agent.connectors.get_connector", return_value=conn):
        conn.supports_attachments.return_value = False
        assert ag.supports_vision() is False
        conn.supports_attachments.return_value = True
        assert ag.supports_vision() is True
    print("  deteksi kapabilitas vision API + web: OK")


def cek_siklus_berkas() -> None:
    folder = Path(_TMP) / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    lama = folder / "live-current.png"
    unik = folder / "live-123.png"
    manual = folder / "sc-manual.png"
    for p in (lama, unik, manual):
        p.write_bytes(b"uji")
    screen.clear_live_capture()
    assert not lama.exists() and not unik.exists()
    assert manual.exists(), "cleanup /live tak boleh menghapus screenshot manual"

    tujuan = folder / "live-uji.png"
    with (patch("agent.tools.screen.time.time_ns", return_value=42),
          patch("agent.tools.screen.capture_screen",
                return_value=tujuan) as capture):
        assert screen.capture_live_screen() == tujuan
        assert capture.call_args.args == ("screenshots/live-42.png",)
    print("  nama unik + cleanup berkas live yang terisolasi: OK")


def cek_varian_web_dengan_lampiran() -> None:
    """Regresi UnboundLocalError `_status` pada giliran live pertama."""
    ag = Agent(model="web/glm")
    ag._web_ctx_sent = True
    ag._web_varian = "GLM-UJI"
    conn = MagicMock()
    conn.supports_attachments.return_value = True
    conn.send.return_value = "jawaban uji"
    conn.last_chat_id = ""
    status: list[str] = []
    with (patch("agent.connectors.playwright_available", return_value=True),
          patch("agent.connectors.get_connector", return_value=conn)):
        hasil = ag._run_connector(
            "lihat layar", on_status=status.append,
            attachments=["dummy.png"])
    assert conn.set_web_option.call_args.args == ("GLM-UJI",)
    assert any("memilih model" in s for s in status), status
    assert "jawaban uji" in hasil
    print("  varian web + attachment: urutan _status aman: OK")


async def cek_alur_ui() -> None:
    spec = models.ModelSpec(
        id="uji/vision", label="Model Vision", multimodal=True)
    ag = MagicMock()
    ag.model_spec = spec
    ag.supports_vision.return_value = True
    panggilan: list[tuple[str, list[str]]] = []

    def run(text, **kwargs):
        panggilan.append((text, kwargs.get("attachments") or []))
        return "jawaban"

    ag.run.side_effect = run
    fake_png = str(Path(_TMP) / "screenshots" / "live-current.png")

    app = BagasAIApp(agent=ag)
    with (patch("agent.tools.screen.screen_capture_available",
                return_value=(True, "")),
          patch("agent.tools.screen.clear_live_capture"),
          patch("agent.tools.screen.capture_live_screen",
                return_value=Path(fake_png)) as capture,
          patch("agent.tools.vision_local.ensure_vision_ready",
                return_value=(True, "probe vision berhasil")) as probe,
          patch("agent.tools.vision_local.describe_image",
                return_value="deskripsi Gemma uji") as describe):
        async with app.run_test(size=(100, 40)) as pilot:
            # Alias /video baru aktif sesudah probe gambar Gemma berhasil.
            app._handle_command("/video on")
            await tunggu(pilot, lambda: app._live_screen,
                         pesan="live harus aktif sesudah probe Gemma")
            assert app._live_screen is True
            statusbar = app.query_one("#statusbar", StatusBar)
            assert statusbar.live_screen is True
            assert statusbar.live_vision_state == "ready"
            assert "Gemma ✓" in statusbar.render().plain
            probe.assert_called_once_with(force_probe=True)

            # Pertanyaan biasa memicu tepat satu capture just-in-time dan
            # dianalisis Gemma lokal; gambar mentah tidak dikirim ke model utama.
            status_fase: list[str] = []
            agent_on_status_asli = app.agent_on_status

            def rekam_status(msg: str) -> None:
                status_fase.append(msg)
                agent_on_status_asli(msg)

            app.agent_on_status = rekam_status
            chatbox = app.query_one("#chatbox")
            chatbox.set_text("apa yang tampil di layar?")
            await pilot.press("enter")
            await tunggu(pilot, lambda: bool(panggilan),
                         pesan="agent.run harus terpanggil")
            await tunggu(pilot, lambda: not app.is_turn_active,
                         pesan="giliran harus selesai")
            assert capture.call_count == 1
            assert len(panggilan) == 1
            assert "apa yang tampil di layar?" in panggilan[0][0]
            assert "deskripsi Gemma uji" in panggilan[0][0]
            assert panggilan[0][1] == []
            describe.assert_called_once_with(Path(fake_png), strict=True)
            assert any("mengambil screenshot" in s for s in status_fase)
            assert any("Gemma 3 4B sedang menganalisis" in s
                       for s in status_fase)

            # Slash command tidak memicu capture baru.
            app._handle_command("/live status")
            assert capture.call_count == 1

            # /stream mengambil alih fungsi /live lama.
            semula = app._tui_mode
            app._handle_command("/stream")
            assert app._tui_mode is not semula

            app._handle_command("/live off")
            assert app._live_screen is False
            assert app.query_one("#statusbar", StatusBar).live_screen is False

            # Probe Gemma gagal -> mode tak boleh mengaku aktif.
            with patch("agent.tools.vision_local.ensure_vision_ready",
                       return_value=(False, "Gemma tak merespons")):
                app._handle_command("/live on")
                await tunggu(pilot, lambda: not app._live_starting,
                             pesan="probe gagal harus selesai")
            assert app._live_screen is False
            assert capture.call_count == 1
            assert statusbar.live_vision_state == "error"
    print("  /live + probe Gemma wajib + konteks lokal + /stream: OK")


async def main() -> None:
    cek_kapabilitas_model()
    cek_siklus_berkas()
    cek_varian_web_dengan_lampiran()
    await cek_alur_ui()
    print("OK - mode live screen terverifikasi tanpa membaca layar nyata")


asyncio.run(main())

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
os.environ["BAGASAI_HOME"] = str(Path(_TMP) / "home")
os.environ["BAGASAI_VIDEO_VISION"] = "gemma"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import models
from agent.core import Agent
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import StatusBar, SourcePickerScreen
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
    from PIL import Image
    with (patch("agent.tools.screen.time.time_ns", return_value=42),
          patch("agent.desktop_capture.capture",
                return_value=(Image.new("RGB", (1921, 80)), None)) as capture):
        tujuan = screen.capture_live_screen()
        assert tujuan.name == "live-42.png"
        with Image.open(tujuan) as image:
            assert image.size == (1921, 80), "OCR harus mempertahankan resolusi asli"
        capture.assert_called_once_with()
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


def cek_deteksi_fallback() -> None:
    from agent.tools.vision_local import response_needs_vision

    assert response_needs_vision(
        "Maaf, saya tidak dapat menganalisis gambar tersebut."
    ) is True
    assert response_needs_vision(
        "I can't see the image from here."
    ) is True
    assert response_needs_vision(
        "Saya tidak dapat melihat atau menganalisis visual tersebut."
    ) is True
    assert response_needs_vision(
        "Teks yang terlihat adalah menu Pengaturan."
    ) is False
    print("  deteksi penolakan model untuk fallback vision: OK")


async def cek_alur_ui() -> None:
    spec = models.ModelSpec(
        id="uji/vision", label="Model Vision", multimodal=True)
    ag = MagicMock()
    ag.model_spec = spec
    ag.supports_vision.return_value = True
    panggilan: list[tuple[str, list[str]]] = []

    def run(text, **kwargs):
        panggilan.append((text, kwargs.get("attachments") or []))
        return "jawaban berdasarkan pengamatan"

    ag.run.side_effect = run
    fake_png = str(Path(_TMP) / "screenshots" / "live-current.png")
    from PIL import Image
    from agent.screen_observer import ScreenText, Word
    from agent.screen_accessibility import AccessibleText
    from agent.ui.icons import footer_icon
    Path(fake_png).parent.mkdir(exist_ok=True)
    Image.new("RGB", (800, 500), "white").save(fake_png)

    app = BagasAIApp(agent=ag)
    with (patch("agent.tools.screen.screen_capture_available",
                return_value=(True, "")),
          patch("agent.tools.screen.clear_live_capture"),
          patch("agent.tools.screen.capture_live_screen",
                return_value=Path(fake_png)) as capture,
          patch("agent.screen_observer.ensure_live_vision_available",
                return_value=(True, "alat tersedia")) as probe,
          patch("agent.desktop_capture.list_windows", return_value=[]),
          patch("agent.interfaces.textual_widgets.source_picker.capture_thumbnail",
                side_effect=lambda _: Image.new("RGB", (160, 90), "blue")),
          patch("agent.screen_observer.read_accessible_text", return_value=AccessibleText()),
          patch("agent.screen_observer.read_screen_text",
                return_value=ScreenText((Word("laporan OCR uji", 20, 20, 120, 20, 95),))) as baca_ocr,
          patch("agent.tools.vision_local.describe_image",
                return_value="deskripsi Gemma uji") as describe):
        async with app.run_test(size=(100, 40)) as pilot:
            # Alias /video aktif sesudah backend fallback tersedia.
            app._handle_command("/video on")
            await tunggu(pilot, lambda: isinstance(app.screen, SourcePickerScreen))
            await pilot.press("enter")
            await tunggu(pilot, lambda: app._live_screen,
                         pesan="live harus aktif sesudah alat tersedia")
            assert app._live_screen is True
            statusbar = app.query_one("#statusbar", StatusBar)
            assert statusbar.live_screen is True
            assert statusbar.live_vision_state == "ready"
            assert "alat " + footer_icon("check") in statusbar.render().plain
            probe.assert_called_once_with()

            # Pertanyaan biasa memicu tepat satu capture just-in-time dan
            # OCR dijalankan lebih dulu; vision belum boleh dipakai bila jawaban
            # model utama sudah memadai.
            status_fase: list[str] = []
            agent_on_status_asli = app.agent_on_status

            def rekam_status(msg: str) -> None:
                status_fase.append(msg)
                agent_on_status_asli(msg)

            app.agent_on_status = rekam_status
            chatbox = app.query_one("#chatbox")
            chatbox.set_text("baca tulisan di layar")
            await pilot.press("enter")
            await tunggu(pilot, lambda: bool(panggilan),
                         pesan="agent.run harus terpanggil")
            await tunggu(pilot, lambda: not app.is_turn_active,
                         pesan="giliran harus selesai")
            assert capture.call_count == 1
            assert len(panggilan) == 1
            assert "baca tulisan di layar" in panggilan[0][0]
            assert "laporan OCR uji" in panggilan[0][0]
            assert panggilan[0][1] == []
            baca_ocr.assert_called_once_with(Path(fake_png), "eng", None)
            describe.assert_not_called()
            assert any("mengambil gambar" in s for s in status_fase)
            assert any("membaca teks" in s for s in status_fase)
            assert not any("sedang menganalisis" in s for s in status_fase)

            # Bila model utama menyatakan tak mampu menganalisis, barulah
            # vision lokal dipanggil untuk gambar kedua.
            chatbox.set_text("jelaskan gambar ini")
            await pilot.press("enter")
            await tunggu(pilot, lambda: len(panggilan) == 2,
                         pesan="giliran fallback harus mencapai model utama")
            await tunggu(pilot, lambda: not app.is_turn_active,
                         pesan="giliran fallback harus selesai")
            assert capture.call_count == 2
            assert baca_ocr.call_count == 1, "Gambar sama memakai cache OCR"
            describe.assert_called_once()
            args, kwargs = describe.call_args
            assert args[0] == Path(fake_png)
            assert "jelaskan gambar ini" in kwargs["prompt"]
            assert kwargs["strict"] is True
            assert "deskripsi Gemma uji" in panggilan[1][0], "Vision disiapkan sebelum jawaban agent"
            ag.replace_last_answer.assert_not_called()
            assert any("sedang menganalisis" in s for s in status_fase)

            # Slash command tidak memicu capture baru.
            app._handle_command("/live status")
            assert capture.call_count == 2

            # /stream mengambil alih fungsi /live lama.
            semula = app._tui_mode
            app._handle_command("/stream")
            assert app._tui_mode is not semula

            app._handle_command("/live off")
            assert app._live_screen is False
            assert app.query_one("#statusbar", StatusBar).live_screen is False

            # Backend fallback gagal -> mode tak boleh mengaku aktif.
            with patch("agent.screen_observer.ensure_live_vision_available",
                       return_value=(False, "Gemma tak merespons")):
                app._handle_command("/live on")
                await tunggu(pilot, lambda: not app._live_starting,
                             pesan="cek alat gagal harus selesai")
            assert app._live_screen is False
            assert capture.call_count == 2
            assert statusbar.live_vision_state == "error"
    print("  /live cache OCR + vision sebelum agent + satu jawaban akhir: OK")


async def cek_pilih_dari_mode_mati() -> None:
    """Pilihan sumber harus mengaktifkan capture sebelum pertanyaan berikutnya."""
    from PIL import Image
    from agent.desktop_capture import CaptureInfo, WindowTarget
    from agent.screen_accessibility import AccessibleText
    from agent.screen_observer import ScreenText, Word

    ag = MagicMock()
    ag.model_spec = models.ModelSpec(id="uji/teks", label="Model Teks")
    ag.memory.messages = []
    ag.run.return_value = "Tab aktif adalah Catatan Proyek."
    path = Path(_TMP) / "screenshots" / "live-source-selection.png"
    path.parent.mkdir(exist_ok=True)
    Image.new("RGB", (800, 500), "white").save(path)
    sources = [WindowTarget(123, 20, "Tab lama - Browser"), WindowTarget(456, 21, "Dokumen")]
    current = [0]
    info = CaptureInfo((1, 123, 20), "Catatan Proyek - Browser", hwnd=123)
    app = BagasAIApp(agent=ag)

    with (patch("agent.tools.screen.screen_capture_available", return_value=(True, "")),
          patch("agent.screen_observer.ensure_live_vision_available", return_value=(True, "siap")) as probe,
          patch("agent.tools.screen.clear_live_capture"),
          patch("agent.tools.screen.capture_live_screen", return_value=path) as capture,
          patch("agent.desktop_capture.list_windows", return_value=sources),
          patch("agent.desktop_capture.select_target", side_effect=lambda hwnd: current.__setitem__(0, hwnd)),
          patch("agent.desktop_capture.selected_target_id", side_effect=lambda: current[0]),
          patch("agent.desktop_capture.source_label", return_value=sources[0].title),
          patch("agent.interfaces.textual_widgets.source_picker.capture_thumbnail",
                side_effect=lambda _: Image.new("RGB", (160, 90), "blue")),
          patch("agent.screen_observer.last_capture_info", return_value=info),
          patch("agent.screen_observer.read_accessible_text", return_value=AccessibleText()),
          patch("agent.screen_observer.read_screen_text", return_value=ScreenText((
              Word("Catatan Proyek dan daftar pekerjaan", 20, 20, 400, 20, 95),))),
          patch("agent.tools.vision_local.describe_image") as vision):
        async with app.run_test(size=(100, 40)) as pilot:
            assert not app._live_screen
            app._handle_command("/video pilih")
            await tunggu(pilot, lambda: isinstance(app.screen, SourcePickerScreen))
            assert not app._live_screen, "Menampilkan picker belum memberi akses capture"
            await pilot.press("escape")
            assert not app._live_screen and not app._live_starting
            capture.assert_not_called()

            for command in ("/video pilih", "/live jendela"):
                app._handle_command(command)
                await tunggu(pilot, lambda: isinstance(app.screen, SourcePickerScreen))
                await pilot.press("enter")
                await tunggu(pilot, lambda: app._live_screen)
                assert current[0] == 123
                statusbar = app.query_one("#statusbar", StatusBar)
                assert statusbar.live_screen and statusbar.live_vision_state == "ready"
                chatbox = app.query_one("#chatbox")
                chatbox.set_text("tab apa yang sedang terbuka")
                count = ag.run.call_count
                await pilot.press("enter")
                await tunggu(pilot, lambda: ag.run.call_count > count and not app.is_turn_active)
                question = ag.run.call_args.args[0]
                assert "[PENGAMATAN LAYAR LOKAL]" in question, question
                assert info.label in question, "Judul harus berasal dari capture terbaru"
                assert "Catatan Proyek dan daftar pekerjaan" in question
                assert ag.run.call_args.kwargs["attachments"] == []

                # Mengganti sumber aktif tidak perlu memeriksa backend ulang.
                probes = probe.call_count
                app._handle_command("/video pilih")
                await tunggu(pilot, lambda: isinstance(app.screen, SourcePickerScreen))
                await pilot.press("right", "enter")
                assert current[0] == 456 and app._live_screen
                assert probe.call_count == probes
                app._handle_command("/video off")
                assert not app._live_screen

            assert capture.call_count == 2
            vision.assert_not_called()
            with patch("agent.screen_observer.ensure_live_vision_available", return_value=(False, "backend uji gagal")):
                app._handle_command("/video pilih")
                await tunggu(pilot, lambda: not app._live_starting)
                assert not app._live_screen and not isinstance(app.screen, SourcePickerScreen)
                assert statusbar.live_vision_state == "error"
    print("  /video pilih dari mati: cancel aman, aktif sesudah konfirmasi, judul/OCR diteruskan ke model teks: OK")


async def main() -> None:
    cek_kapabilitas_model()
    cek_siklus_berkas()
    cek_varian_web_dengan_lampiran()
    cek_deteksi_fallback()
    await cek_alur_ui()
    await cek_pilih_dari_mode_mati()
    print("OK - mode live screen terverifikasi tanpa membaca layar nyata")


asyncio.run(main())

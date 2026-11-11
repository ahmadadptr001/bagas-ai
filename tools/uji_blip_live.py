"""Regresi BLIP-first: routing, cache, deadline, pembatalan dan pelepasan RAM."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix="uji_blip_", ignore_cleanup_errors=True)
ROOT = Path(TEMP.name)
REPO = Path(__file__).resolve().parents[1]
os.environ["BAGASAI_HOME"] = str(ROOT / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(ROOT)
os.environ["BAGASAI_VIDEO_VISION"] = "blip"
sys.path.insert(0, str(REPO / "src"))

from PIL import Image, ImageDraw
from agent import blip_local as blip
from agent.desktop_capture import CaptureInfo
from agent.screen_accessibility import AccessibleText
from agent.screen_observer import ScreenObserver, ScreenText, Word, ensure_live_vision_available
from agent.tools.vision_local import VisionLocalError


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.observer = ScreenObserver()
        self.path = ROOT / "frame.png"
        self.image = Image.new("RGB", (1000, 700), "white")
        self.image.save(self.path)
        self.info = CaptureInfo((1, 12, 34), "Tab proyek - Browser", 12)
        self.text = ScreenText((Word("Judul halaman proyek", 20, 20, 240, 24, 95),))
        self.access = patch("agent.screen_observer.read_accessible_text", return_value=AccessibleText())
        self.access.start()
        self.addCleanup(self.access.stop)

    def prepare(self, question="Jelaskan gambar ini"):
        return self.observer.prepare(question, self.path, info=self.info)

    def test_blip_before_ocr_and_no_gemma_for_general_caption(self):
        order = []
        with (patch.object(blip.captioner, "caption", side_effect=lambda *a, **k: order.append("blip") or "a person reading a book") as caption,
              patch("agent.screen_observer.read_screen_text", side_effect=lambda *a: order.append("ocr") or self.text),
              patch("agent.tools.vision_local.describe_image") as detail):
            result = self.prepare()
        self.assertEqual(order, ["blip", "ocr"])
        self.assertIn("a person reading a book", result)
        self.assertIn("Judul halaman proyek", result)
        self.assertIn("Tab proyek - Browser", result)
        detail.assert_not_called()

    def test_caption_cache_reused_for_different_questions_and_invalidated_on_change(self):
        with (patch.object(blip.captioner, "caption", return_value="a person reading a book") as caption,
              patch("agent.screen_observer.read_screen_text", return_value=self.text),
              patch("agent.tools.vision_local.describe_image") as detail):
            self.prepare()
            self.prepare("Apa yang terlihat pada gambar?")
            self.assertEqual(caption.call_count, 1)
            ImageDraw.Draw(self.image).rectangle((40, 40, 80, 80), fill="blue")
            self.image.save(self.path)
            self.prepare()
            self.info = CaptureInfo((2, 14, 35), "Sumber lain", 14)
            self.prepare()
            self.assertEqual(caption.call_count, 3)
            self.observer.reset()
            self.prepare()
            self.assertEqual(caption.call_count, 4)
        detail.assert_not_called()

    def test_text_question_skips_both_visual_models(self):
        with (patch("agent.screen_observer.read_accessible_text", return_value=AccessibleText("Tab catatan proyek sedang terbuka", True)),
              patch.object(blip.captioner, "caption") as caption,
              patch("agent.screen_observer.read_screen_text") as ocr,
              patch("agent.tools.vision_local.describe_image") as detail):
            result = self.prepare("tab apa yang sedang terbuka")
        self.assertIn("Tab catatan proyek", result)
        caption.assert_not_called()
        ocr.assert_not_called()
        detail.assert_not_called()

    def test_detail_uses_gemma_after_blip_and_reuses_both_caches(self):
        order = []
        with (patch.object(blip.captioner, "caption", side_effect=lambda *a, **k: order.append("blip") or "a page with a button"),
              patch("agent.screen_observer.read_screen_text", return_value=self.text),
              patch("agent.tools.vision_local.describe_image", side_effect=lambda *a, **k: order.append("gemma") or "Tombol berwarna biru") as detail):
            result = self.prepare("Apa warna tombol?")
            self.prepare("Apa warna tombol?")
            self.assertEqual(order, ["blip", "gemma"])
            self.assertIn("Tombol berwarna biru", result)
            self.prepare("Apa posisi tombol?")
            self.assertEqual(detail.call_count, 2)

    def test_blip_failure_falls_back_and_cancel_does_not(self):
        with (patch.object(blip.captioner, "caption", side_effect=blip.BlipError("timeout")),
              patch("agent.screen_observer.read_screen_text", return_value=self.text),
              patch("agent.tools.vision_local.describe_image", return_value="Pengamatan alternatif") as detail):
            self.assertIn("Pengamatan alternatif", self.prepare())
            detail.assert_called_once()
        with (patch.object(blip.captioner, "caption", side_effect=InterruptedError("dibatalkan")),
              patch("agent.screen_observer.read_screen_text") as ocr,
              patch("agent.tools.vision_local.describe_image") as detail):
            with self.assertRaises(InterruptedError):
                self.prepare()
            ocr.assert_not_called()
            detail.assert_not_called()

    def test_reset_rejects_late_caption(self):
        def reset(*args, **kwargs):
            self.observer.reset()
            return "Hasil basi"
        with (patch.object(blip.captioner, "caption", side_effect=reset),
              patch("agent.screen_observer.read_screen_text") as ocr):
            with self.assertRaises(InterruptedError):
                self.prepare()
            self.assertFalse(self.observer._captions)
            ocr.assert_not_called()

    def test_missing_detail_is_not_presented_as_verified(self):
        with (patch.object(blip.captioner, "caption", return_value="a page with a button"),
              patch("agent.screen_observer.read_screen_text", return_value=self.text),
              patch("agent.tools.vision_local.describe_image", side_effect=VisionLocalError("tidak tersedia"))):
            result = self.prepare("Apa warna tombol?")
        self.assertIn("Detail visual belum terverifikasi", result)
        self.assertFalse(self.observer._vision)

    def test_blip_ready_does_not_start_ollama_and_failure_can_use_gemma(self):
        with (patch.object(blip, "ensure_blip_available", return_value=(True, "BLIP siap")),
              patch("agent.tools.vision_local.ensure_vision_available") as gemma):
            self.assertEqual(ensure_live_vision_available(), (True, "BLIP siap"))
            gemma.assert_not_called()
        with (patch.object(blip, "ensure_blip_available", return_value=(False, "belum tersedia")),
              patch("agent.tools.vision_local.ensure_vision_available", return_value=(True, "Gemma siap"))):
            self.assertEqual(ensure_live_vision_available(), (True, "Gemma siap"))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.captioner = blip.BlipCaptioner()
        self.addCleanup(self.captioner.release)
        self.folder = patch.object(blip, "model_directory", return_value=ROOT)
        self.folder.start()
        self.addCleanup(self.folder.stop)
        self.env = patch.dict(os.environ, {"BAGASAI_BLIP_COLD_TIMEOUT": ".4", "BAGASAI_BLIP_TIMEOUT": ".3", "BAGASAI_BLIP_KEEP_ALIVE": "60"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def command(self, body):
        return [sys.executable, "-u", "-c", "import sys,json,time\nfor line in sys.stdin:\n " + body]

    def test_same_worker_reused_then_released(self):
        args = self.command("print(json.dumps({'caption':'a book'}),flush=True)")
        with patch.object(blip, "_launch_args", return_value=args):
            self.assertEqual(self.captioner.caption(ROOT / "fake.png"), "a book")
            process = self.captioner._process
            self.captioner.caption(ROOT / "other.png")
            self.assertIs(self.captioner._process, process)
            self.captioner.release()
            self.assertIsNotNone(process.poll())
            self.assertIsNone(self.captioner._process)

    def test_deadline_terminates_owned_worker(self):
        args = self.command("time.sleep(60)")
        with patch.object(blip, "_launch_args", return_value=args):
            start = time.monotonic()
            with self.assertRaisesRegex(blip.BlipError, "waktu"):
                self.captioner.caption(ROOT / "fake.png")
            self.assertLess(time.monotonic() - start, 2)
            self.assertIsNone(self.captioner._process)

    def test_cancel_stops_waiting_without_fallback(self):
        cancelled = threading.Event()
        timer = threading.Timer(.1, cancelled.set)
        args = self.command("time.sleep(60)")
        with patch.object(blip, "_launch_args", return_value=args):
            timer.start()
            try:
                with self.assertRaises(InterruptedError):
                    self.captioner.caption(ROOT / "fake.png", cancel_event=cancelled)
                self.assertIsNone(self.captioner._process)
            finally:
                timer.cancel()

    def test_idle_worker_releases_memory(self):
        args = self.command("print(json.dumps({'caption':'a book'}),flush=True)")
        with (patch.object(blip, "_launch_args", return_value=args),
              patch.dict(os.environ, {"BAGASAI_BLIP_KEEP_ALIVE": ".05"})):
            self.captioner.caption(ROOT / "fake.png")
            process = self.captioner._process
            deadline = time.monotonic() + 2
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNotNone(process.poll())

    def test_release_during_inference_cancels_without_blocking_ui(self):
        args = self.command("time.sleep(60)")
        errors = []
        def run():
            try:
                self.captioner.caption(ROOT / "fake.png")
            except BaseException as exc:
                errors.append(exc)
        with patch.object(blip, "_launch_args", return_value=args):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 2
            while self.captioner._process is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNotNone(self.captioner._process)
            start = time.monotonic()
            self.captioner.release()
            self.assertLess(time.monotonic() - start, .1)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors and isinstance(errors[0], InterruptedError), errors)
            self.assertIsNone(self.captioner._process)


class ModelCacheTests(unittest.TestCase):
    def test_cache_is_offline_verified_and_corruption_is_rejected(self):
        folder = ROOT / "model-cache"
        folder.mkdir(exist_ok=True)
        path = folder / "model.onnx"
        data = b"cached weights"
        path.write_bytes(data)
        files = {path.name: (len(data), hashlib.sha256(data).hexdigest())}
        with (patch.object(blip, "MODEL_FILES", files),
              patch("huggingface_hub.snapshot_download", return_value=str(folder)) as download):
            self.assertEqual(blip.model_directory(), folder)
            self.assertTrue(download.call_args.kwargs["local_files_only"])
            self.assertEqual(download.call_args.kwargs["revision"], blip.MODEL_REVISION)
            # Ukuran sama tidak cukup; checksum harus diperiksa.
            path.write_bytes(b"x" * len(data))
            with self.assertRaisesRegex(blip.BlipError, "diverifikasi"):
                blip.model_directory()


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Regresi pengamatan video: cache, perubahan kecil, sumber, pembatalan, dan RAM."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

temporary = tempfile.TemporaryDirectory(prefix="uji_video_", ignore_cleanup_errors=True)
ROOT = Path(temporary.name)
REPO = Path(__file__).resolve().parents[1]
os.environ["BAGASAI_HOME"] = str(ROOT / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(ROOT)
os.environ["BAGASAI_VIDEO_VISION"] = "gemma"
sys.path.insert(0, str(REPO / "src"))

from PIL import Image, ImageDraw
from agent import desktop_capture as capture
from agent.desktop_capture import CaptureInfo, WindowTarget
from agent.screen_accessibility import AccessibleText
from agent.screen_observer import ScreenObserver, ScreenText, Word, focus_region, read_screen_text
from agent.tools import vision_local


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.observer = ScreenObserver()
        self.path = ROOT / "frame.png"
        self.image = Image.new("RGB", (1000, 700), "white")
        self.image.save(self.path)
        self.info = CaptureInfo((1, 12, 34), "Window uji", 0)
        self.text = ScreenText((Word("Header tetap", 20, 20, 200, 24, 95),
                                Word("Lama", 20, 420, 80, 24, 90)))
        self.access = patch("agent.screen_observer.read_accessible_text", return_value=AccessibleText())
        self.access.start()
        self.addCleanup(self.access.stop)

    def prepare(self, text="Baca semua tulisan", **kwargs):
        return self.observer.prepare(text, self.path, info=self.info, **kwargs)

    def test_same_pixels_reuse_ocr_across_file_names(self):
        with (patch("agent.screen_observer.read_screen_text", return_value=self.text) as ocr,
              patch("agent.tools.vision_local.describe_image") as vision):
            first = self.prepare()
            other = ROOT / "nama-lain.png"
            self.image.save(other)
            second = self.observer.prepare("Baca header", other, info=self.info)
        self.assertEqual(ocr.call_count, 1)
        vision.assert_not_called()
        self.assertIn("Header tetap", first)
        self.assertIn("Baca header", second)

    def test_small_change_only_ocr_region_and_remove_stale_words(self):
        fresh = ScreenText((Word("Baru", 20, 420, 80, 24, 94),))
        with (patch("agent.screen_observer.read_screen_text", side_effect=[self.text, fresh]) as ocr,
              patch("agent.tools.vision_local.describe_image") as vision):
            self.prepare()
            ImageDraw.Draw(self.image).rectangle((20, 420, 100, 444), fill="black")
            self.image.save(self.path)
            result = self.prepare()
        region = ocr.call_args.args[2]
        self.assertIsNotNone(region)
        self.assertLess((region[2]-region[0])*(region[3]-region[1]), 1000*700*.35)
        self.assertIn("Header tetap", result)
        self.assertIn("Baru", result)
        self.assertNotIn("Lama", result)
        vision.assert_not_called()

    def test_large_change_and_new_source_invalidate_previous_ocr(self):
        with (patch("agent.screen_observer.read_screen_text", return_value=self.text) as ocr,
              patch("agent.tools.vision_local.describe_image")):
            self.prepare()
            self.image.paste("black", (0, 0, 1000, 700))
            self.image.save(self.path)
            self.prepare()
            self.assertIsNone(ocr.call_args.args[2])
            self.info = CaptureInfo((2, 15, 34), "Window berbeda")
            self.prepare()
            self.assertEqual(ocr.call_count, 3)

    def test_uia_document_skips_ocr_and_vision(self):
        with (patch("agent.screen_observer.read_accessible_text", return_value=AccessibleText("Teks halaman aplikasi lengkap", True)),
              patch("agent.screen_observer.read_screen_text") as ocr,
              patch("agent.tools.vision_local.describe_image") as vision):
            result = self.prepare()
        self.assertIn("Teks halaman aplikasi lengkap", result)
        ocr.assert_not_called()
        vision.assert_not_called()

    def test_visual_cache_includes_question_and_focus_crop(self):
        sizes = []
        def describe(path, **kwargs):
            with Image.open(path) as image:
                sizes.append(image.size)
            self.assertEqual(kwargs["max_edge"], 1280)
            return "Fakta visual yang teramati"
        with (patch("agent.screen_observer.read_screen_text", return_value=self.text),
              patch("agent.tools.vision_local.describe_image", side_effect=describe) as vision):
            self.prepare("Apa warna di kiri atas?")
            self.prepare("Apa warna di kiri atas?")
            self.prepare("Apa warna di kanan bawah?")
        self.assertEqual(vision.call_count, 2)
        self.assertEqual(sizes, [(500, 350), (500, 350)])
        self.assertIsNone(focus_region("Bandingkan kiri dan kanan", (1000, 700)))

    def test_cancel_and_reset_during_observation_do_not_start_agent_work(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("agent.screen_observer.read_screen_text") as ocr:
            with self.assertRaises(InterruptedError):
                self.prepare(cancel_event=cancelled)
        ocr.assert_not_called()
        def reset(*args):
            self.observer.reset()
            return self.text
        with (patch("agent.screen_observer.read_screen_text", side_effect=reset),
              patch("agent.tools.vision_local.describe_image") as vision):
            with self.assertRaises(InterruptedError):
                self.prepare()
            self.assertFalse(self.observer._ocr)
        vision.assert_not_called()

    def test_ocr_limits_threads_and_parses_tsv_coordinates(self):
        tsv = "level\tleft\ttop\twidth\theight\tconf\ttext\n5\t4\t6\t20\t10\t92\tTeks\n"
        with (patch("agent.tools.image_local._cari_tesseract", return_value="tesseract"),
              patch("agent.screen_observer.subprocess.run", return_value=MagicMock(returncode=0, stdout=tsv)) as process):
            result = read_screen_text(self.path, region=(100, 200, 500, 600))
        self.assertEqual(result.words[0].box, (104, 206, 124, 216))
        self.assertEqual(process.call_args.kwargs["env"]["OMP_THREAD_LIMIT"], "2")
        self.assertEqual(process.call_args.kwargs["timeout"], 15)

    def test_model_idle_timeout_and_detail_resolution(self):
        result = MagicMock(status_code=200)
        result.json.return_value = {"response": "observasi"}
        with patch.dict(os.environ, {"BAGASAI_VISION_KEEP_ALIVE": "2m"}):
            with patch("requests.post", return_value=result) as post:
                vision_local._inferensi("base64", "uji", timeout=1, num_predict=2)
        self.assertEqual(post.call_args.kwargs["json"]["keep_alive"], "2m")
        image = Image.new("RGB", (2000, 1000))
        large = ROOT / "large.png"
        image.save(large)
        payload = vision_local._gambar_jpeg(large, max_edge=1280)
        with Image.open(io.BytesIO(base64.b64decode(payload))) as decoded:
            self.assertEqual(decoded.size, (1280, 640))

    @unittest.skipUnless(sys.platform == "win32", "Capture Windows")
    def test_capture_uses_printwindow_for_covered_target_and_does_not_start_video(self):
        api = MagicMock()
        api.GetForegroundWindow.return_value = 999
        api.GetWindowRect.return_value = True
        api.GetSystemMetrics.return_value = 1920
        target = WindowTarget(123, 20, "Target")
        with (patch.object(capture, "_target", target), patch.object(capture, "_user32", return_value=api),
              patch.object(capture, "_window", return_value=target), patch.object(capture, "_camera", None),
              patch.object(capture, "_dxgi") as dxgi,
              patch.object(capture, "_print_window", return_value=self.image) as grab):
            image, info = capture.capture()
        grab.assert_called_once_with(123)
        dxgi.assert_not_called()
        self.assertEqual(info.backend, "PrintWindow")
        self.assertEqual(image.size, (1000, 700))

    @unittest.skipUnless(sys.platform == "win32", "DXGI Windows")
    def test_dxgi_uses_minimum_buffer_and_no_capture_loop(self):
        import numpy as np
        camera = MagicMock()
        camera.grab.return_value = np.zeros((80, 100, 3), dtype=np.uint8)
        with patch.object(capture, "_camera", None), patch("dxcam.create", return_value=camera) as create:
            image = capture._dxgi(None)
        self.assertEqual(create.call_args.kwargs["max_buffer_len"], 2)
        self.assertEqual(create.call_args.kwargs["processor_backend"], "numpy")
        camera.start.assert_not_called()
        self.assertEqual(image.size, (100, 80))

    def test_release_during_capture_frees_camera_before_next_request(self):
        camera = MagicMock()

        def frame():
            capture.release_capture()
            return self.image, self.info

        with (patch.object(capture, "_camera", camera),
              patch.object(capture, "_release_pending", False),
              patch.object(capture, "_capture_frame", side_effect=frame)):
            capture.capture()
            self.assertIsNone(capture._camera)
        camera.release.assert_called_once_with()

    def test_printwindow_timeout_keeps_only_one_worker(self):
        gate = threading.Event()
        finished = threading.Event()

        def blocked(_):
            try:
                gate.wait(2)
                return self.image
            finally:
                finished.set()

        with patch.object(capture, "_render_window", side_effect=blocked) as render:
            try:
                with self.assertRaisesRegex(RuntimeError, "tidak merespons"):
                    capture._print_window(123, timeout=.02)
                with self.assertRaisesRegex(RuntimeError, "sebelumnya belum selesai"):
                    capture._print_window(123, timeout=.02)
                self.assertEqual(render.call_count, 1)
            finally:
                gate.set()
                self.assertTrue(finished.wait(2))

    def test_accessibility_timeout_keeps_only_one_worker(self):
        import agent.screen_accessibility as access
        gate = threading.Event()
        finished = threading.Event()

        def blocked(_):
            try:
                gate.wait(2)
                return AccessibleText("Teks fixture", True)
            finally:
                finished.set()

        with patch.object(access, "_read", side_effect=blocked) as read:
            try:
                self.assertEqual(access.read_accessible_text(123, timeout=.02).text, "")
                self.assertEqual(access.read_accessible_text(123, timeout=.02).text, "")
                self.assertEqual(read.call_count, 1)
            finally:
                gate.set()
                self.assertTrue(finished.wait(2))


if __name__ == "__main__":
    unittest.main(verbosity=2)

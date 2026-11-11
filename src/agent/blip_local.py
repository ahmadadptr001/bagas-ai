"""BLIP-base INT8 lokal, worker tunggal dengan batas waktu dan cache model."""
from __future__ import annotations

import atexit
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

MODEL_REPO = "agaretg/photo-app-blip-onnx"
MODEL_REVISION = "142db93b29ea92976863eed3572d176af9f32d3b"
# Ekspor INT8 BLIP-base; revisi dan checksum tetap, tanpa remote Python code.
MODEL_FILES = {
    "blip_vision_encoder_int8.onnx": (88180764, "95f3226005f118dc7846fee637dbe303bf55d5093046dc514658712192bbe9aa"),
    "blip_text_decoder_int8.onnx": (162854357, "5ead679aa8eeb82564ae65c718372b7fce5608f3d7a48ea419d844c322bf84f0"),
    "tokenizer.json": (711396, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
}
_verified = set()


class BlipError(RuntimeError):
    pass


def model_directory(*, download: bool = False) -> Path:
    from huggingface_hub import snapshot_download
    folder = Path(snapshot_download(MODEL_REPO, revision=MODEL_REVISION,
        allow_patterns=list(MODEL_FILES), local_files_only=not download, max_workers=2))
    for name, (length, digest) in MODEL_FILES.items():
        path = folder / name
        stat = path.stat()
        if stat.st_size != length:
            raise BlipError("Berkas model BLIP belum lengkap")
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        if key not in _verified:
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest(stream)
            if actual != digest:
                raise BlipError("Berkas model BLIP gagal diverifikasi")
            _verified.add(key)
    return folder


def _digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(block)
    return result.hexdigest()


def _launch_args():
    # Jalankan file langsung agar worker tidak mengimpor seluruh agent/core.
    return [sys.executable, "-u", str(Path(__file__).with_name("blip_worker.py"))]


def ensure_blip_available(*, download: bool = True) -> tuple[bool, str]:
    for module in ("onnxruntime", "numpy", "tokenizers", "huggingface_hub"):
        if importlib.util.find_spec(module) is None:
            return False, f"Komponen BLIP belum terpasang: {module}"
    try:
        model_directory()
        return True, "BLIP INT8 tersedia"
    except Exception:
        if not download:
            return False, "Model BLIP belum tersedia di cache lokal"
    try:
        result = subprocess.run(_launch_args() + ["--prepare-model"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        if result.returncode:
            return False, "Model BLIP belum dapat disiapkan"
        model_directory()
        return True, "BLIP INT8 tersedia"
    except Exception:
        return False, "Model BLIP belum dapat disiapkan"


def _seconds(name, default, low, high):
    try:
        return max(low, min(float(os.environ.get(name, default)), high))
    except (ValueError, TypeError):
        return default


class BlipCaptioner:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._process = None
        self._answers = None
        self._idle = None
        self._epoch = 0

    def _stop(self):
        process, self._process = self._process, None
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def release(self):
        self._cancel.set()
        if self._lock.acquire(blocking=False):
            try:
                self._stop()
            finally:
                self._lock.release()

    def _expire(self, epoch):
        if self._lock.acquire(blocking=False):
            try:
                if epoch == self._epoch:
                    self._stop()
            finally:
                self._lock.release()

    def caption(self, path: Path, cancel_event=None) -> str:
        # Tidak ada inferensi bersamaan atau antrean frame di latar.
        with self._lock:
            self._cancel.clear()
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Pengamatan layar dibatalkan")
            folder = model_directory()
            if self._cancel.is_set() or (cancel_event is not None and cancel_event.is_set()):
                raise InterruptedError("Pengamatan layar dibatalkan")
            self._epoch += 1
            if self._idle is not None:
                self._idle.cancel()
            cold = self._process is None or self._process.poll() is not None
            if cold:
                self._stop()
                answers = self._answers = queue.Queue()
                self._process = subprocess.Popen(_launch_args(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    encoding="utf-8", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
                output = self._process.stdout

                def read():
                    try:
                        for line in output:
                            answers.put(json.loads(line))
                    except (ValueError, OSError):
                        pass
                    finally:
                        answers.put({"error": "Worker BLIP berhenti"})

                threading.Thread(target=read, daemon=True, name="bagasai-blip-output").start()
            budget = _seconds("BAGASAI_BLIP_TIMEOUT", 4, .2, 20)
            deadline = time.monotonic() + (_seconds("BAGASAI_BLIP_COLD_TIMEOUT", 8, .2, 30) if cold else budget)
            try:
                self._process.stdin.write(json.dumps({"folder": str(folder), "path": str(path.resolve())}) + "\n")
                self._process.stdin.flush()
                while True:
                    if self._cancel.is_set() or (cancel_event is not None and cancel_event.is_set()):
                        raise InterruptedError("Pengamatan layar dibatalkan")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BlipError("Batas waktu BLIP tercapai")
                    try:
                        answer = self._answers.get(timeout=min(.05, remaining))
                        break
                    except queue.Empty:
                        continue
                if answer.get("error") or not str(answer.get("caption", "")).strip():
                    raise BlipError("BLIP belum menghasilkan ringkasan yang dapat dipakai")
                keep = _seconds("BAGASAI_BLIP_KEEP_ALIVE", 60, 0, 300)
                self._idle = threading.Timer(keep, self._expire, args=(self._epoch,))
                self._idle.daemon = True
                self._idle.start()
                return str(answer["caption"]).strip()[:600]
            except BaseException:
                self._stop()
                raise


captioner = BlipCaptioner()
atexit.register(captioner.release)


if __name__ == "__main__":
    ready, why = ensure_blip_available()
    print(why, flush=True)
    raise SystemExit(0 if ready else 1)

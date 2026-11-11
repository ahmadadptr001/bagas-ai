"""Referensi speaker WASAPI untuk AEC; audio hanya disimpan sementara di RAM."""
from __future__ import annotations

from collections import deque
import sys
import threading
import time


def matches_render(audio, history, threshold: float = 0.90) -> bool:
    """Kenali sisa speaker yang masih menyerupai sinyal render, dengan toleransi delay."""
    import numpy as np
    near = np.asarray(audio, np.float32)[-1024:]
    far = np.asarray(history, np.float32)[-16000:]
    n = len(near)
    if n < 320 or len(far) < n:
        return False
    near = near - near.mean()
    energy = float(np.dot(near, near))
    if energy < n * 25:
        return False
    sums = np.concatenate(([0.0], np.cumsum(far, dtype=np.float64)))
    squares = np.concatenate(([0.0], np.cumsum(far.astype(np.float64) ** 2)))
    window_energy = squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n
    correlation = np.abs(np.correlate(far, near, mode="valid"))
    denominator = np.sqrt(np.maximum(window_energy, 1.0) * energy)
    return bool(np.max(correlation / denominator) >= threshold)


class RenderReference:
    """Loopback keluaran default, terpisah dari input mikrofon pengguna."""

    def __init__(self):
        self.active = False
        self.error = ""
        self._pa = self._stream = self._resampler = None
        self._frames = deque(maxlen=60)
        self._history = deque(maxlen=100)
        self._lock = threading.Lock()
        self._pending = None
        self._last_at = 0.0

    def start(self) -> bool:
        if sys.platform != "win32":
            self.error = "referensi speaker WASAPI hanya tersedia di Windows"
            return False
        try:
            import numpy as np
            import pyaudiowpatch as pa
            import soxr
            self._np = np
            self._pending = np.empty(0, np.int16)
            self._pa = pa.PyAudio()
            device = self._pa.get_default_wasapi_loopback()
            channels = int(device["maxInputChannels"])
            rate = int(device["defaultSampleRate"])
            if channels < 1:
                raise RuntimeError("keluaran sistem tidak memiliki kanal loopback")
            self._resampler = soxr.ResampleStream(rate, 16000, 1, dtype="int16", quality="HQ")

            def callback(data, _count, _info, _status):
                try:
                    x = np.frombuffer(data, np.int16).reshape(-1, channels)
                    mono = x.astype(np.float32).mean(axis=1).astype(np.int16)
                    samples = self._resampler.resample_chunk(mono)
                    self._pending = np.concatenate((self._pending, samples))
                    frames = []
                    while len(self._pending) >= 160:
                        frames.append(self._pending[:160].copy())
                        self._pending = self._pending[160:]
                    with self._lock:
                        self._frames.extend(frames)
                        self._history.extend(frames)
                        self._last_at = time.monotonic()
                    return None, pa.paContinue
                except Exception:
                    self.active = False
                    self.error = "aliran referensi speaker terputus"
                    return None, pa.paAbort

            self.active = True
            self._stream = self._pa.open(
                format=pa.paInt16, channels=channels, rate=rate,
                input=True, input_device_index=int(device["index"]),
                frames_per_buffer=max(1, rate // 100), stream_callback=callback,
            )
            if not self.active:
                self.close()
                return False
            return True
        except Exception as exc:
            self.error = f"referensi speaker tidak tersedia ({type(exc).__name__})"
            self.close()
            return False

    def read_frames(self, count: int):
        """10 ms per frame, urut waktu; referensi basi tidak boleh dipakai."""
        np = self._np
        with self._lock:
            if time.monotonic() - self._last_at > 0.25:
                self._frames.clear()
            # Sisakan sedikit jitter buffer, tanpa menumpuk delay sepanjang sesi.
            while len(self._frames) > count + 3:
                self._frames.popleft()
            return [self._frames.popleft() if self._frames else np.zeros(160, np.int16)
                    for _ in range(count)]

    def history(self):
        with self._lock:
            if not self._history or time.monotonic() - self._last_at > 0.25:
                return self._np.empty(0, self._np.int16)
            return self._np.concatenate(list(self._history))

    def close(self):
        self.active = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        with self._lock:
            self._frames.clear()
            self._history.clear()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_args):
        self.close()

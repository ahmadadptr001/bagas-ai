"""Uji C++ WebRTC AEC dan sumber input, tanpa merekam mikrofon pengguna."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import config, dengar
from agent.echo_reference import matches_render


def check_input():
    class Devices:
        default = SimpleNamespace(device=(0, 0))
        def query_devices(self):
            return [
                {"name": "Stereo Mix", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000},
                {"name": "CABLE Output", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000},
                {"name": "Microphone Array", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000},
            ]
        def query_hostapis(self):
            return [{"name": "Windows WASAPI", "default_input_device": 0}]
        def check_input_settings(self, **kwargs):
            pass
    with patch.object(config, "VOICE_INPUT_DEVICE", ""):
        assert dengar._pilih_mikrofon(Devices()).name == "Microphone Array"
    with patch.object(config, "VOICE_INPUT_DEVICE", "Stereo Mix"):
        try:
            dengar._pilih_mikrofon(Devices())
            raise AssertionError("Stereo Mix tidak boleh menjadi sumber ucapan")
        except RuntimeError:
            pass
    print("OK: input sistem/virtual ditolak, mikrofon fisik dipilih")


def check_native_aec():
    rng = np.random.default_rng(11)
    count = 16000 * 10
    far = np.convolve(rng.normal(0, 4500, count), np.ones(5) / 5, "same").astype(np.int16)
    echo = np.concatenate((np.zeros(800), far[:-800] * 0.55))
    human = np.convolve(rng.normal(0, 2600, count), np.ones(3) / 3, "same")
    human[:80000] = 0
    mic = np.clip(echo + human, -32000, 32000).astype(np.int16)

    class Reference:
        active = True
        index = 0
        def read_frames(self, n):
            start = self.index
            self.index += n * 160
            return [far[i:i + 160] for i in range(start, self.index, 160)]
        def history(self):
            return far[max(0, self.index - 16000):self.index]

    front = dengar._AudioFrontEnd(16000)
    front._silero = None  # periksa DSP, bukan pengenalan sinyal sintetis sebagai kata
    assert front.enable_echo(Reference()) and front.echo_enabled
    output = []
    for i in range(0, count, 1024):
        processed, raw, _vad = front.proses(mic[i:i + 1024])
        assert len(processed) == len(raw)
        output.append(processed)
    output = np.concatenate(output).astype(float)
    rms = lambda x: np.sqrt(np.mean(np.asarray(x, float) ** 2))
    region = slice(32000, 64000)
    reduction = 20 * np.log10((rms(mic[region]) + 1) / (rms(output[region]) + 1))
    assert reduction >= 15, reduction
    # AEC memberi delay kecil; cari korelasi maksimum, jangan menuntut nol delay.
    near = human[112000:128000]
    scores = [abs(np.corrcoef(near, output[112000 + lag:128000 + lag])[0, 1])
              for lag in range(-512, 513, 16)]
    assert max(scores) > 0.60, max(scores)
    assert rms(output[112000:128000]) > 100
    assert matches_render(far[2000:3024], far[:5000])
    assert not matches_render(human[100000:101024], far[:16000])
    print(f"OK: AEC native menekan gema {reduction:.1f} dB; sinyal eksternal tetap ada")


def check_barge_in():
    from unittest.mock import MagicMock
    speaking = {"active": True}
    class Front:
        echo_enabled = True
        vad_tersedia = vad_handal = True
        def proses(self, data):
            return data, data, bool(data.max() >= 50)
        def reset(self):
            pass
    quiet = np.full(1024, 2, np.int16)
    external = np.full(1024, 2000, np.int16)
    frames = iter([quiet] * 10 + [external] * 4 + [quiet] * 24)
    with (patch.object(dengar, "pengenal", return_value=MagicMock()),
          patch.object(dengar, "_KALIBRASI", 0),
          patch.object(dengar.Pendengar, "_bagasai_bicara", side_effect=lambda: speaking["active"]),
          patch("agent.suara.diam", side_effect=lambda: speaking.update(active=False)) as stop):
        listener = dengar.Pendengar(lambda text: None)
        class Stream:
            def read(self, _count):
                try:
                    return next(frames), False
                except StopIteration:
                    listener._stop.set()
                    return quiet, False
        listener._gelung(Stream(), np, frontend=Front())
        assert stop.called and not speaking["active"]
        assert listener._antre.qsize() == 1
    print("OK: ucapan eksternal menyela TTS setelah AEC, tanpa kehilangan potongan suara")


def check_without_reference():
    """Speaker keras tidak memicu prompt saat AEC tidak tersedia."""
    from unittest.mock import MagicMock
    quiet = np.full(1024, 2, np.int16)
    loud = np.full(1024, 5000, np.int16)
    external = np.full(1024, 2000, np.int16)
    blocks = [loud] * 30 + [quiet] * 10 + [external] * 6 + [quiet] * 24
    clock = {"t": 0.0, "speaking": True}
    class Front:
        echo_enabled = False
        vad_tersedia = vad_handal = True
        def proses(self, data):
            return data, data, bool(data.max() >= 50)
    with (patch.object(dengar, "pengenal", return_value=MagicMock()),
          patch.object(dengar, "_KALIBRASI", 0),
          patch.object(dengar.time, "monotonic", side_effect=lambda: clock["t"]),
          patch.object(dengar.Pendengar, "_bagasai_bicara", side_effect=lambda: clock["speaking"]),
          patch("agent.suara.diam") as stop):
        listener = dengar.Pendengar(lambda text: None)
        class Stream:
            index = 0
            def read(self, _count):
                clock["t"] += 1024 / 16000
                clock["speaking"] = self.index < 30
                if self.index >= len(blocks):
                    listener._stop.set()
                    return quiet, False
                data = blocks[self.index]
                self.index += 1
                return data, False
        listener._gelung(Stream(), np, frontend=Front())
        stop.assert_not_called()
        assert listener._antre.qsize() == 1
        captured = np.frombuffer(listener._antre.get_nowait(), np.int16)
        assert 5000 not in captured and 2000 in captured
    print("OK: tanpa referensi, TTS diblokir; ucapan setelah speaker selesai tetap diterima")


check_input()
check_native_aec()
check_barge_in()
check_without_reference()

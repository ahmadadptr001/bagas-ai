"""Uji DSP dan dekode adaptif, memakai sinyal sintetis tanpa mikrofon."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import dengar


def check_resampling():
    # 11 kHz harus dibuang sebelum menjadi alias 5 kHz di rekaman 16 kHz.
    t = np.arange(48000) / 48000
    voice = (np.sin(2 * np.pi * 1000 * t) * 10000).astype(np.int16)
    noise = (np.sin(2 * np.pi * 11000 * t) * 10000).astype(np.int16)
    output_voice = dengar._ubah_laju(voice, 48000)
    output_noise = dengar._ubah_laju(noise, 48000)
    assert len(output_voice) == len(output_noise) == 16000
    assert dengar._rms(output_voice[200:-200]) > 6500
    assert dengar._rms(output_noise[200:-200]) < 20
    front = dengar._AudioFrontEnd(44100)
    assert front._resampler is not None
    x = (np.sin(np.arange(44100) * 2 * np.pi * 1000 / 44100) * 8000).astype(np.int16)
    parts = [front._resampler.resample_chunk(x[i:i + 701]) for i in range(0, len(x), 701)]
    parts.append(front._resampler.resample_chunk(np.empty(0, dtype=np.int16), last=True))
    actual = np.concatenate(parts)
    expected = dengar._ubah_laju(x, 44100)
    assert len(actual) == 16000 and np.max(np.abs(actual.astype(float) - expected)) <= 2
    print("OK: filter anti-alias, 44.1/48 kHz -> 16 kHz, kontinuitas lintas blok")


def check_streaming_vad():
    class Session:
        def __init__(self):
            self.calls = []
        def get_inputs(self):
            return [SimpleNamespace(name=name) for name in ("input", "h", "c")]
        def run(self, _, data):
            self.calls.append(data)
            return np.array([[0.95]], np.float32), data["h"] + 1, data["c"] + 1
    session = Session()
    with patch("faster_whisper.vad.get_vad_model", return_value=SimpleNamespace(session=session)):
        vad = dengar._VadSilero()
        assert vad.proba(np.ones(512, np.int16)) == np.float32(0.95)
        assert vad.frames_processed == 1, "ucapan pertama tidak boleh menunggu satu detik"
        vad.proba(np.ones(512, np.int16))
        assert len(session.calls) == 2
        assert all(call["input"].shape == (1, 576) for call in session.calls)
        assert np.all(session.calls[1]["h"] == 1), "state tidak diteruskan"
        fresh = dengar._VadSilero()
        assert np.all(fresh._h == 0), "state ucapan bocor ke sesi lain"
    actual = dengar._VadSilero()
    for _ in range(40):
        assert actual.proba(np.zeros(512, np.int16)) < 0.5
    assert actual.frames_processed == 40
    print("OK: VAD berjalan setelah 32 ms, state berlanjut, sesi terpisah, sunyi ditolak")


def check_uncertain_decode():
    def segment(text, score):
        return SimpleNamespace(text=text, avg_logprob=score, no_speech_prob=0.02)
    engine = dengar._Pengenal()
    engine._whisper = MagicMock()
    engine._whisper.transcribe.side_effect = [
        ([segment("hasil salah", -1.3)], None),
        ([segment("buka file utama", -0.2)], None),
    ]
    audio = np.full(16000, 1000, np.int16).tobytes()
    assert engine.kenali(audio) == ("buka file utama", "whisper")
    calls = engine._whisper.transcribe.call_args_list
    assert [c.kwargs["vad_filter"] for c in calls] == [False, True]
    assert all("initial_prompt" not in c.kwargs and c.kwargs["temperature"] == 0 for c in calls)
    engine._whisper.transcribe.side_effect = [
        ([segment("suara asli pelan", -1.0)], None),
        ([], None),
    ]
    assert engine.kenali(audio)[0] == "suara asli pelan", "VAD kedua membuang ucapan asli"
    print("OK: transkripsi ragu dibaca ulang; hasil valid/pelan dan anti-halusinasi dipertahankan")


check_resampling()
check_streaming_vad()
check_uncertain_decode()

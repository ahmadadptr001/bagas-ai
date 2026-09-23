"""Regresi ucapan pendek/pelan; tidak merekam mikrofon pengguna."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import dengar


def rekam(blok, pakai_vad=True, menyela=False):
    silent = np.full(1024, 2, dtype=np.int16)
    audio = [np.full(1024, value, dtype=np.int16) for value in blok]
    audio += [silent] * 24
    class Front:
        vad_tersedia = True
        def __init__(self, *args):
            pass
        def proses(self, data):
            return data, data, bool(pakai_vad and data.max() >= 50)
        def reset(self):
            pass
    speaking = {"active": menyela}
    with patch.object(dengar, "pengenal", return_value=MagicMock()), \
            patch.object(dengar, "_AudioFrontEnd", Front), \
            patch.object(dengar, "_KALIBRASI", 0.0), \
            patch.object(dengar.Pendengar, "_bagasai_bicara", side_effect=lambda: speaking["active"]), \
            patch("agent.suara.diam", side_effect=lambda: speaking.update(active=False)):
        listener = dengar.Pendengar(lambda text: None)
        iterator = iter(audio)
        class Stream:
            def read(self, _):
                try:
                    return next(iterator), False
                except StopIteration:
                    listener._stop.set()
                    return silent, False
        listener._gelung(Stream(), np)
    return listener


def main():
    # 192 ms, lebih pendek daripada batas lama 350 ms, tetap masuk STT.
    short = rekam([50] * 3)
    assert short._antre.qsize() == 1
    assert short.memproses
    # Klik setelah pre-roll panjang tidak boleh dihitung sebagai ucapan.
    assert rekam([2] * 15 + [2000])._antre.empty()
    assert rekam([2] * 30)._antre.empty()
    # Suara pelan yang dilewatkan VAD tetap masuk lewat jalur energi.
    soft = rekam([50] * 5, pakai_vad=False)
    assert soft._antre.qsize() == 1
    # Jawaban singkat saat TTS aktif: baseline speaker lalu ucapan 192 ms.
    assert rekam([2] * 15 + [2000] * 3, menyela=True)._antre.qsize() == 1
    assert rekam([2] * 15 + [2000], menyela=True)._antre.empty()
    # Pengenal menerima amplitudo yang terangkat, tetapi sunyi tetap nol.
    engine = dengar._Pengenal()
    with patch.object(engine, "_transkripsikan", return_value=("ya", 0.01)) as stt:
        assert engine._whisperkenali(np.full(4096, 100, dtype=np.int16).tobytes()) == "ya"
        assert 0.02 < float(np.max(stt.call_args.args[0])) <= 0.1
        engine._whisperkenali(np.zeros(4096, dtype=np.int16).tobytes())
        assert not np.any(stt.call_args.args[0])
    print("OK: 192 ms tertangkap; suara pelan lolos; klik/sunyi ditolak; penguatan terbatas")


if __name__ == "__main__":
    main()

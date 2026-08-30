# -*- coding: utf-8 -*-
"""Uji pipeline mikrofon lokal tanpa membuka perangkat audio sungguhan."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import dengar


class SoundDevicePalsu:
    default = SimpleNamespace(device=(1, 3))

    @staticmethod
    def query_hostapis():
        return [
            {"name": "MME", "default_input_device": 1},
            {"name": "Windows WASAPI", "default_input_device": 2},
        ]

    @staticmethod
    def query_devices():
        return [
            {"name": "keluaran", "hostapi": 0, "max_input_channels": 0,
             "default_samplerate": 44100},
            {"name": "Mic MME", "hostapi": 0, "max_input_channels": 1,
             "default_samplerate": 44100},
            {"name": "Mic WASAPI", "hostapi": 1, "max_input_channels": 2,
             "default_samplerate": 48000},
        ]

    @staticmethod
    def check_input_settings(**kwargs):
        assert kwargs["channels"] == 1 and kwargs["dtype"] == "int16"


def uji_pilihan_dan_frontend() -> None:
    mic = dengar._pilih_mikrofon(SoundDevicePalsu)
    assert mic.index == 2 and mic.sample_rate == 48000
    assert "WASAPI" in mic.hostapi

    x = (np.sin(np.linspace(0, 8 * np.pi, 3072)) * 3000).astype(np.int16)
    turun = dengar._ubah_laju(x, 48000)
    assert len(turun) == 1024 and turun.dtype == np.int16

    depan = dengar._AudioFrontEnd(48000)
    bersih, mentah, _suara = depan.proses(x)
    assert depan.vad_tersedia
    assert len(bersih) == len(mentah) == 960  # enam bingkai WebRTC 10 ms
    print("  WASAPI + resample + WebRTC NS/AGC/VAD: OK")


def uji_model_tidak_dimuat_ganda() -> None:
    dibuat: list[tuple] = []

    class ModelPalsu:
        def __init__(self, *args, **kwargs):
            dibuat.append((args, kwargs))

    modul = SimpleNamespace(WhisperModel=ModelPalsu)
    p = dengar._Pengenal()
    hasil: list[tuple[bool, str]] = []
    with patch.dict(sys.modules, {"faster_whisper": modul}):
        threads = [threading.Thread(target=lambda: hasil.append(p.pastikan()))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(dibuat) == 1
    assert len(hasil) == 4 and all(ok for ok, _ in hasil)
    print("  pemuatan Whisper terkunci (satu salinan): OK")


def uji_halusinasi_sunyi_dibuang() -> None:
    class Segmen:
        text = " Terima kasih kerana menonton. "

    model = MagicMock()
    model.transcribe.return_value = ([Segmen()], object())
    kenal = dengar._Pengenal()
    kenal._whisper = model
    assert kenal._whisperkenali(np.zeros(16000, dtype=np.int16).tobytes()) == ""
    assert dengar._halusinasi_sunyi(
        "Terima kasih telah menonton. Terima kasih telah menonton.")
    assert not dengar._halusinasi_sunyi("tulis ucapan terima kasih untuk ibu")
    print("  halusinasi Whisper pada audio sunyi dibuang: OK")


def uji_dikte_tanpa_wake_word() -> None:
    # Lima blok derau, delapan blok ucapan, lalu sunyi sampai auto-stop. VAD
    # sengaja dibuat melewatkan ucapan: jaring energi beruntun harus mengambil
    # alih tanpa membuat satu dentuman membuka rekaman.
    tingkat = [20] * 5 + [2000] * 8 + [20] * 20

    class StreamPalsu:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _n):
            nilai = tingkat.pop(0) if tingkat else 20
            return np.full((1024, 1), nilai, dtype=np.int16), False

    class FrontendPalsu:
        vad_tersedia = True

        def __init__(self, _rate):
            pass

        def proses(self, data):
            x = np.asarray(data, dtype=np.int16).reshape(-1)
            return x, x, False

    kenal = MagicMock()
    kenal.pastikan.return_value = (True, "siap")
    kenal.kenali.return_value = ("tolong buka main py", "whisper")
    mic = dengar.PilihanMikrofon(2, "Mic Uji", 16000, "WASAPI")
    fase: list[str] = []
    with (patch.object(dengar, "siap", return_value=(True, "")),
          patch.object(dengar, "pengenal", return_value=kenal),
          patch.object(dengar, "_buka_input",
                       return_value=(StreamPalsu(), mic, 1024)),
          patch.object(dengar, "_AudioFrontEnd", FrontendPalsu),
          patch("agent.suara.diam")):
        teks, info = dengar.dengar_dikte(
            on_status=fase.append, tunggu_mulai=2,
            maks_detik=2, jeda_selesai=0.1)
    assert teks == "tolong buka main py"
    assert info["engine"] == "whisper"
    assert fase == ["menunggu", "merekam", "menganalisis"]
    assert kenal.kenali.called
    print("  dikte tanpa wake word + VAD miss + auto-stop: OK")


def uji_listener_kontinu_tanpa_wake_word() -> None:
    """Satu ucapan dikenali langsung sebagai prompt dalam mode sesi voice."""
    kenal = MagicMock()
    kenal.kenali.return_value = ("buka berkas utama", "whisper")
    terkirim: list[str] = []
    level: list[tuple[float, bool]] = []
    with patch.object(dengar, "pengenal", return_value=kenal):
        p = dengar.Pendengar(
            terkirim.append, langsung=True,
            on_level=lambda nilai, aktif: level.append((nilai, aktif)))
    p._satu_ucapan(b"audio")
    assert terkirim == ["buka berkas utama"]
    assert not p.perakit.merekam
    p.ambang = 100
    p._lapor_level(200, True, paksa=True)
    assert level[-1] == (1.0, True)
    p.berhenti()
    assert level[-1] == (0.0, False)
    p._satu_ucapan(b"audio sesudah esc")
    assert terkirim == ["buka berkas utama"]
    print("  listener kontinu langsung + level orb: OK")


def uji_kontrak_instalasi() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    dengar_src = (root / "src/agent/dengar.py").read_text(encoding="utf-8")
    updater = (root / "src/agent/updater.py").read_text(encoding="utf-8")
    ps1 = (root / "install.ps1").read_text(encoding="utf-8")
    sh = (root / "install.sh").read_text(encoding="utf-8")
    assert '"faster-whisper>=1.0.0"' in pyproject
    assert '"aec-audio-processing>=1.0.1"' in pyproject
    assert "SpeechRecognition" not in pyproject
    assert "recognize_google" not in dengar_src
    for isi in (updater, ps1, sh):
        assert "--prepare-model" in isi
    print("  install + update wajib memverifikasi model: OK")


if __name__ == "__main__":
    uji_pilihan_dan_frontend()
    uji_model_tidak_dimuat_ganda()
    uji_halusinasi_sunyi_dibuang()
    uji_dikte_tanpa_wake_word()
    uji_listener_kontinu_tanpa_wake_word()
    uji_kontrak_instalasi()
    print("OK - seluruh pipeline audio lokal lulus")

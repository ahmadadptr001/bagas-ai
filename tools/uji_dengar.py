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
    class SegmenStok:
        text = " Terima kasih kerana menonton. "
        no_speech_prob = 0.20

    model = MagicMock()
    model.transcribe.return_value = ([SegmenStok()], object())
    kenal = dengar._Pengenal()
    kenal._whisper = model
    assert kenal._whisperkenali(np.zeros(16000, dtype=np.int16).tobytes()) == ""
    assert [c.kwargs["vad_filter"] for c in model.transcribe.call_args_list] == \
        [False, True]
    assert dengar._halusinasi_sunyi(
        "Terima kasih telah menonton. Terima kasih telah menonton.")
    assert dengar._halusinasi_sunyi("Terima kasih.", 0.75)
    assert not dengar._halusinasi_sunyi("Terima kasih.", 0.20)
    assert not dengar._halusinasi_sunyi("tulis ucapan terima kasih untuk ibu")
    assert not dengar._halusinasi_sunyi("buka berkas utama", 0.99)

    class SegmenPendekSunyi:
        text = " Terima kasih. "
        no_speech_prob = 0.75

    model.reset_mock()
    model.transcribe.return_value = ([SegmenPendekSunyi()], object())
    assert kenal._whisperkenali(np.zeros(16000, dtype=np.int16).tobytes()) == ""

    # Filter VAD kedua boleh memulihkan ucapan nyata yang pada pass awal
    # disalahkenali sebagai frasa stok.
    class SegmenPulih:
        text = " Buka berkas utama. "
        no_speech_prob = 0.82

    model.reset_mock()
    model.transcribe.side_effect = [
        ([SegmenPendekSunyi()], object()),
        ([SegmenPulih()], object()),
    ]
    assert kenal._whisperkenali(np.zeros(16000, dtype=np.int16).tobytes()) == \
        "Buka berkas utama."

    # no_speech tinggi tidak boleh lagi membuang kalimat non-stok dari suara
    # pengguna yang pelan atau jauh.
    model.reset_mock()
    model.transcribe.side_effect = None
    model.transcribe.return_value = ([SegmenPulih()], object())
    assert kenal._whisperkenali(np.zeros(16000, dtype=np.int16).tobytes()) == \
        "Buka berkas utama."
    assert model.transcribe.call_count == 1

    class SegmenPendekNyata:
        text = " Terima kasih. "
        no_speech_prob = 0.20

    model.reset_mock()
    model.transcribe.return_value = ([SegmenPendekNyata()], object())
    assert kenal._whisperkenali(np.zeros(16000, dtype=np.int16).tobytes()) == \
        "Terima kasih."
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
            terkirim.append,
            on_level=lambda nilai, aktif: level.append((nilai, aktif)))
    p._satu_ucapan(b"audio")
    assert terkirim == ["buka berkas utama"]
    assert not p.merekam
    p.ambang = 100
    p._lapor_level(200, True, paksa=True)
    assert level[-1] == (1.0, True)
    p.berhenti()
    assert level[-1] == (0.0, False)
    p._satu_ucapan(b"audio sesudah esc")
    assert terkirim == ["buka berkas utama"]
    print("  listener kontinu langsung + level orb: OK")


def uji_jeda_alami_tetap_satu_prompt() -> None:
    """Jeda napas pendek tak boleh membelah prompt langsung.

    Ini menguji pemotong audio sungguhan, bukan hanya memanggil
    ``_satu_ucapan`` dengan blob buatan. Kalimat memiliki sunyi 0,8 detik di
    tengahnya — di bawah endpoint (yang kini gesit demi balasan cepat),
    sehingga tetap menjadi SATU prompt, bukan dua potongan kata pendek.
    """
    blok_ucapan = np.full((1024,), 2000, dtype=np.int16)
    blok_sunyi = np.full((1024,), 20, dtype=np.int16)
    jeda_napas = int(0.8 / dengar._DETIK_BLOK)
    jeda_akhir = int((dengar._DIAM_SELESAI_LANGSUNG + 0.2)
                     / dengar._DETIK_BLOK)
    blok = ([blok_ucapan] * 8 + [blok_sunyi] * jeda_napas
            + [blok_ucapan] * 8 + [blok_sunyi] * jeda_akhir
            + [blok_sunyi] * 2)

    class StreamPalsu:
        def __init__(self, pendengar):
            self.pendengar = pendengar
            self.index = 0

        def read(self, _n):
            if self.index >= len(blok):
                self.pendengar._stop.set()
                return blok_sunyi.reshape(-1, 1), False
            nilai = blok[self.index]
            self.index += 1
            return nilai.reshape(-1, 1), False

    class FrontendPalsu:
        vad_tersedia = True

        def __init__(self, _rate):
            pass

        def proses(self, data):
            x = np.asarray(data, dtype=np.int16).reshape(-1)
            return x, x, bool(np.max(np.abs(x)) > 1000)

        def reset(self):
            pass

    kenal = MagicMock()
    kenal.kenali.return_value = (
        "tolong buka berkas utama lalu jelaskan isinya", "whisper")
    terkirim: list[str] = []
    with (patch.object(dengar, "pengenal", return_value=kenal),
          patch.object(dengar, "_AudioFrontEnd", FrontendPalsu),
          patch.object(dengar, "_KALIBRASI", 0.0),
          patch.object(dengar, "hitung_ambang",
                       return_value=(20.0, 20.0, 100.0, 60.0))):
        p = dengar.Pendengar(terkirim.append)
        p._gelung(StreamPalsu(p), np)

    assert p._antre.qsize() == 1, (
        "jeda napas memecah satu kalimat menjadi beberapa audio")
    blob = p._antre.get_nowait()
    p._stop.clear()
    p._satu_ucapan(blob)
    assert kenal.kenali.call_count == 1
    assert terkirim == [
        "tolong buka berkas utama lalu jelaskan isinya"]
    # Dua bagian ucapan beserta jeda di tengah memang berada dalam satu blob.
    assert len(blob) / 2 / dengar.LAJU > 2.5
    print("  jeda napas 0,8 dtk + tanpa wake word = satu prompt: OK")


def uji_penyela_memotong_tts() -> None:
    """Menyela di tengah TTS: bacaan dipotong & ucapan penyela jadi prompt.

    Dulu semua blok dibuang selama speaker bersuara — mikrofon tuli total dan
    pengguna yang menanggapi di tengah bacaan lenyap. Kini kebocoran speaker
    jadi baseline; lonjakan energi DI ATASNYA (plus VAD) memotong bacaan dan
    memulai rekaman ucapan penyela, lengkap dengan pre-roll sebelum
    lonjakannya."""
    blok_bocor = np.full((1024,), 100, dtype=np.int16)   # gema pengeras suara
    blok_ucap = np.full((1024,), 3000, dtype=np.int16)   # suara penyela
    blok_sunyi = np.full((1024,), 20, dtype=np.int16)
    jeda_akhir = int((dengar._DIAM_SELESAI_LANGSUNG + 0.2)
                     / dengar._DETIK_BLOK)
    # 8 blok kebocoran membangun baseline; 5 blok lonjakan beruntun memicu
    # penyela; sisanya ucapan penyela + sunyi sampai endpoint.
    blok = ([blok_bocor] * 8 + [blok_ucap] * 10
            + [blok_sunyi] * jeda_akhir + [blok_sunyi] * 2)

    class StreamPalsu:
        def __init__(self, pendengar):
            self.pendengar = pendengar
            self.index = 0

        def read(self, _n):
            if self.index >= len(blok):
                self.pendengar._stop.set()
                return blok_sunyi.reshape(-1, 1), False
            nilai = blok[self.index]
            self.index += 1
            return nilai.reshape(-1, 1), False

    class FrontendPalsu:
        vad_tersedia = True

        def __init__(self, _rate):
            pass

        def proses(self, data):
            x = np.asarray(data, dtype=np.int16).reshape(-1)
            return x, x, bool(np.max(np.abs(x)) > 1000)

        def reset(self):
            pass

    kenal = MagicMock()
    kenal.kenali.return_value = ("hentikan, kubahas yang lain", "whisper")
    terkirim: list[str] = []
    tts = {"on": True}

    # Mock dibuat terpisah: `with (...) as x` pada daftar berkurung mengikat
    # TUPLE-nya, bukan mock yang terakhir.
    diam = MagicMock(side_effect=lambda: tts.update(on=False))

    with (patch.object(dengar, "pengenal", return_value=kenal),
          patch.object(dengar, "_AudioFrontEnd", FrontendPalsu),
          patch.object(dengar, "_KALIBRASI", 0.0),
          patch.object(dengar, "hitung_ambang",
                       return_value=(20.0, 20.0, 100.0, 60.0)),
          patch("agent.suara.sibuk", side_effect=lambda: tts["on"]),
          patch("agent.suara.diam", diam)):
        p = dengar.Pendengar(terkirim.append)
        p._gelung(StreamPalsu(p), np)

    assert diam.called, "ucapan penyela harus memotong bacaan TTS"
    assert p._antre.qsize() == 1, "ucapan penyela harus menjadi satu prompt"
    blob = p._antre.get_nowait()
    p._stop.clear()
    p._satu_ucapan(blob)
    assert terkirim == ["hentikan, kubahas yang lain"]
    # Pre-roll ikut terekam: suku kata pertama penyela jatuh SEBELUM
    # ambangnya terlampaui.
    assert len(blob) / 2 / dengar.LAJU > 0.5
    print("  menyela TTS: bacaan dipotong + ucapan penyela terekam: OK")


def uji_potong_kalimat_tts() -> None:
    """Jawaban akhir dipecah per kalimat; potongan lanjutan tak terbuang."""
    from agent import suara

    teks = ("Kalimat pembuka yang pendek. "
            + "Kalimat berikutnya menjelaskan hasil pemeriksaan berkas. " * 8)
    bag = suara.potong_kalimat(teks)
    assert len(bag) >= 2, "jawaban panjang harus dipecah beberapa potongan"
    assert len(bag[0]) <= 160, "potongan pembuka harus pendek agar cepat terdengar"
    assert all(len(b) <= 320 for b in bag)
    assert " ".join(bag) == suara.bersihkan(teks, suara._MAKS_TOTAL)

    # Blok kode tak pantas didengar — tetap dibuang walau dipecah per kalimat.
    assert suara.potong_kalimat("Lihat:\n```python\nx = 1\n```\nSelesai.") == \
        ["Lihat: Selesai."]

    # Antrean: kabar langkah baru membuang kabar langkah lama, TAPI potongan
    # lanjutan jawaban akhir tetap hidup — jawaban tak berhenti setengah.
    p = suara.Pengucap()
    p._antre.put(("kabar langkah lama", False))
    p._antre.put(("kalimat lanjutan jawaban", True))
    p._buang_antre(simpan_sambung=True)
    assert p._antre.qsize() == 1
    assert p._antre.get_nowait() == ("kalimat lanjutan jawaban", True)
    p._antre.put(("kalimat lanjutan", True))
    p._buang_antre()
    assert p._antre.qsize() == 0, "diam() tetap membuang semuanya"
    print("  TTS per kalimat + antrean sambung: OK")


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
    uji_jeda_alami_tetap_satu_prompt()
    uji_penyela_memotong_tts()
    uji_potong_kalimat_tts()
    uji_kontrak_instalasi()
    print("OK - seluruh pipeline audio lokal lulus")

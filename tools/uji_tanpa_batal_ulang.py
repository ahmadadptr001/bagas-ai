# -*- coding: utf-8 -*-
"""Regresi: jawaban yang SUDAH mengalir tidak pernah dibatalkan lalu diulang.

Konteks (2026-09-23). Dulu ada penjaga "stream diam >45 dtk" sesudah token
pertama: thread daemon menutup stream-nya, penutupan itu diterjemahkan jadi
error SEMENTARA, `_call_with_retry` mengulang dari nol, dan sesudah dua kali
gagal core menaikkan effort lalu mengulang putarannya sampai tiga kali.
Di layar pengguna: tulisan yang sedang tumbuh lenyap, lalu model mulai lagi
dari awal — "padahal AI-nya lagi ngejawab". Uji ini mengunci janji
sebaliknya, di KEDUA lapisan tempat pengulangan dulu terjadi:

  1. lapisan llm     — `_call_with_retry(sudah_mengalir=...)` + `stream_completion`
  2. lapisan core    — `Agent.run()` tidak meminta ulang putaran yang sudah
                       mencetak potongan jawaban

Rate limit sebelum token pertama juga tidak boleh mengirim ulang prompt.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

os.environ["BAGASAI_PROJECT_ROOT"] = tempfile.mkdtemp(prefix="uji_ulang_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import openai

from agent import config, llm, models
from agent.core import Agent


class Sibuk(Exception):
    """Galat sementara bergaya throttle/limit — pesannya sengaja kena kata kunci."""

    def __init__(self, pesan: str = "429 rate limit reached") -> None:
        super().__init__(pesan)


class SibukTimeout(Exception):
    """Jeda baca socket yang timeout — SEMENTARA menurut _is_transient."""

    def __init__(self, pesan: str = "read timeout") -> None:
        super().__init__(pesan)


# --- 1. lapisan llm -----------------------------------------------------------


def cek_sebelum_mengalir_tidak_diulang() -> None:
    """Rate limit sebelum ada potongan tampil juga tidak dikirim ulang."""
    jumlah = 0
    kabar: list[tuple[int, float, Exception]] = []

    def panggil() -> str:
        nonlocal jumlah
        jumlah += 1
        if jumlah == 1:
            raise Sibuk()
        return "jawaban"

    try:
        llm._call_with_retry(
            panggil,
            on_retry=lambda a, w, e: kabar.append((a, w, e)),
            sudah_mengalir=lambda: False,
        )
        raise AssertionError("rate limit harus langsung dilaporkan")
    except Sibuk:
        pass
    assert jumlah == 1, jumlah
    assert not kabar, kabar
    print("  sebelum mengalir: throttle tidak mengirim ulang prompt: OK")



def cek_sesudah_mengalir_tidak_diulang() -> None:
    """Satu potong sudah tampil -> galat sementara pun TIDAK memicu ulangan."""
    jumlah = 0
    kabar: list[tuple[int, float, Exception]] = []

    def panggil() -> str:
        nonlocal jumlah
        jumlah += 1
        raise SibukTimeout()

    try:
        llm._call_with_retry(
            panggil,
            on_retry=lambda a, w, e: kabar.append((a, w, e)),
            sudah_mengalir=lambda: True,
        )
        raise AssertionError("galat sesudah mengalir seharusnya dilempar apa adanya")
    except SibukTimeout:
        pass
    assert jumlah == 1, f"jawaban yang sudah mengalir diminta ulang {jumlah}x"
    assert not kabar, "UI tak boleh dibuat menunggu retry yang tak akan datang"
    print("  sesudah mengalir: galat dilempar apa adanya, tanpa prompt ulang: OK")


def cek_jeda_panjang_tidak_membatalkan() -> None:
    """Stream yang DIAM lama di tengah jawaban tetap diselesaikan.

    Dulu jeda >45 dtk cukup untuk membuat stream ditutup paksa dari luar.
    Di sini jedanya dipendekkan (2 dtk) tapi yang diperiksa justru INTI
    keluhan: selama stream diam, tidak ada penjaga apa pun yang hidup dan
    siap membatalkannya.
    """
    potongan = ["Bagian pertama. ", "Bagian kedua. ", "Bagian ketiga."]
    penjaga_saat_diam: list[list[str]] = []
    panggilan: list[int] = []

    class Aliran:
        def __iter__(self):
            for i, teks in enumerate(potongan):
                if i == 1:
                    # "Model sedang menyusun" — jeda panjang di tengah jawaban.
                    time.sleep(2.0)
                    penjaga_saat_diam.append([
                        t.name for t in threading.enumerate()
                        if "watchdog" in t.name.lower() or "stall" in t.name.lower()
                    ])
                yield types.SimpleNamespace(
                    usage=None,
                    choices=[types.SimpleNamespace(
                        finish_reason=None,
                        delta=types.SimpleNamespace(
                            content=teks, reasoning_content=None,
                            tool_calls=None))],
                )

        def close(self) -> None:
            pass

    def create(**_kwargs):
        panggilan.append(1)
        return Aliran()

    klien = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)))

    tampil: list[str] = []
    with patch("agent.llm.get_client", return_value=klien):
        teks, tool_calls, _usage = llm.stream_completion(
            [{"role": "user", "content": "tulis panjang"}],
            model="uji", provider="nvidia", on_content=tampil.append)

    assert teks == "".join(potongan), teks
    assert tampil == potongan, tampil
    assert penjaga_saat_diam == [[]], penjaga_saat_diam
    assert len(panggilan) == 1, f"stream diminta ulang {len(panggilan)}x"
    print("  jeda panjang di tengah jawaban: aliran utuh, tanpa pembatalan: OK")


def cek_penjaga_diam_sudah_hilang() -> None:
    """Penjaga "diam N detik" tak boleh dihidupkan lagi tanpa sadar."""
    assert not hasattr(llm, "StreamStalled"), "kelas StreamStalled hidup lagi"
    assert not hasattr(llm, "_pasang_watchdog"), "watchdog hidup lagi"
    assert not hasattr(config, "STREAM_STALL_TIMEOUT"), "ambang diam hidup lagi"
    assert not hasattr(config, "MAX_STALLS_PER_CALL"), "jatah stall hidup lagi"
    assert config.TTFT_TIMEOUT > 0, "penjaga liveness httpx justru wajib ada"
    print("  penjaga \"stream diam N detik\" benar-benar sudah tiada: OK")


# --- 2. lapisan core ----------------------------------------------------------


def agen_uji() -> Agent:
    spec = models.ModelSpec(
        id="uji/ulang", label="Provider Uji", provider="nvidia",
        api_model="uji", multimodal=False,
    )
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    agent.model_spec = spec
    agent.effort = spec.effort_default or None
    return agent


def cek_core_tidak_meminta_ulang() -> None:
    """Putaran yang sudah mencetak potongan jawaban tidak diulang dari nol."""
    agent = agen_uji()
    jumlah = 0
    tampil: list[str] = []

    def stream(_messages, **kwargs):
        nonlocal jumlah
        jumlah += 1
        on_content = kwargs.get("on_content")
        for teks in ("Jawaban ini sedang", " ditulis", " lalu putus..."):
            if on_content:
                on_content(teks)
            tampil.append(teks)
        # Koneksi putus SESUDAH jawaban mengalir. Dulu inilah pemicu
        # "dibatalkan lalu prompt ulang" (versi watchdog: galat sementara).
        raise SibukTimeout()

    try:
        with patch("agent.core.llm.stream_completion", side_effect=stream):
            agent.run("tulis panjang")
    except SibukTimeout:
        pass
    assert jumlah == 1, f"core meminta ulang putaran yang sudah mengalir {jumlah}x"
    assert tampil == ["Jawaban ini sedang", " ditulis", " lalu putus..."], tampil
    print("  core: potongan yang sudah tampil tidak dihapus & tidak diulang: OK")


def cek_core_masih_pulih_sebelum_mengalir() -> None:
    """Pemulih payload 400 (sebelum ada potongan tampil) TIDAK boleh ikut hilang.

    Ini penjaga sisi lain: yang dihapus hanya pengulangan SESUDAH jawaban
    tampil. Provider yang menolak `extra_body` masih boleh dicoba ulang tanpa
    field itu — dan riwayat tetap tak disentuh.
    """
    spec = models.ModelSpec(
        id="uji/extra-ulang", label="Provider Uji", provider="nvidia",
        api_model="uji", multimodal=False, effort_param="parameter_uji",
        effort_levels=("medium",), effort_default="medium",
    )
    agent = agen_uji()
    agent.model_spec = spec
    agent.effort = "medium"
    extra: list[dict | None] = []

    def stream(_messages, **kwargs):
        extra.append(kwargs.get("extra_body"))
        if len(extra) == 1:
            # 400 generik SEBELUM satu potong pun tampil: core masih boleh
            # mencoba payload yang lebih konservatif.
            raise openai.BadRequestError(
                "Provider returned error",
                response=httpx.Response(
                    400,
                    request=httpx.Request(
                        "POST", "https://provider.invalid/v1")),
                body={"error": {"message": "Provider returned error"}},
            )
        return "jawaban pulih", [], None

    with patch("agent.core.llm.stream_completion", side_effect=stream):
        hasil = agent.run("uji parameter")
    assert hasil == "jawaban pulih", hasil
    assert extra == [{"parameter_uji": "medium"}, None], extra
    print("  core: pemulih 400 sebelum mengalir tetap bekerja: OK")


def cek_web_sibuk_tidak_diulang() -> None:
    from agent import connectors

    agent = agen_uji()
    agent.model_spec = next(s for s in models.MODELS.values() if s.connector)
    agent._web_ctx_sent = True
    agent._web_varian = None
    agent._web_chat_id = "chat-uji"
    calls = []
    tampil = []
    kabar = []

    def send(msg, **kwargs):
        calls.append(msg)
        kwargs["on_token"]("Jawaban masih berjalan")
        raise connectors.WebBusyError("server penuh")

    conn = types.SimpleNamespace(send=send)
    with patch.object(connectors, "playwright_available", return_value=True), \
            patch.object(connectors, "get_connector", return_value=conn), \
            patch.object(agent, "_persist"), \
            patch("agent.core.time.sleep", side_effect=AssertionError("retry tidur")):
        hasil = agent.run(
            "halo", on_token=tampil.append,
            on_retry=lambda *args: kabar.append(args),
        )
    assert len(calls) == 1, calls
    assert tampil == ["Jawaban masih berjalan"], tampil
    assert not kabar, kabar
    assert "tidak dikirim ulang otomatis" in hasil, hasil
    print("  web sibuk: satu pengiriman, tanpa retry atau prompt baru: OK")


def main() -> None:
    cek_sebelum_mengalir_tidak_diulang()
    cek_sesudah_mengalir_tidak_diulang()
    cek_jeda_panjang_tidak_membatalkan()
    cek_penjaga_diam_sudah_hilang()
    cek_web_sibuk_tidak_diulang()
    cek_core_tidak_meminta_ulang()
    cek_core_masih_pulih_sebelum_mengalir()
    print("OK - jawaban yang mengalir tak pernah dibatalkan lalu diulang")


main()

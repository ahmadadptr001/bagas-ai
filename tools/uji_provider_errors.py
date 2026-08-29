# -*- coding: utf-8 -*-
"""Regresi klasifikasi error provider dan pemulihan payload API."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["BAGASAI_PROJECT_ROOT"] = tempfile.mkdtemp(prefix="uji_provider_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import openai
from PIL import Image

from agent import llm, models
from agent.core import Agent


def bad_request(message: str = "Provider returned error"):
    return openai.BadRequestError(
        message,
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://provider.invalid/v1")),
        body={"error": {"message": message}},
    )


def cek_opencode_tanpa_variant() -> None:
    specs = [s for s in models.MODELS.values() if s.provider == "opencode"]
    assert specs
    assert all(not s.effort_param and not s.effort_levels for s in specs)
    assert all(s.extra_body_for("medium") is None for s in specs)
    print("  OpenCode tidak lagi mengirim field API `variant`: OK")


def cek_kuota_gratis_gagal_cepat() -> None:
    jumlah = 0

    class Limit(Exception):
        status_code = 429
        body = {"error": {"type": "FreeUsageLimitError"}}

    def panggil():
        nonlocal jumlah
        jumlah += 1
        raise Limit("FreeUsageLimitError: Rate limit exceeded")

    try:
        llm._call_with_retry(panggil)
        raise AssertionError("FreeUsageLimitError seharusnya dilempar ramah")
    except llm.ProviderQuotaError as exc:
        assert "Kuota gratis OpenCode Zen" in str(exc)
        assert "bukan kerusakan tool" in str(exc)
        assert jumlah == 1, "limit kuota tetap tidak boleh diulang 5 menit"
    print("  FreeUsageLimitError berhenti cepat dengan arahan jelas: OK")


def agen_uji(spec: models.ModelSpec, tools: list[str] | None = None) -> Agent:
    agent = Agent(model="opencode/big-pickle", tool_names=tools or [])
    agent.model_spec = spec
    agent.effort = spec.effort_default or None
    return agent


def cek_extra_dilepas_tanpa_pangkas() -> None:
    spec = models.ModelSpec(
        id="uji/extra", label="Provider Uji", provider="nvidia",
        api_model="uji", multimodal=False, effort_param="parameter_uji",
        effort_levels=("medium",), effort_default="medium",
    )
    agent = agen_uji(spec)
    extra: list[dict | None] = []
    notice: list[str] = []

    def stream(_messages, **kwargs):
        extra.append(kwargs.get("extra_body"))
        if len(extra) == 1:
            raise bad_request("Unknown field: parameter_uji")
        return "jawaban pulih", [], None

    with patch("agent.core.llm.stream_completion", side_effect=stream):
        hasil = agent.run("uji parameter", on_notice=notice.append)
    assert hasil == "jawaban pulih"
    assert extra == [{"parameter_uji": "medium"}, None]
    assert any("parameter API standar" in n for n in notice)
    assert agent._pernah_pangkas is False
    assert any(m.get("content") == "uji parameter" for m in agent.memory.messages)
    print("  HTTP 400 field asing -> retry tanpa extra, riwayat utuh: OK")


def cek_media_benar_benar_dilepas() -> None:
    akar = Path(os.environ["BAGASAI_PROJECT_ROOT"])
    gambar = akar / "media.png"
    Image.new("RGB", (3, 2), (10, 20, 30)).save(gambar)
    spec = models.ModelSpec(
        id="uji/media", label="Vision Uji", provider="nvidia",
        api_model="uji", multimodal=True,
    )
    agent = agen_uji(spec)
    bentuk: list[bool] = []

    def stream(messages, **_kwargs):
        konten = messages[-1].get("content")
        bentuk.append(isinstance(konten, list))
        if len(bentuk) == 1:
            raise bad_request("Provider returned error")
        return "jawaban tanpa media", [], None

    with patch("agent.core.llm.stream_completion", side_effect=stream):
        hasil = agent.run("lihat", attachments=[str(gambar)])
    assert hasil == "jawaban tanpa media"
    assert bentuk == [True, False], bentuk
    assert agent._pernah_pangkas is False
    print("  fallback media 400 benar-benar menghapus data-URL, bukan riwayat: OK")


def cek_error_fatal_tidak_diulang() -> None:
    spec = models.ModelSpec(
        id="uji/fatal", label="Provider Uji", provider="nvidia",
        api_model="model-hilang", multimodal=False,
    )
    agent = agen_uji(spec, tools=["read_file"])
    jumlah = 0

    def stream(_messages, **_kwargs):
        nonlocal jumlah
        jumlah += 1
        raise bad_request("Invalid model: model-hilang does not exist")

    try:
        with patch("agent.core.llm.stream_completion", side_effect=stream):
            agent.run("uji fatal")
        raise AssertionError("invalid model seharusnya tetap dilempar")
    except openai.BadRequestError:
        pass
    assert jumlah == 1
    assert agent._pernah_pangkas is False
    print("  auth/model fatal tidak diulang dan tidak memangkas riwayat: OK")


def main() -> None:
    cek_opencode_tanpa_variant()
    cek_kuota_gratis_gagal_cepat()
    cek_extra_dilepas_tanpa_pangkas()
    cek_media_benar_benar_dilepas()
    cek_error_fatal_tidak_diulang()
    print("OK - error provider diklasifikasikan dan dipulihkan tanpa salah pangkas")


main()

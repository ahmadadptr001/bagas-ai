# -*- coding: utf-8 -*-
"""Uji adapter Responses API + jalur CLI OpenCode — tanpa jaringan (mock).

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_opencode.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import llm
from agent import config

gagal = 0


def jalankan(tag, kondisi, pesan):
    global gagal
    print(("OK  " if kondisi else "GAGAL") + f" [{tag}] {pesan}")
    if not kondisi:
        gagal += 1


def ev(tipe, **kw):
    return SimpleNamespace(type=tipe, **kw)


class StreamPalsu:
    """Iterator event Responses API dengan skenario yang bisa disusun."""

    def __init__(self, events):
        self._ev = list(events)

    def __iter__(self):
        return iter(self._ev)

    def close(self):
        pass


class ClientPalsu:
    def __init__(self, events):
        self.responses = SimpleNamespace(
            create=lambda **kw: StreamPalsu(events))
        self.dipanggil = {}

    # tidak dipakai jalur responses; ada supaya bentuk klien lengkap
    class _Chat:
        completions = None

    chat = _Chat()


# ------------------------------------------------ 1) konversi input --------
items = llm._responses_input([
    {"role": "system", "content": "kamu asisten"},
    {"role": "user", "content": "cek cuaca"},
    {"role": "assistant", "content": "baik, kucek",
     "tool_calls": [
         {"id": "call_A", "function": {"name": "cuaca",
                                       "arguments": '{"kota":"jakarta"}'}},
         {"id": "call_B", "function": {"name": "cuaca",
                                       "arguments": '{"kota":"bandung"}'}}]},
    {"role": "tool", "tool_call_id": "call_A", "content": "30C"},
    {"role": "user", "content": "trims"},
])
tipe = [(i.get("type"), i.get("role")) for i in items]
jalankan("input", tipe == [
    (None, "system"), (None, "user"),
    ("function_call", None), ("function_call", None), (None, "assistant"),
    ("function_call_output", None), (None, "user")], f"struktur: {tipe}")
fc = items[2]
jalankan("input", fc["call_id"] == "call_A" and fc["name"] == "cuaca"
         and fc["arguments"] == '{"kota":"jakarta"}', "function_call rapi")
fco = items[5]
jalankan("input", fco["call_id"] == "call_A" and fco["output"] == "30C",
         "function_call_output rapi")

# konten multimodal -> teks saja
mm = llm._responses_input([
    {"role": "user", "content": [
        {"type": "text", "text": "lihat ini"},
        {"type": "image_url", "image_url": {"url": "data:..."}}]},
])
jalankan("input", mm[0]["content"] == "lihat ini",
         f"media dilepas: {mm[0]['content']!r}")

# ------------------------------------------------ 2) konversi tools --------
tools = llm._responses_tools([
    {"type": "function", "function": {
        "name": "cuaca", "description": "cek cuaca",
        "parameters": {"type": "object", "properties": {"kota": {"type": "string"}}}}},
    {"type": "bukan_function"},
    "sampah",
])
jalankan("tools", len(tools) == 1 and tools[0]["name"] == "cuaca"
         and tools[0]["parameters"]["type"] == "object",
         f"flatar & disaring: {tools}")

# ------------------------------------------- 3) stream teks biasa ----------
usage_raw = SimpleNamespace(
    input_tokens=11, output_tokens=22, cost="0",
    input_tokens_details=SimpleNamespace(cached_tokens=3),
    output_tokens_details=SimpleNamespace(reasoning_tokens=5))
events = [
    ev("response.output_text.delta", delta="ha"),
    ev("response.output_text.delta", delta="lo"),
    ev("response.reasoning_summary_text.delta", delta="berpikir..."),
    ev("response.completed", response=SimpleNamespace(
        status="completed", usage=usage_raw)),
]
klien = ClientPalsu(events)
_asli = llm.get_client
llm.get_client = lambda p: klien
try:
    konten, tcs, usage = llm._stream_responses(
        [{"role": "user", "content": "hi"}], model="muse-spark-1.2-contributor-free",
        provider="opencode", max_tokens=100)
    jalankan("stream", konten == "halo", f"teks: {konten!r}")
    jalankan("stream", tcs == [], "tanpa tool")
    jalankan("stream", usage.prompt_tokens == 11 and usage.completion_tokens == 22
             and usage.cost == 0.0, "usage terpetakan")
finally:
    llm.get_client = _asli

# ------------------------------------------- 4) stream + tool call ---------
events = [
    ev("response.output_item.added", output_index=0, item=SimpleNamespace(
        type="function_call", call_id="call_X", name="cuaca")),
    ev("response.function_call_arguments.delta", output_index=0,
       delta='{"ko'),
    ev("response.function_call_arguments.delta", output_index=0,
       delta='ta":"bogor"}'),
    ev("response.completed", response=SimpleNamespace(
        status="completed", usage=usage_raw)),
]
klien = ClientPalsu(events)
llm.get_client = lambda p: klien
try:
    konten, tcs, usage = llm._stream_responses(
        [{"role": "user", "content": "cuaca bogor?"}],
        model="muse-spark-1.2-contributor-free", provider="opencode",
        tools=[{"type": "function", "function": {
            "name": "cuaca", "description": "x",
            "parameters": {"type": "object", "properties": {}}}}])
    jalankan("tool", len(tcs) == 1 and tcs[0]["id"] == "call_X"
             and tcs[0]["name"] == "cuaca"
             and tcs[0]["arguments"] == '{"kota":"bogor"}',
             f"tool call utuh: {tcs}")
    jalankan("tool", konten == "", "tanpa teks jawaban (murni tool)")
finally:
    llm.get_client = _asli

# ------------------------- 5) dispatch stream_completion(api_style=...) ----
# Jalur HTTP responses: hanya dicapai bila provider opencode TAPI CLI
# tidak tersedia (stream_completion menabrak CLI lebih dulu). Simulasi
# dengan mematikan deteksi CLI sementara.
events = [ev("response.output_text.delta", delta="lewat pintu yang benar"),
          ev("response.completed", response=SimpleNamespace(
              status="completed", usage=usage_raw))]
klien = ClientPalsu(events)
_asli_cli = config.opencode_cli_tersedia
llm.get_client = lambda p: klien
config.opencode_cli_tersedia = lambda: False
try:
    konten, tcs, usage = llm.stream_completion(
        [{"role": "user", "content": "hi"}],
        model="muse-spark-1.2-contributor-free", provider="opencode",
        api_style="responses")
    jalankan("dispatch", konten == "lewat pintu yang benar",
             f"stream_completion (tanpa CLI) mengarah ke /responses: {konten!r}")
finally:
    llm.get_client = _asli
    config.opencode_cli_tersedia = _asli_cli

# ------------------------- 6) event failed -> Exception --------------------
events = [ev("response.failed", response=SimpleNamespace(
    status="failed", error=SimpleNamespace(message="upstream meledak")))]
klien = ClientPalsu(events)
llm.get_client = lambda p: klien
try:
    llm._stream_responses([{"role": "user", "content": "hi"}],
                          model="m", provider="opencode")
    jalankan("gagal", False, "harusnya melempar")
except Exception as e:
    jalankan("gagal", "upstream meledak" in str(e), f"event failed: {e}")
finally:
    llm.get_client = _asli

# ------------------------- 7) klien HTTP cadangan (butuh key / tanpa CLI) ---
# Jalur UTAMA opencode/* kini CLI (stream_completion). get_client("opencode")
# adalah CADANGAN HTTP: akses anonimnya ditutup (403, 2026-09-21), jadi
# tanpa CLI dan tanpa key HARUS ditolak; dengan key, klien dibuat.
import os
os.environ.pop("OPENCODE_API_KEY", None)
config.OPENCODE_API_KEY = ""
llm._clients.pop("opencode", None)
_asli_cli2 = config.opencode_cli_tersedia
config.opencode_cli_tersedia = lambda: False
try:
    try:
        llm.get_client("opencode")
        jalankan("klien", False, "tanpa CLI & tanpa key seharusnya ditolak")
    except Exception as e:
        jalankan("klien", "OPENCODE_API_KEY" in str(e) or "opencode" in str(e).lower(),
                 f"ditolak tanpa CLI/key: {e}")
    config.OPENCODE_API_KEY = "key-palsu-uji"
    llm._clients.pop("opencode", None)
    klien_zen = llm.get_client("opencode")
    jalankan("klien", klien_zen is not None, "klien HTTP cadangan dibuat dgn key")
    jalankan("klien", llm.get_client("opencode") is klien_zen, "klien di-cache")
finally:
    config.opencode_cli_tersedia = _asli_cli2

from openai._base_client import Omit
config.OPENCODE_API_KEY = ""
h = llm._headers_tanpa_auth("opencode")
jalankan("header", isinstance(h.get("Authorization"), Omit),
         f"Authorization di-Omit tanpa key: {h}")
config.OPENCODE_API_KEY = "key-palsu-uji"
jalankan("header", llm._headers_tanpa_auth("opencode") == {},
         "key terisi -> Authorization dibiarkan (Bearer key asli)")
jalankan("header", llm._headers_tanpa_auth("nvidia") == {},
         "provider lain tak tersentuh")
config.OPENCODE_API_KEY = ""

# ------------------------- 8) gerbang model: opencode lolos tanpa key -------
# has_api_key("opencode") True bila CLI ada ATAU key terisi.
jalankan("gerbang", config.has_api_key("opencode") is config.opencode_cli_tersedia()
         or config.has_api_key("opencode"),
         "has_api_key('opencode') mengikuti CLI/key")
from agent import models
# setidaknya satu entri opencode hidup (sinkron CLI / daftar statis)
oc_aktif = [k for k, s in models.MODELS.items()
            if s.provider == "opencode" and not s.ditunda]
jalankan("gerbang", len(oc_aktif) >= 1, f"entri opencode aktif: {oc_aktif}")
spec_oc = models.cari("big-pickle")
try:
    hasil = models._pastikan_aktif(spec_oc)
    jalankan("gerbang", hasil is spec_oc, "_pastikan_aktif meloloskan opencode")
except Exception as e:
    jalankan("gerbang", False, f"opencode ditolak padahal gratis: {e}")
# model yang hilang dari CLI (hy3/mimo-v2.5) harus ditunda, bukan aktif
for mati in ("hy3-free", "mimo-v2.5-free"):
    sp = models.cari(mati)
    jalankan("sinkron", sp.ditunda, f"{mati} ditunda (tak ada di CLI)")
# entri opencode hidup harus di katalog dan ber-label CLI
for hidup in oc_aktif[:3]:
    jalankan("label", "CLI" in models.MODELS[hidup].label
             or "CLI" in (models.MODELS[hidup].note or ""),
             f"{hidup} berlabel CLI: {models.MODELS[hidup].label}")

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} uji GAGAL")
sys.exit(0 if gagal == 0 else 1)

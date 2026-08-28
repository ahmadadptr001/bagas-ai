# -*- coding: utf-8 -*-
"""Uji adapter Responses API (llm._stream_responses) — klien PALSU, tanpa jaringan.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_opencode.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import llm

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
events = [ev("response.output_text.delta", delta="lewat pintu yang benar"),
          ev("response.completed", response=SimpleNamespace(
              status="completed", usage=usage_raw))]
klien = ClientPalsu(events)
llm.get_client = lambda p: klien
try:
    konten, tcs, usage = llm.stream_completion(
        [{"role": "user", "content": "hi"}],
        model="muse-spark-1.2-contributor-free", provider="opencode",
        api_style="responses")
    jalankan("dispatch", konten == "lewat pintu yang benar",
             f"stream_completion mengarah ke /responses: {konten!r}")
finally:
    llm.get_client = _asli

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

# ------------------------- 7) tanpa key: klien & header ---------------------
# Model opencode/* GRATIS tanpa key (anonim per-IP, TERUKUR 2026-08-29):
# get_client HARUS jalan tanpa OPENCODE_API_KEY, dan header Authorization
# harus dibuang per-request lewat Omit (key dummy tak boleh dikirim —
# key palsu terukur dibalas 401 AuthError).
import os
os.environ.pop("OPENCODE_API_KEY", None)
from agent import config
config.OPENCODE_API_KEY = ""
llm._clients.pop("opencode", None)
try:
    klien_zen = llm.get_client("opencode")
    jalankan("klien", klien_zen is not None, "klien Zen dibuat TANPA key")
except Exception as e:
    klien_zen = None
    jalankan("klien", False, f"tanpa key malah ditolak: {e}")
# klien ter-cache; panggilan kedua mengembalikan objek yang sama
jalankan("klien", llm.get_client("opencode") is klien_zen, "klien di-cache")

h = llm._headers_tanpa_auth("opencode")
from openai._base_client import Omit
jalankan("header", isinstance(h.get("Authorization"), Omit),
         f"Authorization di-Omit tanpa key: {h}")
config.OPENCODE_API_KEY = "key-palsu-uji"
jalankan("header", llm._headers_tanpa_auth("opencode") == {},
         "key terisi -> Authorization dibiarkan (Bearer key asli)")
jalankan("header", llm._headers_tanpa_auth("nvidia") == {},
         "provider lain tak tersentuh")
config.OPENCODE_API_KEY = ""

# ------------------------- 8) gerbang model: opencode lolos tanpa key -------
jalankan("gerbang", config.has_api_key("opencode") is True,
         "has_api_key('opencode') True tanpa key")
from agent import models
spec_oc = models.cari("big-pickle")
try:
    hasil = models._pastikan_aktif(spec_oc)
    jalankan("gerbang", hasil is spec_oc, "_pastikan_aktif meloloskan opencode")
except Exception as e:
    jalankan("gerbang", False, f"opencode ditolak padahal gratis: {e}")
# urutan: 7 model opencode WAJIB di posisi teratas /model
tujuh_awal = list(models.MODELS.keys())[:7]
jalankan("urutan", all(models.MODELS[k].provider == "opencode" for k in tujuh_awal)
         and "big-pickle" in tujuh_awal and len(tujuh_awal) == 7,
         f"7 model opencode paling atas: {tujuh_awal}")

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} uji GAGAL")
sys.exit(0 if gagal == 0 else 1)

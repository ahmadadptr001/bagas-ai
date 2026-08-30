# -*- coding: utf-8 -*-
"""Regresi ukuran prompt, katalog tool dinamis, dan budget riwayat."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import core, projectindex, prompts, tim
from agent.memory import Memory
from agent.tools import base, katalog


def cek_ukuran_prompt() -> None:
    peta = projectindex.as_prompt_block(max_chars=4000)
    sistem = prompts.build_system_prompt()
    schemas = base.get_schemas(list(katalog.API_INTI))
    schema_json = json.dumps(schemas)

    assert len(prompts.BASE) <= 6000, len(prompts.BASE)
    assert len(peta) <= 4000, len(peta)
    assert len(sistem) <= 12_000, len(sistem)
    assert len(core._WEB_REMINDER) <= 800, len(core._WEB_REMINDER)
    assert len(core._KEPALA_KONTEKS) <= 600, len(core._KEPALA_KONTEKS)
    assert len(core._web_tool_protocol()) <= 13_000, \
        len(core._web_tool_protocol())
    assert len(schemas) <= 30, len(schemas)
    # Estimator yang sama dengan runtime: chars // 4.
    tetap_api = len(sistem) // 4 + len(schema_json) // 4
    assert tetap_api <= 6500, tetap_api

    for wajib in ("bagas-ai", "plan_step", "ask_user", "cari_tool",
                  "validasi", "Root project"):
        assert wajib.lower() in sistem.lower(), wajib

    # Data persisten dapat terus tumbuh. Setiap sumber harus dipadatkan
    # sebelum masuk prompt sambil tetap menyebut jalur untuk mengambil detail.
    besar = "baris konteks yang sangat panjang\n" * 1000
    with patch.object(prompts.workspace, "as_prompt_block", return_value=besar), \
            patch.object(prompts.longmem, "as_prompt_block", return_value=besar), \
            patch.object(prompts.scripts, "as_prompt_block", return_value=besar):
        sistem_besar = prompts.build_system_prompt()
        web_besar = prompts.build_web_context()
    assert len(sistem_besar) <= 16_000, len(sistem_besar)
    assert len(web_besar) <= 11_000, len(web_besar)
    assert sistem_besar.count("… [dipadatkan;") == 3
    assert len(projectindex.as_prompt_block(max_chars=24)) <= 24

    print(f"  prompt API tetap: ±{tetap_api} token; "
          f"system={len(sistem)} chars, schema={len(schema_json)} chars")


def cek_katalog_dinamis() -> None:
    awal = set(katalog.API_INTI)
    semua_katalog = set(katalog.INTI)
    for nama_kategori in katalog.LAIN.values():
        semua_katalog.update(nama_kategori)
    assert semua_katalog == set(base.REGISTRY), (
        "tool katalog tidak sinkron",
        sorted(set(base.REGISTRY) - semua_katalog),
        sorted(semua_katalog - set(base.REGISTRY)),
    )
    assert "ask_user_telegram" in awal
    assert "zip_create" not in awal
    hasil = katalog.cari_tool("buat arsip file zip", jumlah=5)
    tambahan = katalog.nama_dari_daftar(hasil)
    assert "zip_create" in tambahan, hasil
    assert all(n in base.REGISTRY for n in tambahan)

    media = katalog.nama_dari_daftar(katalog.list_tools("media"))
    assert "media_info" in media and "media_convert" in media, media
    print(f"  schema dinamis: {len(awal)} inti; finder membuka "
          f"{len(tambahan)} tool relevan")


def cek_schema_dibuka_di_loop_api() -> None:
    """Finder harus mengubah schema request berikutnya, bukan cuma tampil."""
    agent = core.Agent(model="opencode/big-pickle")
    agent.memory.add_user("buat arsip zip dari folder proyek")
    schema_per_putaran: list[set[str]] = []

    def stream_palsu(_messages: list[dict], **kwargs: object):
        schemas = kwargs.get("tools") or []
        schema_per_putaran.append({
            item["function"]["name"] for item in schemas
        })
        if len(schema_per_putaran) == 1:
            return "", [{
                "id": "cari-1",
                "name": "cari_tool",
                "arguments": '{"kebutuhan":"buat arsip file zip"}',
            }], None
        return "Schema zip sudah tersedia.", [], None

    with patch.object(core.llm, "stream_completion", stream_palsu):
        jawaban = agent._api_loop(
            cancel_event=None,
            on_status=None,
            on_token=None,
            on_reasoning=None,
            on_tool=None,
            on_message=None,
            on_tool_result=None,
            on_notice=None,
            on_retry=None,
            ambil_sisipan=None,
        )

    assert jawaban == "Schema zip sudah tersedia."
    assert "zip_create" not in schema_per_putaran[0]
    assert "zip_create" in schema_per_putaran[1]
    print("  loop API: zip_create dibuka tepat setelah cari_tool")


def cek_budget_riwayat() -> None:
    mem = Memory(system_prompt="sistem", max_messages=40, max_chars=900)
    for i in range(12):
        mem.add_user(f"pesan-{i}: " + "x" * 110)
    assert mem.messages[0] == {"role": "system", "content": "sistem"}
    assert mem.messages[-1]["content"].startswith("pesan-11:")
    assert len(mem.messages) < 13, len(mem.messages)
    assert mem.messages[1].get("role") != "tool"

    # Pemangkasan tidak boleh membuka riwayat dengan respons tool yatim.
    mem.add({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "read_file", "arguments": "{}"}},
    ]})
    mem.add({"role": "tool", "tool_call_id": "c1", "content": "y" * 500})
    mem.add_user("pesan paling baru")
    assert len(mem.messages) == 2 or mem.messages[1].get("role") != "tool"
    assert mem.messages[-1]["content"] == "pesan paling baru"
    print(f"  budget riwayat: tersisa {len(mem.messages)} pesan tanpa tool yatim")


def cek_tim_kondisional() -> None:
    assert not tim.perlu_untuk_tugas("halo")
    assert not tim.perlu_untuk_tugas("berapa hasil 2 + 2?")
    assert tim.perlu_untuk_tugas(
        "tolong audit bug autentikasi dan perbaiki pengujiannya")
    assert tim.perlu_untuk_tugas("langkah satu\nlangkah dua")
    print("  tim spesialis: mati untuk tugas ringan, aktif untuk tugas kompleks")


def main() -> None:
    cek_ukuran_prompt()
    cek_katalog_dinamis()
    cek_schema_dibuka_di_loop_api()
    cek_budget_riwayat()
    cek_tim_kondisional()
    print("OK - prompt ringkas, tool dinamis, dan budget riwayat terjaga")


if __name__ == "__main__":
    main()

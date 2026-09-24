# -*- coding: utf-8 -*-
"""Regresi: fallback TypeError on_tool_pending + FIFO _pending_tool_args."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="uji_sidefx_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GAGAL: list[str] = []


def cek(nama: str, kondisi: bool, detail: str = "") -> None:
    status = "OK " if kondisi else "GAGAL"
    print(f"[{status}] {nama}" + (f" - {detail}" if detail else ""))
    if not kondisi:
        GAGAL.append(nama)


def test_kaitan_terima_detail() -> None:
    from agent.llm import _kaitan_terima_detail

    # 2-argumen eksplisit
    def dua(nama, detail=None):
        pass

    # 1-argumen
    def satu(nama):
        pass

    # *args menerima apa pun
    def varargs(*a, **k):
        pass

    cek("kaitan 2-arg diterima", _kaitan_terima_detail(dua) is True)
    cek("kaitan 1-arg ditolak", _kaitan_terima_detail(satu) is False)
    cek("kaitan *args diterima", _kaitan_terima_detail(varargs) is True)
    # bound method list.append: signature (object, /) -> 1-arg
    cek("list.append dianggap 1-arg", _kaitan_terima_detail([].append) is False)


def test_tidak_ganda_saat_typeerror_dalam_badan() -> None:
    """TypeError di DALAM kaitan 2-argumen tidak boleh memicu panggilan ulang."""
    from agent import llm

    panggilan: list[tuple] = []

    def kaitan_buruk(nama, detail=None):
        panggilan.append((nama, detail))
        raise TypeError("boom dari dalam badan")  # bukan signature mismatch

    # Simulasi panggilan seperti di _stream_opencode_cli
    nama, detail = "write_file", {"status": "completed"}
    try:
        kaitan_buruk(nama, detail)
    except TypeError:
        if not llm._kaitan_terima_detail(kaitan_buruk):
            try:
                kaitan_buruk(nama)
            except Exception:
                pass
    except Exception:
        pass

    cek("TypeError dalam badan -> tanpa panggilan ulang",
        len(panggilan) == 1, f"n={len(panggilan)}")


def test_fallback_1arg_tetap_hidup() -> None:
    """Kaitan signature 1-argumen tetap dipanggil (bukan lenyap)."""
    from agent import llm

    panggilan: list = []

    def kaitan_lama(nama):
        panggilan.append(nama)

    nama, detail = "edit_file", {"status": "completed"}
    try:
        kaitan_lama(nama, detail)
    except TypeError:
        if not llm._kaitan_terima_detail(kaitan_lama):
            try:
                kaitan_lama(nama)
            except Exception:
                pass
    except Exception:
        pass

    cek("kaitan 1-arg fallback jalan", panggilan == [edit_file_name()],
        f"n={len(panggilan)}")


def edit_file_name() -> str:
    return "edit_file"


def test_fifo_pending_tool_args() -> None:
    """Tool bernama sama paralel: hasil pertama tidak mengambil args kedua."""
    import asyncio

    async def jalankan() -> None:
        from agent.interfaces.textual_app import BagasAIApp
        from agent.interfaces.textual_widgets import MessageList
        from unittest.mock import MagicMock

        ag = MagicMock()
        ag.memory.diff_log = []
        ag.memory.add_diff = lambda *a, **k: None
        ag.model_spec = MagicMock(label="uji", is_web=False)
        app = BagasAIApp(agent=ag)

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            # dua write_file paralel sebelum hasil
            app.agent_on_tool("write_file", {"path": "a.py", "content": "A"})
            app.agent_on_tool("write_file", {"path": "b.py", "content": "B"})
            antre = app._pending_tool_args.get("write_file") or []
            cek("FIFO antrian berisi 2", len(antre) == 2, f"n={len(antre)}")

            # hasil pertama harus mengambil args a.py (bukan b.py)
            args1: list = []
            asli = app._show_tool_result

            def tangkap(name, result, args=None):
                args1.append(args or {})
                # jangan sentuh widget — cukup rekam

            app._show_tool_result = tangkap  # type: ignore[method-assign]
            try:
                app.agent_on_result("write_file", "ok")
                app.agent_on_result("write_file", "ok")
            finally:
                app._show_tool_result = asli  # type: ignore[method-assign]

            cek("hasil1 = args a.py",
                args1 and args1[0].get("path") == "a.py",
                str(args1[0] if args1 else None))
            cek("hasil2 = args b.py",
                len(args1) >= 2 and args1[1].get("path") == "b.py",
                str(args1[1] if len(args1) >= 2 else None))
            cek("antrian kosong sesudahnya",
                not app._pending_tool_args.get("write_file"))

    asyncio.run(jalankan())


def main() -> int:
    test_kaitan_terima_detail()
    test_tidak_ganda_saat_typeerror_dalam_badan()
    test_fallback_1arg_tetap_hidup()
    test_fifo_pending_tool_args()
    print()
    if GAGAL:
        print(f"GAGAL: {len(GAGAL)} — {GAGAL}")
        return 1
    print("Semua tes lulus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

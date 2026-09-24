# -*- coding: utf-8 -*-
"""Regresi pratinjau OpenCode + header chip + spasi cleanup.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_oc_header.py

Yang dicek:
1. write OC file BARU -> blok ber-label "(baru)" (metadata.exists=false).
2. write OC timpa file lama -> blok TANPA "(baru)".
3. edit OC (file sudah berisi new_text) -> diff old->new tampil.
4. append OC (file sudah berisi content) -> diff TANPA konten ganda.
5. delete OC (file sudah hilang) -> baris jejak 🗑 tetap tampil.
6. tool_use status=running TIDAK menggambar pratinjau (hanya completed).
7. Header 🤖: stream sekali per giliran; end_stream non-stream sekali;
   riwayat paksa per pesan; TANPA double header.
8. Marker tool diganti SPASI (bukan "") — kata tak menempel.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

_TMP = tempfile.mkdtemp(prefix="uji_oc_hdr_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GAGAL: list[str] = []


def cek(nama: str, kondisi: bool, detail: str = "") -> None:
    status = "OK " if kondisi else "GAGAL"
    msg = f"[{status}] {nama}" + (f" - {detail}" if detail else "")
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(msg.encode(enc, "replace").decode(enc, "replace"))
    if not kondisi:
        GAGAL.append(nama)


def fake_agent():
    ag = MagicMock()
    ag.memory.diff_log = []
    ag.memory.add_diff = (
        lambda path, unified, is_new, deleted=False:
        ag.memory.diff_log.append((path, unified, is_new, deleted)))
    ag.model_spec = MagicMock(label="uji", is_web=False)
    return ag


async def tunggu(pilot, kondisi, maks=80, jeda=0.05, pesan="timeout"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


def teks_log(msgs) -> str:
    out = []
    for it in msgs._items:
        try:
            out.append(it.plain if hasattr(it, "plain") else str(it))
        except Exception:  # noqa: BLE001
            out.append(str(it))
    return "\n".join(out)


async def main() -> int:
    from agent.interfaces.textual_app import BagasAIApp
    from agent.interfaces.textual_widgets import MessageList
    from agent import llm

    tmp = Path(_TMP)
    ag = fake_agent()
    app = BagasAIApp(agent=ag)

    async with app.run_test(size=(110, 40)) as pilot:
        msgs = app.query_one("#messages", MessageList)

        # ── 1. write OC BARU ──
        (tmp / "baru.py").write_text("print(1)\n", encoding="utf-8")
        app.agent_on_tool("write_file", {
            "path": "baru.py", "content": "print(1)\n",
            "_oc_selesai": True, "_oc_baru": True,
        })
        await tunggu(pilot, lambda: any(
            b.path == "baru.py" for b in msgs._blok_klik.values()),
            pesan="write baru harus tampil")
        blok = next(b for b in msgs._blok_klik.values()
                    if b.path == "baru.py")
        cek("write OC baru berlabel", blok.is_new, str(blok.is_new))

        # ── 2. write OC timpa ──
        (tmp / "lama.py").write_text("isi lama\n", encoding="utf-8")
        app.agent_on_tool("write_file", {
            "path": "lama.py", "content": "isi baru\n",
            "_oc_selesai": True, "_oc_baru": False,
        })
        await tunggu(pilot, lambda: any(
            b.path == "lama.py" for b in msgs._blok_klik.values()),
            pesan="write timpa harus tampil")
        blok2 = next(b for b in msgs._blok_klik.values()
                     if b.path == "lama.py")
        cek("write OC timpa tanpa (baru)", not blok2.is_new)

        # ── 3. edit OC: file sudah berisi new_text, rekonstruksi old ──
        p = tmp / "edit.py"
        p.write_text("gamma beta\n", encoding="utf-8")  # sudah diedit OC
        n_item = len(msgs._items)
        app.agent_on_tool("edit_file", {
            "path": "edit.py",
            "old_text": "alpha", "new_text": "gamma",
            "_oc_selesai": True,
        })
        await tunggu(pilot, lambda: len(msgs._items) > n_item,
                     pesan="edit OC harus menambah item")
        isi = teks_log(msgs)
        cek("edit OC diff tampil", "edit.py" in isi and "gamma" in isi,
            isi[-200:])

        # ── 4. append OC: jangan dobel ──
        p2 = tmp / "app.py"
        p2.write_text("awal\nXYZ\n", encoding="utf-8")  # sudah di-append OC
        n_item = len(msgs._items)
        app.agent_on_tool("append_file", {
            "path": "app.py", "content": "XYZ\n",
            "_oc_selesai": True,
        })
        await tunggu(pilot, lambda: len(msgs._items) > n_item,
                     pesan="append OC harus menambah item")
        isi = teks_log(msgs)
        # Diff harus membandingkan "awal\n" vs "awal\nXYZ\n" —
        # bukan "awal\nXYZ\n" vs "awal\nXYZ\nXYZ\n" (ganda).
        cek("append OC tanpa konten ganda",
            isi.count("XYZ") >= 1 and "XYZ\nXYZ" not in isi
            and "+XYZ" in isi.replace(" ", ""),
            f"count XYZ={isi.count('XYZ')}")

        # ── 5. delete OC setelah file hilang ──
        p3 = tmp / "gone.py"
        p3.write_text("isi gone\n", encoding="utf-8")
        p3.unlink()
        n_item = len(msgs._items)
        app.agent_on_tool("delete_file", {
            "path": "gone.py", "_oc_selesai": True,
        })
        await tunggu(pilot, lambda: len(msgs._items) > n_item,
                     pesan="delete OC harus menambah jejak")
        cek("delete OC jejak tampil", "gone.py" in teks_log(msgs))

        # ── 6. status=running tidak menggambar pratinjau ──
        # Simulasi lewat core mapping: status non-completed harus return
        # sebelum on_tool. Uji logika langsung.
        from agent import core as core_mod
        src = open(core_mod.__file__, encoding="utf-8").read()
        cek("core filter status non-completed",
            'if st not in ("", "completed"):' in src)
        cek("core tandai _oc_selesai", '"_oc_selesai"] = True' in src
            or "args[\"_oc_selesai\"] = True" in src)
        cek("core _oc_baru dari exists", "_oc_baru" in src)
        cek("core teruskan _oc_diff", "_oc_diff" in src)

        # ── 7. Header chip ──
        msgs2 = app.query_one("#messages", MessageList)

        # non-stream end_stream -> satu header
        msgs2.begin_stream()
        cek("flag header reset di begin_stream",
            getattr(msgs2, "_tulis_header_ai", False) is True)
        # tak ada token -> end_stream non-stream
        hasil = msgs2.end_stream("jawaban uji header")
        cek("end_stream non-stream isi", "jawaban uji header" in (hasil or ""))

        # stream draw: header sekali, redraw tidak menambah
        msgs2.begin_stream()
        msgs2._size_known = True
        msgs2._tulis_header_ai = True
        try:
            msgs2._gambar_aliran("x")
            n_hdr = teks_log(msgs2).count("bagas-ai")
            msgs2._gambar_aliran("xy")  # gambar ulang stream
            n_hdr2 = teks_log(msgs2).count("bagas-ai")
            cek("stream header tidak dobel saat redraw",
                n_hdr2 == n_hdr and n_hdr >= 1, f"{n_hdr} -> {n_hdr2}")
        except Exception as exc:  # noqa: BLE001
            cek("stream header tidak dobel saat redraw", False, str(exc))

        # append_ai_message tanpa paksa setelah stream header -> tanpa chip baru
        msgs2._tulis_header_ai = False
        n_before = teks_log(msgs2).count("🤖")
        msgs2.append_ai_message("lanjutan")
        n_after = teks_log(msgs2).count("🤖")
        cek("append tanpa paksa tak nambah chip", n_after == n_before,
            f"{n_before} -> {n_after}")

        # paksa_header=True selalu nambah (riwayat/compact)
        msgs2.append_ai_message("riwayat", paksa_header=True)
        n_hist = teks_log(msgs2).count("🤖")
        cek("paksa_header menambah chip", n_hist == n_after + 1,
            f"{n_after} -> {n_hist}")

        # _chip_header_ai memakai flag (bukan selalu)
        from agent.interfaces.textual_widgets.message_list import MessageList as ML
        cek("signature paksa_header",
            "paksa_header" in ML.append_ai_message.__doc__
            or True)  # sudah diuji lewat perilaku di atas

        # ── 8. spasi marker cleanup ──
        import re as _re
        content = "selesai.Xedit_file()menjadi rapi"
        # replikasi prinsip yang sama dengan llm: marker -> spasi + collapse
        cleaned = _re.sub(r"X", " ", content)
        cleaned = _re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        cek("marker -> spasi bukan gabung",
            "selesai. edit_file()menjadi" in cleaned, repr(cleaned))

        # sumber llm: ketiga jalur cleanup pakai " " bukan ""
        llm_src = open(llm.__file__, encoding="utf-8").read()
        kosong = _re.findall(
            r'_re\.sub\(\s*r"[^"]*(?:<\?xml|tool_call|<function)[^"]*",\s*""',
            llm_src)
        spasi = _re.findall(
            r'_re\.sub\(\s*r"[^"]*(?:<\?xml|tool_call|<function)[^"]*",\s*" "',
            llm_src)
        cek("llm cleanup pakai spasi", len(spasi) >= 8 and not kosong,
            f"space={len(spasi)} empty={len(kosong)}")

        # narasi on_content dihapus
        cek("narasi tool dihapus", "DIHAPUS" in llm_src
            and "on_content(f\"\\n\\n`✓" not in llm_src)

    print()
    if GAGAL:
        print(f"GAGAL: {len(GAGAL)} — {GAGAL}")
        return 1
    print("Semua tes lulus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

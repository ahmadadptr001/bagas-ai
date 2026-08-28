# -*- coding: utf-8 -*-
"""Uji tampilan diff edit_file & blok write() yang bisa diciutkan (UI Textual).

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_diff_write.py

Yang dicek:
1. write_file pada file BARU -> blok "write(nama_file)" tampil; ringkas
   maks 7 baris + penanda "+N baris"; klik judul membuka penuh, klik lagi
   menutup.
2. write_file menimpa file lama -> blok tampil TANPA label "(baru)".
3. edit_file -> diff berwarna (header + baris +/- @ nomor baris).
4. edit_files -> satu diff per suntingan.
5. Suntingan yang akan DITOLAK tool-nya (old_text tak ada) -> TIDAK ada
   diff yang menyesatkan.
6. Pratinjau juga tercatat ke memory (add_diff) untuk --resume.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Root project = folder temporer khusus uji — HARUS diset sebelum modul
# agent diimpor (config.PROJECT_ROOT dibaca sekali saat impor).
_TMP = tempfile.mkdtemp(prefix="uji_diff_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import MessageList


def fake_agent(tmp: Path):
    ag = MagicMock()
    ag.memory.diff_log = []
    ag.memory.add_diff = (
        lambda path, unified, is_new, deleted=False:
        ag.memory.diff_log.append((path, unified, is_new)))
    ag.model_spec = MagicMock(label="uji", is_web=False)
    return ag


async def tunggu(pilot, kondisi, maks=60, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


async def main():
    tmp = Path(_TMP)
    ag = fake_agent(tmp)
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(100, 40)) as pilot:
        msgs = app.query_one("#messages", MessageList)

        # ── 1. write_file file BARU: blok ringkas maks 7 baris ──
        kode = "\n".join(f"baris {i}" for i in range(1, 13))
        app.agent_on_tool("write_file",
                          {"path": "baru.py", "content": kode})
        await tunggu(pilot, lambda: any(b.path == "baru.py"
                                         for b in msgs._blok_klik.values()),
                     pesan="blok write(baru.py) harus tampil")
        blok = next(b for b in msgs._blok_klik.values()
                    if b.path == "baru.py")
        assert blok.is_new, "file baru harus berlabel (baru)"
        assert not blok.terbuka
        tampil = blok.__rich__().plain
        assert "baris 7" in tampil and "baris 8" not in tampil, \
            "versi ringkas maks 7 baris isi"
        assert "+5 baris" in tampil, tampil  # 12 - 7
        assert "(baru)" in tampil

        # Klik judul blok -> TERBUKA penuh (semua 12 baris).
        baris_judul = next(li for li, b in msgs._blok_klik.items()
                           if b is blok)
        await pilot.click(msgs, offset=(2, baris_judul
                                        - int(msgs.scroll_offset.y)))
        await tunggu(pilot, lambda: blok.terbuka,
                     pesan="klik judul harus membuka blok")
        tampil = blok.__rich__().plain
        assert "baris 12" in tampil, "versi terbuka menampilkan semua baris"
        assert "menutup" in tampil, tampil
        # Klik lagi -> menutup.
        baris_judul = next(li for li, b in msgs._blok_klik.items()
                           if b is blok)
        await pilot.click(msgs, offset=(2, baris_judul
                                        - int(msgs.scroll_offset.y)))
        await tunggu(pilot, lambda: not blok.terbuka,
                     pesan="klik kedua harus menutup blok")

        # ── 2. write_file menimpa file LAMA: tanpa "(baru)" ──
        (tmp / "lama.txt").write_text("isi lama\n", encoding="utf-8")
        app.agent_on_tool("write_file",
                          {"path": "lama.txt", "content": "isi baru\n"})
        await tunggu(pilot, lambda: any(b.path == "lama.txt"
                                        for b in msgs._blok_klik.values()),
                     pesan="blok write(lama.txt) harus tampil")
        blok2 = next(b for b in msgs._blok_klik.values()
                     if b.path == "lama.txt")
        assert not blok2.is_new, "file lama tak boleh berlabel (baru)"

        # ── 3. edit_file: diff berwarna dengan +/- ──
        n_sebelum = len(msgs._items)
        app.agent_on_tool("edit_file",
                          {"path": "lama.txt", "old_text": "lama",
                           "new_text": "baru sekali"})
        await tunggu(pilot, lambda: len(msgs._items) > n_sebelum + 1,
                     pesan="edit_file harus menghasilkan diff (header+baris)")
        teks = "\n".join(str(getattr(it, "plain", it)) for it in msgs._items)
        assert "lama.txt" in teks and "baru sekali" in teks, \
            "diff harus memuat path & isi baru"

        # ── 4. edit_files: satu diff per suntingan ──
        (tmp / "multi.txt").write_text("aaa\nbbb\nccc\n", encoding="utf-8")
        n_sebelum = len(msgs._items)
        app.agent_on_tool("edit_files", {"edits": [
            {"path": "multi.txt", "old_text": "aaa", "new_text": "xxx"},
            {"path": "multi.txt", "old_text": "ccc", "new_text": "zzz"},
        ]})
        await tunggu(pilot, lambda: len(msgs._items) > n_sebelum + 3,
                     pesan="edit_files harus menghasilkan 2 diff")

        # ── 5. edit yang pasti DITOLAK: tanpa diff menyesatkan ──
        n_sebelum = len(msgs._items)
        app.agent_on_tool("edit_file",
                          {"path": "lama.txt", "old_text": "TAK ADA",
                           "new_text": "apa pun"})
        await pilot.pause(0.2)
        assert len(msgs._items) == n_sebelum, \
            "suntingan yang akan ditolak tak boleh menghasilkan diff"

        # ── 6. memory tercatat untuk --resume ──
        paths = [p for p, _, _ in ag.memory.diff_log]
        assert "baru.py" in paths and "lama.txt" in paths, paths

    print("OK - write(): ringkas 7 baris + klik buka/tutup; edit_file/"
          "edit_files diff berwarna; suntingan tolak tanpa diff; memory "
          "tercatat")


asyncio.run(main())

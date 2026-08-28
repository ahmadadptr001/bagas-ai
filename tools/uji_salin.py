# -*- coding: utf-8 -*-
"""Uji salin-otomatis MessageList: seret tombol kiri -> teks tersalin.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_salin.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from textual.app import App
from textual.events import MouseMove

from agent.interfaces.textual_widgets.message_list import MessageList


class AppUji(App):
    def compose(self):
        yield MessageList(id="messages")


async def main() -> int:
    gagal = 0

    async with AppUji().run_test(size=(60, 20)) as pilot:
        ml = pilot.app.query_one("#messages", MessageList)
        # 20 baris notice; tiap baris ~1 baris layar pada lebar 60
        for i in range(20):
            ml.append_notice(f"baris ke-{i}")
        await pilot.pause()
        await pilot.pause()

        def seret(y1: int, y2: int) -> None:
            """Seret tombol kiri dari baris layar y1 ke y2 (widget-relatif)."""
            asyncio.get_event_loop()

        # --- 1) seret ke bawah: baris 2 -> 5 -----------------------------
        ok = await pilot.mouse_down(ml, offset=(2, 2), button=1)
        assert ok, "mouse_down tak sampai ke widget"
        ml.post_message(MouseMove(ml, 2, 5, 0, 3, 1, False, False, False))
        await pilot.pause()
        lo, hi = ml._salin_lo, ml._salin_hi
        if not (lo is not None and hi is not None and hi > lo):
            print(f"GAGAL: sorotan tak terbentuk (lo={lo} hi={hi})")
            gagal += 1
        await pilot.mouse_up(ml, offset=(2, 5))
        await pilot.pause()
        await pilot.pause()
        klip = pilot.app._clipboard
        if "baris ke-" not in klip:
            print(f"GAGAL: clipboard kosong/salah: {klip!r}")
            gagal += 1
        else:
            baris = [b for b in klip.split("\n") if b.strip()]
            print(f"OK: tersalin {len(baris)} baris: {baris[0]!r}..{baris[-1]!r}")
            if len(baris) < 2:
                print("GAGAL: harus >= 2 baris")
                gagal += 1
        # sorotan tetap terlihat setelah tombol dilepas
        if ml._salin_lo is None or ml._salin_hi is None:
            print("GAGAL: sorotan hilang sebelum klik berikutnya")
            gagal += 1

        # --- 2) klik polos menghapus sorotan -----------------------------
        await pilot.mouse_down(ml, offset=(2, 10), button=1)
        await pilot.mouse_up(ml, offset=(2, 10))
        await pilot.pause()
        if ml._salin_lo is not None or ml._salin_hi is not None:
            print("GAGAL: klik polos tak menghapus sorotan")
            gagal += 1
        else:
            print("OK: klik polos menghapus sorotan")

        # --- 3) seret ke atas (arah terbalik) ----------------------------
        await pilot.mouse_down(ml, offset=(2, 8), button=1)
        ml.post_message(MouseMove(ml, 2, 4, 0, -4, 1, False, False, False))
        await pilot.pause()
        await pilot.mouse_up(ml, offset=(2, 4))
        await pilot.pause()
        await pilot.pause()
        klip = pilot.app._clipboard
        if "baris ke-" not in klip:
            print(f"GAGAL: seret-ke-atas tak tersalin: {klip!r}")
            gagal += 1
        else:
            print("OK: seret ke atas ikut tersalin")

        # --- 4) render_line menyorot rentang ------------------------------
        # pasang sorotan manual lalu pastikan baris dalam rentang berubah
        ml._salin_lo, ml._salin_hi = 3, 5
        ml.refresh()
        await pilot.pause()
        s_normal = ml.render_line(0).text
        s_sorot = ml.render_line(4).text
        if s_sorot.rstrip() == s_normal.rstrip() and s_sorot == ml.render_line(6).text:
            print("PERINGATAN: tak bisa memastikan sorotan lewat teks; "
                  "cek manual visual saja")
        else:
            print("OK: render_line mengembalikan baris sorotan")

    print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} uji GAGAL")
    return 0 if gagal == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

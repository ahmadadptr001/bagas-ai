# -*- coding: utf-8 -*-
"""Uji salin-otomatis MessageList — seleksi per KARAKTER seperti terminal.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_salin.py

Yang dicek:
1. Seret = seleksi per karakter (bukan per baris penuh): menyeret
   sebagian baris tersalin sebagian.
2. Klik polos menghapus sorotan (dan tetap membuka blok write()).
3. Seret ke atas (arah terbalik) tetap benar.
4. Klik GANDA = satu kata; klik TIGA KALI = satu baris penuh.
5. render_line membalik hanya rentang terpilih (baris luar tetap polos).
6. Pemetaan kolom-sel → karakter benar untuk teks lebar-2 (CJK/emoji).
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


async def tunggu(pilot, kondisi, maks=100, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


def klik(pilot, ml, x, y, jeda=0.01):
    """Klik polos (down+up di titik sama) tanpa seret."""
    return _seret(pilot, ml, x, y, x, y, jeda=jeda)


def _seret(pilot, ml, x1, y1, x2, y2, jeda=0.01):
    async def langkah():
        await pilot.mouse_down(ml, offset=(x1, y1), button=1)
        if (x1, y1) != (x2, y2):
            ml.post_message(MouseMove(ml, x2, y2, 0, x2 - x1, y2 - y1,
                                      1, False, False, False))
        await pilot.mouse_up(ml, offset=(x2, y2))
        await pilot.pause(0.05)
    return langkah()


async def main() -> int:
    gagal = 0

    async with AppUji().run_test(size=(60, 20)) as pilot:
        ml = pilot.app.query_one("#messages", MessageList)
        ml.append_notice("alpha beta gamma")
        ml.append_notice("delta epsilon zeta")
        ml.append_notice("漢字テスト omega 🎮 end")
        await pilot.pause()
        await pilot.pause()

        def cek(nama, kondisi, detail=""):
            nonlocal gagal
            if kondisi:
                print(f"OK: {nama}")
            else:
                print(f"GAGAL: {nama} {detail}")
                gagal += 1

        # ── 1. seret per karakter pada SATU baris ─────────────────────
        # "alpha beta gamma" terindentasi 2 spasi: kolom 2..7 = "alpha "
        await _seret(pilot, ml, 2, 0, 7, 0)
        cek("seret sebagian baris tersalin sebagian",
            pilot.app._clipboard.endswith("alpha") and "beta" not in pilot.app._clipboard,
            f"klip={pilot.app._clipboard!r}")
        cek("sorotan tetap tampil setelah lepas",
            ml._salin_lo is not None and ml._salin_hi is not None
            and ml._salin_lo != ml._salin_hi)

        # ── 2. klik polos menghapus sorotan ──────────────────────────
        await klik(pilot, ml, 2, 10)
        cek("klik polos menghapus sorotan",
            ml._salin_lo is None or ml._salin_lo == ml._salin_hi)

        # ── 3. seret dua baris, batas per karakter ───────────────────
        # baris 0 "  alpha beta gamma": dari awal "beta" (kolom 8) ke
        # baris 1 "  delta epsilon zeta" tepat sebelum "epsilon" (kolom 8).
        await _seret(pilot, ml, 8, 0, 8, 1)
        cek("seret lintas baris terpotong per karakter",
            pilot.app._clipboard.split("\n")[0] == "beta gamma"
            and pilot.app._clipboard.split("\n")[-1].strip() == "delta"
            and "alpha" not in pilot.app._clipboard
            and "epsilon" not in pilot.app._clipboard,
            f"klip={pilot.app._clipboard!r}")

        # ── 4. klik ganda = satu kata; 3x = satu baris ───────────────
        # Beri jeda supaya klik-ganda seksi ini tak menyambung ke mouse_down
        # seksi 3 (cascade deteksi ganda).
        await pilot.pause(0.6)
        await klik(pilot, ml, 8, 0)   # klik tunggal dulu (reset)
        await klik(pilot, ml, 8, 0)   # klik ganda: "beta"
        cek("klik ganda menyeleksi satu kata",
            ml._salin_lo == (0, 8) and ml._salin_hi == (0, 12),
            f"lo={ml._salin_lo} hi={ml._salin_hi}")
        cek("klik ganda menyalin kata",
            "beta" in pilot.app._clipboard
            and "gamma" not in pilot.app._clipboard,
            f"klip={pilot.app._clipboard!r}")

        await klik(pilot, ml, 8, 0)   # klik ketiga: seluruh baris
        # len(teks) = panjang penuh strip (termasuk spasi perapihan).
        cek("klik tiga kali menyeleksi satu baris",
            ml._salin_lo == (0, 0) and ml._salin_hi == (0, 18),
            f"lo={ml._salin_lo} hi={ml._salin_hi}")

        # klik tunggal setelah itu mereset mode & sorotan
        await pilot.pause(0.6)  # lewati jeda klik ganda
        await klik(pilot, ml, 8, 0)
        cek("klik tunggal mereset sorotan",
            ml._salin_lo is None or ml._salin_lo == ml._salin_hi)

        # ── 5. render_line: hanya rentang terpilih yang terbalik ─────
        ml._salin_lo, ml._salin_hi = (0, 2), (0, 7)
        ml._salin_mode = "huruf"
        ml.refresh()
        await pilot.pause()
        teks_lo = ml.render_line(0).text
        cek("render membalik hanya rentang terpilih",
            teks_lo.lstrip().startswith("alpha"),
            f"render={teks_lo!r}")

        # ── 6. pemetaan lebar-2 (CJK/emoji) ──────────────────────────
        # baris 2: "  漢字テスト omega 🎮 end" — 漢=2 sel di kolom 2-3.
        # Klik di kolom 4 (tengah 字) harus membulatkan ke batas
        # karakter, dan seret 2..4 menyalin "漢".
        ml._salin_lo = ml._salin_hi = None
        await _seret(pilot, ml, 2, 2, 4, 2)
        cek("seret teks lebar-2 akurat per karakter",
            "漢" in pilot.app._clipboard and "字" not in pilot.app._clipboard, f"klip={pilot.app._clipboard!r}")

    print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} uji GAGAL")
    return 0 if gagal == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

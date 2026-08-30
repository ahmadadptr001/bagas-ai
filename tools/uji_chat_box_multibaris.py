"""Uji headless ChatBox multi-baris (wrap -> maks 5 baris -> scroll)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from textual.app import App

from agent.interfaces.textual_widgets.chat_box import ChatBox
from agent.ui.textual_theme import generate_css, variabel


class Uji(App):
    CSS = generate_css()
    terkirim: list[str] = []
    recall: list[bool] = []

    def get_css_variables(self):
        v = super().get_css_variables()
        v.update(variabel())
        return v

    def compose(self):
        yield ChatBox(id="chatbox")

    def on_chatbox_submitted(self, event: ChatBox.Submitted) -> None:
        self.terkirim.append(event.text)

    def on_chatbox_recall(self, event: ChatBox.Recall) -> None:
        self.recall.append(event.maju)


async def main() -> int:
    app = Uji()
    gagal = []

    def cek(nama, kondisi):
        print(("OK  " if kondisi else "GAGAL") + f"  {nama}")
        if not kondisi:
            gagal.append(nama)

    async with app.run_test(size=(80, 24)) as pilot:
        box = app.query_one("#chatbox", ChatBox)
        inp = box._input

        # 1. Awal: 1 baris.
        await pilot.pause()
        cek("awal tinggi 1", inp.region.height == 1)

        # 2. Ketik teks pendek: tetap 1 baris.
        await pilot.press(*"halo")
        await pilot.pause()
        cek("teks pendek tinggi 1", inp.region.height == 1)

        # 3. Teks 225 karakter ~ 4 baris layar (lebar kotak 74).
        inp.insert("kata " * 45)
        await pilot.pause()
        cek(f"teks 225 kar -> tinggi 4 (dapat {inp.region.height})",
            inp.region.height == 4)

        # 4. Lebih panjang lagi: MENTOK di 5, tapi virtual lebih tinggi.
        inp.insert("huruf " * 200)  # ~22 baris layar
        await pilot.pause()
        cek(f"teks panjang mentok di 5 (dapat {inp.region.height})",
            inp.region.height == 5)
        cek("virtual size > 5 (menggulir)", inp.virtual_size.height > 5)

        # 5. Enter: kirim teks UTUH lalu kosong & tinggi kembali 1.
        teks_penuh = inp.text
        app.terkirim.clear()
        await pilot.press("enter")
        await pilot.pause()
        cek("Enter mengirim teks utuh", app.terkirim == [teks_penuh.strip()])
        cek("setelah kirim tinggi 1", inp.region.height == 1)
        cek("setelah kirim kosong", inp.text == "")

        # 6. Autocomplete: ketik /m -> dropdown muncul.
        await pilot.press(*"/m")
        await pilot.pause()
        cek("dropdown /m terbuka", box.autocomplete_open)
        cek("kandidat /model teratas", box._matches[0][0] == "/model")

        # 7. Tab melengkapi.
        await pilot.press("tab")
        await pilot.pause()
        cek(f"tab melengkapi ke /model (dapat {inp.text!r})",
            inp.text == "/model ")

        # 8. Escape mengosongkan.
        await pilot.press("escape")
        await pilot.pause()
        cek("escape mengosongkan", inp.text == "")

        # 9. Hapus kata sebelum kursor.
        box.set_text("satu dua tiga")
        box._input.move_cursor((0, 13))
        box.delete_word_before_cursor()
        await pilot.pause()
        cek(f"hapus kata -> 'satu dua ' (dapat {inp.text!r})",
            inp.text == "satu dua ")

        # 10. Panah-atas = Recall (riwayat), bukan gerak kursor.
        app.recall.clear()
        await pilot.press("up")
        await pilot.pause()
        cek("panah-atas jadi Recall", app.recall == [False])

    print()
    return 1 if gagal else 0


sys.exit(asyncio.run(main()))

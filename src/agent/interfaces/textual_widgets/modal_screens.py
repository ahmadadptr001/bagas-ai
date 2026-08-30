"""Modal screens untuk menu interaktif bagas-ai.

Menyediakan SelectScreen, ConfirmScreen, dan TextPromptScreen
yang muncul sebagai overlay di atas UI utama.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from textual.screen import ModalScreen
from textual.binding import Binding
from textual.widgets import Static, OptionList, Input, RichLog
from textual.widgets.option_list import Option
from textual.widget import Widget
from textual.containers import Vertical, Horizontal
from rich.text import Text

from ...ui import tema

# Penanda baris PEMISAH kategori di SelectScreen.options: opsi berbentuk
# (_SEP, "Nama Kategori") dirender sebagai judul kategori (bukan pilihan).
_SEP = "\x00SEP"


class SelectScreen(ModalScreen[str]):
    """Modal screen untuk select menu (pilih satu).

    Usage::

        result = await app.push_screen_wait(SelectScreen(
            title="Pilih Model",
            options=["gemini-2.5-flash", "gpt-4o", "claude-sonnet"],
        ))
    """

    DEFAULT_CSS = """
    SelectScreen {
        align: center middle;
    }

    SelectScreen #select-container {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: tall $border;
        padding: 1 2;
    }

    SelectScreen #select-title {
        width: 100%;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
        text-style: bold;
    }

    SelectScreen #select-hint {
        width: 100%;
        text-align: center;
        padding: 1 0 0 0;
        color: $text-muted;
        text-style: dim;
    }

    SelectScreen #select-empty {
        width: 100%;
        text-align: center;
        padding: 1 0;
        color: $text-muted;
        text-style: dim;
    }

    SelectScreen OptionList {
        width: 100%;
        height: auto;
        max-height: 20;
        background: $surface;
        color: $text;
    }

    SelectScreen OptionList > .option-list--option-highlighted {
        background: $accent;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("enter", "confirm", show=False, priority=True),
    ]

    def __init__(self, title: str = "Pilih", options: list[str] = None,
                 hint: str = "", **kwargs):
        super().__init__(**kwargs)
        self.title_text = title
        # Opsi boleh string polos ATAU (tampilan, nilai): tampilan adalah
        # apa yang dilihat pengguna (bisa berlabel/gaya), nilai adalah
        # string yang dikembalikan dismiss(). String polos = keduanya sama.
        self.options = options or []
        self.hint_text = hint or "↑↓ pilih · ⏎ konfirmasi · esc batal"
        self.result: str | None = None

    def _nilai(self, index: int | None) -> str | None:
        """Nilai asli opsi pada ``index`` (bukan prompt yang dirender)."""
        if index is None or not (0 <= index < len(self.options)):
            return None
        opt = self.options[index]
        return opt[1] if isinstance(opt, tuple) else opt

    def _tampilan(self, opt):
        """Teks yang dirender di OptionList — pemisah kategori atau
        opsi berlabel "(rekomendasi)" (bold, sesuai permintaan)."""
        if isinstance(opt, tuple):
            tampil, _nilai_asli = opt
            if tampil == _SEP:
                # Baris PEMISAH kategori: nama kategori bold + garis.
                t = Text("\n")
                t.append(opt[1], style="bold")
                t.append("  " + "─" * 34, style="dim")
                return t
            if tampil.endswith(" (rekomendasi)"):
                base = tampil[:-len(" (rekomendasi)")]
                t = Text(base + " ")
                t.append("(rekomendasi)", style="bold")
                return t
            return tampil
        return opt

    def _adalah_separator(self, opt) -> bool:
        return isinstance(opt, tuple) and opt[0] == _SEP

    def compose(self):
        with Vertical(id="select-container"):
            yield Static(self.title_text, id="select-title")
            if self.options:
                opt_list = OptionList(id="select-options")
                for opt in self.options:
                    # Pemisah kategori DISABLED: tak bisa disorot/dipilih —
                    # panah melewatinya, jadi navigasi tetap mulus.
                    opt_list.add_option(Option(
                        self._tampilan(opt),
                        disabled=self._adalah_separator(opt)))
                yield opt_list
            else:
                yield Static("(no options)", id="select-empty")
            yield Static(self.hint_text, id="select-hint")

    def on_mount(self):
        # Saat options kosong tak ada #select-options — query_one akan
        # melempar NoMatches dan modal mati sebelum tampil.
        if not self.options:
            return
        opt_list = self.query_one("#select-options", OptionList)
        # Tanpa ini ``highlighted`` bisa None: Enter ditangkap binding
        # priority di Screen lalu ``action_confirm`` pulang tanpa bunyi —
        # modal tampak "macet" dan tidak bisa dikonfirmasi. Sorot opsi
        # pertama yang BUKAN pemisah (baris kategori disabled).
        if opt_list.highlighted is None:
            for i, opt in enumerate(self.options):
                if not self._adalah_separator(opt):
                    opt_list.highlighted = i
                    break
            else:
                opt_list.highlighted = 0
        opt_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        """Pilihan lewat klik."""
        self._pilih(event.option_index)

    def action_confirm(self):
        """Pilihan lewat Enter pada baris yang disorot."""
        if not self.options:
            self.dismiss(None)
            return
        opt_list = self.query_one("#select-options", OptionList)
        # ``highlighted`` adalah INDEKS (int); ``highlighted_option`` adalah
        # objek Option. Versi lama menyuapkan Option ke get_option_at_index()
        # yang minta int, jadi Enter selalu melempar TypeError.
        self._pilih(opt_list.highlighted)

    def _pilih(self, index: int | None):
        """Kembalikan nilai asli pada ``index`` (bukan prompt yang dirender).

        Baris PEMISAH kategori tak bisa dipilih: klik/Enter di atasnya
        diabaikan supaya tak ada "kategori" yang terkirim sebagai pilihan."""
        nilai = self._nilai(index)
        if nilai is None:
            return
        if self._adalah_separator(self.options[index]):
            return
        self.result = nilai
        self.dismiss(self.result)

    def action_cancel(self):
        """Cancel selection."""
        self.dismiss(None)


class MultiSelectScreen(ModalScreen[list[str]]):
    """Modal screen untuk pilih BANYAK (kembar checkbox inquirer di CLI).

    Spasi menandai/melepas tanda, ⏎ mengirim yang bertanda, klik baris
    menandai seperti spasi, esc batal. ``dismiss(None)`` = batal;
    ``dismiss([])`` = tidak memilih apa pun.
    """

    DEFAULT_CSS = """
    MultiSelectScreen {
        align: center middle;
    }

    MultiSelectScreen #select-container {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: tall $border;
        padding: 1 2;
    }

    MultiSelectScreen #select-title {
        width: 100%;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
        text-style: bold;
    }

    MultiSelectScreen #select-hint {
        width: 100%;
        text-align: center;
        padding: 1 0 0 0;
        color: $text-muted;
        text-style: dim;
    }

    MultiSelectScreen OptionList {
        width: 100%;
        height: auto;
        max-height: 20;
        background: $surface;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("enter", "confirm", show=False, priority=True),
        Binding("space", "toggle", show=False),
    ]

    def __init__(self, title: str = "Pilih", options: list[str] = None,
                 hint: str = "", **kwargs):
        super().__init__(**kwargs)
        self.title_text = title
        self.options = options or []
        self.hint_text = hint or "spasi tandai · ⏎ kirim · esc batal"
        self._tanda: set[int] = set()

    def compose(self):
        with Vertical(id="select-container"):
            yield Static(self.title_text, id="select-title")
            opt_list = OptionList(id="select-options")
            for opt in self.options:
                opt_list.add_option(Option(self._prompt(opt)))
            yield opt_list
            yield Static(self.hint_text, id="select-hint")

    def on_mount(self):
        opt_list = self.query_one("#select-options", OptionList)
        if opt_list.highlighted is None:
            opt_list.highlighted = 0
        opt_list.focus()

    def _prompt(self, teks: str, tandai: bool = False) -> str:
        return ("☑ " if tandai else "☐ ") + teks

    def _toggle(self, index: int | None):
        if index is None or not (0 <= index < len(self.options)):
            return
        if index in self._tanda:
            self._tanda.discard(index)
        else:
            self._tanda.add(index)
        # Gambar ulang seluruh daftar: OptionList tak punya API untuk
        # mengganti prompt SATU option di tempat. Daftar ask_user cuma
        # beberapa item — menggambar ulang semuanya jauh lebih murah
        # daripada menambah API.
        opt_list = self.query_one("#select-options", OptionList)
        sorot = opt_list.highlighted
        opt_list.clear_options()
        for i, opt in enumerate(self.options):
            opt_list.add_option(Option(self._prompt(opt, i in self._tanda)))
        opt_list.highlighted = sorot
        opt_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        """Klik baris = menandai (bukan langsung mengirim)."""
        self._toggle(event.option_index)

    def action_toggle(self):
        opt_list = self.query_one("#select-options", OptionList)
        self._toggle(opt_list.highlighted)

    def action_confirm(self):
        terpilih = [self.options[i] for i in sorted(self._tanda)]
        self.dismiss(terpilih)

    def action_cancel(self):
        self.dismiss(None)


class ThemeScreen(SelectScreen):
    """Menu tema dengan PRATINJAU LANGSUNG di seluruh layar.

    Tiap baris menampilkan swatch warna temanya sendiri. Saat sorotan
    berpindah (panah/klik), aplikasi menampilkan tema itu SEKARANG —
    pengguna melihat hasilnya sebelum memutuskan. Esc mengembalikan tema
    semula, ⏎ memakai dan menyimpannya.

    ``options`` berisi ID tema (dipakai ``_pilih`` untuk ``dismiss``);
    barisnya dirender ulang di ``compose`` agar tiap tema tampil dengan
    warnanya sendiri.
    """

    def __init__(self, themes: list[tuple[str, str, str]], **kwargs):
        super().__init__(title="Tema", options=[t[0] for t in themes],
                         **kwargs)
        self.themes = themes

    def compose(self):
        with Vertical(id="select-container"):
            yield Static("Tema", id="select-title")
            opt_list = OptionList(id="select-options")
            for tid, label, desc in self.themes:
                t = tema.TEMA.get(tid, {})
                row = Text(no_wrap=True, overflow="ellipsis")
                # Swatch: tiga blok warna khas tema tsb.
                row.append("██ ", style=t.get("aksen", ""))
                row.append("██ ", style=t.get("aksen_terang", ""))
                row.append("██  ", style=t.get("teks", ""))
                row.append(label, style="bold")
                if desc:
                    row.append(f"  {desc}", style="dim")
                opt_list.add_option(Option(row, id=tid))
            yield opt_list
            yield Static("panah pratinjau · ⏎ pakai · esc batal",
                         id="select-hint")

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        """Sorotan berpindah -> pratinjau tema itu di seluruh layar."""
        event.stop()
        try:
            self.app.pratinjau_tema(event.option_id)
        except Exception:  # noqa: BLE001 — app belum siap
            pass

    def action_cancel(self):
        """Esc: kembalikan tampilan ke tema yang benar-benar aktif."""
        try:
            self.app.pratinjau_tema(None)
        except Exception:  # noqa: BLE001
            pass
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Modal screen untuk konfirmasi Ya/Tidak.

    Usage::

        result = await app.push_screen_wait(ConfirmScreen(
            title="Hapus sesi?",
        ))
    """

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }

    ConfirmScreen #confirm-container {
        width: 50;
        max-width: 80%;
        height: auto;
        background: $surface;
        border: tall $border;
        padding: 1 2;
    }

    ConfirmScreen #confirm-title {
        width: 100%;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
        text-style: bold;
    }

    ConfirmScreen #confirm-buttons {
        width: 100%;
        height: auto;
        padding: 1 0 0 0;
        align: center middle;
    }

    ConfirmScreen #confirm-btn-yes {
        width: 12;
        text-align: center;
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }

    ConfirmScreen #confirm-btn-no {
        width: 12;
        text-align: center;
        padding: 0 1;
        margin: 0 1;
        color: $text-muted;
    }

    ConfirmScreen Static.-active {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("enter", "confirm", show=False, priority=True),
        Binding("left", "prev", show=False),
        Binding("right", "next", show=False),
        Binding("y", "yes", show=False),
        Binding("n", "no", show=False),
    ]

    def __init__(self, title: str = "Konfirmasi", **kwargs):
        super().__init__(**kwargs)
        self.title_text = title
        self._selected = 0  # 0=yes, 1=no

    def compose(self):
        with Vertical(id="confirm-container"):
            yield Static(self.title_text, id="confirm-title")
            with Horizontal(id="confirm-buttons"):
                yield Static("Ya", id="confirm-btn-yes")
                yield Static("Tidak", id="confirm-btn-no")

    def on_mount(self):
        self._update_display()

    def _update_display(self):
        yes = self.query_one("#confirm-btn-yes", Static)
        no = self.query_one("#confirm-btn-no", Static)
        yes.set_class(self._selected == 0, "-active")
        no.set_class(self._selected == 1, "-active")

    def action_prev(self):
        self._selected = 0
        self._update_display()

    def action_next(self):
        self._selected = 1
        self._update_display()

    def action_confirm(self):
        """Confirm current selection via Enter."""
        self.dismiss(self._selected == 0)

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)

    def action_cancel(self):
        self.dismiss(False)


class TextPromptScreen(ModalScreen[str]):
    """Modal screen untuk text input.

    Usage::

        result = await app.push_screen_wait(TextPromptScreen(
            title="Masukkan nama model",
            placeholder="gemini-2.5-flash",
        ))
    """

    DEFAULT_CSS = """
    TextPromptScreen {
        align: center middle;
    }

    TextPromptScreen #text-container {
        width: 60;
        max-width: 90%;
        height: auto;
        background: $surface;
        border: tall $border;
        padding: 1 2;
    }

    TextPromptScreen #text-title {
        width: 100%;
        text-align: center;
        padding: 0 0 1 0;
        color: $text;
        text-style: bold;
    }

    TextPromptScreen Input {
        width: 100%;
        background: $surface;
        color: $text;
        border: tall $border;
    }

    TextPromptScreen #text-hint {
        width: 100%;
        text-align: center;
        padding: 1 0 0 0;
        color: $text-muted;
        text-style: dim;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
    ]

    def __init__(self, title: str = "Input", placeholder: str = "",
                 default: str = "", hint: str = "", **kwargs):
        super().__init__(**kwargs)
        self.title_text = title
        self.placeholder = placeholder
        self.default = default
        self.hint_text = hint or "⏎ kirim · esc batal"

    def compose(self):
        with Vertical(id="text-container"):
            yield Static(self.title_text, id="text-title")
            yield Input(placeholder=self.placeholder, value=self.default,
                        id="text-input")
            yield Static(self.hint_text, id="text-hint")

    def on_mount(self):
        self.query_one("#text-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter — submit text (reject empty input)."""
        if event.value.strip():
            self.dismiss(event.value)
        else:
            self.dismiss(None)

    def action_cancel(self):
        """Cancel input."""
        self.dismiss(None)


class BtwScreen(ModalScreen[None]):
    """Jalur obrolan sampingan yang sepenuhnya terpisah dari terminal chat."""

    DEFAULT_CSS = """
    BtwScreen { align: center middle; background: $background 80%; }
    BtwScreen #btw-container { width: 84%; height: 82%; max-width: 100; background: $surface; border: tall $accent; padding: 1 2; }
    BtwScreen #btw-title { text-align: center; text-style: bold; padding-bottom: 1; }
    BtwScreen #btw-log { height: 1fr; border: round $border; padding: 0 1; }
    BtwScreen #btw-input { width: 1fr; margin-top: 1; }
    BtwScreen #btw-hint { width: auto; padding: 1 0 0 1; color: $text-muted; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False, priority=True)]

    def __init__(self, answer: Callable[[str], str], initial: str = "", **kwargs):
        super().__init__(**kwargs)
        self._answer = answer
        self._initial = initial

    def compose(self):
        with Vertical(id="btw-container"):
            yield Static("/btw · obrolan sampingan", id="btw-title")
            yield RichLog(id="btw-log", markup=False, wrap=True, auto_scroll=True)
            with Horizontal():
                yield Input(value=self._initial, placeholder="Ngobrol santai…", id="btw-input")
                yield Static("esc tutup", id="btw-hint")

    def on_mount(self):
        self.query_one("#btw-input", Input).focus()
        if self._initial:
            self._kirim(self._initial)

    def on_input_submitted(self, event: Input.Submitted):
        self._kirim(event.value)

    def _kirim(self, value: str):
        pertanyaan = value.strip()
        if not pertanyaan:
            return
        log = self.query_one("#btw-log", RichLog)
        log.write(Text(f"kamu: {pertanyaan}"))
        inp = self.query_one("#btw-input", Input)
        inp.value = ""

        def worker():
            try:
                jawaban = self._answer(pertanyaan)
            except Exception as exc:  # noqa: BLE001
                jawaban = f"Gagal: {exc}"
            try:
                self.app.call_from_thread(log.write, Text(f"bagas: {jawaban}"))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def action_cancel(self):
        self.dismiss(None)

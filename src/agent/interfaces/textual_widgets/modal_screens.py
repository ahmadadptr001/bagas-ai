"""Modal screens untuk menu interaktif bagas-ai.

Menyediakan SelectScreen, ConfirmScreen, dan TextPromptScreen
yang muncul sebagai overlay di atas UI utama.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from textual.screen import ModalScreen
from textual.binding import Binding
from textual.widgets import Static, OptionList, Input, RichLog, Button
from textual.widgets.option_list import Option
from textual.widget import Widget
from textual.containers import Vertical, Horizontal
from rich.text import Text

from ...models import _TANDA_DITUNDA
from ...ui import tema
from ...ui.clipboard import copy_text
from .message_list import MessageList

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
            if tampil.startswith(_TANDA_DITUNDA):
                # Model yang tak boleh dipilih: nama tetap terbaca, tapi redup
                # + alasan singkatnya, supaya pengguna tahu ini SENGAJA mati
                # dan bukan katalog yang rusak.
                t = Text(tampil[len(_TANDA_DITUNDA):], style="dim")
                t.append("  ⏸ ditunda sementara", style="dim italic")
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

    def _nonaktif(self, opt) -> bool:
        """True bila opsi ini TIDAK boleh dipilih: baris pemisah kategori,
        atau model yang sedang ditunda (ditandai models._TANDA_DITUNDA)."""
        return self._adalah_separator(opt) or (
            isinstance(opt, tuple) and opt[0].startswith(_TANDA_DITUNDA))

    def compose(self):
        with Vertical(id="select-container"):
            yield Static(self.title_text, id="select-title")
            if self.options:
                opt_list = OptionList(id="select-options")
                for opt in self.options:
                    # Pemisah kategori & model yang ditunda DISABLED: tak bisa
                    # disorot/dipilih — panah melewatinya, jadi navigasi tetap
                    # mulus dan pilihan mati tak bisa dikonfirmasi.
                    opt_list.add_option(Option(
                        self._tampilan(opt),
                        disabled=self._nonaktif(opt)))
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
        # pertama yang BISA dipilih (bukan pemisah / model yang ditunda).
        if opt_list.highlighted is None:
            for i, opt in enumerate(self.options):
                if not self._nonaktif(opt):
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
    BtwScreen { align: center middle; background: #050610 82%; }
    BtwScreen #btw-container {
        width: 86%; height: 84%; max-width: 104;
        background: $t-gema_bg; border: round $t-tepi; padding: 0;
    }
    BtwScreen #btw-head {
        width: 100%; height: 3; padding: 1 1;
        background: $t-menu_bg; color: $t-teks;
    }
    BtwScreen #btw-lights { width: 8; height: 1; }
    BtwScreen #btw-title { width: 1fr; height: 1; text-style: bold; }
    BtwScreen #btw-loading {
        display: none; width: 1fr; height: 1;
        color: $t-redup; margin: 0 2 1 2;
    }
    BtwScreen #btw-close {
        width: 3; min-width: 3; height: 1; min-height: 1;
        border: none; padding: 0; margin: 0;
        background: $t-menu_hover; color: $t-teks;
    }
    BtwScreen #btw-close:hover,
    BtwScreen #btw-close:focus { background: $t-menu_aktif_bg; color: $t-menu_aktif_teks; }
    BtwScreen #btw-log {
        height: 1fr; border: none; padding: 1 2; background: transparent;
        scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1;
        scrollbar-background: $t-gema_bg; scrollbar-color: $t-tepi_redup;
        scrollbar-color-hover: $t-tepi; scrollbar-color-active: $t-aksen;
        overflow-x: hidden; overflow-y: auto;
    }
    BtwScreen #btw-compose {
        width: 1fr; height: 3; min-height: 3; margin: 0 1 1 1;
        border: round $t-tepi_redup; background: $t-menu_bg;
    }
    BtwScreen #btw-compose:focus-within { border: round $t-tepi; }
    BtwScreen #btw-input {
        width: 1fr; height: 1; min-height: 1; margin: 0; padding: 0 1;
        border: none; background: transparent; color: $t-teks;
    }
    BtwScreen #btw-send {
        width: 3; min-width: 3; height: 1; min-height: 1;
        border: none; margin: 0 1 0 0; padding: 0;
        color: $t-menu_aktif_teks; background: $t-menu_aktif_bg;
    }
    BtwScreen.-compact #btw-container { width: 100%; height: 100%; max-width: 100%; }
    BtwScreen.-compact #btw-lights { display: none; }
    BtwScreen.-compact #btw-log { padding: 0 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False, priority=True),
                Binding("ctrl+c", "copy_selection", show=False, priority=True)]

    def __init__(self, answer: Callable[[str], str], initial: str = "", **kwargs):
        super().__init__(**kwargs)
        self._answer = answer
        self._initial = initial
        self._busy = False
        self._spinner = 0
        self._btw_dismissed = False
        self._btw_timer = None

    def compose(self):
        with Vertical(id="btw-container"):
            with Horizontal(id="btw-head"):
                lights = Text()
                for color in ("#ff5f57", "#febc2e", "#28c840"):
                    lights.append("● ", style=color)
                yield Static(lights, id="btw-lights")
                yield Static("Obrolan sampingan", id="btw-title")
                yield Button("×", id="btw-close", compact=True, tooltip="Tutup (Esc)")
            yield MessageList(id="btw-log")
            yield Static("", id="btw-loading")
            with Horizontal(id="btw-compose"):
                yield Input(value=self._initial, placeholder="Tulis pesan…", id="btw-input")
                yield Button("↑", id="btw-send", compact=True, tooltip="Kirim (Enter)")

    def on_mount(self):
        self.query_one("#btw-input", Input).focus()
        self._btw_timer = self.set_interval(0.18, self._tick)
        if self._initial:
            self._kirim(self._initial)

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        self._kirim(event.value)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "btw-close":
            self.action_cancel()
        elif event.button.id == "btw-send":
            self._kirim(self.query_one("#btw-input", Input).value)

    def on_resize(self, event):
        self.set_class(event.size.width < 70 or event.size.height < 28, "-compact")

    def _kirim(self, value: str):
        pertanyaan = value.strip()
        if not pertanyaan or self._busy or self._btw_dismissed:
            return
        log = self.query_one("#btw-log", MessageList)
        log.append_user_message(pertanyaan)
        log.begin_stream()
        inp = self.query_one("#btw-input", Input)
        inp.value = ""
        self._busy = True
        self.query_one("#btw-send", Button).disabled = True
        self._tick()
        app = self.app

        def worker():
            try:
                jawaban = self._answer(pertanyaan)
                error = False
            except Exception as exc:  # noqa: BLE001
                jawaban = f"Gagal: {exc}"
                error = True
            if self._btw_dismissed:
                return
            try:
                app.call_from_thread(self._selesai, jawaban, error)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True, name="bagasai-btw").start()

    def _tick(self):
        if self._busy and not self._btw_dismissed:
            self._spinner = (self._spinner + 1) % 4
            frame = "⠋⠙⠹⠸"[self._spinner]
            loading = self.query_one("#btw-loading", Static)
            loading.display = True
            loading.update(frame + " Menjawab")

    def _selesai(self, jawaban: str, error: bool = False):
        if self._btw_dismissed:
            return
        self._busy = False
        loading = self.query_one("#btw-loading", Static)
        loading.update("")
        loading.display = False
        self.query_one("#btw-send", Button).disabled = False
        log = self.query_one("#btw-log", MessageList)
        if error:
            log.append_notice(jawaban, style=tema.p("exit_footer"))
        else:
            log.append_ai_message(jawaban or "Tidak ada jawaban")
        self.query_one("#btw-input", Input).focus()

    def action_copy_selection(self):
        inp = self.query_one("#btw-input", Input)
        text = inp.selected_text
        if not text:
            log = self.query_one("#btw-log", MessageList)
            if log._salin_lo is not None and log._salin_hi is not None:
                text = log._teks_salin(log._salin_lo, log._salin_hi)
        if text:
            copy_text(self.app, text)

    def on_unmount(self):
        self._btw_dismissed = True
        if self._btw_timer is not None:
            self._btw_timer.stop()

    def action_cancel(self):
        self._btw_dismissed = True
        self.dismiss(None)

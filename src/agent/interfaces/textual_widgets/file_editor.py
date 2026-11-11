"""Editor file teks yang dibuka dari ProjectTree."""
from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from rich.style import Style
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static, TextArea
from textual.widgets.text_area import TextAreaTheme
from textual.document._document import Selection

from ...ui import tema
from ...ui.textual_theme import latar_terang
from ...ui.clipboard import copy_text
from ...ui.diff_view import DiffView
from ...ui.syntax import code_highlights
from ...ui.icons import footer_icon
from ...editor import HTML_SUFFIXES, EditorFileError, open_html_file


SaveCallback = Callable[[str, str], tuple[bool, str]]


class _EditorTextArea(TextArea):
    """Pastikan Esc dari area kode menjalankan lifecycle editor."""

    BINDINGS = [Binding("escape", "close_screen", show=False, priority=True)]

    def __init__(self, *args, **kwargs):
        self._syntax_path = None
        self._syntax_revision = 0
        self._syntax_spans = {}
        self._syntax_timer = None
        super().__init__(*args, **kwargs)

    def set_syntax(self, path: Path) -> None:
        self._syntax_path = path
        self.request_highlight(immediate=True)

    def request_highlight(self, immediate: bool = False) -> None:
        self._syntax_revision += 1
        self._syntax_spans = {}
        cache = getattr(self, "_line_cache", None)
        if cache is not None:
            cache.clear()
        if self._syntax_timer is not None:
            self._syntax_timer.stop()
        if self._syntax_path is not None:
            self._syntax_timer = self.set_timer(0.01 if immediate else 0.12, self._highlight)

    def _highlight(self) -> None:
        revision, text, path = self._syntax_revision, self.text, self._syntax_path
        light, app = latar_terang(), self.app

        def work():
            spans = code_highlights(text, path, light,
                                    lambda: revision != self._syntax_revision)
            if revision == self._syntax_revision and self.is_mounted:
                try:
                    app.call_from_thread(self._apply_highlights, revision, spans)
                except RuntimeError:
                    pass

        self.run_worker(work, thread=True, group="syntax", exclusive=True, exit_on_error=False)

    def _apply_highlights(self, revision, spans) -> None:
        if self.is_mounted and revision == self._syntax_revision:
            self._syntax_spans = spans
            # TextArea menyimpan Strip baris; refresh saja masih memakai
            # warna lama walaupun get_line sudah memiliki span baru.
            cache = getattr(self, "_line_cache", None)
            if cache is not None:
                cache.clear()
            self.refresh()

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        for start, end, style in self._syntax_spans.get(line_index, ()):
            line.stylize(style, start, min(end, len(line)))
        return line

    def on_unmount(self) -> None:
        self._syntax_revision += 1
        if self._syntax_timer is not None:
            self._syntax_timer.stop()

    def action_close_screen(self) -> None:
        action = getattr(self.screen, "action_close_editor", None)
        if callable(action):
            action()

    def action_copy(self) -> None:
        copy_text(self.app, self.selected_text)

    async def _on_mouse_up(self, event) -> None:
        await super()._on_mouse_up(event)
        event.prevent_default()
        if self.selected_text:
            self.action_copy()

    async def _on_click(self, event) -> None:
        # Seleksi native Widget memilih semua teks pada double-click.
        # Editor mempertahankan seleksi dokumennya sendiri per huruf/kata/baris.
        event.prevent_default()
        event.stop()
        if event.button != 1 or event.chain < 2:
            return
        row, column = self.get_target_document_location(event)
        line = self.document.get_line(row)
        start, end = 0, len(line)
        if event.chain == 2:
            for match in re.finditer(r"\w+|[^\w\s]+|\s+", line):
                if match.start() <= column < match.end():
                    start, end = match.span()
                    break
        self.selection = Selection((row, start), (row, end))
        self.action_copy()


class FileEditorScreen(ModalScreen[None]):
    """Editor modal dengan tinjau-diff dua tahap sebelum penyimpanan."""

    DEFAULT_CSS = """
    FileEditorScreen { align: center middle; background: #050610 82%; }
    FileEditorScreen #file-editor-container {
        width: 94%; height: 92%;
        background: $t-gema_bg; border: round $t-tepi; padding: 0;
    }
    FileEditorScreen #file-editor-head {
        height: 3; padding: 0 1; align-vertical: middle;
        background: $t-menu_bg; border-bottom: solid $t-tepi_redup;
    }
    FileEditorScreen #file-editor-lights { width: 8; height: 1; }
    FileEditorScreen #file-editor-title {
        width: 1fr; height: 1; color: $t-teks;
    }
    FileEditorScreen #file-editor-path {
        width: 100%; height: 1; padding: 0 2; color: $t-redup;
        background: $t-menu_meta_bg;
    }
    FileEditorScreen #file-editor-tabs {
        height: 2; padding: 0 1; background: $t-menu_meta_bg;
    }
    FileEditorScreen #file-editor-tabs Button {
        width: 14; min-width: 10; margin: 0; color: $t-redup;
    }
    FileEditorScreen #file-editor-tabs Button.-selected {
        background: $t-gema_bg; color: $t-teks; text-style: bold;
    }
    FileEditorScreen #file-editor-area,
    FileEditorScreen #file-editor-diff {
        height: 1fr; width: 100%; border: none;
        background: $t-gema_bg; color: $t-teks;
        scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1;
        scrollbar-background: $t-gema_bg;
        scrollbar-color: $t-tepi_redup;
        scrollbar-color-hover: $t-tepi;
        scrollbar-color-active: $t-aksen;
    }
    FileEditorScreen #file-editor-diff { display: none; padding: 0 2; }
    FileEditorScreen #file-editor-bottom {
        height: auto; padding: 0 1; background: $t-menu_meta_bg;
        border-top: solid $t-tepi_redup;
    }
    FileEditorScreen #file-editor-status {
        width: 100%; height: auto; color: $t-redup;
    }
    FileEditorScreen #file-editor-actions { height: 1; margin-top: 1; }
    FileEditorScreen #file-editor-position { width: 1fr; height: 1; color: $t-redup; }
    FileEditorScreen Button,
    FileEditorScreen Button.-style-default {
        min-width: 8; width: 12; height: 1; min-height: 1;
        padding: 0; margin-left: 1; border: none;
        background: $t-menu_bg; color: $t-teks;
    }
    FileEditorScreen Button:hover,
    FileEditorScreen Button.-style-default:hover { background: $t-menu_hover; }
    FileEditorScreen #file-editor-btn-save {
        background: $t-menu_aktif_bg; color: $t-menu_aktif_teks;
    }
    FileEditorScreen #file-editor-head Button { width: 3; min-width: 3; }
    FileEditorScreen #file-editor-head-close {
        color: $t-teks; background: $t-menu_hover; text-style: bold;
    }
    FileEditorScreen #file-editor-head #file-editor-browser {
        width: 11; min-width: 11; color: $t-aksen; background: $t-menu_hover;
    }
    FileEditorScreen #file-editor-head #file-editor-browser:hover {
        color: $t-menu_aktif_teks; background: $t-menu_aktif_bg;
    }
    FileEditorScreen.-compact #file-editor-lights { display: none; }
    FileEditorScreen.-compact #file-editor-container { width: 100%; height: 100%; }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", show=False, priority=True),
        Binding("escape", "close_editor", show=False, priority=True),
    ]

    def __init__(self, path: Path, text: str, save: SaveCallback, **kwargs):
        super().__init__(**kwargs)
        self.path = Path(path)
        self._original = text
        self._save = save
        self._preview = False
        self._discard_armed = False

    def compose(self):
        with Vertical(id="file-editor-container"):
            with Horizontal(id="file-editor-head"):
                lights = Text()
                for color in ("#ff5f57", "#febc2e", "#28c840"):
                    lights.append("● ", style=color)
                yield Static(lights, id="file-editor-lights")
                yield Static(self.path.name, id="file-editor-title", markup=False)
                if self.path.suffix.lower() in HTML_SUFFIXES:
                    yield Button(f"{footer_icon('web')} Browser", id="file-editor-browser",
                                 tooltip="Buka HTML tersimpan di browser", compact=True)
                yield Button("×", id="file-editor-head-close", tooltip="Tutup (Esc)", compact=True)
            yield Static(str(self.path), id="file-editor-path", markup=False)
            with Horizontal(id="file-editor-tabs"):
                yield Button("Kode", id="file-editor-btn-code", classes="-selected", compact=True)
                yield Button("Perubahan", id="file-editor-btn-diff", compact=True)
            yield _EditorTextArea.code_editor(
                self._original,
                # Pygments menangani bahasa tanpa ketergantungan tree-sitter.
                language=None,
                soft_wrap=False,
                id="file-editor-area",
            )
            yield RichLog(
                id="file-editor-diff", markup=False, wrap=True, min_width=1,
                auto_scroll=False,
            )
            with Vertical(id="file-editor-bottom"):
                yield Static(
                    "",
                    id="file-editor-status",
                )
                with Horizontal(id="file-editor-actions"):
                    yield Static("Ln 1, Col 1", id="file-editor-position")
                    yield Button("Simpan", id="file-editor-btn-save", variant="primary", compact=True,
                                 tooltip="Ctrl+S")
                    yield Button("Tutup", id="file-editor-btn-close", compact=True, tooltip="Esc")

    def on_mount(self) -> None:
        # Pada beberapa versi Textual, Mount milik Screen dapat tiba satu
        # putaran sebelum seluruh anak hasil compose terpasang.
        self.call_after_refresh(self._focus_editor)

    def _focus_editor(self) -> None:
        area = self.query_one("#file-editor-area", TextArea)
        self._apply_editor_theme(area)
        area.set_syntax(self.path)
        self._update_title()
        self.query_one("#file-editor-path").tooltip = str(self.path)
        area.focus()

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < 70, "-compact")
        if self._preview:
            self.call_after_refresh(self._render_diff)

    def _apply_editor_theme(self, area: TextArea) -> None:
        syntax = {
            "comment": Style(color=tema.p("redup"), italic=True),
            "keyword": Style(color=tema.p("aksen2")),
            "string": Style(color=tema.p("git_footer")),
            "number": Style(color=tema.p("ubah_footer")),
            "function": Style(color=tema.p("aksen")),
            "type": Style(color=tema.p("aksen_terang")),
        }
        base = TextAreaTheme.get_builtin_theme("github_light" if latar_terang() else "vscode_dark")
        area.register_theme(TextAreaTheme(
            name="bagas-editor", base_style=Style(color=tema.p("teks"), bgcolor=tema.p("gema_bg")),
            gutter_style=Style(color=tema.p("redup"), bgcolor=tema.p("gema_bg")),
            cursor_style=Style(color=tema.p("gema_bg"), bgcolor=tema.p("aksen")),
            cursor_line_style=Style(bgcolor=tema.p("menu_bg")),
            cursor_line_gutter_style=Style(color=tema.p("aksen"), bgcolor=tema.p("menu_bg")),
            selection_style=Style(color=tema.p("menu_aktif_teks"), bgcolor=tema.p("menu_aktif_bg")),
            bracket_matching_style=Style(color=tema.p("aksen"), bold=True),
            syntax_styles={**(base.syntax_styles if base else {}), **syntax},
        ))
        area.theme = "bagas-editor"

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        row, column = event.selection.end
        self.query_one("#file-editor-position", Static).update(f"Ln {row + 1}, Col {column + 1}")

    def _set_tabs(self, preview: bool) -> None:
        self.query_one("#file-editor-btn-code").set_class(not preview, "-selected")
        self.query_one("#file-editor-btn-diff").set_class(preview, "-selected")

    @property
    def text(self) -> str:
        return self.query_one("#file-editor-area", TextArea).text

    @property
    def dirty(self) -> bool:
        return self.text != self._original

    def _status(self, value: str) -> None:
        status = self.query_one("#file-editor-status", Static)
        status.update(value)
        status.display = bool(value)

    def _show_editor(self) -> None:
        self._preview = False
        self._set_tabs(False)
        self.query_one("#file-editor-diff", RichLog).display = False
        area = self.query_one("#file-editor-area", TextArea)
        area.display = True
        area.focus()
        self.query_one("#file-editor-btn-save", Button).label = "Simpan"

    def _render_diff(self) -> bool:
        old = self._original.splitlines()
        new = self.text.splitlines()
        lines = list(difflib.unified_diff(
            old, new,
            fromfile=f"a/{self.path.name}",
            tofile=f"b/{self.path.name}",
            lineterm="",
        ))
        if not lines:
            self._status("Tidak ada perubahan.")
            return False
        log = self.query_one("#file-editor-diff", RichLog)
        log.clear()
        log.write(DiffView(self.path.name, "\n".join(lines), interactive=False),
                  width=log.scrollable_content_region.width or None)
        return True

    def action_show_diff(self) -> None:
        if self._preview:
            self._show_editor()
            self._status("")
            return
        if not self._render_diff():
            return
        self._preview = True
        self._set_tabs(True)
        self.query_one("#file-editor-area", TextArea).display = False
        self.query_one("#file-editor-diff", RichLog).display = True
        self.query_one("#file-editor-btn-save", Button).label = "Konfirmasi"
        self._status("Ctrl+S konfirmasi · Esc kembali")

    def action_save(self) -> None:
        if not self.dirty:
            self._status("Tidak ada perubahan")
            return
        if not self._preview:
            self.action_show_diff()
            return
        new_text = self.text
        ok, message = self._save(self._original, new_text)
        if not ok:
            self._show_editor()
            self._status("✗ " + message)
            return
        self._original = new_text
        self._update_title()
        self._discard_armed = False
        self._show_editor()
        self._status("Tersimpan")

    def action_open_browser(self) -> None:
        if self.path.suffix.lower() not in HTML_SUFFIXES:
            return
        try:
            open_html_file(self.path)
        except EditorFileError as exc:
            self._status(f"✗ {exc}")
            return
        self._status("Versi tersimpan dibuka · Ctrl+S untuk memperbarui" if self.dirty
                     else "Dibuka di browser")

    def action_close_editor(self) -> None:
        if self._preview:
            self._show_editor()
            self._status("Belum disimpan")
            return
        if self.dirty and not self._discard_armed:
            self._discard_armed = True
            self._status("Belum disimpan · Esc lagi untuk membuang")
            return
        self.dismiss(None)

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        if isinstance(_event.text_area, _EditorTextArea):
            _event.text_area.request_highlight()
        self._discard_armed = False
        self._update_title()

    def _update_title(self) -> None:
        marker = "  ●" if self.dirty else ""
        self.query_one("#file-editor-title", Static).update(
            f"{self.path.name}{marker}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "file-editor-btn-diff":
            self.action_show_diff()
        elif event.button.id == "file-editor-btn-code":
            self._show_editor()
        elif event.button.id == "file-editor-btn-save":
            self.action_save()
        elif event.button.id == "file-editor-browser":
            self.action_open_browser()
        elif event.button.id in ("file-editor-btn-close", "file-editor-head-close"):
            self.action_close_editor()
        event.stop()


__all__ = ["FileEditorScreen"]

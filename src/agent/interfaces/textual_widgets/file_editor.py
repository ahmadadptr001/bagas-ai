"""Editor file teks yang dibuka dari ProjectTree."""
from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static, TextArea


SaveCallback = Callable[[str, str], tuple[bool, str]]


class _EditorTextArea(TextArea):
    """Pastikan Esc dari area kode menjalankan lifecycle editor."""

    BINDINGS = [Binding("escape", "close_screen", show=False, priority=True)]

    def action_close_screen(self) -> None:
        action = getattr(self.screen, "action_close_editor", None)
        if callable(action):
            action()


def _language(path: Path) -> str | None:
    return {
        ".py": "python", ".pyw": "python",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".json": "json", ".md": "markdown", ".markdown": "markdown",
        ".html": "html", ".htm": "html", ".css": "css",
        ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
        ".sql": "sql",
    }.get(path.suffix.lower())


class FileEditorScreen(ModalScreen[None]):
    """Editor modal dengan tinjau-diff dua tahap sebelum penyimpanan."""

    DEFAULT_CSS = """
    FileEditorScreen { align: center middle; background: $background 88%; }
    FileEditorScreen #file-editor-container {
        width: 94%; height: 92%;
        background: $surface; border: tall $accent; padding: 0 1;
    }
    FileEditorScreen #file-editor-head { height: 3; padding: 0 1; }
    FileEditorScreen #file-editor-title {
        width: 1fr; height: 1; text-style: bold; color: $text;
    }
    FileEditorScreen #file-editor-path {
        width: 1fr; height: 1; color: $text-muted;
    }
    FileEditorScreen #file-editor-area,
    FileEditorScreen #file-editor-diff {
        height: 1fr; width: 100%; border: round $border;
        background: $surface; color: $text;
    }
    FileEditorScreen #file-editor-diff { display: none; padding: 0 1; }
    FileEditorScreen #file-editor-bottom { height: 3; align: left middle; }
    FileEditorScreen #file-editor-status {
        width: 1fr; color: $text-muted; padding: 1 1 0 0;
    }
    FileEditorScreen Button { min-width: 9; width: auto; height: 3; margin-left: 1; }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", show=False, priority=True),
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
            with Vertical(id="file-editor-head"):
                yield Static(f"Edit · {self.path.name}", id="file-editor-title")
                yield Static(str(self.path), id="file-editor-path")
            yield _EditorTextArea.code_editor(
                self._original,
                language=_language(self.path),
                soft_wrap=False,
                id="file-editor-area",
            )
            yield RichLog(
                id="file-editor-diff", markup=False, wrap=False,
                auto_scroll=False,
            )
            with Horizontal(id="file-editor-bottom"):
                yield Static(
                    "Ctrl+S tinjau/simpan · Esc tutup",
                    id="file-editor-status",
                )
                yield Button("Diff", id="file-editor-btn-diff")
                yield Button("Simpan", id="file-editor-btn-save", variant="primary")
                yield Button("Tutup", id="file-editor-btn-close")

    def on_mount(self) -> None:
        # Pada beberapa versi Textual, Mount milik Screen dapat tiba satu
        # putaran sebelum seluruh anak hasil compose terpasang.
        self.call_after_refresh(self._focus_editor)

    def _focus_editor(self) -> None:
        self.query_one("#file-editor-area", TextArea).focus()

    @property
    def text(self) -> str:
        return self.query_one("#file-editor-area", TextArea).text

    @property
    def dirty(self) -> bool:
        return self.text != self._original

    def _status(self, value: str) -> None:
        self.query_one("#file-editor-status", Static).update(value)

    def _show_editor(self) -> None:
        self._preview = False
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
        limit = 1500
        for line in lines[:limit]:
            style = ""
            if line.startswith("+++") or line.startswith("---"):
                style = "bold"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("@@"):
                style = "cyan"
            log.write(Text(line, style=style))
        if len(lines) > limit:
            log.write(Text(
                f"… diff dipotong: {len(lines) - limit} baris lagi",
                style="dim",
            ))
        return True

    def action_show_diff(self) -> None:
        if self._preview:
            self._show_editor()
            self._status("Kembali mengedit · Ctrl+S untuk meninjau lagi.")
            return
        if not self._render_diff():
            return
        self._preview = True
        self.query_one("#file-editor-area", TextArea).display = False
        self.query_one("#file-editor-diff", RichLog).display = True
        self.query_one("#file-editor-btn-save", Button).label = "Simpan perubahan"
        self._status("Tinjau diff · Ctrl+S konfirmasi · Esc kembali")

    def action_save(self) -> None:
        if not self.dirty:
            self._status("Tidak ada perubahan untuk disimpan.")
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
        self._discard_armed = False
        self._show_editor()
        self._status("✓ " + message)

    def action_close_editor(self) -> None:
        if self._preview:
            self._show_editor()
            self._status("Kembali mengedit · perubahan belum disimpan.")
            return
        if self.dirty and not self._discard_armed:
            self._discard_armed = True
            self._status("Perubahan belum disimpan · tekan Esc lagi untuk membuang")
            return
        self.dismiss(None)

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._discard_armed = False
        marker = "● belum disimpan" if self.dirty else "tersimpan"
        self.query_one("#file-editor-title", Static).update(
            f"Edit · {self.path.name} · {marker}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "file-editor-btn-diff":
            self.action_show_diff()
        elif event.button.id == "file-editor-btn-save":
            self.action_save()
        elif event.button.id == "file-editor-btn-close":
            self.action_close_editor()
        event.stop()


__all__ = ["FileEditorScreen"]

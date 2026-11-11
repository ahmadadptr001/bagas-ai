"""Jendela pratinjau berkas, dengan navigasi PDF dan gambar dalam terminal."""
from __future__ import annotations

from pathlib import Path

from rich.align import Align
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ...file_viewer import FilePreview, can_open_external, open_external, render_file
from ...ui import tema
from .message_list import MessageList
from .progress_bar import TurnProgressBar


class FilePreviewScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    FilePreviewScreen { align: center middle; background: #050610 82%; }
    FilePreviewScreen #file-preview-container {
        width: 94%; height: 92%; padding: 0;
        background: $t-gema_bg; border: round $t-tepi;
    }
    FilePreviewScreen #file-preview-head {
        height: 3; padding: 0 1; align-vertical: middle;
        background: $t-menu_bg; border-bottom: solid $t-tepi_redup;
    }
    FilePreviewScreen #file-preview-lights { width: 8; height: 1; }
    FilePreviewScreen #file-preview-title { width: 1fr; height: 1; color: $t-teks; }
    FilePreviewScreen #file-preview-path {
        height: 1; padding: 0 2; color: $t-redup; background: $t-menu_meta_bg;
    }
    FilePreviewScreen #file-preview-body {
        height: 1fr; padding: 1 2; border: none;
        color: $t-teks; background: $t-gema_bg;
        scrollbar-size: 1 1;
        scrollbar-background: $t-gema_bg; scrollbar-color: $t-tepi_redup;
    }
    FilePreviewScreen #file-preview-loading { margin: 0 2; }
    FilePreviewScreen #file-preview-footer {
        height: 2; padding: 0 1; align-vertical: middle;
        border-top: solid $t-tepi_redup; background: $t-menu_meta_bg;
    }
    FilePreviewScreen #file-preview-meta { width: 1fr; height: 1; color: $t-redup; }
    FilePreviewScreen Button,
    FilePreviewScreen Button.-style-default {
        width: 3; min-width: 3; height: 1; min-height: 1; padding: 0;
        margin-left: 1; border: none; background: $t-menu_bg; color: $t-teks;
    }
    FilePreviewScreen Button:hover,
    FilePreviewScreen Button.-style-default:hover { background: $t-menu_hover; }
    FilePreviewScreen #file-preview-close { background: $t-menu_hover; text-style: bold; }
    FilePreviewScreen #file-preview-mode { width: 7; min-width: 7; }
    FilePreviewScreen #file-preview-open { width: 6; min-width: 6; }
    FilePreviewScreen.-compact #file-preview-lights { display: none; }
    FilePreviewScreen.-compact #file-preview-container { width: 100%; height: 100%; }
    """

    BINDINGS = [
        Binding("escape", "close_preview", priority=True, show=False),
        Binding("ctrl+c", "copy_selection", priority=True, show=False),
        Binding("ctrl+l", "ignore", priority=True, show=False),
        Binding("left", "page(-1)", show=False),
        Binding("right", "page(1)", show=False),
    ]

    def __init__(self, path: Path, **kwargs):
        super().__init__(**kwargs)
        self.path = Path(path)
        self.preview: FilePreview | None = None
        self._page = 0
        self._visual = False
        self._generation = 0
        self._resize_timer = None
        self._requested_size = None

    def compose(self):
        with Vertical(id="file-preview-container"):
            with Horizontal(id="file-preview-head"):
                lights = Text()
                for color in ("#ff5f57", "#febc2e", "#28c840"):
                    lights.append("● ", color)
                yield Static(lights, id="file-preview-lights")
                yield Static(self.path.name, markup=False, id="file-preview-title")
                yield Button("×", id="file-preview-close", tooltip="Tutup (Esc)", compact=True)
            yield Static(str(self.path), markup=False, id="file-preview-path")
            yield MessageList(id="file-preview-body")
            yield TurnProgressBar(id="file-preview-loading")
            with Horizontal(id="file-preview-footer"):
                yield Static("", id="file-preview-meta")
                yield Button("‹", id="file-preview-prev", tooltip="Halaman sebelumnya (←)", compact=True)
                yield Button("›", id="file-preview-next", tooltip="Halaman berikutnya (→)", compact=True)
                yield Button("Visual", id="file-preview-mode", tooltip="Ganti teks / visual PDF", compact=True)
                yield Button("Buka", id="file-preview-open", tooltip="Buka di aplikasi bawaan", compact=True)

    def on_mount(self) -> None:
        self.query_one("#file-preview-path").tooltip = str(self.path)
        for selector in ("#file-preview-prev", "#file-preview-next", "#file-preview-mode"):
            self.query_one(selector).display = False
        self.query_one("#file-preview-open").display = can_open_external(self.path)
        self.query_one("#file-preview-body", MessageList).auto_scroll = False
        self.call_after_refresh(self._load)

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < 70, "-compact")
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(0.15, self._reload_resized)

    def _viewport(self):
        body = self.query_one("#file-preview-body", MessageList)
        return max(1, body.scrollable_content_region.width - 1), max(1, body.scrollable_content_region.height - 1)

    def _reload_resized(self) -> None:
        if self.preview is not None and self.preview.visual and self._viewport() != self._requested_size:
            self._load()

    def _load(self) -> None:
        self._generation += 1
        generation, app = self._generation, self.app
        width, height = self._viewport()
        self._requested_size = width, height
        page, visual, background = self._page, self._visual, tema.p("gema_bg")
        self.query_one("#file-preview-loading", TurnProgressBar).show(label="Membuka file")

        def work():
            try:
                result = render_file(self.path, width, height, page, visual, background)
            except Exception as exc:
                result = FilePreview(Text(f"Tidak dapat menampilkan file: {exc}"), "error")
            if self.is_mounted and generation == self._generation:
                try:
                    app.call_from_thread(self._show, generation, result)
                except RuntimeError:
                    pass

        self.run_worker(work, thread=True, group="file-preview", exclusive=True, exit_on_error=False)

    def _show(self, generation: int, result: FilePreview) -> None:
        if not self.is_mounted or generation != self._generation:
            return
        self.preview = result
        self._page, self._visual = result.page, result.visual
        self.query_one("#file-preview-loading", TurnProgressBar).hide()
        body = self.query_one("#file-preview-body", MessageList)
        body.clear_messages()
        body._emit(Align.center(result.content) if result.visual else result.content)
        body.scroll_home(animate=False)
        pdf = result.kind == "pdf"
        for selector in ("#file-preview-prev", "#file-preview-next", "#file-preview-mode"):
            self.query_one(selector).display = pdf
        self.query_one("#file-preview-prev", Button).disabled = result.page == 0
        self.query_one("#file-preview-next", Button).disabled = result.page + 1 >= result.pages
        mode = self.query_one("#file-preview-mode", Button)
        mode.label = "Teks" if result.visual else "Visual"
        mode.disabled = not result.has_text
        mode.tooltip = "Ganti teks / visual PDF" if result.has_text else "Halaman ini tidak memiliki teks"
        meta = f"{result.page + 1}/{result.pages}" if pdf else result.detail
        widget = self.query_one("#file-preview-meta", Static)
        widget.update(meta)
        widget.tooltip = result.detail

    def action_page(self, delta: int) -> None:
        if self.preview is not None and self.preview.kind == "pdf":
            page = max(0, min(self.preview.pages - 1, self._page + delta))
            if page != self._page:
                self._page = page
                self._load()

    def action_close_preview(self) -> None:
        self._generation += 1
        self.dismiss(None)

    def action_copy_selection(self) -> None:
        from ...ui.clipboard import copy_text
        body = self.query_one("#file-preview-body", MessageList)
        if body._salin_lo is not None and body._salin_hi is not None:
            copy_text(self.app, body._teks_salin(body._salin_lo, body._salin_hi))

    def action_ignore(self) -> None:
        pass

    def on_unmount(self) -> None:
        self._generation += 1
        if self._resize_timer is not None:
            self._resize_timer.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = event.button.id
        if action == "file-preview-close":
            self.action_close_preview()
        elif action == "file-preview-prev":
            self.action_page(-1)
        elif action == "file-preview-next":
            self.action_page(1)
        elif action == "file-preview-mode":
            self._visual = not self._visual
            self._load()
        elif action == "file-preview-open":
            try:
                open_external(self.path)
            except Exception as exc:
                self.query_one("#file-preview-meta", Static).update(f"Gagal membuka: {exc}")

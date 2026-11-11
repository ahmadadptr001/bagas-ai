"""Pemilih sumber layar berupa galeri thumbnail yang dimuat saat terlihat."""
from __future__ import annotations

import queue
import threading

from rich.align import Align
from rich.console import Group
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static

from ...desktop_capture import WindowTarget, capture_thumbnail, list_windows
from ...file_viewer import image_cells
from ...ui import tema


class SourceCard(Widget, can_focus=True):
    class Chosen(Message):
        def __init__(self, index: int, confirm: bool = False):
            super().__init__()
            self.index, self.confirm = index, confirm

    def __init__(self, index: int, target: WindowTarget | None):
        super().__init__(id=f"source-card-{index}")
        self.index, self.target = index, target
        self.title = target.title if target else "Monitor utama"
        self.tooltip = self.title
        self.thumbnail = None
        self.loaded = False
        self.failed = False

    def render(self):
        width = max(1, self.content_size.width)
        height = max(1, self.content_size.height - 2)
        if self.thumbnail is not None:
            preview = image_cells(self.thumbnail, width, height, tema.p("menu_bg"))
        else:
            preview = Text("Pratinjau tidak tersedia" if self.failed else "Memuat pratinjau…",
                           style=tema.p("redup"), no_wrap=True, overflow="ellipsis")
            preview.truncate(width, overflow="ellipsis")
        name = Text(self.title, style=("bold " if self.has_class("-selected") else "") + tema.p("teks"),
                    no_wrap=True, overflow="ellipsis")
        name.truncate(width, overflow="ellipsis")
        return Group(Align(preview, align="center", vertical="middle", height=height), Text(""), name)

    async def _on_click(self, event):
        event.prevent_default()
        event.stop()
        if event.button == 1:
            self.focus()
            self.post_message(self.Chosen(self.index, event.chain >= 2))

    def on_focus(self):
        self.post_message(self.Chosen(self.index))

    def on_unmount(self):
        if self.thumbnail is not None:
            self.thumbnail.close()
            self.thumbnail = None


class _SourceScroll(VerticalScroll):
    def watch_scroll_y(self, old_value: float, new_value: float):
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted:
            self.screen._schedule_previews()


class SourcePickerScreen(ModalScreen[str]):
    DEFAULT_CSS = """
    SourcePickerScreen { align: center middle; background: #050610 82%; }
    SourcePickerScreen #source-picker-container {
        width: 94%; max-width: 112; height: 92%;
        border: round $t-tepi; background: $t-gema_bg; padding: 0;
    }
    SourcePickerScreen #source-picker-head {
        height: 3; padding: 0 1; align-vertical: middle;
        background: $t-menu_bg; border-bottom: solid $t-tepi_redup;
    }
    SourcePickerScreen #source-picker-lights { width: 8; height: 1; }
    SourcePickerScreen #source-picker-title { width: 1fr; height: 1; color: $t-teks; }
    SourcePickerScreen #source-picker-scroll {
        height: 1fr; padding: 1; border: none;
        scrollbar-size-vertical: 1; scrollbar-background: $t-gema_bg;
        scrollbar-color: $t-tepi_redup;
    }
    SourcePickerScreen #source-picker-grid {
        layout: grid; grid-size: 3; grid-columns: 1fr 1fr 1fr;
        grid-rows: 11; grid-gutter: 1 2; height: auto; width: 100%;
    }
    SourcePickerScreen SourceCard {
        width: 100%; height: 11; padding: 0 1;
        border: round $t-tepi_redup; background: $t-menu_bg;
    }
    SourcePickerScreen SourceCard:hover { border: round $t-tepi; }
    SourcePickerScreen SourceCard.-selected,
    SourcePickerScreen SourceCard:focus { border: round $t-aksen; }
    SourcePickerScreen #source-picker-footer {
        height: 3; padding: 0 1; align-vertical: middle;
        border-top: solid $t-tepi_redup; background: $t-menu_meta_bg;
    }
    SourcePickerScreen #source-picker-hint { width: 1fr; height: 1; color: $t-redup; }
    SourcePickerScreen Button,
    SourcePickerScreen Button.-style-default {
        width: 8; min-width: 8; height: 1; min-height: 1; padding: 0;
        border: none; margin-left: 1; background: $t-menu_bg; color: $t-teks;
    }
    SourcePickerScreen Button:hover,
    SourcePickerScreen Button.-style-default:hover { background: $t-menu_hover; }
    SourcePickerScreen #source-picker-refresh { width: 10; min-width: 10; }
    SourcePickerScreen #source-picker-close {
        width: 3; min-width: 3; background: $t-menu_hover; text-style: bold;
    }
    SourcePickerScreen #source-picker-confirm {
        background: $t-menu_aktif_bg; color: $t-menu_aktif_teks;
    }
    SourcePickerScreen.-medium #source-picker-grid { grid-size: 2; grid-columns: 1fr 1fr; }
    SourcePickerScreen.-narrow #source-picker-container { width: 100%; height: 100%; }
    SourcePickerScreen.-narrow #source-picker-lights { display: none; }
    SourcePickerScreen.-narrow #source-picker-grid { grid-size: 1; grid-columns: 1fr; grid-rows: 10; }
    SourcePickerScreen.-narrow SourceCard { height: 10; }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("ctrl+c", "cancel", show=False, priority=True),
        Binding("enter", "confirm", show=False),
        Binding("left", "move(-1, 0)", show=False, priority=True),
        Binding("right", "move(1, 0)", show=False, priority=True),
        Binding("up", "move(0, -1)", show=False, priority=True),
        Binding("down", "move(0, 1)", show=False, priority=True),
        Binding("home", "edge(False)", show=False, priority=True),
        Binding("end", "edge(True)", show=False, priority=True),
        Binding("f5", "refresh_sources", show=False),
    ]

    def __init__(self, windows: list[WindowTarget], current: int | None = None, **kwargs):
        super().__init__(**kwargs)
        self._sources = [*windows, None]
        self._cards = [SourceCard(index, source) for index, source in enumerate(self._sources)]
        self._source_index = next((i for i, source in enumerate(self._sources)
            if (source.hwnd if source else 0) == current), 0)
        self.AUTO_FOCUS = f"#source-card-{self._source_index}"
        self._column_count = 3
        self._preview_revision = 0
        self._preview_stop = threading.Event()
        self._preview_queue = queue.Queue()
        self._preview_requested = set()
        self._preview_timer = None
        self._refreshing_sources = False

    def compose(self):
        with Vertical(id="source-picker-container"):
            with Horizontal(id="source-picker-head"):
                lights = Text()
                for color in ("#ff5f57", "#febc2e", "#28c840"):
                    lights.append("● ", color)
                yield Static(lights, id="source-picker-lights")
                yield Static("Pilih sumber", id="source-picker-title")
                yield Button("Segarkan", id="source-picker-refresh", tooltip="Perbarui jendela dan thumbnail (F5)", compact=True)
                yield Button("×", id="source-picker-close", tooltip="Tutup (Esc)", compact=True)
            with _SourceScroll(id="source-picker-scroll"):
                with Vertical(id="source-picker-grid"):
                    yield from self._cards
            with Horizontal(id="source-picker-footer"):
                yield Static("Panah pilih · Enter konfirmasi", id="source-picker-hint")
                yield Button("Batal", id="source-picker-cancel", compact=True)
                yield Button("Pilih", id="source-picker-confirm", compact=True)

    def on_mount(self):
        app = self.app

        def work():
            while not self._preview_stop.is_set():
                job = self._preview_queue.get()
                if job is None or self._preview_stop.is_set():
                    return
                revision, index, target = job
                if revision != self._preview_revision:
                    continue
                try:
                    thumbnail = capture_thumbnail(target)
                except Exception:
                    thumbnail = None
                if self._preview_stop.is_set():
                    if thumbnail is not None:
                        thumbnail.close()
                    return
                try:
                    app.call_from_thread(self._show_preview, revision, index, thumbnail)
                except RuntimeError:
                    if thumbnail is not None:
                        thumbnail.close()
                    return

        self.run_worker(work, thread=True, group="source-thumbnails", exit_on_error=False)
        self.call_after_refresh(self._reveal_selection)

    def on_resize(self, event):
        self._column_count = 1 if event.size.width < 68 else 2 if event.size.width < 106 else 3
        self.set_class(self._column_count == 1, "-narrow")
        self.set_class(self._column_count == 2, "-medium")
        if self.is_mounted:
            self.query_one("#source-picker-hint", Static).update(
                "↑↓ pilih" if self._column_count == 1 else "Panah pilih · Enter konfirmasi")
            self.call_after_refresh(self._reveal_selection)

    def _reveal_selection(self):
        if self._preview_stop.is_set() or not self.is_mounted or not self._cards[self._source_index].is_mounted:
            return
        self._select(self._source_index)

    def _schedule_previews(self):
        if self._preview_stop.is_set() or not self.is_mounted:
            return
        if self._preview_timer is not None:
            self._preview_timer.stop()
        # Tunggu layout/scroll stabil sebelum membaca posisi kartu.
        self._preview_timer = self.set_timer(.08, self._queue_visible)

    def _queue_visible(self):
        if self._preview_stop.is_set() or not self.is_mounted:
            return
        viewport = self.query_one("#source-picker-scroll").content_region
        for card in self._cards:
            key = self._preview_revision, card.index
            if card.is_mounted and not card.loaded and key not in self._preview_requested and card.region.overlaps(viewport):
                self._preview_requested.add(key)
                self._preview_queue.put((self._preview_revision, card.index, card.target))

    def _show_preview(self, revision, index, thumbnail):
        if self._preview_stop.is_set() or not self.is_mounted or revision != self._preview_revision:
            if thumbnail is not None:
                thumbnail.close()
            return
        card = self._cards[index]
        card.thumbnail, card.failed, card.loaded = thumbnail, thumbnail is None, True
        card.refresh()

    def _select(self, index: int, *, focus=True):
        self._source_index = max(0, min(index, len(self._cards) - 1))
        for card in self._cards:
            card.set_class(card.index == self._source_index, "-selected")
        card = self._cards[self._source_index]
        if focus and card.is_mounted:
            card.focus(scroll_visible=False)
            self.query_one("#source-picker-scroll", VerticalScroll).scroll_to_widget(card, animate=False, immediate=True)
        self._schedule_previews()

    def on_source_card_chosen(self, event: SourceCard.Chosen):
        event.stop()
        if self._source_index != event.index:
            self._select(event.index, focus=False)
        if event.confirm:
            self.action_confirm()

    def action_move(self, horizontal: int, vertical: int):
        self._select(self._source_index + horizontal + vertical * self._column_count)

    def action_edge(self, last: bool):
        self._select(len(self._cards) - 1 if last else 0)

    def action_confirm(self):
        target = self._sources[self._source_index]
        self._preview_stop.set()
        self._preview_queue.put(None)
        self.dismiss(str(target.hwnd if target else 0))

    def action_cancel(self):
        self._preview_stop.set()
        self._preview_queue.put(None)
        self.dismiss(None)

    async def action_refresh_sources(self):
        if self._refreshing_sources or self._preview_stop.is_set():
            return
        self._refreshing_sources = True
        try:
            await self._refresh_sources()
        finally:
            self._refreshing_sources = False

    async def _refresh_sources(self):
        current = self._sources[self._source_index]
        current_id = current.hwnd if current else 0
        try:
            windows = list_windows()
        except Exception:
            return
        self._preview_revision += 1
        self._preview_requested.clear()
        self._sources = [*windows, None]
        self._cards = [SourceCard(i, source) for i, source in enumerate(self._sources)]
        self._source_index = next((i for i, source in enumerate(self._sources)
            if (source.hwnd if source else 0) == current_id), 0)
        grid = self.query_one("#source-picker-grid")
        await grid.remove_children()
        if self._preview_stop.is_set() or not self.is_mounted:
            return
        await grid.mount(*self._cards)
        self.call_after_refresh(self._reveal_selection)

    async def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id in {"source-picker-close", "source-picker-cancel"}:
            self.action_cancel()
        elif event.button.id == "source-picker-confirm":
            self.action_confirm()
        elif event.button.id == "source-picker-refresh":
            await self.action_refresh_sources()

    def on_unmount(self):
        self._preview_stop.set()
        self._preview_queue.put(None)
        if self._preview_timer is not None:
            self._preview_timer.stop()

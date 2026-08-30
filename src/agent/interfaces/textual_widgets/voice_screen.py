"""Layar khusus /voice: visual reaktif tanpa duplikasi percakapan."""
from __future__ import annotations

import math
from collections.abc import Callable

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class VoiceOrb(Static):
    """Orb terminal yang bergerak menurut fase dan level suara."""

    _PALET = {
        "menyiapkan": ("#7868ff", "#50b8ff", "#d9e8ff"),
        "mendengar": ("#5877ff", "#57d5ff", "#d9f6ff"),
        "menangkap": ("#43bfff", "#53f0c0", "#e4fff7"),
        "berpikir": ("#8a62ff", "#d063ff", "#f4dcff"),
        "berbicara": ("#e45dff", "#ff7e9f", "#fff0b8"),
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._fase = "menyiapkan"
        self._level = 0.0
        self._bingkai = 0

    def set_state(self, fase: str, level: float) -> None:
        self._fase = fase if fase in self._PALET else "mendengar"
        self._level = max(0.0, min(1.0, float(level)))
        self._bingkai += 1
        self.refresh()

    def render(self) -> Text:
        # Orb sengaja menjadi elemen dominan layar voice. Ukuran mengikuti
        # ruang widget, sedangkan rasio sel terminal dikoreksi agar hasilnya
        # tetap tampak bulat pada terminal lebar maupun sempit.
        lebar = max(25, self.size.width - 2)
        tinggi = max(13, self.size.height - 1)
        radius_y = max(5.0, min((tinggi - 2) / 2, (lebar - 2) / 4))
        radius_x = radius_y * 2.0
        t = self._bingkai * 0.16
        level = self._level
        if self._fase == "berbicara":
            level = max(level, 0.54 + 0.22 * math.sin(t * 1.8))
        elif self._fase == "berpikir":
            level = max(level, 0.23 + 0.09 * math.sin(t))
        elif self._fase == "menyiapkan":
            level = max(level, 0.13 + 0.06 * math.sin(t * 0.8))
        else:
            level = max(level, 0.08 + 0.035 * math.sin(t * 0.7))

        utama, tengah, terang = self._PALET[self._fase]
        hasil = Text()
        for y in range(tinggi):
            ny = (y - (tinggi - 1) / 2) / radius_y
            for x in range(lebar):
                nx = (x - (lebar - 1) / 2) / radius_x
                sudut = math.atan2(ny, nx)
                jarak = math.sqrt(nx * nx + ny * ny)
                gelombang = math.sin(sudut * 3 + t) * 0.055 * level
                gelombang += math.sin(sudut * 5 - t * 1.35) * 0.035 * level
                radius = 0.78 + 0.16 * level + gelombang
                if jarak > radius:
                    hasil.append(" ")
                    continue
                isi = max(0.0, min(1.0, 1.0 - jarak / max(radius, 0.01)))
                kilau = math.sin(nx * 2.4 - ny * 1.7 + t) * 0.10
                nilai = max(0.0, min(1.0, isi + kilau + level * 0.10))
                if nilai > 0.72:
                    karakter, warna = "█", terang
                elif nilai > 0.43:
                    karakter, warna = "▓", tengah
                elif nilai > 0.18:
                    karakter, warna = "▒", utama
                else:
                    karakter, warna = "░", utama
                hasil.append(karakter, style=warna)
            if y != tinggi - 1:
                hasil.append("\n")
        return hasil


class VoiceScreen(ModalScreen[None]):
    """Overlay penuh /voice; percakapan tetap dirender di layar utama."""

    DEFAULT_CSS = """
    VoiceScreen {
        align: center middle;
        background: #080a12;
    }

    VoiceScreen #voice-stage {
        width: 100%;
        height: 100%;
        align: center middle;
        background: #080a12;
        padding: 1 2;
    }

    VoiceScreen #voice-orb {
        width: 92%;
        height: 1fr;
        min-height: 15;
        content-align: center middle;
        text-align: center;
        background: transparent;
    }

    VoiceScreen #voice-close {
        width: 7;
        min-width: 7;
        height: 3;
        margin: 1 0 0 0;
        border: none;
        background: #171b29;
        color: #e8ecff;
        text-style: bold;
    }

    VoiceScreen #voice-close:hover,
    VoiceScreen #voice-close:focus {
        background: #252b40;
        color: white;
    }
    """

    BINDINGS = [Binding("escape", "close", show=False, priority=True)]

    def __init__(self, state_getter: Callable[[], tuple[str, float]],
                 on_close: Callable[[], None], **kwargs):
        super().__init__(**kwargs)
        self._state_getter = state_getter
        self._on_close = on_close
        # Jangan pakai nama ``_closing``: itu state internal MessagePump
        # Textual. Menimpanya membuat Prune ditolak dan screen tak pernah
        # selesai di-unmount sesudah Esc.
        self._dismiss_requested = False
        self._timer = None

    def compose(self):
        with Vertical(id="voice-stage"):
            yield VoiceOrb(id="voice-orb")
            yield Button("⌵", id="voice-close", variant="default")

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._tick)
        self._tick()

    def on_unmount(self) -> None:
        self._stop_animation()

    def _stop_animation(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def _tick(self) -> None:
        try:
            fase, level = self._state_getter()
            self.query_one("#voice-orb", VoiceOrb).set_state(fase, level)
        except Exception:
            # Animasi bersifat dekoratif; kegagalannya tak boleh menghentikan
            # sesi mikrofon yang masih menyimpan percakapan di layar utama.
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "voice-close":
            event.stop()
            self.action_close()

    def action_close(self) -> None:
        if self._dismiss_requested:
            return
        self._dismiss_requested = True
        self._stop_animation()
        try:
            self._on_close()
        finally:
            self.dismiss(None)

    def close_from_app(self) -> None:
        """Tutup tanpa memanggil callback penghentian untuk kedua kalinya."""
        if self._dismiss_requested:
            return
        self._dismiss_requested = True
        self._stop_animation()
        self.dismiss(None)

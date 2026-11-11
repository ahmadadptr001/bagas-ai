"""Layar khusus /voice: visual reaktif tanpa duplikasi percakapan."""
from __future__ import annotations

import math
from collections.abc import Callable

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ...ui import tema


class VoiceOrb(Static):
    """Orb terminal yang bergerak menurut fase dan level suara."""

    _PALET = {
        "menyiapkan": ("#344467", "#6488c8", "#bdd1f2"),
        "mendengar": ("#5877ff", "#57d5ff", "#d9f6ff"),
        "menangkap": ("#4472c4", "#67acff", "#c3e0ff"),
        "berpikir": ("#62589b", "#9c91d4", "#d7d2ef"),
        "berbicara": ("#416b98", "#78b0df", "#d0e9ff"),
        "berbicara-aman": ("#416b98", "#78b0df", "#d0e9ff"),
        "mengenali": ("#5877ff", "#57d5ff", "#d9f6ff"),
        "kurang-jelas": ("#b49155", "#e7c684", "#fff0c8"),
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
        lebar = max(1, self.size.width - 2)
        tinggi = max(1, self.size.height - 1)
        radius_y = max(1.0, min((tinggi - 2) / 2, (lebar - 2) / 4))
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
                # Kontur ringan dengan pusat kosong; intensitas mengikuti mic.
                if jarak < radius - 0.18:
                    if abs(ny) < 0.08 and abs(nx) < 0.5:
                        hasil.append("─", style=utama)
                    else:
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
        background: #050610 82%;
    }

    VoiceScreen #voice-stage {
        width: 76;
        max-width: 100%;
        height: 92%;
        align: center middle;
        background: $t-gema_bg;
        border: round $t-tepi;
        padding: 0 1;
    }

    VoiceScreen #voice-titlebar {
        width: 100%; height: 3; align-vertical: middle;
        border-bottom: solid $t-tepi_redup; background: $t-menu_bg;
        padding: 0 1;
    }
    VoiceScreen #voice-lights { width: 8; height: 1; }
    VoiceScreen #voice-title { width: 1fr; height: 1; color: $t-teks; }
    VoiceScreen #voice-caption {
        display: none;
        width: 100%; height: 1; color: $t-redup; text-align: center;
        margin-top: 1;
    }

    VoiceScreen #voice-orb {
        width: 100%;
        max-width: 64;
        height: 1fr;
        max-height: 14;
        min-height: 4;
        content-align: center middle;
        text-align: center;
        background: transparent;
    }

    VoiceScreen #voice-close {
        width: 18;
        min-width: 18;
        height: 1;
        min-height: 1;
        margin: 0;
        border: none;
        background: $t-menu_bg;
        color: $t-teks;
    }

    VoiceScreen #voice-close:hover,
    VoiceScreen #voice-close:focus {
        background: $t-menu_hover;
        color: $t-teks;
    }
    VoiceScreen #voice-status,
    VoiceScreen #voice-hint, VoiceScreen #voice-meter {
        width: 100%;
        height: 1;
        text-align: center;
        color: $t-teks;
    }
    VoiceScreen #voice-hint { color: $t-redup; margin-top: 1; height: 2; }
    VoiceScreen #voice-meter { color: $t-aksen; height: 1; }
    VoiceScreen #voice-controls {
        width: 100%; height: 3; margin-top: 1; align: center middle;
    }
    VoiceScreen #voice-footer {
        display: none;
        width: 100%; height: 1; text-align: center; color: $t-redup;
    }
    VoiceScreen.-compact #voice-stage { height: 100%; }
    VoiceScreen.-compact #voice-lights { display: none; }
    VoiceScreen.-compact #voice-caption { margin: 0; }
    VoiceScreen.-compact #voice-hint { margin: 0; }
    VoiceScreen.-compact #voice-controls { margin: 0; height: 2; }
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
            with Horizontal(id="voice-titlebar"):
                lights = Text()
                for color in ("#ff5f57", "#febc2e", "#28c840"):
                    lights.append("● ", style=color)
                yield Static(lights, id="voice-lights")
                yield Static("Percakapan suara", id="voice-title")
            yield Static("Bicara langsung, tanpa kata pemicu", id="voice-caption")
            yield VoiceOrb(id="voice-orb")
            yield Static("Menyiapkan mikrofon…", id="voice-status")
            yield Static("", id="voice-meter")
            yield Static("Beri jeda singkat untuk mengirim ucapan", id="voice-hint")
            with Horizontal(id="voice-controls"):
                yield Button("Tutup", id="voice-close", variant="default", compact=True,
                             tooltip="Esc / F4")
            yield Static("F4 / Esc untuk kembali ke chat", id="voice-footer")

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._tick)
        self._tick()

    def on_unmount(self) -> None:
        self._stop_animation()

    def on_resize(self, event) -> None:
        self.set_class(event.size.height < 30 or event.size.width < 70, "-compact")

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
            labels = {
                "menyiapkan": "Menyiapkan mikrofon…",
                "mendengar": "Mendengarkan",
                "menangkap": "Suaramu tertangkap",
                "mengenali": "Mengenali ucapan…",
                "berpikir": "Menyiapkan jawaban…",
                "berbicara": "Sedang menjawab",
                "berbicara-aman": "Sedang menjawab · mikrofon dijeda",
                "kurang-jelas": "Ucapan belum jelas, silakan ulangi",
            }
            self.query_one("#voice-status", Static).update(labels.get(fase, "Mendengarkan"))
            bars = round(max(0.0, min(1.0, level)) * 12)
            meter = Text()
            meter.append("━" * bars, style=tema.p("aksen"))
            meter.append("─" * (12 - bars), style=tema.p("tepi_redup"))
            self.query_one("#voice-meter", Static).update(meter)
            hint = {
                "menyiapkan": "Mikrofon dan pengenal suara sedang disiapkan",
                "menangkap": "Beri jeda singkat untuk mengirim ucapan",
                "mengenali": "Ucapan sedang diproses di perangkat ini",
                "berbicara": "Bicara untuk menyela jawaban",
                "berbicara-aman": "Mikrofon aktif kembali setelah suara selesai",
                "kurang-jelas": "Ulangi ucapan atau dekatkan mikrofon",
            }.get(fase, "Jeda untuk mengirim ucapan")
            self.query_one("#voice-hint", Static).update(hint)
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

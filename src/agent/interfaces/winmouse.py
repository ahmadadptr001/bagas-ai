"""Pembaca MOUSE inline untuk Windows (tanpa layar-penuh) — eksperimen.

Mengaktifkan input mouse pada konsol Windows (Console API) SEHINGGA klik bisa
dibaca SELAGI output tetap mengalir di terminal biasa (bukan alternate screen).
Dipakai CLI untuk membuka/menutup hasil langkah dengan KLIK, bukan shortcut.

Catatan: saat mode ini aktif, seleksi teks biasa jadi Shift+klik (karena
QuickEdit dimatikan supaya klik sampai ke aplikasi). Dikembalikan saat selesai.

Semua terisolasi di sini; kalau bukan Windows / gagal, pemanggil pakai jalur lain.
"""
from __future__ import annotations

import sys

try:
    import ctypes
    from ctypes import wintypes
    _OK = sys.platform == "win32"
except Exception:  # pragma: no cover
    _OK = False


def available() -> bool:
    return _OK


if _OK:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_MOUSE_INPUT = 0x0010
    ENABLE_QUICK_EDIT_MODE = 0x0040
    ENABLE_EXTENDED_FLAGS = 0x0080
    KEY_EVENT = 0x0001
    MOUSE_EVENT = 0x0002
    FROM_LEFT_1ST_BUTTON_PRESSED = 0x0001
    MOUSE_WHEELED = 0x0004

    class _COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _MOUSE_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("dwMousePosition", _COORD),
            ("dwButtonState", wintypes.DWORD),
            ("dwControlKeyState", wintypes.DWORD),
            ("dwEventFlags", wintypes.DWORD),
        ]

    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("UnicodeChar", wintypes.WCHAR),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class _EVENT_UNION(ctypes.Union):
        _fields_ = [
            ("KeyEvent", _KEY_EVENT_RECORD),
            ("MouseEvent", _MOUSE_EVENT_RECORD),
            ("pad", ctypes.c_byte * 20),
        ]

    class _INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT_UNION)]

    class _SMALL_RECT(ctypes.Structure):
        _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                    ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]

    class _CSBI(ctypes.Structure):
        _fields_ = [
            ("dwSize", _COORD), ("dwCursorPosition", _COORD),
            ("wAttributes", wintypes.WORD), ("srWindow", _SMALL_RECT),
            ("dwMaximumWindowSize", _COORD),
        ]

    class MouseReader:
        """Aktifkan input mouse, baca klik & tombol tanpa memblok."""

        def __init__(self) -> None:
            self._hin = _k32.GetStdHandle(STD_INPUT_HANDLE)
            self._hout = _k32.GetStdHandle(STD_OUTPUT_HANDLE)
            self._old = wintypes.DWORD()
            self._active = False

        def enable(self) -> bool:
            if not _k32.GetConsoleMode(self._hin, ctypes.byref(self._old)):
                return False
            new = (self._old.value | ENABLE_MOUSE_INPUT | ENABLE_EXTENDED_FLAGS
                   | ENABLE_PROCESSED_INPUT) & ~ENABLE_QUICK_EDIT_MODE
            if not _k32.SetConsoleMode(self._hin, new):
                return False
            self._active = True
            return True

        def disable(self) -> None:
            if self._active:
                _k32.SetConsoleMode(self._hin, self._old)
                self._active = False

        @property
        def active(self) -> bool:
            """True bila capture mouse sedang terpasang (klik bisa dibaca)."""
            return self._active

        def cursor_row(self) -> int | None:
            """Baris (buffer) posisi kursor kini = dasar region live saat ini."""
            info = _CSBI()
            if _k32.GetConsoleScreenBufferInfo(self._hout, ctypes.byref(info)):
                return int(info.dwCursorPosition.Y)
            return None

        def poll(self) -> list:
            """Kuras event yang tertunda -> daftar ('click', x, y) | ('key', ch).
            Non-blok: kembalikan [] bila tak ada."""
            n = wintypes.DWORD(0)
            if not _k32.GetNumberOfConsoleInputEvents(self._hin, ctypes.byref(n)):
                return []
            if n.value == 0:
                return []
            count = n.value
            buf = (_INPUT_RECORD * count)()
            read = wintypes.DWORD(0)
            if not _k32.ReadConsoleInputW(self._hin, buf, count, ctypes.byref(read)):
                return []
            out = []
            for i in range(read.value):
                rec = buf[i]
                if rec.EventType == MOUSE_EVENT:
                    me = rec.Event.MouseEvent
                    # dwEventFlags==0 -> klik/lepas tombol (bukan gerak/scroll)
                    if me.dwEventFlags == 0 and (
                            me.dwButtonState & FROM_LEFT_1ST_BUTTON_PRESSED):
                        out.append(("click", int(me.dwMousePosition.X),
                                    int(me.dwMousePosition.Y)))
                    elif me.dwEventFlags & MOUSE_WHEELED:
                        # SCROLL WHEEL: selama capture aktif, event ini DITELAN
                        # konsol (terminal tak menggulung sendiri). Laporkan ke
                        # pemanggil agar capture bisa DILEPAS sementara sehingga
                        # scroll kembali berfungsi normal.
                        out.append(("wheel",))
                elif rec.EventType == KEY_EVENT:
                    ke = rec.Event.KeyEvent
                    if ke.bKeyDown and ke.UnicodeChar:
                        out.append(("key", ke.UnicodeChar))
            return out

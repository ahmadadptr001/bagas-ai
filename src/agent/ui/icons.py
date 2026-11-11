"""Ikon footer dengan pemeriksaan glyph font terminal dan cadangan biasa."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sys


# Nerd Font / Unicode umum / ASCII. Semua ikon selebar satu sel dan
# menggunakan BMP agar tidak bergantung pada fallback emoji terminal.
_FOOTER = {
    "brand": ("\ueba6", "\u25b6", ">"),
    "model": ("\uee0d", "\u25c6", "*"),
    "web": ("\ueb01", "\u25ce", "@"),
    "git": ("\ue725", "\u251c", "|"),
    "changed": ("\ueae1", "\u2261", "~"),
    "live": ("\uead9", "\u25a3", "#"),
    "voice": ("\uf130", "\u266a", "o"),
    "loading": ("\ueb19", "\u25cc", "~"),
    "ready": ("\uea71", "\u25cf", "*"),
    "check": ("\ueab2", "\u2713", "+"),
    "error": ("\uea76", "\u00d7", "x"),
    "sidebar": ("\uebf3", "\u2261", "="),
    "sidebar_closed": ("\uec02", "\u2261", "="),
}


def _terminal_font() -> str | None:
    """Ambil profil yang benar-benar aktif; tidak mengubah pengaturan terminal."""
    if sys.platform != "win32" or not os.environ.get("WT_SESSION"):
        return None
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    base = Path(local)
    paths = [base / "Packages" / package / "LocalState/settings.json" for package in (
        "Microsoft.WindowsTerminal_8wekyb3d8bbwe",
        "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe",
    )] + [base / "Microsoft/Windows Terminal/settings.json"]
    for path in paths:
        try:
            # JSONC: string dipertahankan, termasuk // di URL. Komentar dan
            # trailing comma di luar string boleh dibuang.
            raw = re.sub(r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*.*?\*/|,\s*(?=[}\]])',
                         lambda match: match[1] or "", path.read_text(encoding="utf-8-sig"),
                         flags=re.S)
            settings = json.loads(raw)
            profiles = settings.get("profiles", {})
            if isinstance(profiles, list):
                defaults, entries = {}, profiles
            else:
                defaults, entries = profiles.get("defaults", {}), profiles.get("list", [])
            profile_id = os.environ.get("WT_PROFILE_ID", settings.get("defaultProfile", ""))
            profile = next((p for p in entries
                            if str(p.get("guid", "")).lower() == profile_id.lower()), None)
            if profile is None:
                continue
            return (profile.get("font", {}).get("face") or profile.get("fontFace")
                    or defaults.get("font", {}).get("face") or defaults.get("fontFace"))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


def _glyphs_in_font(face: str, characters: str) -> frozenset[str] | None:
    """Periksa indeks glyph native Windows; glyph kosong tidak dianggap ikon."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes as w
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [w.HDC], w.HDC
    gdi.CreateFontW.argtypes = [ctypes.c_int] * 5 + [w.DWORD] * 8 + [w.LPCWSTR]
    gdi.CreateFontW.restype = w.HFONT
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = [w.HDC, w.HANDLE], w.HANDLE
    gdi.GetGlyphIndicesW.argtypes = [w.HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(w.WORD), w.DWORD]
    gdi.GetGlyphIndicesW.restype = w.DWORD
    gdi.GetTextFaceW.argtypes, gdi.GetTextFaceW.restype = [w.HDC, ctypes.c_int, w.LPWSTR], ctypes.c_int
    gdi.DeleteObject.argtypes, gdi.DeleteObject.restype = [w.HANDLE], w.BOOL
    gdi.DeleteDC.argtypes, gdi.DeleteDC.restype = [w.HDC], w.BOOL
    dc = gdi.CreateCompatibleDC(None)
    font = gdi.CreateFontW(-16, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0, face)
    previous = None
    try:
        if not dc or not font:
            return None
        previous = gdi.SelectObject(dc, font)
        actual_face = ctypes.create_unicode_buffer(128)
        if not gdi.GetTextFaceW(dc, len(actual_face), actual_face) or actual_face.value.casefold() != face.casefold():
            return None
        indices = (w.WORD * len(characters))()
        if gdi.GetGlyphIndicesW(dc, characters, len(characters), indices, 1) == 0xFFFFFFFF:
            return None
        return frozenset(char for char, index in zip(characters, indices) if index not in (0, 0xFFFF))
    finally:
        if previous:
            gdi.SelectObject(dc, previous)
        if font:
            gdi.DeleteObject(font)
        if dc:
            gdi.DeleteDC(dc)


@lru_cache(maxsize=1)
def _font_coverage() -> frozenset[str] | None:
    face = _terminal_font()
    if face:
        try:
            return _glyphs_in_font(face, "".join(dict.fromkeys(c for icons in _FOOTER.values() for c in icons)))
        except (OSError, ValueError, AttributeError):
            pass
    return None


def footer_icon(name: str) -> str:
    nerd, ordinary, ascii_icon = _FOOTER[name]
    coverage = _font_coverage()
    if coverage is None:
        return ordinary
    return next((icon for icon in (nerd, ordinary) if icon in coverage), ascii_icon)

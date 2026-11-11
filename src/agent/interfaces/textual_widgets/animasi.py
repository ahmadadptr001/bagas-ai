"""Animasi kecil untuk panel yang muncul/hilang di footer.

Prinsipnya sengaja konservatif:

- Hanya OPACITY yang dianimasikan. Opacity tidak mengubah tinggi/lebar,
  jadi RichLog, sidebar, dan breakpoint responsif tidak ikut dihitung
  ulang — tidak ada lompatan tata letak di tengah streaming.
- Fade-in hanya saat widget benar-benar berubah dari tersembunyi ke
  tampil. Pemanggilan berulang tiap token (ThinkingBlock) tidak boleh
  merestart animasi dan membuat panel berkedip.
- Durasi pendek (~0.18 dtk): cukup terasa "datang", tidak menunda
  informasi. Bila animasi dimatikan (TEXTUAL_ANIMATIONS=none), Textual
  langsung melompat ke nilai akhir — widget tetap terlihat.
"""
from __future__ import annotations

from textual.widget import Widget

# Detik — fade-in panel footer. Sengaja di bawah 0.2 dtk.
DURASI_FADE = 0.18


def tampilkan_halus(widget: Widget, durasi: float = DURASI_FADE) -> None:
    """Tampilkan ``widget`` dengan fade-in singkat bila sebelumnya tersembunyi."""
    if widget.display:
        return
    try:
        widget.styles.opacity = 0.0
        widget.display = True
        widget.styles.animate("opacity", 1.0, duration=durasi)
    except Exception:  # noqa: BLE001 — belum ter-mount / animator mati
        # Jangan pernah meninggalkan widget pada opacity 0: lebih baik
        # tampil tanpa animasi daripada tak terlihat sama sekali.
        try:
            widget.display = True
            widget.styles.opacity = 1.0
        except Exception:
            pass

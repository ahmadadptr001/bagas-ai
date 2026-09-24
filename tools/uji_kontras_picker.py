# -*- coding: utf-8 -*-
from agent.ui import textual_theme, tema


def lum(h: str) -> float:
    h = h.lstrip("#")
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def f(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = f(r), f(g), f(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = lum(a), lum(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


checks = [
    ("sep redup/bg", "redup", "menu_bg"),
    ("hint redup/gema", "redup", "gema_bg"),
    ("title aksen/gema", "aksen", "gema_bg"),
    ("opt teks/bg", "menu_teks", "menu_bg"),
    ("opt teks/sorot", "menu_teks", "menu_sorot"),
    ("opt teks/hover", "menu_teks", "tepi_redup"),
    ("meta/gema", "menu_meta_teks", "gema_bg"),
    ("aktif pair", "menu_aktif_teks", "menu_aktif_bg"),
]
for tid in tema.TEMA:
    vv = textual_theme.variabel(tid)
    print("==", tid)
    for name, a, b in checks:
        c = contrast(vv["t-" + a], vv["t-" + b])
        flag = "OK " if c >= 4.5 else ("!! " if c >= 3 else "BAD")
        print(f"  {flag} {name:18} {c:.2f}  {vv['t-' + a]} on {vv['t-' + b]}")

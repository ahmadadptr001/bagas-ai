"""Emblem BagasAI: dua pita yang saling mengait, tanpa tulisan atau font."""
from __future__ import annotations

from functools import lru_cache


# Satu geometri dipakai untuk SVG dan pratinjau piksel terminal.
_PATHS = (
    (("M", 18, 18), ("L", 47, 18), ("C", 64, 18, 76, 29, 76, 44),
     ("C", 76, 55, 70, 63, 61, 68), ("L", 50, 57),
     ("C", 56, 54, 60, 50, 60, 44), ("C", 60, 38, 55, 34, 47, 34),
     ("L", 18, 34), ("Z",)),
    (("M", 78, 78), ("L", 49, 78), ("C", 32, 78, 20, 67, 20, 52),
     ("C", 20, 41, 26, 33, 35, 28), ("L", 46, 39),
     ("C", 40, 42, 36, 46, 36, 52), ("C", 36, 58, 41, 62, 49, 62),
     ("L", 78, 62), ("Z",)),
)


@lru_cache(maxsize=8)
def mark_image(color: str):
    from PIL import Image, ImageDraw

    scale = 4
    image = Image.new("RGBA", (96 * scale, 96 * scale))
    draw = ImageDraw.Draw(image)
    for path in _PATHS:
        points = []
        current = (0, 0)
        for command, *values in path:
            if command in {"M", "L"}:
                current = tuple(values)
                points.append(current)
            elif command == "C":
                start, a, b, end = current, values[:2], values[2:4], values[4:6]
                for step in range(1, 33):
                    t, u = step / 32, 1 - step / 32
                    points.append(tuple(u**3 * start[i] + 3*u*u*t*a[i]
                                        + 3*u*t*t*b[i] + t**3*end[i] for i in (0, 1)))
                current = tuple(end)
        draw.polygon([(round(x * scale), round(y * scale)) for x, y in points], fill=color)
    return image


def mark_svg(color: str = "#528bff") -> str:
    paths = "\n".join('  <path d="' + " ".join(
        str(command[0]) + " ".join(map(str, command[1:])) for command in path) + '"/>'
        for path in _PATHS)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" '
            'role="img" aria-label="BagasAI">\n'
            f'<g fill="{color}">\n{paths}\n</g>\n</svg>\n')

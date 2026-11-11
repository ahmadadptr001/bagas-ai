"""Pewarnaan kode tanpa parser native; indeks tetap mengikuti dokumen asli."""
from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.token import Comment, Error, Keyword, Name, Number, Operator, String
from pygments.util import ClassNotFound
from rich.style import Style


def code_highlights(text: str, path: Path, light: bool = False,
                    cancelled=lambda: False) -> dict[int, list[tuple[int, int, Style]]]:
    """Kembalikan rentang per baris, termasuk token multiline dan Unicode."""
    try:
        lexer = get_lexer_for_filename(path.name, text, stripnl=False, ensurenl=False)
    except ClassNotFound:
        lexer = TextLexer(stripnl=False, ensurenl=False)
    colors = [
        (Comment, "#008000" if light else "#6a9955"),
        (String, "#a31515" if light else "#ce9178"),
        (Number, "#098658" if light else "#b5cea8"),
        (Keyword.Type, "#267f99" if light else "#4ec9b0"),
        (Keyword, "#0000ff" if light else "#c586c0"),
        (Name.Function, "#795e26" if light else "#dcdcaa"),
        (Name.Class, "#267f99" if light else "#4ec9b0"),
        (Name.Tag, "#800000" if light else "#569cd6"),
        (Name.Attribute, "#e50000" if light else "#9cdcfe"),
        (Name.Variable, "#001080" if light else "#9cdcfe"),
        (Name.Builtin, "#267f99" if light else "#4ec9b0"),
        (Name.Constant, "#0070c1" if light else "#4fc1ff"),
        (Operator.Word, "#0000ff" if light else "#c586c0"),
        (Error, "#cd3131" if light else "#f48771"),
    ]
    starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
    spans: dict[int, list[tuple[int, int, Style]]] = {}
    styles = {}
    for index, (offset, token, value) in enumerate(lexer.get_tokens_unprocessed(text)):
        if index % 128 == 0 and cancelled():
            return {}
        if token not in styles:
            color = next((color for group, color in colors if token in group), None)
            styles[token] = Style(color=color, italic=token in Comment) if color else None
        style = styles[token]
        if style is None:
            continue
        row = bisect_right(starts, offset) - 1
        parts = value.split("\n")
        for part_index, part in enumerate(parts):
            length = len(part.rstrip("\r"))
            if length:
                col = offset - starts[row]
                spans.setdefault(row, []).append((col, col + length, style))
            offset += len(part)
            if part_index + 1 < len(parts):
                offset += 1
                row += 1
    return spans

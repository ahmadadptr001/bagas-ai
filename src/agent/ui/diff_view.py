"""Kartu diff terminal dengan gutter lama/baru dan warna mengikuti tema."""
from __future__ import annotations

import re

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import tema
from .textual_theme import latar_terang


class DiffView:
    def __init__(self, path: str, diff: str, *, is_new: bool = False,
                 deleted: bool = False, syntax=None, preview: int = 14,
                 interactive: bool = True, max_lines: int = 1500):
        self.path = path
        self.diff = diff
        self.is_new = is_new
        self.deleted = deleted
        self.syntax = syntax
        self.preview = preview
        self.interactive = interactive
        self.max_lines = max_lines
        self.terbuka = not interactive
        self.baris_judul: int | None = None
        self.rows = self._rows(diff)
        self.added = sum(row[0] == "+" for row in self.rows)
        self.removed = sum(row[0] == "-" for row in self.rows)

    @property
    def plain(self) -> str:
        return self.path + "\n" + self.diff

    @staticmethod
    def _rows(diff: str):
        rows = []
        old = new = 0
        in_hunk = False
        for line in diff.splitlines():
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if match:
                in_hunk = True
                old, new = (int(n) - 1 for n in match.groups())
                rows.append(("@", None, None, line))
            elif (not in_hunk and line.startswith(("---", "+++"))) or line.startswith(("diff ", "index ", "\\")):
                continue
            elif line.startswith("+"):
                new += 1
                rows.append(("+", None, new, line[1:]))
            elif line.startswith("-"):
                old += 1
                rows.append(("-", old, None, line[1:]))
            elif line.startswith(" "):
                old += 1
                new += 1
                rows.append((" ", old, new, line[1:]))
        return rows

    def __rich_console__(self, console, options):
        light = latar_terang()
        add_bg, del_bg = ("#e6ffec", "#ffebe9") if light else ("#10271e", "#301a23")
        add_fg, del_fg = ("#1a7f37", "#cf222e") if light else ("#8cdbad", "#ef9ca9")
        table = Table.grid(expand=True, padding=0)
        table.add_column(width=4, justify="right", no_wrap=True)
        table.add_column(width=5, justify="right", no_wrap=True)
        table.add_column(width=3, justify="center", no_wrap=True)
        table.add_column(ratio=1, overflow="fold")
        limit = self.max_lines if self.terbuka else self.preview
        for kind, old, new, code in self.rows[:limit]:
            if kind == "@":
                table.add_row("", "", "⋯", Text(code, style=tema.p("redup")))
                continue
            bg = add_bg if kind == "+" else del_bg if kind == "-" else tema.p("gema_bg")
            fg = add_fg if kind == "+" else del_fg if kind == "-" else tema.p("teks")
            body = Text(code, style=f"{fg} on {bg}")
            if kind != "-" and self.syntax and new and new <= len(self.syntax):
                body = self.syntax[new - 1].copy()
                body.style = f"{fg} on {bg}"
                body.no_wrap = False
                body.overflow = "fold"
            gutter = f"{tema.p('redup')} on {bg}"
            marker = Text(kind, style=f"bold {fg} on {bg}")
            table.add_row(Text(str(old or ""), style=gutter),
                          Text(str(new or ""), style=gutter), marker, body,
                          style=f"{fg} on {bg}")
        parts = [table]
        remaining = len(self.rows) - limit
        if remaining > 0:
            hint = f"  +{remaining} baris"
            if self.interactive and not self.terbuka:
                hint += " · klik untuk membuka"
            parts.append(Text(hint, style=tema.p("redup")))
        name = self.path.replace("\\", "/")
        try:
            from pathlib import Path
            from .. import config
            source = Path(self.path)
            if source.is_absolute():
                name = source.relative_to(config.PROJECT_ROOT).as_posix()
        except (ValueError, OSError):
            pass
        if options.max_width < 70:
            name = name.rsplit("/", 1)[-1]
        budget = max(12, options.max_width - 30)
        if len(name) > budget:
            name = "…" + name[-budget + 1:]
        label = Text()
        if self.interactive:
            label.append("▾ " if self.terbuka or len(self.rows) <= limit else "▸ ",
                         style=tema.p("redup"))
        label.append("📝 " + name, style=tema.p("teks"))
        if self.is_new:
            label.append(" · baru", style=tema.p("redup"))
        if self.deleted:
            label.append(" · dihapus", style=tema.p("exit_footer"))
        label.append(f"  +{self.added}", style=add_fg)
        label.append(f" −{self.removed}", style=del_fg)
        yield Panel(Group(*parts), title=label, title_align="left", box=box.ROUNDED,
                    padding=(0, 0), border_style=tema.p("tepi"),
                    style=f"{tema.p('teks')} on {tema.p('gema_bg')}")

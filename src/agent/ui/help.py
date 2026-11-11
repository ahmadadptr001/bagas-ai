"""Bantuan terminal yang mengikuti lebar layar dan palet aktif."""
from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import tema


GROUPS = (
    ("Percakapan", (
        ("/new", "", "Mulai sesi baru"),
        ("/reset", "", "Reset percakapan"),
        ("/delete", "", "Hapus sesi"),
        ("/clear", "", "Bersihkan layar"),
        ("/btw", "<pesan>", "Obrolan sampingan"),
        ("/export", "[path]", "Ekspor riwayat"),
    )),
    ("Model & tampilan", (
        ("/model", "[nama]", "Pilih model"),
        ("/effort", "[level]", "Atur usaha berpikir"),
        ("/theme", "[nama]", "Pilih tema"),
        ("/stream", "", "Tampilan mengalir"),
        ("/version", "", "Versi aplikasi"),
    )),
    ("Konteks & ingatan", (
        ("/compact", "", "Padatkan konteks"),
        ("/send-compact", "", "Kirim ingatan terakhir"),
        ("/memory", "[aksi]", "Kelola ingatan"),
        ("/add-dir", "<path>", "Tambah folder"),
        ("/dirs", "", "Folder konteks"),
        ("/rm-dir", "<path>", "Lepas folder"),
    )),
    ("Suara & media", (
        ("/mic", "[on|off]", "Bacakan jawaban"),
        ("/voice", "[off]", "Percakapan suara"),
        ("/live", "[on|off|pilih]", "Layar · alias /video"),
        ("/image", "<path>", "Baca gambar lokal"),
    )),
    ("Proyek", (
        ("/scan", "", "Segarkan peta proyek"),
        ("/review", "", "Periksa bug proyek"),
        ("/tim", "", "Tim review"),
    )),
    ("Integrasi", (
        ("/bot", "", "Bot Telegram"),
        ("/permissions-bot", "", "Izin bot Telegram"),
    )),
)


_COMMON = {"/new", "/clear", "/export", "/model", "/effort", "/theme",
           "/mic", "/voice", "/image", "/compact", "/memory", "/add-dir"}


class HelpContent:
    def __init__(self, *, cli: bool = False, full: bool = False):
        self.cli = cli
        self.full = full

    @property
    def groups(self):
        groups = GROUPS
        if not self.cli:
            # Menu ini belum menjalankan aksi yang dijanjikan pada Textual.
            unavailable = {"/tim", "/delete", "/bot", "/permissions-bot"}
            groups = tuple((title, tuple(row for row in commands if row[0] not in unavailable))
                           for title, commands in groups
                           if any(row[0] not in unavailable for row in commands))
        if not self.full:
            return tuple((title, tuple(row for row in commands if row[0] in _COMMON))
                         for title, commands in groups
                         if any(row[0] in _COMMON for row in commands))
        if not self.cli:
            return groups
        additions = {
            "Percakapan": (("/menu", "", "Menu interaktif"),),
            "Model & tampilan": (("/update", "", "Cek pembaruan"),),
            "Konteks & ingatan": (("/scripts", "", "Skrip tersimpan"),),
        }
        return tuple((title, commands + additions.get(title, ()))
                     for title, commands in groups)

    @property
    def plain(self) -> str:
        lines = ["Bantuan · bagas-ai"]
        for title, commands in self.groups:
            lines.extend(("", title))
            lines.extend(f"{command} {args}  {description}".rstrip()
                         for command, args, description in commands)
        lines.extend(("", "/help semua  Semua perintah", "/exit  Keluar",
                      self._shortcuts))
        return "\n".join(lines)

    @property
    def _shortcuts(self) -> str:
        return ("Enter kirim · Tab lengkapi" if self.cli else
                "Enter kirim · Tab lengkapi · F4 suara")

    @staticmethod
    def _category(title, commands, width):
        heading = Text(title, style=f"bold {tema.p('teks')}")
        if width < 38:
            rows = []
            for command, args, description in commands:
                line = Text(command, style=tema.p("aksen"))
                if args:
                    line.append(" " + args, style=tema.p("redup"))
                line.append("\n  " + description, style=tema.p("redup"))
                rows.append(line)
            return Group(heading, *rows)
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(width=18, overflow="fold")
        table.add_column(ratio=1)
        for command, args, description in commands:
            name = Text(command, style=tema.p("aksen"))
            if args:
                name.append(" " + args, style=tema.p("redup"))
            table.add_row(name, Text(description, style=tema.p("redup")))
        return Group(heading, table)

    def __rich_console__(self, console, options):
        width = max(1, options.max_width - 4)
        title = Text("Bantuan", style=f"bold {tema.p('teks')}")
        title.append("  ·  bagas-ai", style=tema.p("redup"))
        parts = [title, Text("")]
        if width >= 84:
            columns = Table.grid(expand=True, padding=(0, 3))
            columns.add_column(ratio=1)
            columns.add_column(ratio=1)
            cell_width = (width - 3) // 2
            groups = self.groups
            for index in range(0, len(groups), 2):
                if index:
                    columns.add_row(Text(""), Text(""))
                columns.add_row(*(self._category(title, commands, cell_width)
                                  for title, commands in groups[index:index + 2]))
            parts.append(columns)
        else:
            for index, (heading, commands) in enumerate(self.groups):
                if index:
                    parts.append(Text(""))
                parts.append(self._category(heading, commands, width))
        parts.append(Text(""))
        footer = Text("/help" if self.full else "/help semua", style=tema.p("aksen"))
        footer.append(" ringkas    " if self.full else " semua perintah    ", style=tema.p("redup"))
        footer.append("/exit", style=tema.p("exit_footer"))
        footer.append(" keluar", style=tema.p("redup"))
        parts.append(footer)
        yield Panel(Group(*parts), box=box.ROUNDED, padding=(0, 1),
                    border_style=tema.p("tepi_redup"), style=f"on {tema.p('menu_bg')}")

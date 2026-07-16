"""Antarmuka CLI bagasAI (sinkron & bersih).

Desain: rich memegang terminal penuh (warna/emoji/panel mulus, tanpa bocor kode
ANSI). Animasi loading realtime (spinner + token + waktu) NEMPEL inline pada tiap
task via rich Live. Input pakai prompt_toolkit (hanya saat idle) supaya
Ctrl+Backspace bisa hapus per-kata. Tanpa antrean — satu tugas satu waktu.
"""
from __future__ import annotations

import difflib
import re
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from InquirerPy import inquirer  # noqa: E402
from InquirerPy.base.control import Choice  # noqa: E402
from openai import RateLimitError  # noqa: E402
from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.formatted_text import HTML  # noqa: E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: E402
from prompt_toolkit.styles import Style as PTStyle  # noqa: E402
from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.padding import Padding  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

try:
    from pyfiglet import Figlet  # noqa: E402
except Exception:  # pragma: no cover
    Figlet = None  # type: ignore

from .. import config, interaction, llm, longmem, models, prefs, scripts, updater  # noqa: E402
from .. import session as session_mod  # noqa: E402
from ..core import Agent  # noqa: E402
from ..session import Session  # noqa: E402

console = Console()  # auto-detect VT (Windows Terminal) -> warna/emoji mulus

# Padding tepi supaya konten tidak mepet ke pinggir terminal (kiri/kanan/bawah).
_LPAD = 2


def pout(renderable, *, bottom: int = 1) -> None:
    """Cetak renderable dengan padding kiri/kanan (+bawah) yang konsisten."""
    console.print(Padding(renderable, (0, _LPAD, bottom, _LPAD)))

# Gradasi ungu -> biru (magenta neon) untuk teks shadow.
_GRAD = ["#f0abfc", "#e879f9", "#c084fc", "#a855f7", "#7c3aed", "#4f46e5", "#2563eb"]

# ASCII "virus" (spike protein) — aksen di atas logo.
_VIRUS = r"""
        .  o   o  .
      o   \  |  /   o
   o ---- ( ((*)) ) ---- o
      o   /  |  \   o
        '  o   o  '
"""


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _fmt_elapsed(sec: float) -> str:
    """Format durasi bertingkat: <60s -> '12.3s', lalu 'm s', 'h m', 'd h'."""
    if sec < 60:
        return f"{sec:.1f}s"
    total = int(sec)
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


# Warna gaya editor (GitHub-like): teks terang di atas bg gelap hijau/merah.
_ADD = "#c9f5cf on #123d1c"
_DEL = "#f5c9c9 on #3d1212"
_CTX = "grey50"
_GUT_A = "#5bd66f on #0d2a14"
_GUT_D = "#e06b6b on #2a0d0d"


def _row(lineno: str, sign: str, text: str, style: str) -> None:
    """Cetak satu baris gaya editor '123 + kode' dengan bg + margin tepi."""
    inner = min(console.width - 2 * _LPAD, 108)
    line = Text(" " * _LPAD)  # margin kiri tanpa background
    line.append(f" {lineno:>4} {sign} ", style=style)
    body = f"{text}".replace("\t", "    ")
    line.append(body, style=style)
    pad = (_LPAD + inner) - line.cell_len  # isi bg sampai batas kanan
    if pad > 0:
        line.append(" " * pad, style=style)
    console.print(line)


def _print_diff(path: str, old: str, new: str, is_new: bool, limit: int = 200) -> None:
    """Tampilan editor: header status + line-numbered diff (bg hijau/merah)."""
    icon, label = ("✨", "dibuat") if is_new else ("📝", "diubah")
    console.print(f"\n  [bold]{icon} [cyan]{path}[/cyan][/bold] [dim]({label})[/dim]")
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     lineterm="", n=2))
    body = diff[2:] if len(diff) >= 2 and diff[0].startswith("---") else diff
    old_ln = new_ln = 0
    shown = 0
    for line in body:
        if shown >= limit:
            console.print("  [dim]... (diff dipotong)[/dim]")
            break
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
            if shown:
                console.print("  [dim]⋮[/dim]")
            continue
        tag, content = line[:1], line[1:]
        if tag == "+":
            _row(str(new_ln), "+", content, _ADD)
            new_ln += 1
        elif tag == "-":
            _row(str(old_ln), "-", content, _DEL)
            old_ln += 1
        else:
            _row(str(new_ln), " ", content, _CTX)
            old_ln += 1
            new_ln += 1
        shown += 1


def _print_delete(path: str, content: str, limit: int = 80) -> None:
    console.print(f"\n  [bold]🗑 [cyan]{path}[/cyan][/bold] [dim](dihapus)[/dim]")
    for i, line in enumerate(content.splitlines(), start=1):
        if i > limit:
            console.print("  [dim]... (dipotong)[/dim]")
            break
        _row(str(i), "-", line, _DEL)


def show_logo() -> None:
    m = " " * _LPAD  # indent kiri agar tidak mepet
    # Virus (hijau neon)
    for ln in _VIRUS.split("\n"):
        if ln.strip():
            console.print(Text(m + ln, style="bold green"))
    # Teks "bagasAI" gaya ANSI Shadow, gradasi per baris (efek glow/shadow)
    if Figlet is not None:
        try:
            art = Figlet(font="ansi_shadow").renderText("bagasAI")
            lines = [ln for ln in art.split("\n") if ln.strip()]
        except Exception:
            lines = ["b a g a s A I"]
    else:
        lines = ["b a g a s A I"]
    for i, ln in enumerate(lines):
        console.print(Text(m + ln, style=f"bold {_GRAD[min(i, len(_GRAD) - 1)]}"))
    sub = Text(m + "  ")
    sub.append("AI agent serbaguna  ", style="bold white")
    sub.append("· ditenagai NVIDIA (gratis)", style="dim italic")
    console.print(sub)


# ---------------------------------------------------------------------------
# Indikator "berpikir" realtime (rich Live) — nempel inline pada task
# ---------------------------------------------------------------------------
class Status:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.start = time.time()
        self.tool: str | None = None
        self.disp = 0.0
        self.retry_until = 0.0
        self.retry_msg = ""

    def note_retry(self, wait: float, msg: str) -> None:
        """Tandai bahwa bagasAI sedang menunggu rate limit lalu melanjutkan."""
        self.retry_until = time.time() + wait
        self.retry_msg = msg

    def __rich__(self) -> Text:
        el = time.time() - self.start
        now = time.time()
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]

        dot = "[#45475a]•[/]"

        # Mode menunggu rate limit: tampilkan hitung mundur + jaminan lanjut.
        if now < self.retry_until:
            left = self.retry_until - now
            t = Text()
            t.append(f"  {frame} ", style="bold #f9e2af")
            t.append("NVIDIA sibuk — menunggu lalu melanjutkan", style="#f9e2af")
            t.append(f"  {left:.0f}s", style="bold #fab387")
            if self.retry_msg:
                t.append(f"  ·  {self.retry_msg}", style="dim #f9e2af")
            t.append("     Ctrl+C batal", style="dim italic")
            return t

        target = float(self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30  # easing -> angka mengalir
        if abs(target - self.disp) < 1:
            self.disp = target
        t = Text()
        t.append(f"  {frame} ", style="bold #cba6f7")
        t.append("berpikir", style="#cba6f7")
        t.append(f"  {_fmt_elapsed(el)}", style="bold #89b4fa")
        t.append("   ")
        t.append_text(Text.from_markup(dot))
        t.append(f"  ⚡ {_fmt(int(self.disp))}", style="#f9e2af")
        t.append(" token", style="dim")
        if self.tool:
            t.append("   ")
            t.append_text(Text.from_markup(dot))
            t.append(f"  🔧 {self.tool}", style="#f5c2e7")
        t.append("     Ctrl+C batal", style="dim italic")
        return t


# ---------------------------------------------------------------------------
# Komponen tampilan
# ---------------------------------------------------------------------------
def _banner(agent: Agent, resumed: bool) -> Panel:
    spec = agent.model_spec
    kind = ("🧠 reasoning" if spec.reasoning
            else "🖼 multimodal" if spec.multimodal else "🤖 chat")
    # Kolom label rata kanan (abu) + nilai berwarna -> sejajar & profesional.
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="#7f849c", min_width=7)
    grid.add_column(overflow="fold")
    eff = (f"   [#7f849c]·[/]   [#f5c2e7]◇ {agent.effort}[/]"
           if agent.effort else "")
    tag = "dilanjutkan" if resumed else "sesi baru"
    grid.add_row("Model", f"[bold #89b4fa]{spec.label}[/]   [dim]{kind}[/]{eff}")
    grid.add_row("Folder", f"[#a6e3a1]{config.PROJECT_ROOT}[/]")
    grid.add_row("Sesi", f"[#f9e2af]{agent.session.id}[/]   [dim]· {tag}[/]")

    head = Text.assemble(
        ("● ", "bold #a6e3a1"), ("siap", "bold #a6e3a1"),
        ("   dimana pun, dari terminal ini", "dim italic"),
    )
    hint = Text.from_markup(
        "[dim]ketik pesan untuk mengobrol[/dim]   "
        "[#94e2d5]/menu[/] [dim]menu[/dim]   "
        "[#94e2d5]/model[/] [dim]ganti model[/dim]   "
        "[#f38ba8]/exit[/] [dim]keluar[/dim]"
    )
    body = Group(head, Text(), grid, Rule(style="#313244"), hint)
    return Panel(body, border_style="#cba6f7", box=box.ROUNDED, padding=(1, 2),
                 title="[bold #cba6f7]⬢ bagasAI[/]", title_align="left",
                 subtitle="[dim]NVIDIA · gratis[/dim]", subtitle_align="right")


def _models_panel(current_id: str) -> Panel:
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=False)
    tbl.add_column("#", style="dim", justify="right")
    tbl.add_column("alias", style="bold cyan")
    tbl.add_column("model", style="white")
    tbl.add_column("kemampuan", style="dim")
    tbl.add_column("aktif", justify="center")
    for i, key, spec in models.catalog():
        tags = []
        if spec.multimodal:
            tags.append("multimodal")
        if spec.reasoning:
            tags.append("thinking")
        mark = "[bold green]●[/bold green]" if spec.id == current_id else ""
        tbl.add_row(str(i), key, spec.label, ", ".join(tags) or "-", mark)
    return Panel(tbl, title="[bold]🔀 Model tersedia[/bold]", border_style="cyan",
                 box=box.ROUNDED)




# ---------------------------------------------------------------------------
# Loop utama
# ---------------------------------------------------------------------------
def main(resume: bool = False) -> None:
    config.require_api_key()
    console.clear()

    resumed = False
    if resume:
        session = session_mod.latest()
        if session:
            resumed = True
        else:
            session = Session.create()
    else:
        session = Session.create()

    agent = Agent(session=session)

    show_logo()
    console.print()
    pout(_banner(agent, resumed), bottom=0)
    if resumed:
        console.print(Padding(Rule("[dim]percakapan sebelumnya[/dim]",
                                    style="#313244"), (1, 0, 0, 0)))
        for m in agent.memory.messages:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                console.print(f"\n  [bold #cba6f7]❯[/] [#cba6f7]{content}[/]")
            elif role == "assistant" and content:
                console.print("\n  [bold #89b4fa]🤖 bagasAI[/]")
                console.print(Padding(Markdown(content), (0, 3, 1, 3)))
        console.print(Rule("[dim]lanjut di bawah[/dim]", style="#313244"))
    console.print()

    live_holder: dict = {"live": None}
    status_obj = Status(agent)
    # Total token PERSISTEN lintas semua sesi ("dimanapun").
    # "sesi" (agent.tokens_session) kini persisten per-sesi (ikut saat --resume),
    # dan sudah termasuk di total global. Agar tidak dobel saat resume, base =
    # total global dikurangi token sesi yang sudah dihitung.
    grand = {"base": prefs.get_total_tokens() - agent.tokens_session.total}

    def _save_total() -> None:
        prefs.set_total_tokens(grand["base"] + agent.tokens_session.total)

    def choice_handler(question: str, options: list[str], multiple: bool) -> str:
        live = live_holder.get("live")
        if live:
            live.stop()
        console.print(f"\n[bold yellow]❔ {question}[/bold yellow]")
        try:
            if multiple:
                res = inquirer.checkbox(
                    message=question, choices=options, pointer="❯",
                    instruction="(spasi pilih, enter konfirmasi)").execute()
                answer = ", ".join(res) if res else "(tidak memilih)"
            else:
                answer = inquirer.select(
                    message=question, choices=options, pointer="❯").execute()
        except (KeyboardInterrupt, EOFError):
            answer = "(dibatalkan)"
        console.print(f"[dim]-> {answer}[/dim]")
        if live:
            live.start()
        return answer

    interaction.set_choice_handler(choice_handler)

    def on_tool(name: str, args: dict) -> None:
        """Progres inline: perubahan kode ditampilkan sebagai diff berwarna."""
        status_obj.tool = name
        p = args.get("path") if isinstance(args, dict) else None
        if name == "write_file" and p:
            full = config.PROJECT_ROOT / p
            exists = full.exists()
            old = full.read_text(encoding="utf-8", errors="replace") if exists else ""
            new = args.get("content", "") if isinstance(args, dict) else ""
            _print_diff(p, old, new, is_new=not exists)
        elif name == "delete_file" and p:
            full = config.PROJECT_ROOT / p
            content = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
            _print_delete(p, content)
        elif name == "read_file" and p:
            console.print(f"  [dim]📖  membaca [cyan]{p}[/cyan][/dim]")
        elif name == "list_dir":
            console.print(f"  [dim]📂  melihat isi [cyan]{args.get('path', '.')}[/cyan][/dim]")
        elif name == "web_search":
            console.print(f"  [dim]🔎  mencari: {args.get('query', '')}[/dim]")
        elif name == "run_command":
            console.print(f"  [dim]▶  menjalankan[/dim] [white]{args.get('command', '')}[/white]")
        elif name == "run_python":
            console.print("  [dim]▶  menjalankan kode Python[/dim]")
        elif name == "run_script":
            console.print(f"  [dim]▶  menjalankan skrip [cyan]{args.get('name', '')}[/cyan][/dim]")
        elif name == "save_script":
            console.print(f"  [green]✚  menyimpan skrip [bold]{args.get('name', '')}[/bold][/green]")

    def process(text: str) -> None:
        nonlocal status_obj
        status_obj = Status(agent)
        header = {"shown": False}

        def say(content: str) -> None:
            """Tampilkan ucapan/narasi bagasAI: 1 header per giliran, indentasi rapi."""
            if not content or not content.strip():
                return
            console.print()
            if not header["shown"]:
                console.print("  [bold #89b4fa]🤖 bagasAI[/]")
                header["shown"] = True
            console.print(Padding(Markdown(content.strip()), (0, 3, 1, 3)))

        def on_retry(attempt: int, wait: float, exc: Exception) -> None:
            """NVIDIA rate-limit: bagasAI menunggu lalu MELANJUTKAN, bukan gagal."""
            status_obj.note_retry(wait, f"percobaan ke-{attempt}")

        try:
            with Live(status_obj, console=console, refresh_per_second=12,
                      transient=True) as live:
                live_holder["live"] = live
                answer = agent.run(text, on_tool=on_tool, on_message=say,
                                   on_retry=on_retry)
            live_holder["live"] = None
            say(answer)
        except KeyboardInterrupt:
            # Memory sudah dirapikan & disimpan di dalam agent.run(); di sini
            # cukup tampilkan pesan ke pengguna.
            live_holder["live"] = None
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        except RateLimitError:
            live_holder["live"] = None
            console.print("\n  [yellow]⏳ rate limit NVIDIA (~40 permintaan/menit) — "
                          "tunggu ~1 menit lalu coba lagi[/yellow]\n")
        except Exception as exc:  # noqa: BLE001
            live_holder["live"] = None
            console.print(f"\n  [red]✖ error:[/red] {exc}\n")

    # --- aksi menu (inquirer) ---
    def pick_model() -> None:
        def _describe(spec) -> str:
            # Satu baris: nama (rata) + badge kemampuan + SARAN "cocok untuk apa".
            badge = ""
            if spec.reasoning:
                badge += "🧠"
            if spec.multimodal:
                badge += "🖼"
            badge = f" {badge}" if badge else "  "
            note = f"  —  {spec.note}" if spec.note else ""
            return f"{spec.label:<28}{badge}{note}"

        choices = [
            Choice(key, _describe(spec)) for _, key, spec in models.catalog()
        ]
        try:
            sel = inquirer.select(
                message="Pilih model (tiap model ada sarannya)",
                choices=choices, pointer="❯",
                default=next((k for _, k, s in models.catalog()
                              if s.id == agent.model), None),
            ).execute()
            console.print(f"[green]✓ Model: {agent.set_model(sel)}[/green] "
                          f"[dim]({agent.model})[/dim]")
        except (KeyboardInterrupt, EOFError):
            pass

    def pick_effort() -> None:
        if not agent.model_spec.supports_effort():
            console.print(
                f"  [dim]Model [bold]{agent.model_spec.label}[/bold] menjawab langsung "
                "— tidak punya mode berpikir yang bisa diatur.[/dim]"
            )
            return
        choices = [
            Choice(key, f"{icon}  {title:<9} —  {desc}")
            for key, title, desc, icon in agent.model_spec.effort_info()
        ]
        try:
            sel = inquirer.select(
                message="Mode berpikir — seberapa dalam bagasAI menalar?",
                choices=choices, default=agent.effort, pointer="❯",
                long_instruction="Makin dalam = makin cermat tapi lebih lambat & boros token.",
            ).execute()
            agent.set_effort(sel)
            title = dict((k, t) for k, t, _, _ in agent.model_spec.effort_info()).get(sel, sel)
            console.print(f"  [green]✓ Mode berpikir: [bold]{title}[/bold][/green]")
        except (KeyboardInterrupt, EOFError):
            pass

    def delete_sessions() -> None:
        sessions = session_mod.list_sessions()
        if not sessions:
            console.print("[dim](tidak ada sesi di folder ini)[/dim]")
            return
        try:
            if len(sessions) == 1:
                s = sessions[0]
                if inquirer.confirm(
                        message=f"Hapus sesi {s.id} ({session_mod.user_msg_count(s)} pesan)?",
                        default=False).execute():
                    session_mod.delete(s)
                    console.print("[green]✓ 1 sesi dihapus.[/green]")
                return
            choices = [Choice(s.path.name,
                              f"{s.id}  ({session_mod.user_msg_count(s)} pesan)"
                              + ("  (aktif)" if s.id == agent.session.id else ""))
                       for s in sessions]
            picked = inquirer.checkbox(message="Pilih sesi untuk DIHAPUS",
                                       choices=choices, pointer="❯",
                                       instruction="(spasi pilih, enter konfirmasi)").execute()
        except (KeyboardInterrupt, EOFError):
            return
        count = sum(1 for s in sessions if s.path.name in picked and session_mod.delete(s))
        console.print(f"[green]✓ {count} sesi dihapus.[/green]" if count
                      else "[dim](tidak ada yang dihapus)[/dim]")

    def show_help() -> None:
        c = "#94e2d5"
        pout(Panel(
            "[dim]ketik pesan biasa untuk mengobrol dengan bagasAI[/dim]\n\n"
            f"[{c}]/menu[/]    menu interaktif        [{c}]/model[/]   pilih model + saran\n"
            f"[{c}]/effort[/]  mode berpikir           [{c}]/new[/]     sesi baru\n"
            f"[{c}]/delete[/]  hapus sesi              [{c}]/reset[/]   kosongkan riwayat\n"
            f"[{c}]/memory[/]  memori jangka panjang   [{c}]/scripts[/] skrip tersimpan\n"
            f"[{c}]/clear[/]   bersihkan layar         [{c}]/update[/]  cek pembaruan\n"
            f"[dim]                                 [/dim][#f38ba8]/exit[/]    keluar",
            title="[bold #cba6f7]❔ Bantuan[/]", title_align="left",
            border_style="#cba6f7", box=box.ROUNDED, padding=(1, 2)))

    def do_update() -> None:
        console.print("\n  [dim]🔄 memeriksa pembaruan di GitHub…[/dim]")
        try:
            res = updater.check()
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memeriksa:[/red] {e}\n")
            return
        st = res.get("status")

        if st == "up_to_date":
            console.print(
                f"  [bold #a6e3a1]✓ bagasAI sudah versi terbaru.[/]  "
                f"[dim]({res.get('local','')})[/dim]\n"
            )
            return
        if st == "no_repo":
            console.print(
                "  [yellow]ℹ Instalasi ini bukan dari git,[/yellow] jadi tak bisa "
                "auto-update.\n  [dim]Pasang lewat installer "
                "(install.sh / install.ps1) agar /update aktif.[/dim]\n"
            )
            return
        if st == "no_git":
            console.print("  [red]✖ git tidak ditemukan[/red] — pasang git dulu.\n")
            return
        if st == "no_upstream":
            console.print("  [yellow]ℹ Tidak ada remote/upstream yang dilacak.[/yellow]\n")
            return
        if st == "fetch_error":
            console.print(f"  [red]✖ gagal fetch:[/red] {res.get('detail','')}\n")
            return
        if st != "update_available":
            console.print(f"  [red]✖ status tak terduga:[/red] {st}\n")
            return

        # Ada pembaruan.
        n = res.get("behind", "?")
        log = res.get("log", "")
        body = Text()
        body.append(f"{n} pembaruan tersedia  ", style="bold #f9e2af")
        body.append(f"({res.get('local','')} → {res.get('remote','')})\n\n",
                    style="dim")
        if log:
            for line in log.splitlines():
                body.append("  • ", style="#89b4fa")
                body.append(line + "\n")
        pout(Panel(body, title="[bold #cba6f7]🔄 Pembaruan bagasAI[/]",
                   title_align="left", border_style="#cba6f7",
                   box=box.ROUNDED, padding=(1, 2)))
        try:
            go = inquirer.confirm(message="Terapkan pembaruan sekarang?",
                                  default=True).execute()
        except (KeyboardInterrupt, EOFError):
            go = False
        if not go:
            console.print("  [dim](dilewati)[/dim]\n")
            return

        console.print("  [dim]⏳ menarik & memasang pembaruan…[/dim]")
        try:
            out = updater.apply()
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memperbarui:[/red] {e}\n")
            return
        if out.get("status") == "pull_error":
            console.print(f"  [red]✖ git pull gagal:[/red] {out.get('detail','')}\n")
            return
        if out.get("status") != "updated":
            console.print(f"  [red]✖ gagal:[/red] {out.get('status')}\n")
            return
        note = "" if out.get("reinstalled") else (
            f"  [dim](catatan pip: {out.get('pip_detail','')})[/dim]")
        console.print(
            "  [bold #a6e3a1]✓ bagasAI diperbarui![/]  "
            "[dim]jalankan ulang[/dim] [#94e2d5]bagasAI[/] "
            "[dim]agar perubahan aktif.[/dim]" + note + "\n"
        )

    def do_action(action: str) -> bool:
        nonlocal agent, session
        if action in ("exit", "quit"):
            return True
        if action == "model":
            pick_model()
        elif action == "effort":
            pick_effort()
        elif action == "delete":
            delete_sessions()
        elif action == "new":
            _save_total()  # persist kontribusi sesi lama ke total global
            session = Session.create()
            agent = Agent(session=session)
            grand["base"] = prefs.get_total_tokens()  # sesi baru mulai dari total
            console.clear()
            show_logo()
            console.print()
            pout(_banner(agent, False), bottom=0)
            console.print()
        elif action == "reset":
            agent.reset()
            console.print("[dim](riwayat dikosongkan)[/dim]")
        elif action == "clear":
            console.clear()
            show_logo()
            console.print()
            pout(_banner(agent, False), bottom=0)
            console.print()
        elif action == "memory":
            facts = longmem.all_facts()
            pout(Panel("\n".join(f"• {f}" for f in facts) or "[dim]kosong[/dim]",
                       title="[bold #a6e3a1]🧠 Memory jangka panjang[/]",
                       title_align="left", border_style="#a6e3a1",
                       box=box.ROUNDED, padding=(1, 2)))
        elif action == "scripts":
            items = scripts.index_list()
            txt = "\n".join(f"• [#89b4fa]{it['name']}[/]: {it.get('description') or '-'}"
                            for it in items) or "[dim]belum ada[/dim]"
            pout(Panel(txt, title="[bold #89b4fa]📜 Script memory[/]",
                       title_align="left", border_style="#89b4fa",
                       box=box.ROUNDED, padding=(1, 2)))
        elif action == "help":
            show_help()
        elif action == "update":
            do_update()
        elif action == "models":
            pout(_models_panel(agent.model))
        return False

    def open_menu() -> bool:
        try:
            action = inquirer.select(
                message="Menu bagasAI", pointer="❯",
                choices=[Choice("model", "🔀 Ganti model"),
                         Choice("effort", "🎚 Mode / effort"),
                         Choice("new", "✨ Sesi baru"),
                         Choice("delete", "🗑 Hapus sesi"),
                         Choice("memory", "🧠 Memory"),
                         Choice("scripts", "📜 Scripts"),
                         Choice("reset", "🧹 Reset riwayat"),
                         Choice("clear", "🖥 Bersihkan layar"),
                         Choice("update", "🔄 Cek pembaruan"),
                         Choice("help", "❔ Bantuan"),
                         Choice("exit", "🚪 Keluar"),
                         Choice("cancel", "↩ Batal")]).execute()
        except (KeyboardInterrupt, EOFError):
            return False
        if action == "cancel":
            return False
        return do_action(action)

    # --- input (prompt_toolkit hanya saat idle) ---
    # Backspace polos = hapus 1 HURUF (perilaku default). Hapus 1 KATA hanya
    # saat menahan Ctrl (Ctrl+Backspace) atau Ctrl+W / Alt+Backspace.
    #
    # Tombol Backspace mengirim kode berbeda tergantung OS:
    #   - Windows conhost: Backspace polos = c-h (\x08); Ctrl+Backspace = backspace (\x7f)
    #   - Terminal Unix   : Backspace polos = backspace (\x7f); Ctrl+Backspace = c-h (\x08)
    # Jadi kita mengikat hapus-kata ke varian yang BUKAN Backspace polos di OS ini,
    # dan membiarkan Backspace polos memakai default (1 huruf).
    kb = KeyBindings()

    def _del_word(event):
        pos = event.current_buffer.document.find_start_of_previous_word()
        if pos is not None:
            event.current_buffer.delete_before_cursor(count=-pos)

    kb.add("c-w")(_del_word)  # Ctrl+W (universal)
    kb.add("escape", "backspace")(_del_word)  # Alt+Backspace
    if sys.platform == "win32":
        kb.add("backspace")(_del_word)  # Windows: ini = Ctrl+Backspace
    else:
        kb.add("c-h")(_del_word)  # Unix: ini = Ctrl+Backspace
    # Gaya status bar: latar gelap "catppuccin" + aksen warna per segmen.
    _pt_style = PTStyle.from_dict({
        "bottom-toolbar": "bg:#181825 #cdd6f4 noreverse",
        "sep": "#45475a",
        "brand": "#cba6f7 bold",
        "model": "#89b4fa bold",
        "eff": "#f5c2e7",
        "sesi": "#f9e2af",
        "total": "#a6e3a1",
        "cmd": "#94e2d5",
        "exit": "#f38ba8",
        "muted": "#7f849c",
    })
    session_pt: PromptSession = PromptSession(key_bindings=kb, style=_pt_style)

    # Status bar token PERMANEN di paling bawah (selalu terlihat & rapi).
    def bottom_toolbar():
        s = agent.tokens_session
        total = grand["base"] + s.total
        spec = agent.model_spec
        kind = "🧠" if spec.reasoning else ("🖼" if spec.multimodal else "🤖")
        eff = f" <eff>◇ {agent.effort}</eff>" if agent.effort else ""
        sep = " <sep>│</sep> "
        return HTML(
            " <brand>⬢ bagasAI</brand>"
            + sep
            + f"{kind} <model>{spec.label}</model>{eff}"
            + sep
            + f"<sesi>⚡ {_fmt(s.total)}</sesi> <muted>sesi</muted>"
            + sep
            + f"<total>🔋 {_fmt(total)}</total> <muted>total</muted>"
            + sep
            + "<cmd>/menu</cmd> <muted>·</muted> <exit>/exit</exit> "
        )

    while True:
        try:
            text = session_pt.prompt(
                HTML('<style fg="#cba6f7"><b>❯</b></style> '),
                bottom_toolbar=bottom_toolbar).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not text:
            continue
        if text.startswith("/"):
            cmd = text[1:].strip().lower()
            if cmd == "menu":
                if open_menu():
                    break
            elif cmd.startswith("model ") or cmd == "model":
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    try:
                        console.print(f"[green]✓ Model: {agent.set_model(parts[1])}[/green]")
                    except ValueError as e:
                        console.print(f"[red]{e}[/red]")
                else:
                    pick_model()
            else:
                if do_action(cmd):
                    break
            continue
        process(text)
        _save_total()

    _save_total()
    console.clear()
    console.print("\n  [#cba6f7]⬢ bagasAI[/]  [dim]— sampai jumpa! 👋[/dim]\n")


if __name__ == "__main__":
    main()

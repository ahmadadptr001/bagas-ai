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

from .. import config, interaction, llm, longmem, models, prefs, scripts  # noqa: E402
from .. import session as session_mod  # noqa: E402
from ..core import Agent  # noqa: E402
from ..session import Session  # noqa: E402

console = Console()  # auto-detect VT (Windows Terminal) -> warna/emoji mulus

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


# Warna gaya editor (GitHub-like): teks terang di atas bg gelap hijau/merah.
_ADD = "#c9f5cf on #123d1c"
_DEL = "#f5c9c9 on #3d1212"
_CTX = "grey50"
_GUT_A = "#5bd66f on #0d2a14"
_GUT_D = "#e06b6b on #2a0d0d"


def _row(lineno: str, sign: str, text: str, style: str) -> None:
    """Cetak satu baris gaya editor: '  123 + kode' dengan bg selebar layar."""
    width = min(console.width, 110)
    line = Text()
    line.append(f" {lineno:>4} {sign} ", style=style)
    body = f"{text}".replace("\t", "    ")
    line.append(body, style=style)
    pad = width - line.cell_len
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
    # Virus (hijau neon)
    for ln in _VIRUS.split("\n"):
        if ln.strip():
            console.print(Text(ln, style="bold green"))
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
        console.print(Text(ln, style=f"bold {_GRAD[min(i, len(_GRAD) - 1)]}"))
    sub = Text()
    sub.append("   AI agent serbaguna  ", style="bold white")
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

        # Mode menunggu rate limit: tampilkan hitung mundur + jaminan lanjut.
        if now < self.retry_until:
            left = self.retry_until - now
            t = Text()
            t.append(f"  {frame} ", style="bold yellow")
            t.append("⏳ NVIDIA sibuk (rate limit) — menunggu lalu lanjut",
                     style="yellow")
            t.append(f"  {left:.0f}s", style="bold yellow")
            if self.retry_msg:
                t.append(f"  •  {self.retry_msg}", style="dim yellow")
            t.append("   (Ctrl+C batal)", style="dim italic")
            return t

        target = float(self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30  # easing -> angka mengalir
        if abs(target - self.disp) < 1:
            self.disp = target
        t = Text()
        t.append(f"  {frame} ", style="bold magenta")
        t.append("berpikir ", style="magenta")
        t.append(f"{el:.1f}s", style="bold cyan")
        t.append("  •  ", style="dim")
        t.append(f"⚡ {_fmt(int(self.disp))} token", style="yellow")
        if self.tool:
            t.append("  •  ", style="dim")
            t.append(f"🔧 {self.tool}", style="bright_magenta")
        t.append("   ", style="dim")
        t.append("(Ctrl+C batal)", style="dim italic")
        return t


# ---------------------------------------------------------------------------
# Komponen tampilan
# ---------------------------------------------------------------------------
def _banner(agent: Agent, resumed: bool) -> Panel:
    spec = agent.model_spec
    # Kolom label lebar tetap & rata kiri (tanpa emoji) -> semua baris sejajar.
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold magenta", justify="left", min_width=9)
    grid.add_column(overflow="fold")
    eff = f"  ·  effort [bold]{agent.effort}[/bold]" if agent.effort else ""
    tag = "(dilanjutkan)" if resumed else "(baru)"
    grid.add_row("Model", f"[bold cyan]{spec.label}[/bold cyan]{eff}")
    grid.add_row("Project", f"[green]{config.PROJECT_ROOT}[/green]")
    grid.add_row("Sesi", f"[yellow]{agent.session.id}[/yellow]  [dim]{tag}[/dim]")
    hint = ("[dim]Ketik pesan untuk mengobrol   [/dim][bold cyan]/menu[/bold cyan]"
            "[dim] menu   [/dim][bold cyan]/exit[/bold cyan][dim] keluar[/dim]")
    return Panel(Group(grid, Text(), Text.from_markup(hint)), border_style="magenta",
                 box=box.ROUNDED, padding=(1, 2),
                 title="[bold magenta]bagasAI[/bold magenta]")


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
    console.print(_banner(agent, resumed))
    if resumed:
        console.print("\n[dim]-- percakapan sebelumnya --[/dim]")
        for m in agent.memory.messages:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                console.print(f"\n[bold magenta]❯[/bold magenta] [magenta]{content}[/magenta]")
            elif role == "assistant" and content:
                console.print(Panel(Markdown(content),
                                    title="[bold cyan]🤖 bagasAI[/bold cyan]",
                                    title_align="left", border_style="cyan",
                                    box=box.ROUNDED, padding=(1, 2)))
        console.print("[dim]-- lanjut di bawah --[/dim]")
    console.print()

    live_holder: dict = {"live": None}
    status_obj = Status(agent)
    # Total token PERSISTEN lintas sesi ("dimanapun"). base = total sebelum sesi ini.
    grand = {"base": prefs.get_total_tokens()}

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
                console.print("  [bold cyan]🤖  bagasAI[/bold cyan]")
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
        choices = [Choice(key, f"{spec.label}"
                          + ("  · thinking/effort" if spec.reasoning else ""))
                   for _, key, spec in models.catalog()]
        try:
            sel = inquirer.select(message="Pilih model", choices=choices,
                                  pointer="❯").execute()
            console.print(f"[green]✓ Model: {agent.set_model(sel)}[/green] "
                          f"[dim]({agent.model})[/dim]")
        except (KeyboardInterrupt, EOFError):
            pass

    def pick_effort() -> None:
        if not agent.model_spec.supports_effort():
            console.print(f"[dim]Model {agent.model_spec.label} tidak punya mode/effort.[/dim]")
            return
        try:
            sel = inquirer.select(message="Mode berpikir (effort)",
                                  choices=list(agent.model_spec.effort_options().keys()),
                                  default=agent.effort, pointer="❯").execute()
            agent.set_effort(sel)
            console.print(f"[green]✓ Effort: {sel}[/green]")
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
        console.print(Panel(
            "ketik pesan biasa untuk mengobrol\n\n"
            "[cyan]/menu[/cyan]    menu interaktif       [cyan]/model[/cyan]   pilih model\n"
            "[cyan]/effort[/cyan]  mode berpikir          [cyan]/new[/cyan]     sesi baru\n"
            "[cyan]/delete[/cyan]  hapus sesi             [cyan]/reset[/cyan]   kosongkan riwayat\n"
            "[cyan]/memory[/cyan]  memori jangka panjang  [cyan]/scripts[/cyan] skrip tersimpan\n"
            "[cyan]/clear[/cyan]   bersihkan layar        [cyan]/exit[/cyan]    keluar",
            title="[bold]❔ Bantuan[/bold]", border_style="cyan", box=box.ROUNDED))

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
            grand["base"] += agent.tokens_session.total  # simpan total sebelum reset
            session = Session.create()
            agent = Agent(session=session)
            console.clear()
            show_logo()
            console.print()
            console.print(_banner(agent, False))
            console.print()
        elif action == "reset":
            agent.reset()
            console.print("[dim](riwayat dikosongkan)[/dim]")
        elif action == "clear":
            console.clear()
            show_logo()
            console.print()
            console.print(_banner(agent, False))
            console.print()
        elif action == "memory":
            facts = longmem.all_facts()
            console.print(Panel("\n".join(f"• {f}" for f in facts) or "[dim]kosong[/dim]",
                                title="[bold]🧠 Memory jangka panjang[/bold]",
                                border_style="green", box=box.ROUNDED))
        elif action == "scripts":
            items = scripts.index_list()
            txt = "\n".join(f"• [cyan]{it['name']}[/cyan]: {it.get('description') or '-'}"
                            for it in items) or "[dim]belum ada[/dim]"
            console.print(Panel(txt, title="[bold]📜 Script memory[/bold]",
                                border_style="blue", box=box.ROUNDED))
        elif action == "help":
            show_help()
        elif action == "models":
            console.print(_models_panel(agent.model))
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
                         Choice("help", "❔ Bantuan"),
                         Choice("exit", "🚪 Keluar"),
                         Choice("cancel", "↩ Batal")]).execute()
        except (KeyboardInterrupt, EOFError):
            return False
        if action == "cancel":
            return False
        return do_action(action)

    # --- input (prompt_toolkit hanya saat idle -> Ctrl+Backspace hapus kata) ---
    kb = KeyBindings()

    def _del_word(event):
        pos = event.current_buffer.document.find_start_of_previous_word()
        if pos is not None:
            event.current_buffer.delete_before_cursor(count=-pos)

    kb.add("c-h")(_del_word)
    kb.add("c-w")(_del_word)
    kb.add("escape", "backspace")(_del_word)
    session_pt: PromptSession = PromptSession(key_bindings=kb)

    # Status bar token PERMANEN di paling bawah (selalu terlihat).
    def bottom_toolbar():
        s = agent.tokens_session
        total = grand["base"] + s.total
        eff = f" · {agent.effort}" if agent.effort else ""
        return HTML(
            f"  🧠 <b>{agent.model_spec.label}</b>{eff}"
            f"     ⚡ sesi <b>{_fmt(s.total)}</b>"
            f"     🔋 total <b>{_fmt(total)}</b>"
            f"     💡 /menu  🚪 /exit "
        )

    while True:
        try:
            text = session_pt.prompt(HTML("<ansimagenta><b>❯</b></ansimagenta> "),
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
    console.print("[magenta]Sampai jumpa! 👋[/magenta]")


if __name__ == "__main__":
    main()

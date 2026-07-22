"""Wizard login/setup interaktif untuk bagas-ai.

Dipanggil lewat `bagas-ai login` (atau `bagas-ai setup`). Yang ditanyakan cuma
bot Telegram — dan itu pun OPSIONAL, karena bagas-ai tak lagi punya kredensial
wajib: seluruh modelnya berbasis browser dan memakai akun yang sudah kamu
pakai sehari-hari, login-nya lewat jendela Chrome saat model pertama kali
dipilih. Langkah "tempel API key NVIDIA" beserta validasinya ke endpoint sudah
DIHAPUS bersama model ber-API-key.
"""
from __future__ import annotations

from pathlib import Path

import requests

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import config

# Urutan penulisan key di file .env (agar rapi & mudah dibaca manusia).
_ENV_ORDER = [
    "CHAT_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "MAX_TOOL_ITERATIONS",
    "ALLOW_CODE_EXEC",
    "CODE_EXEC_TIMEOUT",
]

_DEFAULTS = {
    "CHAT_MODEL": "web/kimi",
    "MAX_TOOL_ITERATIONS": "8",
    "ALLOW_CODE_EXEC": "true",
    "CODE_EXEC_TIMEOUT": "30",
}


def _read_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def _write_env(path: Path, data: dict[str, str]) -> None:
    lines = [
        "# Konfigurasi bagas-ai — dibuat oleh 'bagas-ai login'.",
        "# Tidak ada API key: semua model bagas-ai lewat browser (login sekali",
        "# di jendela Chrome). TELEGRAM_BOT_TOKEN hanya perlu bila memakai bot.",
        "",
    ]
    for k in _ENV_ORDER:
        if k in data and data[k] != "":
            lines.append(f"{k}={data[k]}")
    for k, v in data.items():  # simpan key lain yang mungkin ditambahkan manual
        if k not in _ENV_ORDER:
            lines.append(f"{k}={v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_telegram(token: str) -> tuple[bool, str]:
    """Cek token bot Telegram via getMe. Return (ok, keterangan/username)."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        r = requests.get(url, timeout=20)
    except requests.RequestException as e:
        return False, f"koneksi gagal: {str(e)[:80]}"
    if r.status_code == 401:
        return False, "token tidak valid (401)"
    try:
        data = r.json()
    except ValueError:
        return False, f"HTTP {r.status_code}"
    if data.get("ok"):
        return True, "@" + str(data["result"].get("username", "bot"))
    return False, str(data.get("description", "token ditolak"))[:80]


def _prompt_secret(console: Console, message: str) -> str:
    """Minta input rahasia (tersembunyi). Pakai InquirerPy bila ada.

    Melempar EOFError bila stdin habis (mis. dijalankan non-interaktif) supaya
    pemanggil bisa berhenti dan tidak loop tak berujung.
    """
    try:
        from InquirerPy import inquirer  # type: ignore

        val = inquirer.secret(message=message, qmark="🔑", amark="🔑").execute()
        return (val or "").strip()
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        import getpass

        return getpass.getpass(message + " ").strip()


def run(console: Console | None = None) -> bool:
    """Wizard setup interaktif. Return True bila konfigurasi tersimpan.

    Dulu mengembalikan True hanya bila API key valid. Kini tak ada kredensial
    wajib, jadi wizard selalu berhasil selama file .env bisa ditulis — satu-
    satunya pertanyaan (bot Telegram) boleh dilewati."""
    console = console or Console()
    env = _read_env(config.ENV_FILE)
    for k, v in _DEFAULTS.items():
        env.setdefault(k, v)

    title = Text("bagas-ai", style="bold magenta")
    title.append("  ·  setup", style="dim")
    console.print(Panel(title, border_style="magenta", padding=(0, 2)))
    console.print(
        "  [dim]Tak ada API key yang perlu diisi.[/dim] Semua model bagas-ai "
        "lewat [bold]browser[/bold]\n"
        "  [dim]— login sekali di jendela Chrome saat kamu memilih model "
        "lewat[/dim] [bold cyan]/model[/bold cyan][dim].[/dim]\n"
    )

    # --- Telegram (opsional) ---
    want_tg = False
    try:
        from InquirerPy import inquirer  # type: ignore

        want_tg = inquirer.confirm(
            message="Hubungkan bot Telegram sekarang? (opsional)", default=False
        ).execute()
    except Exception:
        want_tg = False

    if want_tg:
        console.print(
            "  [dim]Buat bot & token di[/dim] [cyan]https://t.me/BotFather[/cyan]"
            " [dim](/newbot).[/dim]"
        )
        while True:
            token = _prompt_secret(console, "Tempel token bot Telegram:").strip()
            if not token:
                break
            console.print("  [dim]Memeriksa token…[/dim]")
            ok, info = validate_telegram(token)
            if ok:
                console.print(
                    f"  [bold green]✓ Bot terhubung[/bold green] "
                    f"[dim]({info})[/dim]\n"
                )
                env["TELEGRAM_BOT_TOKEN"] = token
                break
            console.print(f"  [red]✗ Token gagal:[/red] {info}\n")
            try:
                from InquirerPy import inquirer  # type: ignore

                if not inquirer.confirm(
                    message="Coba token lain?", default=True
                ).execute():
                    break
            except Exception:
                break

    # --- Simpan ---
    _write_env(config.ENV_FILE, env)
    console.print(f"  [green]✔ Konfigurasi disimpan:[/green] [dim]{config.ENV_FILE}[/dim]")
    console.print(
        "\n  [bold]Selesai![/bold] Ketik [bold cyan]bagas-ai[/bold cyan] untuk mulai chat"
        + ("  ·  [bold cyan]bagas-ai telegram[/bold cyan] untuk bot." if want_tg else ".")
    )
    return True

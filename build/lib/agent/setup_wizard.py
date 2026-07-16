"""Wizard login/setup interaktif untuk bagasAI.

Dipanggil lewat `bagasAI login` (atau `bagasAI setup`). Meminta API key NVIDIA,
MEMVALIDASINYA langsung ke endpoint, lalu opsional menghubungkan bot Telegram —
semuanya disimpan ke ~/.bagasai/.env. Dirancang agar mulus dipakai di laptop
baru setelah instalasi satu-perintah.
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
    "NVIDIA_API_KEY",
    "NVIDIA_BASE_URL",
    "CHAT_MODEL",
    "VISION_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "MAX_TOOL_ITERATIONS",
    "TEMPERATURE",
    "ALLOW_CODE_EXEC",
    "CODE_EXEC_TIMEOUT",
    "RETRY_MAX_SECONDS",
]

_DEFAULTS = {
    "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
    "CHAT_MODEL": "deepseek-ai/deepseek-v4-pro",
    "VISION_MODEL": "meta/llama-3.2-90b-vision-instruct",
    "MAX_TOOL_ITERATIONS": "8",
    "TEMPERATURE": "0.6",
    "ALLOW_CODE_EXEC": "true",
    "CODE_EXEC_TIMEOUT": "30",
    "RETRY_MAX_SECONDS": "300",
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
        "# Konfigurasi bagasAI — dibuat oleh 'bagasAI login'.",
        "# Ambil API key NVIDIA gratis di https://build.nvidia.com (Get API Key).",
        "# 100% NVIDIA cloud; tidak ada model lokal.",
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


def validate_nvidia_key(key: str) -> tuple[bool, str]:
    """Cek API key secara BENAR via chat completion mungil (butuh auth).

    Catatan: endpoint /v1/models NVIDIA bersifat publik (tidak memeriksa auth),
    jadi TIDAK bisa dipakai memvalidasi key. Chat completion mengembalikan
    401/403 bila key salah, sehingga inilah cek yang sahih.
    """
    url = config.NVIDIA_BASE_URL.rstrip("/") + "/chat/completions"
    body = {
        "model": "meta/llama-3.1-8b-instruct",  # kecil & cepat
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + key},
            json=body,
            timeout=45,
        )
    except requests.RequestException as e:
        return False, f"koneksi gagal: {str(e)[:80]}"
    if r.status_code in (401, 403):
        return False, "key ditolak (401/403) — pastikan key benar & diawali 'nvapi-'"
    if r.status_code == 200:
        return True, "autentikasi berhasil"
    # 429/5xx = key kemungkinan valid tapi sedang sibuk; anggap lolos agar tak
    # menghalangi setup, beri catatan.
    if r.status_code == 429 or r.status_code >= 500:
        return True, f"key diterima (server sibuk, HTTP {r.status_code})"
    return False, f"HTTP {r.status_code}: {r.text[:80]}"


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
    """Jalankan wizard login interaktif. Return True bila API key tersimpan valid."""
    console = console or Console()
    env = _read_env(config.ENV_FILE)
    for k, v in _DEFAULTS.items():
        env.setdefault(k, v)

    title = Text("bagasAI", style="bold magenta")
    title.append("  ·  login / setup", style="dim")
    console.print(Panel(title, border_style="magenta", padding=(0, 2)))
    console.print(
        "  [dim]Ambil API key gratis di[/dim] "
        "[cyan]https://build.nvidia.com[/cyan] [dim](Get API Key, awalan[/dim] "
        "[bold]nvapi-[/bold][dim]).[/dim]\n"
    )

    # --- 1. NVIDIA API key ---
    existing = env.get("NVIDIA_API_KEY", "")
    if existing and not existing.startswith("nvapi-xxxx"):
        tail = existing[-6:]
        console.print(f"  [green]● Key tersimpan[/green] [dim]…{tail}[/dim]")
        try:
            from InquirerPy import inquirer  # type: ignore

            if not inquirer.confirm(
                message="Ganti API key NVIDIA?", default=False
            ).execute():
                key = existing
            else:
                key = ""
        except Exception:
            key = existing
    else:
        key = ""

    empties = 0
    while True:
        if not key:
            try:
                key = _prompt_secret(console, "Tempel NVIDIA API key:").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n  [yellow]Dibatalkan.[/yellow]")
                return False
        if not key:
            empties += 1
            if empties >= 3:
                console.print("  [red]Tidak ada key dimasukkan. Batal.[/red]")
                return False
            console.print("  [red]Key kosong.[/red] Coba lagi.\n")
            continue
        console.print("  [dim]Memeriksa key ke NVIDIA…[/dim]")
        ok, info = validate_nvidia_key(key)
        if ok:
            console.print(f"  [bold green]✓ Key valid[/bold green] [dim]({info})[/dim]\n")
            env["NVIDIA_API_KEY"] = key
            break
        console.print(f"  [red]✗ Key gagal:[/red] {info}\n")
        try:
            from InquirerPy import inquirer  # type: ignore

            if not inquirer.confirm(message="Coba key lain?", default=True).execute():
                return False
        except Exception:
            return False
        key = ""

    # --- 2. Telegram (opsional) ---
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
                    f"  [bold green]✓ Bot terhubung[/bold green] [dim]({info})[/dim]\n"
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

    # --- 3. Simpan ---
    _write_env(config.ENV_FILE, env)
    console.print(f"  [green]✔ Konfigurasi disimpan:[/green] [dim]{config.ENV_FILE}[/dim]")
    console.print(
        "\n  [bold]Selesai![/bold] Ketik [bold cyan]bagasAI[/bold cyan] untuk mulai chat"
        + ("  ·  [bold cyan]bagasAI telegram[/bold cyan] untuk bot." if want_tg else ".")
    )
    return True

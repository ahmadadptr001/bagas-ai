"""Wizard login/setup interaktif untuk bagas-ai.

Dipanggil lewat `bagas-ai login` (atau `bagas-ai setup`). TAK ADA kredensial
WAJIB: model (web) memakai akun yang sudah kamu pakai sehari-hari dan login
sekali lewat jendela browser saat model pertama kali dipilih. Dua yang
ditanyakan wizard — NVIDIA_API_KEY & bot Telegram — keduanya OPSIONAL dan boleh
dilewati; melewatinya cuma menutup model (API) dan mode telegram, bukan
menggagalkan pemasangan.
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
    "NVIDIA_API_KEY",
    "CONNECTOR_BROWSER_CHANNEL",
    "VOICE_JANGKAUAN",
    "TELEGRAM_BOT_TOKEN",
    "MAX_TOOL_ITERATIONS",
    "ALLOW_CODE_EXEC",
    "CODE_EXEC_TIMEOUT",
]

# Keterangan yang ditulis TEPAT DI ATAS key-nya di .env. Setelan yang tak
# pernah muncul di berkasnya sama saja dengan setelan yang tak ada: pengguna
# tak bisa mengganti apa yang tak ia ketahui keberadaannya.
_ENV_KOMENTAR = {
    "NVIDIA_API_KEY": [
        "# Kunci untuk model (API) nvidia/* - OPSIONAL. Tanpa ini bagas-ai",
        "# tetap jalan penuh dengan model (web) lewat browser.",
        "# Key gratis: https://build.nvidia.com",
    ],
    "CONNECTOR_BROWSER_CHANNEL": [
        "# Browser yang dipakai connector. Pilihan: brave, chrome,",
        "# chrome-beta, msedge. Kosongkan untuk memaksa Chromium bawaan",
        "# Playwright (paling sering diblok situs - hindari).",
        "# Yang diminta belum terpasang? Browser asli lain dipakai otomatis.",
    ],
    "VOICE_JANGKAUAN": [
        "# Seberapa jauh mikrofon /voice boleh mendengar: dekat, normal, jauh.",
        "# 'jauh' cukup untuk memberi perintah sambil rebahan atau dari",
        "# ruangan sebelah; turunkan bila kipas/AC mulai dikira bicara.",
        "# Ukur dari tempat dudukmu sendiri: /voice jangkau",
    ],
}

_DEFAULTS = {
    # Ikut models._DITUNDA: menulis model yang ditunda ke .env pemasangan baru
    # berarti tiap sesi dimulai dengan pemetaan-ulang diam-diam.
    "CHAT_MODEL": "web/glm",
    # Ditulis TEGAS ke .env, bukan dibiarkan mengandalkan bawaan di config.py:
    # bawaannya pernah berubah (chrome -> brave) lewat pembaruan, dan pemasangan
    # yang tak menuliskannya ikut berpindah browser tanpa pernah diberitahu.
    "CONNECTOR_BROWSER_CHANNEL": "brave",
    "VOICE_JANGKAUAN": "jauh",
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
        "# Tak ada setelan yang WAJIB di sini. NVIDIA_API_KEY hanya untuk",
        "# model (API) nvidia/*; model (web) tak butuh key sama sekali (login",
        "# sekali di jendela browsernya). TELEGRAM_BOT_TOKEN hanya perlu bila",
        "# memakai bot. Berkas ini boleh disunting tangan.",
        "",
    ]
    for k in _ENV_ORDER:
        if k in data and data[k] != "":
            for baris in _ENV_KOMENTAR.get(k, []):
                lines.append(baris)
            lines.append(f"{k}={data[k]}")
    for k, v in data.items():  # simpan key lain yang mungkin ditambahkan manual
        if k not in _ENV_ORDER:
            lines.append(f"{k}={v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_nvidia_key(key: str) -> tuple[bool, str]:
    """Cek NVIDIA_API_KEY dengan SATU permintaan chat sungguhan (max_tokens=1).

    SENGAJA bukan GET /v1/models, walau itu lebih murah dan lebih jelas
    maksudnya. TERUKUR 2026-08-23 di integrate.api.nvidia.com: /v1/models
    menjawab HTTP 200 dengan 102 model MESKI Authorization-nya key ngawur
    ("nvapi-salah"), jadi memakainya berarti setiap key palsu dinyatakan
    "valid" lalu gagal nanti di tengah giliran pengguna. Endpoint yang
    benar-benar memeriksa kredensial adalah chat/completions: key benar -> 200
    (±0,8 detik), key ngawur -> 403 "Authorization failed" (±0,5 detik).

    Modelnya muse-glimmer, yang paling cepat memulai dari ketiganya; deepseek
    butuh ratusan detik sampai token pertama, terlalu lama untuk sekadar
    memeriksa kunci.
    """
    try:
        r = requests.post(
            f"{config.NVIDIA_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": "meta/muse-glimmer-30b",
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1, "stream": False},
            timeout=60,
        )
    except requests.RequestException as e:
        return False, f"koneksi gagal: {str(e)[:80]}"
    if r.status_code == 200:
        return True, "key valid"
    if r.status_code in (401, 403):
        return False, "key ditolak (Authorization failed)"
    if r.status_code == 429:
        # Ditolak karena RAMAI, bukan karena salah. Menyatakannya tidak valid
        # akan menyuruh pengguna mengganti key yang sebenarnya benar.
        return True, "key diterima (endpoint sedang penuh, kuota per menit)"
    detail = ""
    try:
        detail = str(r.json().get("detail") or r.json().get("title") or "")[:80]
    except ValueError:
        detail = r.text[:80]
    return False, f"HTTP {r.status_code} {detail}".strip()


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
    """Minta input rahasia (tersembunyi) lewat prompt bagas-ai (ui/menu.py).

    Melempar EOFError bila stdin habis (mis. dijalankan non-interaktif) supaya
    pemanggil bisa berhenti dan tidak loop tak berujung.
    """
    try:
        from .ui.menu import inquirer

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
    WAJIB, jadi wizard berhasil selama file .env bisa ditulis — kedua
    pertanyaannya (NVIDIA_API_KEY, bot Telegram) boleh dilewati."""
    console = console or Console()
    env = _read_env(config.ENV_FILE)
    for k, v in _DEFAULTS.items():
        env.setdefault(k, v)

    title = Text("bagas-ai", style="bold magenta")
    title.append("  ·  setup", style="dim")
    console.print(Panel(title, border_style="magenta", padding=(0, 2)))
    console.print(
        "  [dim]Tak ada yang WAJIB diisi.[/dim] Model [bold]"
        "(web)[/bold] lewat browser\n"
        "  [dim]— login sekali di jendela browsernya saat kamu memilih model "
        "lewat[/dim] [bold cyan]/model[/bold cyan][dim].[/dim]\n"
    )

    # --- NVIDIA API key (opsional) ---
    # Ditawarkan, TIDAK dipaksa: melewatinya hanya menutup model (API), dan itu
    # dikatakan terus terang supaya pengguna tahu apa yang ia lewatkan alih-alih
    # menebak nanti saat /model menolak entri nvidia/*.
    punya = bool(env.get("NVIDIA_API_KEY", "").strip())
    console.print(
        "  [dim]Model[/dim] [bold]nvidia/*[/bold] [dim]lewat API (tanpa "
        "browser, jauh lebih cepat) butuh NVIDIA_API_KEY.[/dim]\n"
        f"  [dim]Key gratis di[/dim] [cyan]https://build.nvidia.com[/cyan]"
        + ("  [dim](sudah ada di .env)[/dim]" if punya else "")
        + "\n"
    )
    want_key = False
    try:
        from .ui.menu import inquirer

        want_key = inquirer.confirm(
            message=("Ganti NVIDIA_API_KEY sekarang? (opsional)" if punya
                     else "Isi NVIDIA_API_KEY sekarang? (opsional)"),
            default=False,
        ).execute()
    except Exception:
        want_key = False

    if want_key:
        while True:
            key = _prompt_secret(console, "Tempel NVIDIA_API_KEY:").strip()
            if not key:
                break
            console.print("  [dim]Memeriksa key…[/dim]")
            ok, info = validate_nvidia_key(key)
            if ok:
                console.print(f"  [bold green]✓ {info}[/bold green]\n")
                env["NVIDIA_API_KEY"] = key
                break
            console.print(f"  [red]✗ {info}[/red]\n")
            try:
                from .ui.menu import inquirer

                if not inquirer.confirm(
                    message="Coba key lain?", default=True
                ).execute():
                    break
            except Exception:
                break

    # --- Telegram (opsional) ---
    want_tg = False
    try:
        from .ui.menu import inquirer

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
                from .ui.menu import inquirer

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

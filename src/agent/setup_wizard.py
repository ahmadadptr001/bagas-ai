"""Wizard login/setup interaktif untuk bagas-ai.

Dipanggil lewat `bagas-ai login` (atau `bagas-ai setup`).

Model yang tersedia sekarang HANYA `opencode/*` lewat CLI `opencode run` —
GRATIS dan TANPA API key (cukup binary `opencode` di PATH; pasang dengan
`npm i -g opencode-ai`). NVIDIA_API_KEY dan OPENROUTER_API_KEY sudah tidak
ditanyakan karena model nvidia/* dan openrouter/* dihapus dari katalog.
OPENCODE_API_KEY juga tak ditanyakan: jalur utama lewat CLI; bila diisi
manual atau lewat `opencode auth login`, config membacanya sendiri
(config._baca_key_opencode).

Melewati kredensial TIDAK menggagalkan pemasangan — wizard tak pernah
memaksa. Kredensial yang SUDAH terisi di .env dilewati otomatis;
menggantinya lewat pertanyaan "Ganti kredensial". Wizard dibuka DISCLAIMER
yang wajib disetujui sebelum apa pun ditanya atau disimpan.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import config
from . import termux

# Urutan penulisan key di file .env (agar rapi & mudah dibaca manusia).
_ENV_ORDER = [
    "CHAT_MODEL",
    "OPENCODE_API_KEY",
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
    "OPENCODE_API_KEY": [
        "# OPSIONAL — model opencode/* JALAN TANPA key ini lewat CLI",
        "# `opencode run` (binary di PATH; npm i -g opencode-ai).",
        "# Key dari https://opencode.ai/auth hanya untuk jalur cadangan",
        "# HTTP bila CLI absen; `opencode auth login` menyimpannya di",
        "# auth.json dan config membacanya otomatis.",
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
    # Ikut models: bawaan TIDAK menunjuk model (web) yang ditunda. Model
    # opencode/* jalan tanpa API key lewat CLI selama binary `opencode` ada
    # di PATH.
    "CHAT_MODEL": "opencode/big-pickle",
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
        "# Model opencode/* GRATIS lewat CLI `opencode run`; cukup binary",
        "# `opencode` di PATH (npm i -g opencode-ai). Tidak perlu API key.",
        "# TELEGRAM_BOT_TOKEN hanya perlu bila memakai bot. Berkas ini boleh",
        "# disunting tangan.",
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


# Teks Ketentuan yang WAJIB disetujui sebelum wizard menanyakan/menyimpan
# apa pun. Sumber tunggalnya sistem.KETENTUAN (dipakai juga oleh installer
# pra-pasang, yang belum punya rich); markup [bold] baru ditambahkan di sini.
# Jujur soal kuasa agent: ia menjalankan perintah & menulis berkas.
from .sistem import KETENTUAN as _KETENTUAN_TEKS
_DISKLAIMER = (
    _KETENTUAN_TEKS
    .replace("bagas-ai adalah agent AI",
             "[bold]bagas-ai adalah agent AI[/bold]", 1)
    .replace("Seluruh risiko pemakaian menjadi tanggungan pengguna.",
             "[bold]Seluruh risiko pemakaian menjadi tanggungan "
             "pengguna.[/bold]", 1)
)

# Kredensial yang ditangani wizard: nama env -> validator + prompt + info.
# NVIDIA/OpenRouter sengaja tidak ada: model keduanya sudah dihapus dari
# katalog, jadi menanyakan key-nya hanya menjanjikan fitur yang tak ada.
_KREDENSIAL = {
    "TELEGRAM_BOT_TOKEN": {
        "validator": validate_telegram,
        "prompt": "Tempel token bot Telegram:",
        "info": "Buat bot & token di https://t.me/BotFather (/newbot)",
    },
}
# Sengaja TAK ADA entri OPENCODE_API_KEY di atas, dan itu keputusan, bukan
# kelalaian: jalur utama opencode/* kini lewat CLI `opencode run` tanpa key
# (lihat docstring). Key tetap dihormati bila diisi manual di .env / lewat
# `opencode auth login` — dipakai cadangan HTTP bila binary `opencode`
# hilang dari PATH.


def _tanya_ya_tidak(pesan: str, bawaan: bool = False) -> bool:
    """Pertanyaan Ya/Tidak yang TAK PERNAH membisukan pertanyaannya.

    Prompt kotak (ui/menu) dicoba dulu. Bila ia GAGAL karena alasan teknis
    apa pun, JANGAN menganggapnya jawaban "tidak" — dulu kegagalan render
    diam-diam dibaca sebagai penolakan dan wizard langsung "Dibatalkan"
    tanpa pengguna sempat membaca pertanyaannya. Jawaban hanya sah kalau
    benar-benar diminta: jatuhlah ke input() polos yang pasti tampil di
    terminal mana pun. KeyboardInterrupt/EOFError tetap diteruskan — itu
    pengguna yang membatalkan sungguhan."""
    try:
        from .ui.menu import _interaktif, inquirer

        if _interaktif():
            hasil = inquirer.confirm(message=pesan, default=bawaan).execute()
            return bool(hasil)
        # Terminal tak interaktif: prompt kotak akan MELAYANKAN bawaan tanpa
        # bertanya — jangan biarkan. Jalur input() di bawah yang bertanya.
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        pass  # prompt kotak bermasalah -> jalur cadangan di bawah
    try:
        tandanya = "Y/n" if bawaan else "y/N"
        jawab = input(f"{pesan} [{tandanya}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise
    return jawab in ("y", "ya", "yes", "j")


def _isi_kredensial(console: Console, env: dict[str, str], nama: str) -> bool:
    """Tanya + validasi SATU kredensial, simpan ke env bila valid.

    Return True bila tersimpan. Key/token SALAH -> pesan galatnya tampil lalu
    input dibuka LAGI (Enter pada "Coba lagi?" = langsung mencoba; jawaban
    tidak / input kosong berhenti tanpa mengubah apa pun).
    """
    meta = _KREDENSIAL[nama]
    console.print(f"  [dim]{meta['info']}[/dim]")
    while True:
        nilai = _prompt_secret(console, meta["prompt"]).strip()
        if not nilai:
            return False
        console.print("  [dim]Memeriksa…[/dim]")
        ok, ket = meta["validator"](nilai)
        if ok:
            console.print(f"  [bold green]✓ {ket}[/bold green]\n")
            env[nama] = nilai
            return True
        console.print(
            f"  [bold #f0603c]✗ {nama} tidak valid:[/bold #f0603c] "
            f"[red]{ket}[/red]\n")
        # Parameternya `bawaan` — dulu tertulis default=True di sini dan
        # TypeError-nya baru meledak SAAT key pertama salah, tepat di jalur
        # yang paling sering dilewati pengguna baru.
        if not _tanya_ya_tidak("Coba lagi?", bawaan=True):
            return False


def run(console: Console | None = None) -> bool:
    """Wizard setup interaktif. Return True bila konfigurasi tersimpan.

    Urutannya: disclaimer (WAJIB disetujui) -> deteksi kredensial -> tawaran
    isi yang belum ada -> tawaran ganti yang sudah ada -> simpan. Kredensial
    yang SUDAH ADA di .env DILEWATI (tidak ditanya ulang); menggantinya tetap
    bisa lewat pertanyaan "Ganti kredensial". Tak ada pertanyaan yang MEMAKSA:
    menolak/menlewati semuanya tetap menghasilkan pemasangan yang sah, dan
    menolak disclaimer membatalkan wizard tanpa menulis apa pun. Satu-satunya
    syarat model adalah binary `opencode` di PATH — dan bila ia absen, itu
    diberitahukan di akhir, bukan dibiarkan ditemukan sendiri.
    """
    console = console or Console()
    env = _read_env(config.ENV_FILE)
    for k, v in _DEFAULTS.items():
        env.setdefault(k, v)

    title = Text("bagas-ai", style="bold magenta")
    title.append("  ·  setup", style="dim")
    console.print(Panel(title, border_style="magenta", padding=(0, 2)))
    if termux.IS_TERMUX:
        console.print(
            "  [dim]Termux/Android: yang praktis wajib adalah binary[/dim] "
            "[bold]opencode[/bold] [dim]Android aarch64.\n"
            "  Pasang lewat install-termux.sh atau unduh rilis\n"
            "  guysoft/opencode-termux. Model opencode/* gratis & tanpa API key.\n"
            "  Browser, vision lokal, dan Whisper tidak tersedia di Android.[/dim]\n"
        )
    else:
        console.print(
            "  [dim]Yang praktis wajib: binary[/dim] [bold]opencode[/bold] "
            "[dim]di PATH.\n"
            "  Model opencode/* gratis & tanpa API key — pasang dengan[/dim]\n"
            "  [bold cyan]npm i -g opencode-ai[/bold cyan][dim].\n"
            "  Seluruh model (web) sedang ditunda.[/dim]\n"
        )

    # Installer sudah meminta persetujuan sebelum memasang apa pun dan menaruh
    # penanda hanya untuk proses wizard anak ini. ``bagas-ai login`` yang
    # dijalankan sendiri tetap wajib menampilkan serta meminta persetujuan.
    sudah_setuju = os.environ.get("BAGASAI_DISCLAIMER_ACCEPTED") == "1"
    if not sudah_setuju:
        console.print(Panel(
            _DISKLAIMER, title="disclaimer", border_style="yellow", padding=(0, 2),
        ))
        if not _tanya_ya_tidak(
            "Saya sudah membaca & MENYETUJUI ketentuan di atas", bawaan=True
        ):
            console.print(
                "  [yellow]Dibatalkan — tak ada yang diubah.[/yellow]\n"
                "  [dim]Jalankan lagi 'bagas-ai login' bila berubah pikiran.[/dim]"
            )
            return False

    # --- Deteksi kredensial -------------------------------------------------
    # Yang SUDAH ada DILEWATI (tidak ditanya ulang); yang belum ada
    # ditawarkan satu per satu. Statusnya dicetak dulu supaya pengguna tahu
    # kenapa beberapa pertanyaan tidak muncul.
    console.print("  [bold]Status kredensial:[/bold]")
    ada_awal: list[str] = []
    belum: list[str] = []
    for nama in _KREDENSIAL:
        terisi = bool(env.get(nama, "").strip())
        console.print(
            f"    • {nama}: "
            + ("[green]✓ terdeteksi[/green] [dim](dilewati)[/dim]"
               if terisi else "[yellow]belum ada[/yellow]")
        )
        (ada_awal if terisi else belum).append(nama)
    console.print("")

    for nama in belum:
        if _tanya_ya_tidak(f"Isi {nama} sekarang? (opsional)"):
            _isi_kredensial(console, env, nama)

    # --- Fitur ganti token yang sudah ada -----------------------------------
    # Terpisah dari deteksi supaya pemasangan baru cepat lewat; pengguna lama
    # tetap bisa mengganti key/token tanpa menyunting .env manual.
    if ada_awal and _tanya_ya_tidak(
        "Ganti kredensial yang sudah ada? (opsional)"
    ):
        pilih: str | None = None
        try:
            from .ui.menu import Choice, inquirer

            pilih = inquirer.select(
                message="Ganti yang mana?",
                choices=[Choice(n, n) for n in ada_awal],
                pointer="❯",
            ).execute()
        except Exception:
            pilih = None
        if pilih:
            _isi_kredensial(console, env, pilih)

    # --- Simpan ---
    _write_env(config.ENV_FILE, env)
    if termux.IS_TERMUX:
        console.print(
            "  [dim]Vision lokal (Ollama + Gemma) dilewati di Termux/Android — "
            "Android tak menjalankan Ollama.[/dim]")
    else:
        try:
            from .updater import _pasang_vision_gemma
            console.print("  [dim]Memastikan Ollama + Gemma 3 4B vision (wajib)...[/dim]")
            hasil_vision = _pasang_vision_gemma()
            console.print("  " + hasil_vision)
            if hasil_vision.startswith("GAGAL:"):
                console.print("  [red]Setup dihentikan: vision lokal wajib tersedia.[/red]")
                return False
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]Setup dihentikan: Ollama wajib ({exc})[/red]")
            return False
    console.print(f"  [green]✔ Konfigurasi disimpan:[/green] [dim]{config.ENV_FILE}[/dim]")
    # Diperingatkan SETELAH simpan, bukan sebagai penghalang: pemasangan tetap
    # sah. Tanpa binary `opencode`, belum ada satu pun model yang bisa dipilih.
    if not config.opencode_cli_tersedia():
        if termux.IS_TERMUX:
            console.print(
                "  [yellow]Catatan:[/yellow] binary `opencode` tak ada di PATH, "
                "jadi BELUM ADA model\n"
                "  yang bisa dipilih. Pasang OpenCode Termux (Android aarch64) "
                "lewat\n  [cyan]bash install-termux.sh[/cyan] atau rilis "
                "guysoft/opencode-termux, lalu jalankan\n"
                "  [bold cyan]bagas-ai login[/bold cyan] lagi."
            )
        else:
            console.print(
                "  [yellow]Catatan:[/yellow] binary `opencode` tak ada di PATH, "
                "jadi BELUM ADA model\n"
                "  yang bisa dipilih. Pasang CLI dengan "
                "[cyan]npm i -g opencode-ai[/cyan],\n"
                "  lalu jalankan [bold cyan]bagas-ai login[/bold cyan] lagi."
            )
    console.print(
        "\n  [bold]Selesai![/bold] Ketik [bold cyan]bagas-ai[/bold cyan] untuk mulai chat"
        " ·  [bold cyan]bagas-ai telegram[/bold cyan] untuk bot."
    )
    return True

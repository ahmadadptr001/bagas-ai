"""Wizard login/setup interaktif untuk bagas-ai.

Dipanggil lewat `bagas-ai login` (atau `bagas-ai setup`). TAK ADA kredensial
WAJIB: model (web) memakai akun yang sudah kamu pakai sehari-hari dan login
sekali lewat jendela browser saat model pertama kali dipilih. Yang ditanyakan
wizard — NVIDIA_API_KEY, OPENROUTER_API_KEY, & bot Telegram — semuanya OPSIONAL
dan boleh dilewati; melewatinya cuma menutup model (API) dan mode telegram,
bukan menggagalkan pemasangan. Kredensial yang SUDAH terisi di .env dilewati
otomatis; menggantinya tetap bisa lewat pertanyaan "Ganti kredensial". Wizard
dibuka DISCLAIMER yang wajib disetujui sebelum apa pun ditanya atau disimpan.
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
    "OPENROUTER_API_KEY",
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
    "OPENROUTER_API_KEY": [
        "# Kunci untuk model (API) openrouter/* (mis. ox-alpha) - OPSIONAL.",
        "# Ambil key: https://openrouter.ai/keys (awalan sk-or-...).",
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


def validate_openrouter_key(key: str) -> tuple[bool, str]:
    """Cek OPENROUTER_API_KEY dengan SATU permintaan chat sungguhan.

    Sama alasannya dengan validate_nvidia_key: endpoint yang benar-benar
    memeriksa kredensial adalah chat/completions. Modelnya ox-alpha sendiri —
    key yang valid untuk model lain tak menjamin model ini bisa dipakai.
    """
    try:
        r = requests.post(
            f"{config.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": "stealth/ox-alpha",
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 1, "stream": False},
            timeout=60,
        )
    except requests.RequestException as e:
        return False, f"koneksi gagal: {str(e)[:80]}"
    if r.status_code == 200:
        return True, "key valid"
    if r.status_code == 401:
        return False, "key ditolak (No auth credentials)"
    if r.status_code == 402:
        # 402 = kredit habis. Key-nya BENAR; menyatakannya tidak valid akan
        # menyuruh pengguna mengganti key yang sebenarnya benar.
        return True, "key diterima (kredit habis — isi saldo dulu di dashboard)"
    if r.status_code == 429:
        # Ditolak karena RAMAI/rate limit, bukan karena salah.
        return True, "key diterima (endpoint sedang penuh / rate limit)"
    detail = ""
    try:
        err = r.json().get("error")
        detail = (err.get("message") if isinstance(err, dict) else str(err))[:80]
    except ValueError:
        detail = r.text[:80]
    return False, f"HTTP {r.status_code} {detail or 'gagal'}".strip()


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


# Teks disclaimer yang WAJIB disetujui sebelum wizard menanyakan/menyimpan
# apa pun. Jujur soal kuasa agent: ia menjalankan perintah & menulis berkas.
_DISKLAIMER = (
    "[bold]bagas-ai adalah agent AI[/bold] yang atas permintaanmu dapat:\n"
    "  • menjalankan perintah & kode di komputer ini,\n"
    "  • membaca/menulis berkas di folder kerja,\n"
    "  • mengakses internet (web & API model).\n"
    "\n"
    "Percakapan dan konteks dikirim ke layanan model pilihanmu (situs web-AI\n"
    "atau API NVIDIA/OpenRouter). API key yang ditempel di wizard ini disimpan\n"
    "LOKAL di ~/.bagasai/.env dan hanya dikirim ke penyedianya saat autentikasi.\n"
    "\n"
    "[bold]Seluruh risiko pemakaian menjadi tanggungan pengguna.[/bold] Periksa\n"
    "setiap perintah/berkas yang kamu setujui — bagas-ai bisa keliru."
)

# Kredensial yang ditangani wizard: nama env -> validator + prompt + info.
_KREDENSIAL = {
    "NVIDIA_API_KEY": {
        "validator": validate_nvidia_key,
        "prompt": "Tempel NVIDIA_API_KEY:",
        "info": "Key gratis: https://build.nvidia.com",
    },
    "OPENROUTER_API_KEY": {
        "validator": validate_openrouter_key,
        "prompt": "Tempel OPENROUTER_API_KEY:",
        "info": "Buat key: https://openrouter.ai/keys (awalan sk-or-...)",
    },
    "TELEGRAM_BOT_TOKEN": {
        "validator": validate_telegram,
        "prompt": "Tempel token bot Telegram:",
        "info": "Buat bot & token di https://t.me/BotFather (/newbot)",
    },
}


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
    bisa lewat pertanyaan "Ganti kredensial" di akhir. Tak ada kredensial
    WAJIB: menolak/menlewati semua pertanyaan tetap menghasilkan pemasangan
    yang sah, dan menolak disclaimer membatalkan wizard tanpa menulis apa pun.
    """
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

    # --- Disclaimer: WAJIB disetujui sebelum apa pun ditanya/disimpan ------
    console.print(Panel(
        _DISKLAIMER, title="disclaimer", border_style="yellow", padding=(0, 2),
    ))
    if not _tanya_ya_tidak("Saya sudah membaca & MENYETUJUI ketentuan di atas"):
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
    console.print(f"  [green]✔ Konfigurasi disimpan:[/green] [dim]{config.ENV_FILE}[/dim]")
    console.print(
        "\n  [bold]Selesai![/bold] Ketik [bold cyan]bagas-ai[/bold cyan] untuk mulai chat"
        " ·  [bold cyan]bagas-ai telegram[/bold cyan] untuk bot."
    )
    return True

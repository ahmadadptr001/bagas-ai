"""Entry point CLI global bagasAI.

Dipasang sebagai perintah `bagasAI` (lihat pyproject.toml). Penggunaan:

    bagasAI            # chat di terminal (default)
    bagasAI chat       # sama dengan di atas
    bagasAI login      # wizard: masukkan API key NVIDIA (+ Telegram opsional)
    bagasAI telegram   # jalankan bot Telegram
    bagasAI api        # jalankan server API (FastAPI)
    bagasAI setup      # sama dengan login
    bagasAI version
    bagasAI help
"""
from __future__ import annotations

import sys

# Paksa output UTF-8 agar emoji & banner tidak crash di console Windows (cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from . import config

__version__ = "1.0.0"

HELP = f"""\
bagasAI v{__version__} — AI agent serbaguna (NVIDIA free API)

Penggunaan:
  bagasAI              Buka sesi chat BARU di folder saat ini
  bagasAI --resume     Lanjutkan percakapan terakhir di folder ini
  bagasAI login        Wizard: masukkan API key NVIDIA (+ Telegram opsional)
  bagasAI telegram     Jalankan bot Telegram
  bagasAI api          Jalankan server API di http://localhost:8000
  bagasAI setup        Sama dengan 'login'
  bagasAI version      Tampilkan versi
  bagasAI help         Tampilkan bantuan ini

Config  : {config.CONFIG_HOME}
Project : {config.PROJECT_ROOT}   (folder terminal aktif = root project)
"""


def _cmd_login() -> None:
    """Wizard login interaktif (validasi key ke NVIDIA + Telegram opsional)."""
    from .setup_wizard import run as run_wizard

    try:
        run_wizard()
    except KeyboardInterrupt:
        print("\nDibatalkan.")


def _need_key() -> bool:
    if config.has_api_key():
        return False
    print("[!] NVIDIA_API_KEY belum diisi.")
    print("   Jalankan: bagasAI login   (wizard memandu memasukkan API key)")
    print("   Ambil key gratis di https://build.nvidia.com\n")
    return True


def main() -> None:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("-")}
    positional = [a for a in args if not a.startswith("-")]
    mode = positional[0].lower() if positional else "chat"
    resume = "--resume" in flags or "-r" in flags

    if mode in ("help",) or flags & {"-h", "--help"}:
        print(HELP)
        return
    if mode in ("version",) or flags & {"-v", "--version"}:
        print(f"bagasAI v{__version__}")
        return
    if mode in ("setup", "login"):
        _cmd_login()
        return

    if mode in ("chat", "cli"):
        if _need_key():
            sys.exit(1)
        from .interfaces.cli import main as run
        run(resume=resume)
        return
    if mode == "telegram":
        if _need_key():
            sys.exit(1)
        from .interfaces.telegram_bot import main as run
        run()
        return
    if mode == "api":
        if _need_key():
            sys.exit(1)
        from .interfaces.api import main as run
        run()
        return

    print(f"Perintah tidak dikenal: {mode}\n")
    print(HELP)
    sys.exit(1)


if __name__ == "__main__":
    main()

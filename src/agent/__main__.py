"""Entry point CLI global bagasAI.

Dipasang sebagai perintah `bagasAI` (lihat pyproject.toml). Penggunaan:

    bagasAI            # chat di terminal (default)
    bagasAI chat       # sama dengan di atas
    bagasAI login      # wizard: masukkan API key NVIDIA (+ Telegram opsional)
    bagasAI update     # cek & terapkan pembaruan dari GitHub
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
  bagasAI add-dir <p>  Tambah folder konteks agar bagasAI memahaminya
  bagasAI update       Cek & terapkan pembaruan dari GitHub
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


def _cmd_update() -> None:
    """Cek & terapkan pembaruan bagasAI dari GitHub (dari terminal)."""
    from . import updater

    print("🔄 Memeriksa pembaruan di GitHub…")
    res = updater.check()
    st = res.get("status")
    if st == "up_to_date":
        print(f"✓ bagasAI sudah versi terbaru. ({res.get('local','')})")
        return
    if st == "no_git":
        print("✖ git tidak ditemukan — pasang git dulu agar bisa memperbarui.")
        return
    if st == "no_repo":
        print("ℹ Tak bisa menentukan sumber pembaruan (REPO_URL kosong).")
        return
    if st in ("no_upstream", "fetch_error"):
        print(f"✖ {st}: {res.get('detail','tidak ada remote/upstream')}")
        return

    if st == "setup_needed":
        # Instalasi tanpa repo git penopang (salinan pip / installer dari folder).
        # Bisa disiapkan otomatis dengan clone lalu reinstall.
        print("ℹ Auto-update belum disiapkan untuk instalasi ini.")
        print(f"  Sumber: {res.get('repo_url','')} (branch {res.get('branch','')})")
        try:
            ans = input("Siapkan & perbarui sekarang? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no", "t", "tidak"):
            print("Dilewati.")
            return
        print("⏳ Menyiapkan repo & memasang pembaruan…")
    elif st == "update_available":
        print(f"\n{res.get('behind','?')} pembaruan tersedia "
              f"({res.get('local','')} → {res.get('remote','')}):")
        if res.get("log"):
            for line in res["log"].splitlines():
                print("  • " + line)
        try:
            ans = input("\nTerapkan sekarang? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no", "t", "tidak"):
            print("Dilewati.")
            return
        print("⏳ Menarik & memasang pembaruan…")
    else:
        print(f"✖ status tak terduga: {st}")
        return

    out = updater.apply()
    if out.get("status") == "updated":
        tail = "" if out.get("reinstalled") else f" (catatan pip: {out.get('pip_detail','')})"
        print("✓ bagasAI diperbarui! Jalankan ulang perintah bagasAI." + tail)
    else:
        print(f"✖ gagal ({out.get('status')}): {out.get('detail','')}")


def _cmd_add_dir(args: list[str]) -> None:
    """Tambah folder konteks dari terminal: bagasAI add-dir <path>."""
    from . import workspace

    paths = [a for a in args if not a.startswith("-")][1:]  # buang 'add-dir'
    if not paths:
        print("Pakai: bagasAI add-dir <path folder>")
        return
    for path in paths:
        try:
            p = workspace.add(path)
        except ValueError as e:
            print(f"[!] {e}")
            continue
        print(f"[+] Folder konteks ditambahkan: {p}")
        print("    bagasAI akan memahami & bisa mengaksesnya di sesi berikutnya.")


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
    if mode == "update":
        _cmd_update()
        return
    if mode in ("add-dir", "adddir"):
        _cmd_add_dir(positional)
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
        from . import osinfo
        osinfo.sync_to_memory()  # deteksi & simpan OS (senyap) untuk penyesuaian perintah
        from .interfaces.telegram_bot import main as run
        run()
        return
    if mode == "api":
        if _need_key():
            sys.exit(1)
        from . import osinfo
        osinfo.sync_to_memory()
        from .interfaces.api import main as run
        run()
        return

    print(f"Perintah tidak dikenal: {mode}\n")
    print(HELP)
    sys.exit(1)


if __name__ == "__main__":
    main()

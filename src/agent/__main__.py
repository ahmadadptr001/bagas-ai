"""Entry point CLI global bagas-ai.

Dipasang sebagai perintah `bagas-ai` (lihat pyproject.toml). Penggunaan:

    bagas-ai            # chat di terminal (default)
    bagas-ai chat       # sama dengan di atas
    bagas-ai login      # wizard: hubungkan bot Telegram (opsional)
    bagas-ai update     # cek & terapkan pembaruan dari GitHub
    bagas-ai telegram   # jalankan bot Telegram
    bagas-ai api        # jalankan server API (FastAPI)
    bagas-ai setup      # sama dengan login
    bagas-ai version
    bagas-ai help
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

# Versi dibaca dari METADATA paket terpasang — SATU sumber kebenaran, yaitu
# pyproject.toml (yang dinaikkan otomatis tiap commit oleh .githooks/pre-commit).
# Dulu ditulis tangan di sini dan tak pernah ikut naik, jadi banner selamanya
# bilang 1.0.0 padahal paketnya sudah jauh lebih baru — bikin sulit memastikan
# sebuah pembaruan benar-benar terpasang.
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("bagasai")
except Exception:  # noqa: BLE001 - belum terpasang / metadata rusak
    __version__ = "0.0.0+dev"

HELP = f"""\
bagas-ai v{__version__} — AI agent serbaguna (model via browser)

Penggunaan:
  bagas-ai              Buka sesi chat BARU di folder saat ini
  bagas-ai --resume     Lanjutkan percakapan terakhir di folder ini
  bagas-ai --resume ID  Lanjutkan sesi ber-ID itu (ID dicetak saat kamu keluar)
  bagas-ai --legacy     Pakai CLI lama (prompt_toolkit + Rich Live)
  bagas-ai login        Wizard: hubungkan bot Telegram (opsional)
  bagas-ai add-dir <p>  Tambah folder konteks agar bagas-ai memahaminya
  bagas-ai update       Cek & terapkan pembaruan dari GitHub
  bagas-ai telegram     Jalankan bot Telegram
  bagas-ai api          Jalankan server API di http://localhost:8000
  bagas-ai mcp          Jalankan server MCP (stdio) — sasaran tepat untuk AI
  bagas-ai setup        Sama dengan 'login'
  bagas-ai uninstall    Copot bagas-ai + HAPUS semua datanya (~/.bagasai)
  bagas-ai version      Tampilkan versi
  bagas-ai help         Tampilkan bantuan ini

Izin akses:
  bagas-ai hanya menyentuh berkas di dalam folder proyek + folder konteks
  (`add-dir`). Kalau ia perlu berkas DI LUAR itu, kamu ditanya dulu — sekali
  per folder, dengan pilihan: sekali saja / selama sesi / permanen / tolak.

  --skip-permissions    Jangan tanya apa pun; semua folder boleh disentuh.
                        Berlaku juga untuk mode telegram & api. Hati-hati:
                        satu langkah keliru bisa menulis di mana saja.

Catatan: `pip uninstall bagasai` HANYA menghapus paketnya — data di
{config.CONFIG_HOME} tetap tertinggal (pip tak punya hook uninstall).
Pakai `bagas-ai uninstall` bila ingin keduanya hilang sekaligus.
  Opsi: --yes (tanpa konfirmasi)  --data-only (data saja)  --keep-data (paket saja)

Config  : {config.CONFIG_HOME}
Project : {config.PROJECT_ROOT}   (folder terminal aktif = root project)
"""


def _cmd_login() -> None:
    """Wizard setup interaktif (bot Telegram opsional; tak ada API key)."""
    from .setup_wizard import run as run_wizard

    try:
        run_wizard()
    except KeyboardInterrupt:
        print("\nDibatalkan.")


def _cmd_update() -> None:
    """Cek & terapkan pembaruan bagas-ai dari GitHub (dari terminal)."""
    from . import updater
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.text import Text

    console = Console()
    console.print()
    console.print(Panel(
        "[dim]GitHub · paket terpasang · dependensi runtime[/dim]",
        title="[bold magenta]bagas-ai update[/bold magenta]",
        title_align="left", border_style="magenta", box=box.ROUNDED,
        padding=(1, 2),
    ))
    with console.status("[cyan]Memeriksa versi dan sumber pembaruan…[/cyan]",
                        spinner="dots"):
        res = updater.check()
    st = res.get("status")

    def _ringkas_versi() -> None:
        try:
            v = updater.versions()
        except Exception:  # noqa: BLE001
            return
        baris = f"  terpasang {v.get('terpasang') or '?'}"
        if v.get("repo"):
            baris += f"   repo {v['repo']}"
        if v.get("remote") and v["remote"] != v.get("repo"):
            baris += f"   remote {v['remote']}"
        if v.get("commit_lokal"):
            baris += f"   commit {v['commit_lokal']}"
        console.print(f"  [dim]{baris.strip()}[/dim]")

    if st == "up_to_date":
        console.print(Panel(
            f"[bold green]✓ Sudah versi terbaru[/bold green]\n"
            f"[dim]Versi lokal {escape(str(res.get('local', '')))}[/dim]",
            border_style="green", box=box.ROUNDED, padding=(0, 2),
        ))
        _ringkas_versi()
        with console.status("[cyan]Memastikan Ollama, Gemma, Python, dan Chromium…[/cyan]",
                            spinner="dots"):
            runtime = updater.ensure_runtime()
        if runtime.get("status") != "ok":
            body = Text("Runtime belum sepenuhnya siap\n", style="bold yellow")
            body.append(runtime.get("runtime", "") + "\n")
            body.append(runtime.get("vision", ""))
            console.print(Panel(body, border_style="yellow", box=box.ROUNDED,
                                padding=(0, 2)))
        else:
            console.print("  [bold green]✓[/bold green] Ollama, Gemma 3 4B vision, "
                          "dependensi Python, dan Chromium siap.\n")
        return

    if st == "stale_install":
        # Yang menentukan apa yang berjalan adalah salinan terpasang, bukan repo.
        rincian = []
        for b in (res.get("beda") or [])[:10]:
            rincian.append("• " + escape(str(b)))
        console.print(Panel(
            "[bold yellow]Repo sudah mutakhir, tetapi paket terpasang tertinggal[/bold yellow]"
            + (("\n\n" + "\n".join(rincian)) if rincian else ""),
            title="[bold yellow]⚠ Pemasangan tertinggal[/bold yellow]",
            title_align="left", border_style="yellow", box=box.ROUNDED,
            padding=(1, 2),
        ))
        try:
            ans = input("Pasang ulang sekarang? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no", "t", "tidak"):
            print("Dilewati.")
            return
        with console.status("[cyan]Memasang ulang dan memverifikasi paket…[/cyan]",
                            spinner="dots"):
            out = updater.apply()
        _lapor_update(out, _ringkas_versi, console)
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
        console.print(Panel(
            "[bold cyan]Auto-update belum disiapkan untuk instalasi ini.[/bold cyan]\n\n"
            f"[dim]Sumber[/dim]  {escape(str(res.get('repo_url', '')))}\n"
            f"[dim]Branch[/dim]  {escape(str(res.get('branch', '')))}",
            title="[bold cyan]ℹ Siapkan pembaruan[/bold cyan]",
            title_align="left", border_style="cyan", box=box.ROUNDED,
            padding=(1, 2),
        ))
        try:
            ans = input("Siapkan & perbarui sekarang? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no", "t", "tidak"):
            print("Dilewati.")
            return
        proses = "Menyiapkan repo dan memasang pembaruan…"
    elif st == "update_available":
        isi = (f"[bold cyan]{escape(str(res.get('behind', '?')))} pembaruan tersedia[/bold cyan]\n"
               f"[dim]{escape(str(res.get('local', '')))} → "
               f"{escape(str(res.get('remote', '')))}[/dim]")
        if res.get("log"):
            isi += "\n"
            for line in res["log"].splitlines():
                isi += "\n• " + escape(line)
        console.print(Panel(
            isi, title="[bold magenta]🔄 Pembaruan tersedia[/bold magenta]",
            title_align="left", border_style="magenta", box=box.ROUNDED,
            padding=(1, 2),
        ))
        try:
            ans = input("\nTerapkan sekarang? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no", "t", "tidak"):
            print("Dilewati.")
            return
        proses = "Menarik, memasang, dan memverifikasi pembaruan…"
    else:
        print(f"✖ status tak terduga: {st}")
        return

    with console.status(f"[cyan]{proses}[/cyan]", spinner="dots"):
        out = updater.apply()
    _lapor_update(out, _ringkas_versi, console)


def _lapor_update(out: dict, ringkas_versi=None, console=None) -> None:
    """Laporkan hasil apply() apa adanya — dan hanya sebut 'berhasil' bila
    isinya BENAR-BENAR terverifikasi sama dengan repo, bukan karena pip pulang
    dengan kode 0 (yang pernah terbukti berbohong)."""
    if console is None:
        from rich.console import Console
        console = Console()
    from rich import box
    from rich.markup import escape
    from rich.panel import Panel

    if out.get("status") != "updated":
        console.print(Panel(
            f"[bold red]✖ Pembaruan gagal[/bold red]\n"
            f"[dim]{escape(str(out.get('status', '')))}[/dim]  "
            f"{escape(str(out.get('detail', '')))}",
            border_style="red", box=box.ROUNDED, padding=(0, 2),
        ))
        return

    if out.get("verified"):
        pesan = (f"[bold green]✓ Pembaruan selesai dan terverifikasi[/bold green]\n"
                 f"[dim]Versi {escape(str(out.get('version', '')))}[/dim]\n")
        if out.get("how") == "langsung":
            pesan += "Kode terbaru sudah aktif. Jalankan ulang perintahnya saja."
        else:
            pesan += "Jalankan ulang perintah bagas-ai agar perubahan aktif."
        if out.get("note"):
            pesan += "\n[cyan]ℹ[/cyan] " + escape(str(out["note"]))
        console.print(Panel(pesan, border_style="green", box=box.ROUNDED,
                            padding=(1, 2)))
        if ringkas_versi:
            ringkas_versi()
        return

    pesan = "[bold yellow]⚠ Pembaruan belum tuntas[/bold yellow]"
    if out.get("note"):
        pesan += "\n" + escape(str(out["note"]))
    for b in (out.get("diff") or [])[:6]:
        pesan += "\n  • " + escape(str(b))
    detail = (out.get("pip_detail") or "").strip()
    if detail:
        pesan += f"\n[dim]catatan pip: {escape(detail)}[/dim]"
    console.print(Panel(pesan, border_style="yellow", box=box.ROUNDED,
                        padding=(1, 2)))


def _cmd_uninstall(flags: set[str]) -> None:
    """Copot bagas-ai + SELURUH datanya (~/.bagasai).

    Ada sebagai perintah karena `pip uninstall` TIDAK bisa dikaitkan ke kode
    apa pun (lihat penjelasan panjang di agent/uninstall.py): pip cuma
    menghapus berkas paket, jadi data di ~/.bagasai akan tertinggal selamanya
    kalau tak ada perintah seperti ini."""
    from . import uninstall as unin

    hanya_data = "--data-only" in flags
    simpan_data = "--keep-data" in flags
    if hanya_data and simpan_data:
        print("✖ --data-only dan --keep-data saling bertentangan; pilih salah satu.")
        sys.exit(1)

    info = unin.ringkasan()
    print("\n⚠  MENCOPOT bagas-ai — yang akan DIHAPUS PERMANEN:\n")
    if not simpan_data:
        if info["data_ada"]:
            print(f"  • Data & konfigurasi : {info['data_dir']}")
            print(f"      {info['berkas']} berkas, {info['ukuran']} — berisi sesi "
                  "percakapan, memory jangka panjang,")
            print("      script memory, profil login browser (harus login ulang), "
                  "dan .env")
        else:
            print(f"  • Data & konfigurasi : {info['data_dir']} (sudah tidak ada)")
    if not hanya_data:
        versi = info["versi"]
        if versi:
            print(f"  • Paket Python       : bagasai {versi} "
                  "(perintah bagas-ai / bagasai / bagas)")
        else:
            print("  • Paket Python       : tidak terpasang lewat pip "
                  "(dijalankan dari salinan repo?) — pencopotan pip dilewati")
    print("\n  Berkas PROYEKMU sendiri tidak disentuh sama sekali.\n")

    if "--yes" not in flags and "-y" not in flags:
        try:
            jawab = input("Ketik HAPUS (huruf besar) untuk melanjutkan: ").strip()
        except (EOFError, KeyboardInterrupt):
            jawab = ""
        if jawab != "HAPUS":
            print("Dibatalkan — tidak ada yang dihapus.")
            return

    if not simpan_data:
        ok, catatan = unin.hapus_data()
        if ok:
            print(f"✓ Data dihapus: {info['data_dir']}"
                  + (f" ({catatan})" if catatan else ""))
        else:
            print(f"⚠ Sebagian data belum terhapus — {catatan}")

    if hanya_data:
        print("\nSelesai. Paket bagas-ai masih terpasang "
              "(`bagas-ai uninstall` tanpa --data-only untuk mencopotnya juga).")
        return

    if not info["versi"]:
        print("\nSelesai. Tak ada paket pip yang perlu dicopot.")
        return

    # Pencopotan HARUS menunggu proses ini keluar: kita sedang berjalan dari
    # bagasai.exe, dan pip tak bisa menghapus berkas yang masih dipakai.
    ok, jejak = unin.jadwalkan_pencopotan(hapus_juga_data=not simpan_data)
    if ok:
        print("\n⏳ Pencopotan paket berjalan OTOMATIS beberapa detik setelah "
              "perintah ini selesai.")
        print("   Jangan buka bagas-ai lagi sampai itu tuntas.")
        print(f"   Log hasilnya: {jejak}")
        print("\nTerima kasih sudah memakai bagas-ai 👋")
        return
    print(f"\n⚠ Tak bisa menjadwalkan pencopotan otomatis ({jejak}).")
    print("   Copot manual dengan: pip uninstall -y bagasai")


def _cmd_add_dir(args: list[str]) -> None:
    """Tambah folder konteks dari terminal: bagas-ai add-dir <path>."""
    from . import workspace

    paths = [a for a in args if not a.startswith("-")][1:]  # buang 'add-dir'
    if not paths:
        print("Pakai: bagas-ai add-dir <path folder>")
        return
    for path in paths:
        try:
            p = workspace.add(path)
        except ValueError as e:
            print(f"[!] {e}")
            continue
        print(f"[+] Folder konteks ditambahkan: {p}")
        print("    bagas-ai akan memahami & bisa mengaksesnya di sesi berikutnya.")


# _need_key() DIHAPUS: bagas-ai tak lagi punya kredensial wajib. Model dipilih
# lewat /model lalu login dilakukan SEKALI di jendela browser, jadi tak ada lagi
# gerbang "isi API key dulu" sebelum chat/telegram/api boleh dijalankan.


def _preload_with_bar() -> None:
    """Bar loading BERTAHAP saat memuat pustaka berat — fase paling lambat (~1 dtk)
    dari startup. Tiap pustaka diimpor satu per satu sambil bar terisi bertahap,
    lalu impor CLI jadi instan (semua sudah ter-cache)."""
    if not sys.stdout or not sys.stdout.isatty():
        return
    import importlib
    import shutil
    pkg = __package__ or "agent"
    steps = [
        ("tampilan (rich)", "rich.console"),
        ("live view", "rich.live"),
        ("markdown", "rich.markdown"),
        ("input terminal", "prompt_toolkit"),
        ("menu interaktif", f"{pkg}.ui.menu"),
        ("logo", "pyfiglet"),
        ("pencarian web", "ddgs"),
        ("inti agent", f"{pkg}.core"),
        ("textual ui", f"{pkg}.interfaces.textual_app"),
        ("antarmuka", f"{pkg}.interfaces.cli"),
    ]
    total = len(steps)
    cols = max(40, min(shutil.get_terminal_size((80, 24)).columns, 120))
    w = 22 if cols >= 70 else 12
    for i, (label, mod) in enumerate(steps, 1):
        try:
            importlib.import_module(mod)
        except Exception:
            pass
        filled = round(w * i / total)
        bar = "█" * filled + "░" * (w - filled)
        try:
            txt = f"  ⬢ bagas-ai  memuat  {bar}  {round(100 * i / total):3d}%  {label:<18}"
            if len(txt) > cols - 1:
                txt = txt[:cols - 2] + "…"
            sys.stdout.write(f"\r{txt:<{cols - 1}}")
            sys.stdout.flush()
        except Exception:
            pass
    try:
        sys.stdout.write("\r" + " " * (cols - 1) + "\r")  # bersihkan baris
        sys.stdout.flush()
    except Exception:
        pass


def main() -> None:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("-")}
    # Nilai sesudah --resume/-r adalah ID SESI (mis. `bagas-ai --resume
    # 20250825-101030-a1b2`). Ia TIDAK boleh dihitung sebagai mode/positional:
    # tanpa ini, "bagas-ai --resume 20250825" dibaca sebagai perintah mode
    # "20250825" yang tak dikenal.

    def _nilai_bendera(nama: str) -> str:
        if nama in args:
            i = args.index(nama)
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1]
        return ""

    resume_id = _nilai_bendera("--resume") or _nilai_bendera("-r")
    positional = [a for a in args
                  if not a.startswith("-") and a != resume_id]
    mode = positional[0].lower() if positional else "chat"
    resume = "--resume" in flags or "-r" in flags

    # Lewati konfirmasi akses folder LUAR (lihat permissions.py). Dipasang
    # SEBELUM perintah apa pun dijalankan supaya berlaku juga untuk mode
    # telegram & api. Ejaan panjang ala Claude Code ikut diterima agar
    # kebiasaan dari sana tak menghasilkan galat "perintah tak dikenal".
    if flags & {"--skip-permissions", "--dangerously-skip-permissions"}:
        from . import permissions

        permissions.set_skip(True)

    if mode in ("help",) or flags & {"-h", "--help"}:
        print(HELP)
        return
    if mode in ("version",) or flags & {"-v", "--version"}:
        print(f"bagas-ai v{__version__}")
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
    if mode in ("uninstall", "copot"):
        _cmd_uninstall(flags)
        return

    if mode in ("chat", "cli"):
        _preload_with_bar()  # bar loading BERTAHAP selama impor pustaka (~1 dtk)
        # --legacy memaksa CLI lama (prompt_toolkit + Rich Live)
        if "--legacy" in flags:
            from .interfaces.cli import main as run
            run(resume=resume, resume_id=resume_id)
            return
        # Default: Textual UI baru; fallback ke CLI lama bila textual tak ada
        try:
            from .interfaces.textual_app import BagasAIApp
            from .core import Agent
            from . import session as session_mod
            from .session import Session

            if resume and resume_id:
                # Galat ID (tak ketemu / ambigu) dijelaskan BESERTA daftar
                # sesi yang ada — kembaran cli.py; tanpa ini jalur Textual
                # mati dengan traceback mentah.
                try:
                    ses = session_mod.find(resume_id)
                except ValueError as exc:
                    print(f"✗ {exc}")
                    try:
                        kandidat = session_mod.list_sessions()[:5]
                    except Exception:  # noqa: BLE001
                        kandidat = []
                    if kandidat:
                        print("Sesi terakhir di folder ini:")
                        for s in kandidat:
                            print(f"  {s.id} · {session_mod.user_msg_count(s)}"
                                  " pesan")
                    return
                if ses is None:
                    ses = Session.create()
            elif resume:
                ses = session_mod.latest()
            else:
                ses = Session.create()
            if ses is None:
                ses = Session.create()

            # Peta proyek: baca cache disk APA ADANYA (instan, mungkin basi)
            # sebelum Agent() membangun system prompt — kembaran cli.py.
            # Tanpa ini Agent() memindai SELURUH folder secara sinkron dan UI
            # chat di folder besar/proyek baru terasa "stuck" setelah bar
            # loading. Kesegaran peta diperiksa di thread latar oleh app
            # (lihat BagasAIApp.on_mount), lalu system prompt disegarkan
            # otomatis begitu peta terbaru siap.
            try:
                from . import projectindex
                projectindex.prime(config.PROJECT_ROOT)
            except Exception:  # noqa: BLE001 — peta opsional, jangan halangi chat
                pass

            agent = Agent(session=ses)
            app = BagasAIApp(agent=agent, resume=resume,
                             resume_id=resume_id)
            app.run()
            # Pesan penutup SETELAH UI Textual ditutup (terminal sudah
            # kembali normal): satu baris, langsung PAKAI — cara lanjutkan
            # sekaligus ID-nya, kembaran cli.py. Persist sekali lagi di sini:
            # jalan keluar selain ctrl+c/ctrl+d (mis. galat) tetap menyimpan.
            try:
                agent._persist()
            except Exception:  # noqa: BLE001 — penyimpanan tak boleh menggelapkan pesan
                pass
            print(f"\n  bagas-ai — sampai jumpa! 👋")
            print(f"  bagas-ai --resume {ses.id} untuk melanjutkan\n")
        except ImportError:
            from .interfaces.cli import main as run
            run(resume=resume, resume_id=resume_id)
        return
    if mode == "telegram":
        from . import osinfo
        osinfo.sync_to_memory()  # deteksi & simpan OS (senyap) untuk penyesuaian perintah
        osinfo.sync_hardware_to_memory()  # spek laptop: lokal, sekali saja
        from .interfaces.telegram_bot import main as run
        run()
        return
    if mode == "api":
        from . import osinfo
        osinfo.sync_to_memory()
        osinfo.sync_hardware_to_memory()
        from .interfaces.api import main as run
        run()
        return
    if mode in ("mcp", "mcp-server"):
        from .mcp_server import main as run
        run()
        return

    print(f"Perintah tidak dikenal: {mode}\n")
    print(HELP)
    sys.exit(1)


if __name__ == "__main__":
    main()

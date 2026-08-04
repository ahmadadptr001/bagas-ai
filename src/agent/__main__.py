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
  bagas-ai login        Wizard: hubungkan bot Telegram (opsional)
  bagas-ai add-dir <p>  Tambah folder konteks agar bagas-ai memahaminya
  bagas-ai update       Cek & terapkan pembaruan dari GitHub
  bagas-ai telegram     Jalankan bot Telegram
  bagas-ai api          Jalankan server API di http://localhost:8000
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

    print("🔄 Memeriksa pembaruan (GitHub + paket yang benar-benar terpasang)…")
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
        print(baris)

    if st == "up_to_date":
        print(f"✓ bagas-ai sudah versi terbaru. ({res.get('local','')})")
        _ringkas_versi()
        return

    if st == "stale_install":
        # Yang menentukan apa yang berjalan adalah salinan terpasang, bukan repo.
        print("⚠ Repo sudah mutakhir, tapi paket yang TERPASANG tertinggal:")
        for b in (res.get("beda") or [])[:10]:
            print("  • " + str(b))
        try:
            ans = input("Pasang ulang sekarang? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("n", "no", "t", "tidak"):
            print("Dilewati.")
            return
        print("⏳ Memasang ulang…")
        _lapor_update(updater.apply(), _ringkas_versi)
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

    _lapor_update(updater.apply(), _ringkas_versi)


def _lapor_update(out: dict, ringkas_versi=None) -> None:
    """Laporkan hasil apply() apa adanya — dan hanya sebut 'berhasil' bila
    isinya BENAR-BENAR terverifikasi sama dengan repo, bukan karena pip pulang
    dengan kode 0 (yang pernah terbukti berbohong)."""
    if out.get("status") != "updated":
        print(f"✖ gagal ({out.get('status')}): {out.get('detail','')}")
        return

    if out.get("verified"):
        print(f"✓ bagas-ai diperbarui & diverifikasi (v{out.get('version','')}).")
        if out.get("how") == "langsung":
            print("  Kode terbaru SUDAH aktif — tak ada yang perlu ditutup "
                  "atau ditunggu. Jalankan ulang perintahnya saja.")
        else:
            print("  Jalankan ulang perintah bagas-ai.")
        if out.get("note"):
            print("  ℹ " + out["note"])
        if ringkas_versi:
            ringkas_versi()
        return

    print("⚠ Pembaruan belum tuntas.")
    if out.get("note"):
        print("  " + out["note"])
    for b in (out.get("diff") or [])[:6]:
        print("  • " + str(b))
    detail = (out.get("pip_detail") or "").strip()
    if detail:
        print(f"  catatan pip: {detail}")


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


def _enforce_update() -> None:
    """PAKSA pasang pembaruan sebelum chat bila cek terakhir menemukannya.

    Instan saat tidak ada pembaruan: hanya membaca cache hasil cek latar
    (tanpa jaringan), jadi startup tetap cepat. Bila cache bilang ada
    pembaruan, update dipasang otomatis (tanpa tanya) lalu bagas-ai
    dimulai ulang dengan versi baru.
    """
    from . import updater

    try:
        cache = updater.read_cache()
    except Exception:
        return
    st = cache.get("status")
    if st not in ("update_available", "stale_install"):
        return

    if st == "stale_install":
        # Repo mutakhir tapi paket yang dijalankan tertinggal — tak terlihat
        # oleh pemeriksaan versi mana pun yang cuma membaca angka.
        print("⬆ Paket bagas-ai yang terpasang tertinggal dari repo — "
              "dipasang ulang dulu…")
        for b in (cache.get("beda") or [])[:5]:
            print("   • " + str(b))
    else:
        local, remote = cache.get("local", "?"), cache.get("remote", "?")
        print(f"⬆ Pembaruan bagas-ai tersedia ({local} → {remote}) — "
              f"dipasang otomatis dulu…")
        for line in (cache.get("log") or "").splitlines()[:5]:
            print("   • " + line)
    try:
        out = updater.apply()
    except KeyboardInterrupt:
        print("\n✖ Update dibatalkan — bagas-ai butuh versi terbaru. Jalankan lagi ya.")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ Gagal memasang pembaruan: {exc}")
        print("  Lanjut pakai versi sekarang; coba `bagas-ai update` nanti.\n")
        return

    # `verified`, bukan `reinstalled`: memulai ulang hanya masuk akal bila kode
    # barunya BENAR-BENAR sudah mendarat. Proses ini masih memegang modul lama
    # di memori, jadi exec ulang itulah yang mengaktifkannya.
    if out.get("status") == "updated" and out.get("verified"):
        print("✓ bagas-ai diperbarui — memulai ulang…\n")
        import subprocess
        rc = subprocess.call(
            [sys.executable, "-m", __package__ or "agent", *sys.argv[1:]])
        sys.exit(rc)
    if out.get("status") == "updated":
        # Sisa kasus yang benar-benar tak bisa dipasang sekarang. Sejak kode
        # bisa disalin langsung ke site-packages, jalur ini jarang terpakai.
        print("⚠ Kode terbaru sudah ditarik, tapi pemasangannya belum tuntas.")
        if out.get("note"):
            print("  " + out["note"])
        for b in (out.get("diff") or [])[:5]:
            print("  • " + str(b))
        print("  Lanjut pakai versi sekarang.\n")
        return
    # Gagal (jaringan/git) -> jangan kunci pengguna dari AI-nya; beri tahu & lanjut.
    print(f"⚠ Gagal memasang pembaruan ({out.get('status')}): "
          f"{out.get('detail', '')}")
    print("  Lanjut pakai versi sekarang; coba `bagas-ai update` nanti.\n")


# _need_key() DIHAPUS: bagas-ai tak lagi punya kredensial wajib. Model dipilih
# lewat /model lalu login dilakukan SEKALI di jendela browser, jadi tak ada lagi
# gerbang "isi API key dulu" sebelum chat/telegram/api boleh dijalankan.


def _preload_with_bar() -> None:
    """Bar loading BERTAHAP saat memuat pustaka berat — fase paling lambat (~1 dtk)
    dari startup. Tiap pustaka diimpor satu per satu sambil bar terisi bertahap,
    lalu impor CLI jadi instan (semua sudah ter-cache)."""
    import importlib
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
        ("antarmuka", f"{pkg}.interfaces.cli"),
    ]
    total = len(steps)
    w = 22
    for i, (label, mod) in enumerate(steps, 1):
        try:
            importlib.import_module(mod)
        except Exception:
            pass
        filled = round(w * i / total)
        bar = "█" * filled + "░" * (w - filled)
        try:
            sys.stdout.write(
                f"\r  ⬢ bagas-ai  memuat  {bar}  {round(100 * i / total):3d}%"
                f"  {label:<18}")
            sys.stdout.flush()
        except Exception:
            pass
    try:
        sys.stdout.write("\r" + " " * 74 + "\r")  # bersihkan baris
        sys.stdout.flush()
    except Exception:
        pass


def main() -> None:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("-")}
    positional = [a for a in args if not a.startswith("-")]
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
        _enforce_update()    # paksa update bila cek latar menemukan pembaruan
        _preload_with_bar()  # bar loading BERTAHAP selama impor pustaka (~1 dtk)
        from .interfaces.cli import main as run
        run(resume=resume)
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

    print(f"Perintah tidak dikenal: {mode}\n")
    print(HELP)
    sys.exit(1)


if __name__ == "__main__":
    main()

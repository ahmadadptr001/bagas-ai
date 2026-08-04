"""Tool VALIDASI proyek — menentukan sendiri cara memeriksa kode lalu menjalankannya.

Gunanya: sesudah bagas-ai selesai mengubah kode, ia tak boleh cuma berkata
"selesai". Ia harus MEMBUKTIKAN kode masih waras. Tapi cara membuktikan berbeda
tiap ekosistem — npm run lint di proyek Node, `ruff`/kompilasi di Python,
`cargo check` di Rust, `go vet` di Go, `php -l` di PHP, dan seterusnya.

validate_project MENDETEKSI ekosistemnya dari berkas penanda (package.json,
pyproject.toml, Cargo.toml, go.mod, composer.json, Makefile) lalu memilih
pemeriksa yang paling tepat: statik dulu (lint / type-check / kompilasi — bukan
seluruh test suite yang bisa lama), DITAMBAH build penuh untuk framework yang
galatnya baru muncul saat build (Next.js dkk. — lint lulus bukan jaminan), dan
smoke-run singkat untuk skrip Python yang diubah ("jalankan dan lihat
hasilnya"). Semuanya dijalankan lalu dirangkum LULUS/GAGAL. Bila tak ada
pemeriksa yang cocok, ia jujur mengatakannya alih-alih diam seolah lulus.

Menjalankan APLIKASI menetap (server dev) tetap diserahkan ke AI lewat
run_command_bg — itu butuh penilaian (port, argumen, kapan berhenti); smoke-run
di sini sengaja bertimeout pendek dan menganggap "masih berjalan" = start sehat.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import config
from .base import tool
from .shell import _clip, _execute, _guard

ROOT: Path = config.PROJECT_ROOT

# Batas waktu per pemeriksaan. Lebih longgar dari cek sintaks ringan (lint/
# type-check bisa memindai banyak berkas) tapi tetap dibatasi agar tak
# menggantung bila sebuah perkakas menunggu sesuatu.
_VAL_TIMEOUT = 180
# Build penuh (mis. `next build`) memang lambat — beri napas lebih panjang,
# karena inilah satu-satunya pemeriksa yang menangkap galat tipe/route Next.js.
_BUILD_TIMEOUT = 600
# Smoke-run skrip Python: cukup untuk crash-saat-import/start terlihat; program
# yang MASIH berjalan melewati batas ini justru pertanda start-nya sehat.
_SMOKE_TIMEOUT = 20
# Test suite penuh bisa lama, tapi tetap wajib berbatas.
_TEST_TIMEOUT = 600


def _pm() -> str:
    """Package manager Node yang dipakai proyek ini (dari lockfile + ketersediaan)."""
    if (ROOT / "pnpm-lock.yaml").is_file() and shutil.which("pnpm"):
        return "pnpm"
    if (ROOT / "yarn.lock").is_file() and shutil.which("yarn"):
        return "yarn"
    if (ROOT / "bun.lockb").is_file() and shutil.which("bun"):
        return "bun"
    return "npm" if shutil.which("npm") else ""


def _pkg_json() -> dict:
    """Isi package.json (kosong bila tak ada / tak terbaca)."""
    pkg = ROOT / "package.json"
    if not pkg.is_file():
        return {}
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - package.json rusak: perlakukan tak ada
        return {}


def _pkg_scripts() -> dict:
    """Isi bagian "scripts" package.json (kosong bila tak ada / tak terbaca)."""
    s = _pkg_json().get("scripts")
    return s if isinstance(s, dict) else {}


def _pkg_deps() -> set[str]:
    """Nama semua dependency package.json (deps + devDeps), untuk deteksi framework."""
    data = _pkg_json()
    out: set[str] = set()
    for bagian in ("dependencies", "devDependencies"):
        d = data.get(bagian)
        if isinstance(d, dict):
            out.update(d.keys())
    return out


def _split_paths(paths: str) -> list[Path]:
    """Pisah daftar path (dipisah koma/spasi/baris) jadi berkas yang benar ada."""
    if not paths:
        return []
    kasar = paths.replace(",", " ").replace("\n", " ").split()
    out: list[Path] = []
    for p in kasar:
        t = (ROOT / p) if not Path(p).is_absolute() else Path(p)
        if t.is_file():
            out.append(t)
    return out


# Framework yang galat tipe/route-nya BARU ketahuan saat build penuh — untuk
# proyek begini, lint saja belum membuktikan apa pun; build ikut diwajibkan.
_BUILD_FRAMEWORKS = {"next", "nuxt", "astro", "@remix-run/dev"}


def _detect_checks(paths: str) -> list[tuple[str, str | list[str], bool, int]]:
    """Susun daftar pemeriksaan (label, perintah, shell, timeout) sesuai isi proyek.

    Urutannya sengaja: yang paling menandakan "kode valid" (type-check, lint,
    kompilasi) didahulukan. Hanya menambahkan pemeriksa yang perkakasnya BENAR
    ada di sistem — biar tak menghasilkan "command not found" yang menyesatkan.
    """
    checks: list[tuple[str, str | list[str], bool, int]] = []
    touched = _split_paths(paths)

    # --- Node / TypeScript / JavaScript --------------------------------------
    scripts = _pkg_scripts()
    if scripts:
        pm = _pm()
        # Skrip proyek sendiri paling dipercaya: penulisnya sudah menyetel
        # aturan lint/type-check yang mereka mau. Ambil yang lazim, tanpa
        # menjalankan "test" (bisa lama / butuh layanan).
        urutan = ["typecheck", "type-check", "tsc", "lint", "check", "lint:fix"]
        dipakai = [n for n in urutan if n in scripts]
        if pm:
            for n in dipakai:
                checks.append((f"{pm} run {n}", f"{pm} run {n}", True,
                               _VAL_TIMEOUT))
        # Tak ada skrip type-check tapi ada tsconfig -> tsc --noEmit langsung.
        if (not any(n in scripts for n in ("typecheck", "type-check", "tsc"))
                and (ROOT / "tsconfig.json").is_file()
                and (shutil.which("npx") or shutil.which("tsc"))):
            checks.append(("tsc --noEmit", "npx --no-install tsc --noEmit", True,
                           _VAL_TIMEOUT))
        # Next.js dkk.: banyak galat (tipe, route, import server/client) BARU
        # muncul saat `run build` — lint lulus bukan jaminan. Maka untuk
        # framework di daftar itu build SELALU ikut; proyek Node lain tetap
        # memakai build hanya sebagai pemeriksa terakhir bila tak ada yang lain.
        if "build" in scripts and pm and (not dipakai
                                          or _pkg_deps() & _BUILD_FRAMEWORKS):
            checks.append((f"{pm} run build", f"{pm} run build", True,
                           _BUILD_TIMEOUT))

    # --- Python --------------------------------------------------------------
    ada_py = (ROOT / "pyproject.toml").is_file() or (ROOT / "setup.py").is_file() \
        or any(ROOT.glob("*.py")) or any(ROOT.rglob("*.py"))
    if ada_py:
        if shutil.which("ruff"):
            checks.append(("ruff check", ["ruff", "check", "."], False,
                           _VAL_TIMEOUT))
        elif shutil.which("flake8"):
            checks.append(("flake8", ["flake8"], False, _VAL_TIMEOUT))
        # mypy hanya bila proyek memang mengonfigurasinya (kalau tidak, ribuan
        # galat tipe pihak-ketiga cuma bikin bising).
        if shutil.which("mypy") and _mypy_dikonfigurasi():
            checks.append(("mypy", ["mypy", "."], False, _VAL_TIMEOUT))
        # Kompilasi (parse) berkas .py yang DISENTUH — cepat, tak menjalankan.
        py_touched = [str(t) for t in touched if t.suffix.lower() in (".py", ".pyw")]
        if py_touched:
            checks.append(("py_compile (berkas yang diubah)",
                           ["python", "-m", "py_compile", *py_touched], False,
                           _VAL_TIMEOUT))
        elif not shutil.which("ruff") and not shutil.which("flake8"):
            # Tak ada linter & tak ada daftar berkas: kompilasi seluruh paket
            # secara diam (bounded oleh timeout) sebagai jaring minimal.
            checks.append(("compileall", ["python", "-m", "compileall", "-q", "."],
                           False, _VAL_TIMEOUT))

    # --- Rust ----------------------------------------------------------------
    if (ROOT / "Cargo.toml").is_file() and shutil.which("cargo"):
        checks.append(("cargo check", ["cargo", "check", "--quiet"], False,
                       _VAL_TIMEOUT))

    # --- Go ------------------------------------------------------------------
    if (ROOT / "go.mod").is_file() and shutil.which("go"):
        checks.append(("go vet", ["go", "vet", "./..."], False, _VAL_TIMEOUT))
        checks.append(("go build", ["go", "build", "./..."], False, _VAL_TIMEOUT))

    # --- PHP -----------------------------------------------------------------
    if shutil.which("php"):
        php_touched = [str(t) for t in touched if t.suffix.lower() == ".php"]
        for f in php_touched:
            checks.append((f"php -l {Path(f).name}", ["php", "-l", f], False,
                           _VAL_TIMEOUT))

    # --- Makefile lint (proyek yang menaruh perintahnya di Make) -------------
    mk = ROOT / "Makefile"
    if mk.is_file() and shutil.which("make"):
        try:
            teks = mk.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            teks = ""
        for target in ("lint", "check", "typecheck"):
            if f"\n{target}:" in teks or teks.startswith(f"{target}:"):
                checks.append((f"make {target}", ["make", target], False,
                               _VAL_TIMEOUT))
                break

    return checks


def _smoke_python(touched: list[Path]) -> list[tuple[str, str, bool]]:
    """Jalankan sebentar skrip Python yang DIUBAH — "tinggal jalanin dan lihat
    hasilnya": crash saat import/start (ImportError, dependency hilang,
    NameError di level modul) ketahuan di sini padahal lolos lint.

    Hanya berkas yang punya guard __main__ (memang dirancang dijalankan) yang
    dicoba; modul murni cukup diperiksa statik + py_compile. Hasil per berkas:
    (label, keterangan, gagal?). Semantik khusus:
      - timeout  = program MASIH berjalan setelah _SMOKE_TIMEOUT -> start sehat,
        dihentikan, LULUS (server/loop memang tak akan pernah "selesai").
      - EOFError = skrip menunggu input interaktif (stdin sengaja ditutup) ->
        bukan kegagalan kode, dilewati dengan catatan.
    """
    kandidat: list[Path] = []
    for t in touched:
        if t.suffix.lower() not in (".py", ".pyw"):
            continue
        try:
            teks = t.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - tak terbaca: biar pemeriksa lain yang lapor
            continue
        if "__main__" in teks:
            kandidat.append(t)
    hasil: list[tuple[str, str, bool]] = []
    for f in kandidat[:3]:  # dibatasi: smoke-run itu pelengkap, bukan test suite
        label = f"python {f.name} (smoke-run)"
        rc, out, timed_out = _execute(["python", str(f)], shell=False,
                                      timeout=_SMOKE_TIMEOUT)
        if timed_out:
            hasil.append((label,
                          f"masih BERJALAN setelah {_SMOKE_TIMEOUT}s tanpa "
                          "crash (dihentikan) — start dianggap sehat", False))
        elif rc == 0:
            hasil.append((label, "selesai tanpa error (exit=0)", False))
        elif "EOFError" in out:
            hasil.append((label,
                          "menunggu input interaktif (stdin ditutup saat "
                          "validasi) — dilewati, bukan kegagalan kode", False))
        else:
            hasil.append((label, f"CRASH (exit={rc})\n{_clip(out, 3000)}", True))
    return hasil


def _mypy_dikonfigurasi() -> bool:
    """True bila proyek memang menyetel mypy (jangan paksa kalau tidak)."""
    if (ROOT / "mypy.ini").is_file() or (ROOT / ".mypy.ini").is_file():
        return True
    for nama in ("pyproject.toml", "setup.cfg"):
        p = ROOT / nama
        if p.is_file():
            try:
                if "[tool.mypy]" in p.read_text(encoding="utf-8", errors="replace") \
                        or "[mypy]" in p.read_text(encoding="utf-8", errors="replace"):
                    return True
            except Exception:  # noqa: BLE001
                pass
    return False


def _ada_py_test() -> bool:
    """Deteksi keberadaan berkas test Python TANPA memindai seluruh pohon
    (rglob dari root bisa menyapu node_modules/.git yang raksasa)."""
    for d in ("tests", "test"):
        p = ROOT / d
        if p.is_dir() and (any(p.rglob("test_*.py")) or any(p.rglob("*_test.py"))):
            return True
    for pat in ("test_*.py", "*_test.py"):
        if any(ROOT.glob(pat)):
            return True
        src = ROOT / "src"
        if src.is_dir() and any(src.rglob(pat)):
            return True
    return False


def _deteksi_tests() -> list[tuple[str, str | list[str], bool]]:
    """(label, perintah, shell) test runner per ekosistem yang BENAR terdeteksi."""
    runs: list[tuple[str, str | list[str], bool]] = []

    scripts = _pkg_scripts()
    t = scripts.get("test", "")
    pm = _pm()
    # Placeholder bawaan `npm init` bukan test sungguhan — jangan dijalankan.
    if pm and isinstance(t, str) and t.strip() and "no test specified" not in t:
        runs.append((f"{pm} test", f"{pm} test", True))

    if _ada_py_test():
        # pytest bila tersedia di interpreter yang sama; kalau tidak, unittest.
        rc, _, timed = _execute(["python", "-m", "pytest", "--version"],
                                shell=False, timeout=30)
        if rc == 0 and not timed:
            runs.append(("pytest", ["python", "-m", "pytest", "-q",
                                    "--maxfail=10"], False))
        else:
            runs.append(("unittest", ["python", "-m", "unittest", "discover"],
                         False))

    if (ROOT / "go.mod").is_file() and shutil.which("go"):
        runs.append(("go test", ["go", "test", "./..."], False))
    if (ROOT / "Cargo.toml").is_file() and shutil.which("cargo"):
        runs.append(("cargo test", ["cargo", "test", "--quiet"], False))
    return runs


@tool
def run_tests() -> str:
    """Jalankan TEST SUITE proyek — deteksi sendiri runnernya (npm/pnpm/yarn test, pytest/unittest, go test, cargo test) lalu laporkan LULUS/GAGAL + output pentingnya. Pelengkap validate_project (yang hanya cek statik): pakai ini sesudah perubahan besar atau saat proyek memang punya test.

    Tanpa argumen — seluruh suite. Untuk menjalankan subset/test tertentu
    (mis. `pytest tests/test_x.py -k nama`), pakai run_command langsung.
    """
    blocked = _guard()
    if blocked:
        return blocked
    runs = _deteksi_tests()
    if not runs:
        return (
            "[tes] Tak ada test suite yang terdeteksi (tidak ada skrip "
            "package.json 'test' yang sungguhan, berkas test_*.py, go.mod, "
            "maupun Cargo.toml — atau runnernya belum terpasang). Kalau kamu "
            "yakin proyek ini punya test, jalankan langsung dengan run_command."
        )
    bagian: list[str] = []
    gagal = 0
    for label, cmd, sh in runs:
        rc, out, timed_out = _execute(cmd, shell=sh, timeout=_TEST_TIMEOUT)
        if timed_out:
            gagal += 1
            bagian.append(
                f"⏱ {label}: TIMEOUT (> {_TEST_TIMEOUT}s, dihentikan) — anggap "
                f"BELUM lulus. Output terakhir:\n{_clip(out, 2000)}")
        elif rc == 0:
            # Ekor output disertakan: "LULUS" tanpa bukti (mis. 'Ran 0 tests')
            # menyesatkan — biarkan modelnya menilai jumlah test yang jalan.
            ekor = "\n".join(out.strip().splitlines()[-5:])
            bagian.append(f"✓ {label}: LULUS\n{_clip(ekor, 500)}")
        else:
            gagal += 1
            bagian.append(f"✗ {label}: GAGAL (exit={rc})\n{_clip(out, 4000)}")
    kepala = (
        f"[tes] {len(runs) - gagal}/{len(runs)} runner lulus."
        if gagal else f"[tes] SEMUA {len(runs)} runner LULUS.")
    if gagal:
        kepala += (" Ada test yang GAGAL — baca detailnya, perbaiki, lalu "
                   "jalankan lagi. JANGAN nyatakan tugas selesai.")
    return kepala + "\n\n" + "\n\n".join(bagian)


@tool
def validate_project(paths: str = "") -> str:
    """Validasi ulang kode proyek SESUAI EKOSISTEMNYA: DETEKSI SENDIRI cara memeriksanya (Next.js: npm run lint + npm run build; Python: ruff/py_compile + menjalankan skrip yang diubah; Rust: cargo check; Go: go vet+build; PHP: php -l; dll.) lalu jalankan & laporkan LULUS/GAGAL. Panggil ini SEBELUM menyatakan tugas selesai.

    Memilih pemeriksa sesuai ekosistem proyek: statik dulu (lint / type-check /
    kompilasi), lalu build penuh untuk framework yang galatnya baru muncul saat
    build (Next.js dkk.), plus smoke-run singkat untuk skrip Python yang diubah
    ("jalankan dan lihat hasilnya"). Bila ada yang GAGAL, perbaiki dulu; jangan
    anggap tugas selesai.

    paths: opsional, daftar berkas yang baru kamu ubah (dipisah spasi/koma) agar
        pemeriksaan per-berkas (py_compile, smoke-run, php -l) menyasar tepat
        ke sana.
    """
    blocked = _guard()
    if blocked:
        return blocked
    checks = _detect_checks(paths)
    smoke = _smoke_python(_split_paths(paths))
    if not checks and not smoke:
        return (
            "[validasi] Tak ada cara validasi otomatis yang terdeteksi untuk "
            "proyek ini (tak ada package.json/pyproject/Cargo.toml/go.mod/… yang "
            "dikenali, atau perkakasnya belum terpasang). Validasi manual: "
            "jalankan program/entry point-nya dengan run_command / run_command_bg "
            "dan pastikan tak ada error saat start."
        )

    bagian: list[str] = []
    gagal = 0
    for label, cmd, shell, batas in checks:
        rc, out, timed_out = _execute(cmd, shell=shell, timeout=batas)
        if timed_out:
            gagal += 1
            bagian.append(
                f"⏱ {label}: TIMEOUT (> {batas}s, dihentikan) — anggap "
                f"BELUM lulus.\n{_clip(out, 1500)}")
        elif rc == 0:
            bagian.append(f"✓ {label}: LULUS")
        else:
            gagal += 1
            bagian.append(
                f"✗ {label}: GAGAL (exit={rc})\n{_clip(out, 3000)}")

    for label, ket, buruk in smoke:
        if buruk:
            gagal += 1
            bagian.append(f"✗ {label}: {ket}")
        else:
            bagian.append(f"✓ {label}: {ket}")

    total = len(checks) + len(smoke)
    kepala = (
        f"[validasi] {total - gagal}/{total} pemeriksaan lulus."
        if gagal else
        f"[validasi] SEMUA {total} pemeriksaan LULUS.")
    if gagal:
        kepala += (" Ada yang GAGAL — baca detailnya, PERBAIKI kodenya, lalu "
                   "validasi lagi. JANGAN nyatakan tugas selesai.")
    return kepala + "\n\n" + "\n\n".join(bagian)


# --- project_info -----------------------------------------------------------
#
# Pengetahuan yang dipakai validate_project untuk memilih pemeriksaan (ekosistem
# apa, package manager mana, skrip apa saja yang tersedia) selama ini TERKUNCI di
# dalamnya — model tak bisa melihatnya, jadi tiap sesi ia mengulang penjajakan
# yang sama: list_dir, read_file package.json, read_file pyproject.toml. Tiga
# bolak-balik ke situs AI hanya untuk tahu hal yang sudah kita ketahui.
# project_info membukanya dalam satu panggilan.
_PENANDA = (
    ("package.json", "Node/JavaScript"),
    ("pyproject.toml", "Python"),
    ("setup.py", "Python"),
    ("requirements.txt", "Python"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("composer.json", "PHP"),
    ("pom.xml", "Java (Maven)"),
    ("build.gradle", "Java/Kotlin (Gradle)"),
    ("Gemfile", "Ruby"),
    ("pubspec.yaml", "Dart/Flutter"),
    ("Makefile", "Make"),
)
# Framework dikenali dari dependency-nya, bukan dari struktur folder: struktur
# gampang menipu (folder `app/` ada di banyak framework), sedangkan dependency
# tak bisa berbohong.
_FRAMEWORK = (
    ("next", "Next.js"), ("nuxt", "Nuxt"), ("@angular/core", "Angular"),
    ("svelte", "Svelte"), ("vue", "Vue"), ("react", "React"),
    ("vite", "Vite"), ("astro", "Astro"), ("express", "Express"),
    ("fastify", "Fastify"), ("electron", "Electron"),
    ("tailwindcss", "Tailwind CSS"), ("typescript", "TypeScript"),
)


@tool
def project_info() -> str:
    """Ringkasan TEKNIS proyek dalam satu panggilan: bahasa/ekosistemnya, framework yang dipakai, package manager, skrip yang tersedia (npm run apa saja), entry point, dan apakah ini repo git. Panggil ini DI AWAL sebelum menjelajah — ia menggantikan rangkaian list_dir + membaca package.json/pyproject.toml satu per satu.

    Tidak mengubah apa pun.
    """
    baris: list[str] = [f"root: {ROOT}"]

    eko = []
    for berkas, nama in _PENANDA:
        if (ROOT / berkas).is_file() and nama not in eko:
            eko.append(nama)
    baris.append("ekosistem: " + (", ".join(eko) if eko else
                                  "tak terdeteksi (tak ada berkas penanda)"))

    deps = _pkg_deps()
    if deps:
        fw = [nama for kunci, nama in _FRAMEWORK if kunci in deps]
        if fw:
            baris.append("framework/pustaka kunci: " + ", ".join(fw))
        baris.append(f"jumlah dependency: {len(deps)}")

    pm = _pm()
    if (ROOT / "package.json").is_file():
        baris.append(f"package manager: {pm or 'tak ada npm/pnpm/yarn di PATH'}")

    skrip = _pkg_scripts()
    if skrip:
        pakai = pm or "npm"
        baris.append("skrip tersedia:")
        for nama, isi in list(skrip.items())[:20]:
            teks = str(isi)
            if len(teks) > 70:
                teks = teks[:67] + "…"
            baris.append(f"  {pakai} run {nama}  →  {teks}")

    # Entry point: yang benar-benar ADA saja, supaya model tak mencoba menjalankan
    # berkas yang tak pernah ada.
    kandidat = ("main.py", "app.py", "manage.py", "run.py", "index.js",
                "server.js", "src/main.py", "src/index.ts", "src/main.ts",
                "src/App.tsx", "app/page.tsx", "src/app/page.tsx", "main.go",
                "src/main.rs", "Cargo.toml")
    ada = [k for k in kandidat if (ROOT / k).is_file()]
    if ada:
        baris.append("entry point yang ada: " + ", ".join(ada[:8]))

    baris.append("git: " + ("ya — pakai git_status/git_diff untuk melihat "
                            "perubahan yang belum di-commit"
                            if (ROOT / ".git").exists() else "bukan repo git"))
    return "\n".join(baris)

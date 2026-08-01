"""Tool VALIDASI proyek — menentukan sendiri cara memeriksa kode lalu menjalankannya.

Gunanya: sesudah bagas-ai selesai mengubah kode, ia tak boleh cuma berkata
"selesai". Ia harus MEMBUKTIKAN kode masih waras. Tapi cara membuktikan berbeda
tiap ekosistem — npm run lint di proyek Node, `ruff`/kompilasi di Python,
`cargo check` di Rust, `go vet` di Go, `php -l` di PHP, dan seterusnya.

validate_project MENDETEKSI ekosistemnya dari berkas penanda (package.json,
pyproject.toml, Cargo.toml, go.mod, composer.json, Makefile) lalu memilih
perintah pemeriksa yang paling tepat DAN CEPAT (statik: lint / type-check /
kompilasi — bukan menjalankan seluruh test suite yang bisa lama), menjalankan
semuanya, lalu merangkum LULUS/GAGAL. Bila tak ada pemeriksa yang cocok, ia
jujur mengatakannya alih-alih diam seolah lulus.

Menjalankan APLIKASI-nya (server dev, skrip) diserahkan ke AI lewat run_command/
run_command_bg — itu butuh penilaian (port, argumen, kapan berhenti), sedangkan
di sini fokusnya pemeriksaan statik yang aman & deterministik.
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


def _pm() -> str:
    """Package manager Node yang dipakai proyek ini (dari lockfile + ketersediaan)."""
    if (ROOT / "pnpm-lock.yaml").is_file() and shutil.which("pnpm"):
        return "pnpm"
    if (ROOT / "yarn.lock").is_file() and shutil.which("yarn"):
        return "yarn"
    if (ROOT / "bun.lockb").is_file() and shutil.which("bun"):
        return "bun"
    return "npm" if shutil.which("npm") else ""


def _pkg_scripts() -> dict:
    """Isi bagian "scripts" package.json (kosong bila tak ada / tak terbaca)."""
    pkg = ROOT / "package.json"
    if not pkg.is_file():
        return {}
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        s = data.get("scripts")
        return s if isinstance(s, dict) else {}
    except Exception:  # noqa: BLE001 - package.json rusak: perlakukan tak ada skrip
        return {}


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


def _detect_checks(paths: str) -> list[tuple[str, str | list[str], bool]]:
    """Susun daftar pemeriksaan (label, perintah, shell) sesuai isi proyek.

    Urutannya sengaja: yang paling menandakan "kode valid" (type-check, lint,
    kompilasi) didahulukan. Hanya menambahkan pemeriksa yang perkakasnya BENAR
    ada di sistem — biar tak menghasilkan "command not found" yang menyesatkan.
    """
    checks: list[tuple[str, str | list[str], bool]] = []
    touched = _split_paths(paths)

    # --- Node / TypeScript / JavaScript --------------------------------------
    scripts = _pkg_scripts()
    if scripts:
        pm = _pm()
        # Skrip proyek sendiri paling dipercaya: penulisnya sudah menyetel
        # aturan lint/type-check yang mereka mau. Ambil yang lazim, tanpa
        # menjalankan "test" (bisa lama / butuh layanan) atau "build" penuh
        # kecuali tak ada pemeriksa lain.
        urutan = ["typecheck", "type-check", "tsc", "lint", "check", "lint:fix"]
        dipakai = [n for n in urutan if n in scripts]
        if pm:
            for n in dipakai:
                checks.append((f"{pm} run {n}", f"{pm} run {n}", True))
        # Tak ada skrip type-check tapi ada tsconfig -> tsc --noEmit langsung.
        if (not any(n in scripts for n in ("typecheck", "type-check", "tsc"))
                and (ROOT / "tsconfig.json").is_file()
                and (shutil.which("npx") or shutil.which("tsc"))):
            checks.append(("tsc --noEmit", "npx --no-install tsc --noEmit", True))
        # Kalau sama sekali tak ada pemeriksa statik, jatuh ke build sebagai
        # bukti minimal bahwa proyek masih ter-compile.
        if not dipakai and "build" in scripts and pm:
            checks.append((f"{pm} run build", f"{pm} run build", True))

    # --- Python --------------------------------------------------------------
    ada_py = (ROOT / "pyproject.toml").is_file() or (ROOT / "setup.py").is_file() \
        or any(ROOT.glob("*.py")) or any(ROOT.rglob("*.py"))
    if ada_py:
        if shutil.which("ruff"):
            checks.append(("ruff check", ["ruff", "check", "."], False))
        elif shutil.which("flake8"):
            checks.append(("flake8", ["flake8"], False))
        # mypy hanya bila proyek memang mengonfigurasinya (kalau tidak, ribuan
        # galat tipe pihak-ketiga cuma bikin bising).
        if shutil.which("mypy") and _mypy_dikonfigurasi():
            checks.append(("mypy", ["mypy", "."], False))
        # Kompilasi (parse) berkas .py yang DISENTUH — cepat, tak menjalankan.
        py_touched = [str(t) for t in touched if t.suffix.lower() in (".py", ".pyw")]
        if py_touched:
            checks.append(("py_compile (berkas yang diubah)",
                           ["python", "-m", "py_compile", *py_touched], False))
        elif not shutil.which("ruff") and not shutil.which("flake8"):
            # Tak ada linter & tak ada daftar berkas: kompilasi seluruh paket
            # secara diam (bounded oleh timeout) sebagai jaring minimal.
            checks.append(("compileall", ["python", "-m", "compileall", "-q", "."],
                           False))

    # --- Rust ----------------------------------------------------------------
    if (ROOT / "Cargo.toml").is_file() and shutil.which("cargo"):
        checks.append(("cargo check", ["cargo", "check", "--quiet"], False))

    # --- Go ------------------------------------------------------------------
    if (ROOT / "go.mod").is_file() and shutil.which("go"):
        checks.append(("go vet", ["go", "vet", "./..."], False))
        checks.append(("go build", ["go", "build", "./..."], False))

    # --- PHP -----------------------------------------------------------------
    if shutil.which("php"):
        php_touched = [str(t) for t in touched if t.suffix.lower() == ".php"]
        for f in php_touched:
            checks.append((f"php -l {Path(f).name}", ["php", "-l", f], False))

    # --- Makefile lint (proyek yang menaruh perintahnya di Make) -------------
    mk = ROOT / "Makefile"
    if mk.is_file() and shutil.which("make"):
        try:
            teks = mk.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            teks = ""
        for target in ("lint", "check", "typecheck"):
            if f"\n{target}:" in teks or teks.startswith(f"{target}:"):
                checks.append((f"make {target}", ["make", target], False))
                break

    return checks


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


@tool
def validate_project(paths: str = "") -> str:
    """Validasi ulang kode proyek: DETEKSI SENDIRI cara memeriksanya (npm run lint/typecheck, ruff/py_compile, cargo check, go vet, php -l, make lint, dll.) lalu jalankan & laporkan LULUS/GAGAL. Panggil ini SEBELUM menyatakan tugas selesai.

    Memilih pemeriksa statik yang cepat (lint / type-check / kompilasi) sesuai
    ekosistem proyek — bukan menjalankan seluruh test. Bila ada yang GAGAL,
    perbaiki dulu; jangan anggap tugas selesai.

    paths: opsional, daftar berkas yang baru kamu ubah (dipisah spasi/koma) agar
        pemeriksaan per-berkas (mis. py_compile, php -l) menyasar tepat ke sana.
    """
    blocked = _guard()
    if blocked:
        return blocked
    checks = _detect_checks(paths)
    if not checks:
        return (
            "[validasi] Tak ada cara validasi otomatis yang terdeteksi untuk "
            "proyek ini (tak ada package.json/pyproject/Cargo.toml/go.mod/… yang "
            "dikenali, atau perkakasnya belum terpasang). Validasi manual: "
            "jalankan program/entry point-nya dengan run_command / run_command_bg "
            "dan pastikan tak ada error saat start."
        )

    bagian: list[str] = []
    gagal = 0
    for label, cmd, shell in checks:
        rc, out, timed_out = _execute(cmd, shell=shell, timeout=_VAL_TIMEOUT)
        if timed_out:
            gagal += 1
            bagian.append(
                f"⏱ {label}: TIMEOUT (> {_VAL_TIMEOUT}s, dihentikan) — anggap "
                f"BELUM lulus.\n{_clip(out, 1500)}")
        elif rc == 0:
            bagian.append(f"✓ {label}: LULUS")
        else:
            gagal += 1
            bagian.append(
                f"✗ {label}: GAGAL (exit={rc})\n{_clip(out, 3000)}")

    kepala = (
        f"[validasi] {len(checks) - gagal}/{len(checks)} pemeriksaan lulus."
        if gagal else
        f"[validasi] SEMUA {len(checks)} pemeriksaan LULUS.")
    if gagal:
        kepala += (" Ada yang GAGAL — baca detailnya, PERBAIKI kodenya, lalu "
                   "validasi lagi. JANGAN nyatakan tugas selesai.")
    return kepala + "\n\n" + "\n\n".join(bagian)

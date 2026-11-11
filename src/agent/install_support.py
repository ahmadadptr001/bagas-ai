"""Pekerjaan installer yang bisa dipakai ulang oleh PowerShell dan Bash.

Dapat dijalankan langsung sebelum dependensi tersedia. Pemeriksaan paket
memakai metadata dan isi sumber; komponen yang valid tidak dipasang ulang.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib
import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


def _is_termux() -> bool:
    """True bila berjalan di Termux (Android).

    Deteksi dilakukan tanpa mengimpor paket ``agent``: modul ini dijalankan
    langsung sebagai skrip sebelum dependensi terpasang.
    """
    return (bool(os.environ.get("TERMUX_VERSION"))
            or "com.termux" in os.environ.get("PREFIX", ""))


def _requirement_lines(path: Path) -> list[str]:
    return [line.split("#", 1)[0].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()]


def _requirements(source: Path) -> list[str]:
    """Dependensi yang BENAR-BENAR dipasang di mesin ini.

    Termux memakai requirements-termux.txt: pyproject.toml memuat Playwright,
    Whisper, sounddevice, dan paket desktop lain yang tak ada/tak bisa
    dibangun di Android.
    """
    if _is_termux():
        termux_req = source / "requirements-termux.txt"
        if termux_req.is_file():
            return _requirement_lines(termux_req)
    return _project(source)["project"].get("dependencies", [])


def _project(source: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        try:
            from pip._vendor import tomli as tomllib
        except ImportError:
            text = (source / "pyproject.toml").read_text(encoding="utf-8")
            version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
            requirements = [line.split("#", 1)[0].strip()
                            for line in (source / "requirements.txt").read_text(encoding="utf-8").splitlines()]
            return {"project": {"version": version.group(1) if version else "",
                                "dependencies": [r for r in requirements if r]},
                    "build-system": {"requires": ["setuptools>=61.0"]}}
    return tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))


def missing_requirements(requirements: list[str]) -> list[str]:
    try:
        from packaging.requirements import Requirement
    except ImportError:
        from pip._vendor.packaging.requirements import Requirement
    missing = []
    for raw in requirements:
        req = Requirement(raw)
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue
        try:
            present = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            missing.append(raw)
            continue
        if req.specifier and not req.specifier.contains(present, prereleases=True):
            missing.append(raw)
    return missing


def _modules(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
            for p in root.rglob("*.py") if "__pycache__" not in p.parts}


def package_current(source: Path) -> bool:
    try:
        dist = metadata.distribution("bagasai")
        if dist.version != _project(source)["project"]["version"]:
            return False
        package = Path(dist.locate_file("agent"))
        # Editable: distribusi bisa mencatat src lewat direct_url.json.
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            from urllib.parse import unquote, urlsplit
            from urllib.request import url2pathname
            package = Path(url2pathname(unquote(urlsplit(direct["url"]).path))) / "src" / "agent"
        expected = _modules(source / "src" / "agent")
        scripts = {ep.name for ep in dist.entry_points if ep.group == "console_scripts"}
        return bool(expected) and {"bagas-ai", "bagasai", "bagas"} <= scripts and _modules(package) == expected
    except (metadata.PackageNotFoundError, OSError, ValueError, KeyError):
        return False


class InstallerError(RuntimeError):
    pass


class Reporter:
    def __init__(self):
        home = Path(os.environ.get("BAGASAI_HOME", Path.home() / ".bagasai"))
        self.log = Path(os.environ.get("BAGASAI_INSTALL_LOG", home / "logs" / "install.log"))
        self.log.parent.mkdir(parents=True, exist_ok=True)

    def run(self, args: list[str], label: str, *, timeout: int = 1800,
            cwd: Path | None = None) -> None:
        start = time.monotonic()
        tty = sys.stdout.isatty()
        if not tty:
            print(f"  ... {label}", flush=True)
        with self.log.open("a", encoding="utf-8") as log:
            log.write("\n" + label + "\n" + json.dumps(args, ensure_ascii=True) + "\n")
            log.flush()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.Popen(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, creationflags=flags)
            try:
                while process.poll() is None:
                    elapsed = int(time.monotonic() - start)
                    if elapsed >= timeout:
                        raise InstallerError(f"{label}: melewati batas waktu")
                    if tty:
                        frame = "|/-\\"[int((time.monotonic() - start) * 5) % 4]
                        line = f"  {frame} {label}  {elapsed // 60}:{elapsed % 60:02d}"
                        width = max(1, shutil.get_terminal_size().columns - 1)
                        if len(line) > width:
                            line = line[:max(0, width - 3)] + "." * min(3, width)
                        print("\r" + line, end="", flush=True)
                    time.sleep(0.15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if tty:
                    print("\r" + " " * min(100, shutil.get_terminal_size().columns - 1) + "\r", end="", flush=True)
            if process.returncode:
                raise InstallerError(f"{label} gagal (kode {process.returncode})")


def _source_metadata(source: Path) -> None:
    """Catat checkout asli, bukan direktori build sementara, ke metadata pip."""
    dist = metadata.distribution("bagasai")
    entry = next((p for p in dist.files or []
                  if p.name == "METADATA" and p.parent.name.endswith(".dist-info")), None)
    if entry is None:
        return
    info = Path(dist.locate_file(entry)).parent
    direct = info / "direct_url.json"
    data = json.dumps({"url": source.as_uri(), "dir_info": {}}, ensure_ascii=True).encode()
    direct.write_bytes(data)
    record = info / "RECORD"
    if record.is_file():
        with record.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        name = info.name + "/direct_url.json"
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows = [row for row in rows if row[0] != name]
        rows.append([name, "sha256=" + digest, str(len(data))])
        with record.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)


def install_python(source: Path, reporter: Reporter, *, user: bool = False,
                   break_system: bool = False) -> None:
    project = _project(source)
    requirements = _requirements(source)
    missing = missing_requirements(requirements)
    current = package_current(source)
    if current and not missing:
        print("  [ok] Paket Python sudah tersedia", flush=True)
        return
    # Termux: pip sudah menulis ke $PREFIX (setara venv) — --user justru
    # menaruh skrip di ~/.local/bin yang belum tentu ada di PATH.
    flags = (["--user"] if user and not _is_termux()
             and sys.prefix == sys.base_prefix else [])
    if break_system:
        flags.append("--break-system-packages")
    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
           "--quiet", "--progress-bar", "off", *flags]
    if missing:
        reporter.run([*pip, *missing], f"Menyiapkan {len(missing)} dependensi Python")
        importlib.invalidate_caches()
    if not current:
        build = missing_requirements([*project.get("build-system", {}).get("requires", []), "wheel"])
        if build:
            reporter.run([*pip, *build], "Menyiapkan alat pemasangan")
        # Build bersih mencegah build/lib lama masuk lagi ke paket. Cache pip
        # tetap dipakai, dan dependensi yang sudah ada tidak disentuh.
        with tempfile.TemporaryDirectory(prefix="bagasai-build-") as tmp:
            staging = Path(tmp)
            for name in ("pyproject.toml", "README.md", "requirements.txt", "LICENSE"):
                if (source / name).is_file():
                    shutil.copy2(source / name, staging / name)
            shutil.copytree(source / "src", staging / "src",
                            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"))
            reporter.run([*pip, "--no-deps", "--no-build-isolation", str(staging)],
                         "Memasang aplikasi", cwd=staging)
        importlib.invalidate_caches()
        _source_metadata(source)
    if not package_current(source) or missing_requirements(requirements):
        raise InstallerError("Paket Python belum sesuai dengan sumber")
    print("  [ok] Paket Python siap", flush=True)


def chromium_ready() -> bool:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).is_file():
            return False
        try:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                return False
            # Runtime ada tetapi gagal berjalan: mengunduh ulang tidak
            # memperbaiki pustaka OS yang hilang atau kebijakan perangkat.
            raise InstallerError("Chromium tersedia tetapi gagal dijalankan: " + str(exc).splitlines()[0]) from exc
    return True


def ensure_browser(reporter: Reporter) -> None:
    if _is_termux():
        print("  [info] Termux/Android: Playwright Chromium tidak tersedia — "
              "dilewati. Model berjalan lewat CLI OpenCode.", flush=True)
        return
    if chromium_ready():
        print("  [ok] Chromium sudah tersedia", flush=True)
        return
    reporter.run([sys.executable, "-m", "playwright", "install", "chromium"], "Mengunduh Chromium")
    if not chromium_ready():
        raise InstallerError("Chromium belum siap setelah pemasangan")
    print("  [ok] Chromium siap", flush=True)


def find_ollama() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    for variable, suffix in (("LOCALAPPDATA", "Programs/Ollama/ollama.exe"),
                             ("ProgramFiles", "Ollama/ollama.exe")):
        root = os.environ.get(variable)
        if root and (Path(root) / suffix).is_file():
            return str(Path(root) / suffix)
    return ""


def ensure_blip(reporter: Reporter) -> None:
    from agent.blip_local import ensure_blip_available
    ready, _ = ensure_blip_available(download=False)
    if not ready:
        reporter.run([sys.executable, str(Path(__file__).with_name("blip_worker.py")), "--prepare-model"],
                     "Menyiapkan model BLIP", timeout=900)
        ready, why = ensure_blip_available(download=False)
        if not ready:
            raise InstallerError(why)
    print("  [ok] BLIP siap", flush=True)


def ensure_vision(reporter: Reporter) -> None:
    if _is_termux():
        print("  [info] Termux/Android: Ollama + Gemma vision tidak tersedia — "
              "dilewati. Gunakan model yang bisa melihat gambar lewat chat.", flush=True)
        return
    ensure_blip(reporter)
    from agent.tools import vision_local as vision
    executable = find_ollama()
    if executable:
        os.environ["PATH"] = str(Path(executable).parent) + os.pathsep + os.environ.get("PATH", "")
    alive, why = vision._mulai_ollama()
    if not alive:
        raise InstallerError(why)
    if not vision._model_ada():
        if not executable:
            raise InstallerError("Ollama tidak tersedia untuk mengunduh model gambar")
        reporter.run([executable, "pull", vision._nama_model()], "Mengunduh model gambar")
    else:
        print("  [ok] Model gambar sudah tersedia", flush=True)
    reporter.run([sys.executable, str(Path(__file__).resolve()), "vision-probe"],
                 "Memverifikasi model gambar", timeout=300)
    print("  [ok] Model gambar siap", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pemeriksaan dan pemasangan komponen bagas-ai")
    parser.add_argument("action", choices=("python", "browser", "voice", "vision", "vision-probe", "find-ollama", "plan"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--break-system-packages", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "find-ollama":
        found = find_ollama()
        if found:
            print(found)
        return 0 if found else 1
    source = args.source.resolve()
    if args.action == "plan":
        deps = _requirements(source)
        print(json.dumps({"package_current": package_current(source), "missing_requirements":
            missing_requirements(deps)}))
        return 0
    reporter = Reporter()
    try:
        if args.action == "python":
            if importlib.util.find_spec("pip") is None:
                reporter.run([sys.executable, "-m", "ensurepip", "--default-pip"], "Menyiapkan pip")
            install_python(source, reporter, user=args.user, break_system=args.break_system_packages)
        elif args.action == "browser":
            ensure_browser(reporter)
        elif args.action == "voice":
            if _is_termux():
                print("  [info] Termux/Android: Whisper/sounddevice tidak tersedia — "
                      "dikte memakai mesin STT Android (termux-speech-to-text).",
                      flush=True)
            else:
                reporter.run([sys.executable, "-m", "agent.dengar", "--prepare-model"],
                             "Memverifikasi model suara")
                print("  [ok] Model suara siap", flush=True)
        elif args.action == "vision":
            ensure_vision(reporter)
        elif args.action == "vision-probe":
            from agent.tools.vision_local import ensure_vision_ready
            ready, why = ensure_vision_ready(force_probe=True)
            print(why, flush=True)
            return 0 if ready else 1
    except KeyboardInterrupt:
        print("\n  Dibatalkan. Komponen yang sudah tersedia tetap disimpan.", flush=True)
        return 130
    except (InstallerError, OSError, ImportError, ValueError) as exc:
        with reporter.log.open("a", encoding="utf-8") as log:
            log.write(str(exc) + "\n")
        print(f"  [x] {exc}\n      Log: {reporter.log}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

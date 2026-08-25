"""Tool profesional: find_todos, code_metrics, diagnose, bookmark, changelog.

Tool-tool yang menambah kesan elegan dan profesionalitas bagas-ai —
bukan operasi dasar, tapi sentuhan yang bikin agent terasa paham codebase."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .. import config
from .base import tool
from .search import _telusuri, _isi, _rel, _LEWATI, _EKS_BINER

ROOT = config.PROJECT_ROOT

# Berkas bookmark disimpan di folder data bagas-ai
_BOOKMARK_DIR = Path.home() / ".bagasai" / "bookmarks"


@tool
def find_todos(pattern: str = "", max_results: int = 80) -> str:
    """Cari TODO/FIXME/HACK/NOTE/XXX di seluruh proyek, dikelompokkan per file.
    Berguna untuk melihat utang teknis dan catatan pengembang.

    pattern: batasi ke berkas tertentu, mis. '*.py' (kosong = semua).
    max_results: batas jumlah temuan (default 80).
    """
    max_results = max(1, int(max_results or 80))
    _POLA = re.compile(
        r"\b(TODO|FIXME|HACK|NOTE|XXX|BUG|WARN|OPTIMIZE|REFACTOR)\b",
        re.IGNORECASE,
    )
    _LEWATI_DIR = _LEWATI | {"venv", ".venv", "node_modules"}

    def _lolos(p: Path) -> bool:
        if p.suffix.lower() in _EKS_BINER:
            return False
        for bagian in p.parts:
            if bagian in _LEWATI_DIR:
                return False
        if pattern.strip():
            import fnmatch
            if not fnmatch.fnmatch(p.name, pattern.strip()):
                return False
        return True

    temuan: list[tuple[str, list[tuple[int, str, str]]]] = []
    total = 0
    for p in _telusuri(ROOT):
        if not _lolos(p):
            continue
        teks = _isi(p)
        if teks is None:
            continue
        baris: list[tuple[int, str, str]] = []
        for i, line in enumerate(teks.split("\n"), 1):
            m = _POLA.search(line)
            if m:
                baris.append((i, m.group(1).upper(), line.strip()))
                total += 1
                if total >= max_results:
                    break
        if baris:
            temuan.append((_rel(p), baris))
        if total >= max_results:
            break

    if not temuan:
        return "tak ada TODO/FIXME/HACK/NOTE ditemukan."

    # Urutkan: file dengan FIXME/HACK lebih dulu
    def _prioritas(item):
        _, items = item
        tags = {tag for _, tag, _ in items}
        return (0 if tags & {"FIXME", "HACK", "BUG", "XXX"} else 1,
                item[0])
    temuan.sort(key=_prioritas)

    out = [f"{total} temuan di {len(temuan)} berkas:"]
    for rel, items in temuan:
        out.append(f"\n{rel}:")
        for i, tag, line in items:
            out.append(f"  {i:>5} [{tag}] {line[:120]}")
    return "\n".join(out)


@tool
def code_metrics(pattern: str = "") -> str:
    """Statistik proyek: jumlah file, baris, fungsi, class per bahasa/ekstensi.
    Memberi AI kesadaran skala proyek supaya respons-nya proporsional.

    pattern: batasi ke ekstensi tertentu, mis. '*.py' (kosong = semua).
    """
    _EK_LANG = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".jsx": "JSX", ".tsx": "TSX", ".rs": "Rust", ".go": "Go",
        ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
        ".php": "PHP", ".cs": "C#", ".cpp": "C++", ".c": "C",
        ".h": "C/C++ header", ".html": "HTML", ".css": "CSS",
        ".scss": "SCSS", ".vue": "Vue", ".svelte": "Svelte",
        ".md": "Markdown", ".json": "JSON", ".yaml": "YAML",
        ".toml": "TOML", ".sql": "SQL", ".sh": "Shell",
        ".ps1": "PowerShell", ".bat": "Batch",
    }
    _POLA_DEF = re.compile(
        r"^\s*(?:def |async def |class |function |const |let |var |"
        r"pub fn |pub async fn |fn |async fn |impl |trait |"
        r"public |private |protected |static )",
    )

    def _lolos(p: Path) -> bool:
        if p.suffix.lower() in _EKS_BINER:
            return False
        for bagian in p.parts:
            if bagian in _LEWATI:
                return False
        if pattern.strip():
            import fnmatch
            if not fnmatch.fnmatch(p.name, pattern.strip()):
                return False
        return True

    stats: dict[str, dict] = {}  # ext -> {files, lines, defs, chars}
    all_files: list[tuple[int, str]] = []
    for p in _telusuri(ROOT):
        if not _lolos(p):
            continue
        teks = _isi(p)
        if teks is None:
            continue
        ext = p.suffix.lower() or "(tanpa ext)"
        if ext not in stats:
            stats[ext] = {"files": 0, "lines": 0, "defs": 0, "chars": 0}
        baris = teks.split("\n")
        stats[ext]["files"] += 1
        stats[ext]["lines"] += len(baris)
        stats[ext]["chars"] += len(teks)
        stats[ext]["defs"] += sum(1 for b in baris if _POLA_DEF.search(b))
        all_files.append((len(baris), _rel(p)))

    if not stats:
        return "tak ada berkas ditemukan."

    total_files = sum(s["files"] for s in stats.values())
    total_lines = sum(s["lines"] for s in stats.values())
    total_defs = sum(s["defs"] for s in stats.values())

    # Urutkan per bahasa, file terbanyak dulu
    items = sorted(stats.items(), key=lambda x: x[1]["lines"], reverse=True)

    out = [f"{total_files} berkas, {total_lines:,} baris, {total_defs} def/class:\n"]
    out.append(f"{'Ekstensi':<12} {'Bahasa':<14} {'File':>6} {'Baris':>9} {'Def':>6}")
    out.append(f"{'─'*12} {'─'*14} {'─'*6} {'─'*9} {'─'*6}")
    for ext, s in items[:25]:
        lang = _EK_LANG.get(ext, "")
        out.append(f"{ext:<12} {lang:<14} {s['files']:>6} {s['lines']:>9,} {s['defs']:>6}")
    if len(items) > 25:
        out.append(f"  … {len(items) - 25} ekstensi lagi")

    # Top file terbesar — dikumpulkan pada lintasan statistik di atas;
    # membaca ulang seluruh proyek dua kali hanya untuk ini terlalu mahal.
    all_files.sort(reverse=True)
    out.append(f"\nBerkas terbesar:")
    for n, rel in all_files[:5]:
        out.append(f"  {n:>6,} baris  {rel}")
    return "\n".join(out)


@tool
def diagnose() -> str:
    """Cek kesehatan environment: Python versi, pip/venv, git status,
    disk space, dan dependensi. Satu panggilan = gambaran lengkap.

    Berguna di awal sesi atau saat sesi bermasalah untuk memastikan
    prasyarat terpenuhi.
    """
    import sys
    import shutil
    out: list[str] = []

    # Python
    out.append(f"Python: {sys.version}")
    out.append(f"Executable: {sys.executable}")

    # pip & venv
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    out.append(f"Virtual env: {'YA' if in_venv else 'TIDAK'} ({sys.prefix})")
    pip_path = shutil.which("pip") or shutil.which("pip3")
    out.append(f"pip: {pip_path or 'TIDAK DITEMUKAN'}")

    # Git
    git_path = shutil.which("git")
    out.append(f"git: {git_path or 'TIDAK DITEMUKAN'}")
    if git_path:
        try:
            r = subprocess.run(["git", "--version"], capture_output=True,
                               text=True, timeout=5)
            out.append(f"  versi: {r.stdout.strip()}")
        except Exception:
            pass
        try:
            r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                               cwd=str(ROOT), capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                out.append(f"  branch: {r.stdout.strip()}")
        except Exception:
            pass

    # Disk space
    try:
        import shutil as _shutil
        usage = _shutil.disk_usage(str(ROOT))
        total_gb = usage.total / (1024**3)
        free_gb = usage.free / (1024**3)
        used_pct = (usage.used / usage.total) * 100
        out.append(f"Disk: {free_gb:.1f} GB bebas dari {total_gb:.1f} GB "
                   f"({used_pct:.0f}% terpakai)")
    except Exception:
        pass

    # Project root
    out.append(f"Project root: {ROOT}")
    try:
        files = list(ROOT.rglob("*"))
        out.append(f"Jumlah berkas: {len(files)}")
    except Exception:
        pass

    # Dependency audit (best-effort)
    out.append("")
    # pip-audit
    if shutil.which("pip-audit"):
        try:
            r = subprocess.run(["pip-audit", "--format", "json"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                vulns = data.get("dependencies", [])
                n = sum(len(d.get("vulns", [])) for d in vulns)
                out.append(f"pip-audit: {n} kerentanan ditemukan" if n
                           else "pip-audit: bersih, tak ada kerentanan")
            else:
                out.append("pip-audit: tak bisa menjalankan")
        except Exception:
            out.append("pip-audit: error")
    else:
        out.append("pip-audit: tak terpasang (pip install pip-audit)")

    # npm audit
    if (ROOT / "package.json").exists() and shutil.which("npm"):
        try:
            r = subprocess.run(["npm", "audit", "--json"], cwd=str(ROOT),
                               capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout)
            meta = data.get("metadata", {})
            v = meta.get("vulnerabilities", {})
            total_v = sum(v.values()) if isinstance(v, dict) else v
            out.append(f"npm audit: {total_v} kerentanan" if total_v
                       else "npm audit: bersih")
        except Exception:
            out.append("npm audit: tak bisa menjalankan")

    return "\n".join(out)


@tool
def bookmark(action: str = "list", name: str = "", path: str = "", line: int = 0) -> str:
    """Simpan/restore posisi kode (file + baris) supaya bisa kembali tanpa re-read.

    action: \"save\" = simpan posisi, \"list\" = daftar bookmark,
        \"go\" = tampilkan posisi tersimpan, \"delete\" = hapus bookmark.
    name: nama bookmark (hanya untuk save/delete/go).
    path: file path (hanya untuk save).
    line: nomor baris 1-based (hanya untuk save).
    """
    _BOOKMARK_DIR.mkdir(parents=True, exist_ok=True)
    _BM_FILE = _BOOKMARK_DIR / "bookmarks.json"

    def _load() -> dict:
        if _BM_FILE.exists():
            try:
                return json.loads(_BM_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(data: dict) -> None:
        _BM_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    action = (action or "list").strip().lower()
    data = _load()
    # Key by project root so bookmarks don't leak across projects
    key = str(ROOT)
    project_bms = data.get(key, {})

    if action == "save":
        if not name.strip():
            return "[error] nama bookmark wajib diisi."
        if not path.strip():
            return "[error] path wajib diisi."
        nline = max(1, int(line or 1))
        project_bms[name.strip()] = {
            "path": path.strip(), "line": nline,
            "time": time.strftime("%Y-%m-%d %H:%M"),
        }
        data[key] = project_bms
        _save(data)
        return f"bookmark '{name.strip()}' disimpan: {path.strip()}:{nline}"

    if action == "list":
        if not project_bms:
            return "tak ada bookmark."
        out = [f"{len(project_bms)} bookmark:"]
        for n, bm in sorted(project_bms.items()):
            out.append(f"  {n}: {bm['path']}:{bm['line']} ({bm.get('time', '?')})")
        return "\n".join(out)

    if action == "go":
        if not name.strip():
            return "[error] nama bookmark wajib diisi."
        bm = project_bms.get(name.strip())
        if not bm:
            return f"bookmark '{name.strip()}' tak ditemukan."
        return f"{bm['path']}:{bm['line']} (disimpan {bm.get('time', '?')})"

    if action == "delete":
        if not name.strip():
            return "[error] nama bookmark wajib diisi."
        if name.strip() not in project_bms:
            return f"bookmark '{name.strip()}' tak ditemukan."
        del project_bms[name.strip()]
        data[key] = project_bms
        _save(data)
        return f"bookmark '{name.strip()}' dihapus."

    return (f"aksi '{action}' tak dikenal. Yang tersedia: "
            "save, list, go, delete.")


@tool
def changelog(since_tag: str = "", limit: int = 50) -> str:
    """Generate changelog dari git log sejak tag terakhir (atau sejak commit N).
    Profesional saat release — menunjukkan apa saja yang berubah.

    since_tag: tag awal (kosong = deteksi tag terakhir secara otomatis).
    limit: maks commit yang dicantumkan (default 50).
    """
    from .git_tool import _git

    # Deteksi tag terakhir bila tak diberi
    if not since_tag.strip():
        ok, tags = _git("tag", "--sort=-version:refname")
        if ok and tags.strip():
            since_tag = tags.strip().splitlines()[0]
        else:
            since_tag = ""

    n = max(1, min(int(limit or 50), 200))
    args = ["log", f"-{n}", "--no-color", "--date=short",
            "--pretty=format:%h  %ad  %an  %s"]
    if since_tag.strip():
        # -{n} ikut dibawa: dulunya limit hanya terpakai saat tanpa tag.
        args = ["log", f"{since_tag.strip()}..HEAD", f"-{n}", "--no-color",
                "--date=short", "--pretty=format:%h  %ad  %an  %s"]

    ok, out = _git(*args)
    if not ok:
        return out
    if not out.strip():
        if since_tag:
            return f"tak ada commit sejak tag {since_tag}."
        return "tak ada commit."

    # Kelompokkan per tanggal
    baris = out.strip().splitlines()
    per_tanggal: dict[str, list[str]] = {}
    for b in baris:
        # format: hash  date  author  subject. Pecah pada DUA spasi, bukan
        # sembarang whitespace: nama penulis bisa memuat spasi tunggal, dan
        # split(None) akan memotong namanya jadi dua bagian.
        bagian = b.split("  ", 3)
        if len(bagian) >= 4:
            tgl = bagian[1]
            entri = f"  {bagian[0]}  {bagian[3]}  ({bagian[2]})"
        else:
            tgl = "?"
            entri = f"  {b}"
        per_tanggal.setdefault(tgl, []).append(entri)

    header = f"Changelog" + (f" sejak {since_tag}" if since_tag else "")
    result = [header, "=" * len(header), ""]
    for tgl, entris in sorted(per_tanggal.items(), reverse=True):
        result.append(f"{tgl}")
        result.extend(entris)
        result.append("")
    return "\n".join(result)

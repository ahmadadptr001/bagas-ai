"""Peta/indeks proyek — ringkasan STRUKTUR + simbol kunci tiap file kode, dibuat
sekali lalu DI-CACHE per-proyek dan selalu disisipkan ke system prompt.

Tujuan: bagas-ai langsung "paham" proyek di SETIAP giliran, ganti model, dan
`--resume`, TANPA harus membaca ulang seluruh file tiap kali. Peta ini ringkas
(hanya tanda tangan fungsi/kelas/ekspor, bukan seluruh isi kode), jadi muat di
konteks meski proyeknya besar. bagas-ai cukup membaca file tertentu HANYA saat
butuh detail implementasinya.

Cache disimpan di ~/.bagasai/project_index/<hash-root>.md dan dibangun ulang
otomatis bila proyek berubah (jumlah file / mtime terbaru berubah), atau lewat
perintah /scan.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import config
from .workspace import _IGNORE

# Ekstensi file kode yang dipetakan simbolnya.
_CODE_EXT = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".swift", ".vue", ".svelte",
}
# File konfigurasi/penting yang cukup DICATAT keberadaannya (tanpa simbol).
_NOTABLE = {
    "package.json", "requirements.txt", "pyproject.toml", "go.mod", "cargo.toml",
    "dockerfile", "docker-compose.yml", "makefile", "tsconfig.json",
    "vite.config.js", "vite.config.ts", "next.config.js", "readme.md", ".env.example",
}

_MAX_FILES = 300
_MAX_SYMS = 14
_MAX_CHARS = 16000
_MAX_READ = 240_000  # jangan baca file raksasa

# Pola tanda tangan per bahasa (baris yang mendefinisikan simbol publik).
_PY = re.compile(r"^\s*(?:async\s+def|def|class)\s+\w+.*?:", re.MULTILINE)
_JS = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+\s*\([^)]*\)"
    r"|^\s*(?:export\s+)?(?:abstract\s+)?class\s+\w+.*"
    r"|^\s*export\s+(?:const|let|default|function|class)\b.*"
    r"|^\s*(?:export\s+)?(?:const|let)\s+\w+\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
    re.MULTILINE,
)
_GENERIC = re.compile(
    r"^\s*(?:pub\s+)?(?:public|private|protected|static|final|func|fn|def|class|"
    r"struct|impl|interface|enum|type)\b.*",
    re.MULTILINE,
)


def _store_dir() -> Path:
    d = config.CONFIG_HOME / "project_index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(root: Path) -> str:
    return hashlib.md5(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _paths(root: Path) -> tuple[Path, Path]:
    h = _hash(root)
    return _store_dir() / f"{h}.md", _store_dir() / f"{h}.meta.json"


def _iter_files(root: Path):
    """Semua file kode/penting di proyek (melewati folder yang diabaikan)."""
    for p in sorted(root.rglob("*")):
        parts = set(p.parts)
        if parts & _IGNORE or any(x.startswith(".") and x not in (".env.example",)
                                  for x in p.relative_to(root).parts[:-1]):
            continue
        if not p.is_file():
            continue
        name = p.name.lower()
        if p.suffix.lower() in _CODE_EXT or name in _NOTABLE:
            yield p


def _signature(root: Path) -> dict:
    """Sidik jari ringan proyek (jumlah file + mtime terbaru) untuk deteksi basi."""
    count = 0
    newest = 0.0
    try:
        for p in _iter_files(root):
            count += 1
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
            if count > _MAX_FILES * 2:
                break
    except Exception:
        pass
    return {"count": count, "newest": round(newest, 2)}


def _symbols(path: Path, limit: int = _MAX_SYMS) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    if len(text) > _MAX_READ:
        text = text[:_MAX_READ]
    ext = path.suffix.lower()
    if ext in (".py", ".pyw"):
        pat = _PY
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"):
        pat = _JS
    else:
        pat = _GENERIC
    out: list[str] = []
    seen: set[str] = set()
    for m in pat.finditer(text):
        s = m.group(0).strip().rstrip("{").strip()
        s = re.sub(r"\s+", " ", s)[:120]
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def count_files(root: Path | None = None) -> int:
    """Jumlah file yang akan dipetakan (cepat, hanya jalan-jalan nama) — dipakai
    UI untuk menetapkan TOTAL bar progres agar DETERMINATE (bukan pulsing)."""
    root = Path(root or config.PROJECT_ROOT).resolve()
    n = 0
    for _ in _iter_files(root):
        n += 1
        if n >= _MAX_FILES:
            break
    return n


def build(root: Path | None = None, progress=None) -> str:
    """Bangun teks peta proyek (markdown ringkas).

    `progress(done, total)` dipanggil tiap file diproses -> UI bisa menampilkan
    bar realtime sesuai berapa banyak data (file) yang sudah dibaca.
    """
    root = Path(root or config.PROJECT_ROOT).resolve()
    files = list(_iter_files(root))          # kumpulkan dulu agar tahu TOTAL
    total = min(len(files), _MAX_FILES)
    lines = [f"Peta proyek `{root.name}` (struktur + simbol kunci; "
             f"baca file utuh HANYA bila butuh detail):", ""]
    for i, p in enumerate(files):
        if i >= _MAX_FILES:
            lines.append(f"… (>{_MAX_FILES} file, sisanya dipotong)")
            break
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() in _CODE_EXT:
            syms = _symbols(p)
            lines.append(f"- {rel}")
            for s in syms:
                lines.append(f"    · {s}")
        else:
            lines.append(f"- {rel}")
        if progress:
            try:
                progress(i + 1, total)
            except Exception:
                pass
    text = "\n".join(lines)
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n… (peta dipotong agar hemat konteks)"
    return text


# Memo singkat: ensure() dipanggil BERUNTUN saat startup (main() lalu Agent() ->
# system prompt). Tanpa memo, tiap panggilan memindai ulang SELURUH file proyek
# hanya untuk signature. TTL pendek agar perubahan file tetap cepat terdeteksi.
_MEMO = {"root": "", "text": "", "ts": 0.0}
_MEMO_TTL = 5.0


def ensure(root: Path | None = None, force: bool = False, progress=None) -> str:
    """Kembalikan peta proyek dari cache; bangun ulang bila belum ada / basi /force.

    `progress(done, total)` hanya dipanggil bila memang MEMBANGUN ulang (membaca
    file); bila memakai cache, tak ada progres (instan)."""
    root = Path(root or config.PROJECT_ROOT).resolve()
    now = time.time()
    if (not force and _MEMO["text"] and _MEMO["root"] == str(root)
            and now - _MEMO["ts"] < _MEMO_TTL):
        return _MEMO["text"]
    md_path, meta_path = _paths(root)
    sig = _signature(root)
    text = ""
    if not force and md_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("sig") == sig:
                text = md_path.read_text(encoding="utf-8")
        except Exception:
            text = ""
    if not text:
        text = build(root, progress=progress)
        try:
            md_path.write_text(text, encoding="utf-8")
            meta_path.write_text(json.dumps({"sig": sig}, ensure_ascii=False),
                                 encoding="utf-8")
        except OSError:
            pass
    _MEMO.update(root=str(root), text=text, ts=now)
    return text


def invalidate() -> None:
    """Paksa ensure() berikutnya memeriksa ulang disk (abaikan memo singkat).
    Dipanggil setelah AI menulis/menghapus file agar peta tak pernah basi."""
    _MEMO["ts"] = 0.0


def as_prompt_block(root: Path | None = None) -> str:
    """Peta proyek untuk system prompt (dibangun/di-cache otomatis)."""
    try:
        return ensure(root)
    except Exception:
        return ""

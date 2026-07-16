"""Folder konteks tambahan (fitur `add-dir`).

Selain root project (folder tempat `bagasai` dipanggil), pengguna bisa menambah
folder lain sebagai konteks. bagasAI lalu:
  - BOLEH membaca/menulis file di folder itu (tool file mengizinkannya), dan
  - OTOMATIS MEMAHAMI isinya (struktur folder disisipkan ke system prompt).

Daftar folder disimpan persisten, dipisah per root project, di
~/.bagasai/context_dirs.json — jadi bertahan lintas sesi & `--resume`.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

_STORE = config.CONFIG_HOME / "context_dirs.json"

# Folder yang dilewati saat membuat ringkasan struktur (biar bersih & hemat token).
_IGNORE = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".cache", "target", ".DS_Store",
}


def _load() -> dict:
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _STORE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _key() -> str:
    return str(config.PROJECT_ROOT.resolve())


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = config.PROJECT_ROOT / p
    return p.resolve()


def list_dirs() -> list[Path]:
    """Folder konteks aktif untuk project ini (yang masih ada di disk)."""
    out: list[Path] = []
    for s in _load().get(_key(), []):
        p = Path(s)
        if p.is_dir():
            out.append(p)
    return out


def add(path: str) -> Path:
    """Tambah folder konteks. Lempar ValueError bila folder tak valid."""
    p = _resolve(path)
    if not p.exists():
        raise ValueError(f"Folder tidak ditemukan: {p}")
    if not p.is_dir():
        raise ValueError(f"Bukan sebuah folder: {p}")
    data = _load()
    key = _key()
    lst = data.get(key, [])
    if str(p) not in lst:
        lst.append(str(p))
    data[key] = lst
    _save(data)
    return p


def remove(path: str) -> bool:
    """Hapus folder dari daftar konteks. True bila memang ada & terhapus."""
    p = _resolve(path)
    data = _load()
    key = _key()
    lst = data.get(key, [])
    if str(p) in lst:
        lst.remove(str(p))
        data[key] = lst
        _save(data)
        return True
    return False


def allowed_roots() -> list[Path]:
    """Root yang boleh diakses tool file: root project + semua folder konteks."""
    roots = [config.PROJECT_ROOT.resolve()]
    for d in list_dirs():
        r = d.resolve()
        if r not in roots:
            roots.append(r)
    return roots


def tree(root: Path, max_depth: int = 2, max_entries: int = 60) -> str:
    """Ringkasan struktur folder (dangkal & dibatasi) untuk konteks system prompt."""
    lines: list[str] = []
    count = [0]

    def walk(d: Path, depth: int, prefix: str) -> None:
        if depth > max_depth or count[0] >= max_entries:
            return
        try:
            entries = sorted(
                d.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return
        for p in entries:
            if count[0] >= max_entries:
                lines.append(prefix + "… (dipotong)")
                return
            if p.name in _IGNORE or p.name.startswith("."):
                continue
            count[0] += 1
            if p.is_dir():
                lines.append(f"{prefix}{p.name}/")
                walk(p, depth + 1, prefix + "  ")
            else:
                lines.append(f"{prefix}{p.name}")

    walk(root, 1, "")
    return "\n".join(lines) if lines else "(kosong)"


def as_prompt_block() -> str:
    """Blok teks berisi tiap folder konteks + struktur ringkasnya (untuk prompt)."""
    dirs = list_dirs()
    if not dirs:
        return ""
    return "\n\n".join(f"### {d}\n{tree(d)}" for d in dirs)

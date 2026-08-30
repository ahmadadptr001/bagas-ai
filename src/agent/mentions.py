"""Resolusi mention ``@path`` menjadi konteks teks yang aman untuk model."""
from __future__ import annotations

import re
from pathlib import Path

from . import workspace

_MENTION = re.compile(r"(?<![\w@])@(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))")
_MAX_CHARS = 20_000


def mention_candidates(prefix: str = "", limit: int = 40) -> list[tuple[str, str]]:
    """Cari file/folder yang aman untuk dropdown mention UI."""
    q = (prefix or "").lstrip("@").lower()
    out: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for root in [Path.cwd(), *workspace.allowed_roots()]:
        root = root.resolve()
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        try:
            items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            continue
        for item in items:
            if item.name.startswith((".", "__pycache__")):
                continue
            rel = str(item.relative_to(root)).replace("\\", "/")
            if q and q not in rel.lower():
                continue
            out.append((rel + ("/" if item.is_dir() else ""), str(item)))
            if len(out) >= limit:
                return out
    return out


def expand_mentions(text: str) -> tuple[str, list[str]]:
    """Sisipkan isi berkas yang dirujuk ``@path``; mention tak dikenal dibiarkan."""
    found: list[str] = []
    blocks: list[str] = []
    roots = [r.resolve() for r in workspace.allowed_roots()]
    for m in _MENTION.finditer(text or ""):
        raw = next((x for x in m.groups() if x), "").rstrip(",.;:!?)]}")
        if not raw or raw.startswith(("http://", "https://")):
            continue
        p = Path(raw).expanduser()
        candidates = ([p] if p.is_absolute() else
                      [Path.cwd() / p, *[root / p for root in roots]])
        target = next((c.resolve() for c in candidates if c.is_file() or c.is_dir()),
                      (candidates[0].resolve() if candidates else p.resolve()))
        if not target.is_file() and not target.is_dir():
            continue
        if not any(target == root or root in target.parents for root in roots):
            continue
        if target.is_dir():
            items = sorted(x for x in target.rglob("*") if x.is_file())[:200]
            content = "\n".join(str(x) for x in items) or "(folder kosong)"
        else:
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        truncated = len(content) > _MAX_CHARS
        if truncated:
            content = content[:_MAX_CHARS] + "\n… [mention dipotong]"
        found.append(str(target))
        blocks.append(f"\n\n[MENTION: {target}]\n```text\n{content}\n```\n")
    return (text + "".join(blocks), found) if blocks else (text, [])

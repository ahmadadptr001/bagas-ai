"""Penyimpanan sesi percakapan, dipisah per folder project.

Setiap folder project (cwd tempat `bagasai` dipanggil) punya daftar sesinya
sendiri di ~/.bagasai/sessions/<hash-folder>/<session-id>.json. Ini yang
membuat `bagasai --resume` bisa melanjutkan percakapan terakhir di folder itu.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from . import config


def _project_key(project_root: Path | None = None) -> str:
    root = str(project_root or config.PROJECT_ROOT)
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:12]
    # sertakan nama folder agar mudah dikenali manusia
    name = (project_root or config.PROJECT_ROOT).name or "root"
    return f"{name}-{digest}"


def _project_dir(project_root: Path | None = None) -> Path:
    d = config.SESSIONS_DIR / _project_key(project_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


class Session:
    """Satu sesi percakapan yang bisa disimpan & dimuat."""

    def __init__(
        self,
        session_id: str,
        project_root: str,
        messages: list[dict[str, Any]] | None = None,
        created: float | None = None,
        tokens: dict[str, int] | None = None,
        web_chats: dict[str, str] | None = None,
    ) -> None:
        self.id = session_id
        self.project_root = project_root
        self.messages = messages or []
        self.created = created or time.time()
        self.updated = time.time()
        # Kaitan ke percakapan di AI web: {service: chat_id}. Dipakai agar
        # `--resume` menyambung ke chat yang SAMA di situs — konteks proyek &
        # protokol tool sudah ada di sana, jadi tak perlu dikirim ulang.
        self.web_chats: dict[str, str] = dict(web_chats or {})
        # Token kumulatif SESI ini (persisten lintas --resume).
        self.tokens = {"prompt": 0, "completion": 0}
        if tokens:
            self.tokens["prompt"] = int(tokens.get("prompt", 0) or 0)
            self.tokens["completion"] = int(tokens.get("completion", 0) or 0)

    @property
    def path(self) -> Path:
        return _project_dir(Path(self.project_root)) / f"{self.id}.json"

    def save(
        self,
        messages: list[dict[str, Any]],
        tokens: dict[str, int] | None = None,
    ) -> None:
        self.messages = messages
        self.updated = time.time()
        if tokens is not None:
            self.tokens = {
                "prompt": int(tokens.get("prompt", 0) or 0),
                "completion": int(tokens.get("completion", 0) or 0),
            }
        data = {
            "id": self.id,
            "project_root": self.project_root,
            "created": self.created,
            "updated": self.updated,
            "tokens": self.tokens,
            "web_chats": self.web_chats,
            "messages": messages,
        }
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # --- factory ---
    @classmethod
    def create(cls, project_root: Path | None = None) -> "Session":
        root = str(project_root or config.PROJECT_ROOT)
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        return cls(sid, root)

    @classmethod
    def load(cls, path: Path) -> "Session":
        data = json.loads(path.read_text(encoding="utf-8"))
        s = cls(
            data["id"],
            data["project_root"],
            data.get("messages", []),
            data.get("created"),
            data.get("tokens"),
            data.get("web_chats"),
        )
        s.updated = data.get("updated", s.created)
        return s


def latest(project_root: Path | None = None) -> Session | None:
    """Sesi terakhir (paling baru diperbarui) untuk folder ini, atau None."""
    d = _project_dir(project_root)
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return Session.load(files[0]) if files else None


def list_sessions(project_root: Path | None = None) -> list[Session]:
    d = _project_dir(project_root)
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [Session.load(f) for f in files]


def delete(sess: Session) -> bool:
    """Hapus file sesi. Kembalikan True bila berhasil."""
    try:
        sess.path.unlink()
        return True
    except OSError:
        return False


def user_msg_count(sess: Session) -> int:
    return len([m for m in sess.messages if m.get("role") == "user"])

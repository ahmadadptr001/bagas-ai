"""Normalisasi path dari drop terminal Windows, kutip, dan file URI."""
from __future__ import annotations

from pathlib import Path
import shlex
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from .ascii_art import is_image_path, is_video_path


def media_paths(text: str) -> list[str]:
    def resolve(value):
        value = value.strip().strip('"').strip("'")
        if value.lower().startswith("file:"):
            url = urlsplit(value)
            value = url2pathname(unquote(url.path))
            if url.netloc and url.netloc != "localhost":
                value = "//" + url.netloc + value
        if is_image_path(value) or is_video_path(value):
            return str(Path(value).resolve())
        return None

    try:
        single = resolve(text)
        if single:
            return [single]
        parts = shlex.split(text.strip(), posix=False)
        if parts and parts[0] == "&":
            parts.pop(0)
        paths = [resolve(part) for part in parts]
        return list(dict.fromkeys(paths)) if paths and all(paths) else []
    except (OSError, ValueError):
        return []

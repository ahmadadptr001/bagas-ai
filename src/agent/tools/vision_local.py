"""Backend vision lokal ringan berbasis Ollama (Gemma 3n E2B)."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path


MODEL_DEFAULT = "gemma3n:e2b"


def describe_image(path: Path, prompt: str = "Jelaskan isi gambar secara faktual. Baca teks yang terlihat dan sebutkan elemen UI/objek penting.") -> str:
    """Minta caption semantik ke Ollama; gagal dengan aman bila belum tersedia."""
    try:
        import requests
        from PIL import Image
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((768, 768))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=78, optimize=True)
        payload = {
            "model": os.environ.get("BAGASAI_VISION_MODEL", MODEL_DEFAULT),
            "prompt": prompt,
            "images": [base64.b64encode(buf.getvalue()).decode("ascii")],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 350},
        }
        res = requests.post(
            os.environ.get("BAGASAI_OLLAMA_URL", "http://127.0.0.1:11434/api/generate"),
            json=payload, timeout=90,
        )
        if res.status_code != 200:
            return ""
        text = str(res.json().get("response", "")).strip()
        return text[:6000]
    except Exception:
        return ""


__all__ = ["MODEL_DEFAULT", "describe_image"]

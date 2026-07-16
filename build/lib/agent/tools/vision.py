"""Tool multimodal: analisis gambar via model VLM NVIDIA.

Ini tidak didaftarkan sebagai fungsi tool-calling biasa, melainkan dipakai
langsung oleh antarmuka (mis. saat pengguna mengirim foto). Model VLM dipanggil
lewat format `image_url` yang kompatibel OpenAI.
"""
from __future__ import annotations

import base64
from pathlib import Path

from .. import config, llm


def _encode_image(path: str | Path) -> str:
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    suffix = Path(path).suffix.lower().lstrip(".") or "jpeg"
    if suffix == "jpg":
        suffix = "jpeg"
    return f"data:image/{suffix};base64,{b64}"


def analyze_image(
    image_path: str | Path,
    prompt: str = "Deskripsikan gambar ini secara detail.",
    *,
    model: str | None = None,
) -> str:
    """Kirim gambar lokal + pertanyaan ke model vision NVIDIA, kembalikan teks."""
    data_url = _encode_image(image_path)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    response = llm.chat_completion(
        messages, model=model or config.VISION_MODEL
    )
    return response.choices[0].message.content or ""

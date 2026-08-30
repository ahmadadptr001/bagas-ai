"""Backend vision lokal wajib berbasis Ollama dan Gemma multimodal."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import shutil
import subprocess
import time


# gemma3n:e2b di registry resmi Ollama hanya menerima teks. Gemma 3 4B adalah
# varian Gemma terkecil yang metadata resminya memuat input Text + Image.
MODEL_DEFAULT = "gemma3:4b"
_MODEL_TERVERIFIKASI = False


class VisionLocalError(RuntimeError):
    """Ollama/Gemma tidak siap atau gagal memproses gambar."""


def _base_url() -> str:
    url = os.environ.get(
        "BAGASAI_OLLAMA_URL", "http://127.0.0.1:11434/api/generate"
    )
    return url.split("/api/", 1)[0].rstrip("/")


def _generate_url() -> str:
    return _base_url() + "/api/generate"


def _server_hidup(timeout: float = 2.0) -> bool:
    try:
        import requests
        return requests.get(
            _base_url() + "/api/tags", timeout=timeout
        ).status_code == 200
    except Exception:
        return False


def _mulai_ollama() -> tuple[bool, str]:
    """Pastikan server Ollama hidup; mulai proses lokal bila perlu."""
    if _server_hidup():
        return True, "Ollama aktif"
    if os.environ.get("BAGASAI_OLLAMA_URL"):
        return False, f"server Ollama tidak merespons di {_base_url()}"
    ollama = shutil.which("ollama")
    if not ollama:
        return False, "executable Ollama tidak ditemukan"
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen(
            [ollama, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        return False, f"Ollama gagal dijalankan: {exc}"
    batas = time.monotonic() + 25
    while time.monotonic() < batas:
        if _server_hidup():
            return True, "Ollama aktif"
        time.sleep(0.5)
    return False, "Ollama sudah dijalankan tetapi server tidak merespons"


def _nama_model() -> str:
    return (
        os.environ.get("BAGASAI_VISION_MODEL", MODEL_DEFAULT).strip()
        or MODEL_DEFAULT
    )


def _model_ada() -> bool:
    try:
        import requests
        res = requests.get(_base_url() + "/api/tags", timeout=5)
        if res.status_code != 200:
            return False
        model = _nama_model().lower()
        return any(
            str(item.get("name", "")).lower() == model
            or str(item.get("model", "")).lower() == model
            for item in res.json().get("models", [])
        )
    except Exception:
        return False


def _gambar_jpeg(path: Path | None = None) -> str:
    from PIL import Image
    if path is None:
        # Probe benar-benar multimodal, bukan sekadar prompt teks.
        img = Image.new("RGB", (32, 32), (35, 120, 210))
    else:
        with Image.open(path) as sumber:
            img = sumber.convert("RGB")
            img.thumbnail((768, 768))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=78, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _inferensi(
    gambar: str, prompt: str, *, timeout: float, num_predict: int
) -> str:
    import requests
    payload = {
        "model": _nama_model(),
        "prompt": prompt,
        "images": [gambar],
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.1,
            "num_predict": num_predict,
            # Konteks kecil mengurangi tekanan RAM pada mesin 4 GB.
            "num_ctx": 2048,
        },
    }
    try:
        res = requests.post(_generate_url(), json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise VisionLocalError(f"Ollama tidak merespons: {exc}") from exc
    if res.status_code != 200:
        try:
            detail = str(res.json().get("error", ""))
        except Exception:
            detail = res.text
        raise VisionLocalError(
            f"Gemma menolak gambar (HTTP {res.status_code}): {detail[:300]}"
        )
    try:
        data = res.json()
    except ValueError as exc:
        raise VisionLocalError("Ollama mengembalikan respons bukan JSON") from exc
    teks = str(data.get("response", "")).strip()
    if not teks:
        raise VisionLocalError("Gemma selesai tanpa menghasilkan jawaban")
    return teks[:6000]


def ensure_vision_ready(*, force_probe: bool = False) -> tuple[bool, str]:
    """Nyalakan Ollama dan buktikan Gemma dapat menjawab gambar nyata."""
    global _MODEL_TERVERIFIKASI
    hidup, alasan = _mulai_ollama()
    if not hidup:
        return False, alasan
    if not _model_ada():
        return False, f"model {_nama_model()} belum terpasang"
    if _MODEL_TERVERIFIKASI and not force_probe:
        return True, f"Ollama + {_nama_model()} aktif"
    try:
        jawaban = _inferensi(
            _gambar_jpeg(),
            "Ini tes vision. Jawab singkat warna utama gambar ini.",
            timeout=240,
            num_predict=20,
        )
    except VisionLocalError as exc:
        _MODEL_TERVERIFIKASI = False
        return False, str(exc)
    _MODEL_TERVERIFIKASI = True
    return True, f"Ollama + {_nama_model()} merespons gambar: {jawaban[:80]}"


def ensure_vision_available() -> tuple[bool, str]:
    """Pastikan backend fallback tersedia tanpa menjalankan inferensi gambar."""
    hidup, alasan = _mulai_ollama()
    if not hidup:
        return False, alasan
    if not _model_ada():
        return False, f"model {_nama_model()} belum terpasang"
    return True, f"Ollama + {_nama_model()} tersedia"


def response_needs_vision(text: str) -> bool:
    """Deteksi jawaban AI yang menyatakan tak mampu membaca gambar."""
    jawaban = " ".join(str(text or "").casefold().split())
    if not jawaban:
        return True
    penolakan = (
        "tidak dapat menganalisis gambar",
        "tidak bisa menganalisis gambar",
        "tidak dapat melihat gambar",
        "tidak bisa melihat gambar",
        "tidak dapat membaca gambar",
        "tidak bisa membaca gambar",
        "tidak memiliki akses ke gambar",
        "tidak dapat mengakses gambar",
        "tidak ada gambar yang",
        "cannot analyze the image",
        "can't analyze the image",
        "unable to analyze the image",
        "cannot see the image",
        "can't see the image",
        "unable to view the image",
        "cannot access the image",
        "don't have access to the image",
        "no image was provided",
    )
    if any(frasa in jawaban for frasa in penolakan):
        return True
    ada_gambar = any(k in jawaban for k in (
        "gambar", "image", "screenshot", "visual",
    ))
    ada_tidak_mampu = any(k in jawaban for k in (
        "tidak dapat", "tidak bisa", "tak dapat", "tak bisa",
        "cannot", "can't", "unable", "don't have access",
    ))
    ada_aksi = any(k in jawaban for k in (
        "lihat", "melihat", "baca", "membaca", "analisis", "menganalisis",
        "see", "view", "read", "analy",
    ))
    return ada_gambar and ada_tidak_mampu and ada_aksi


def describe_image(
    path: Path,
    prompt: str = (
        "Jelaskan isi gambar secara faktual. Baca teks yang terlihat dan "
        "sebutkan elemen UI/objek penting."
    ),
    *,
    strict: bool = False,
) -> str:
    """Analisis gambar dengan Gemma; mode strict tidak boleh fallback diam-diam."""
    hidup, alasan = _mulai_ollama()
    if not hidup:
        if strict:
            raise VisionLocalError(alasan)
        return ""
    try:
        return _inferensi(
            _gambar_jpeg(path), prompt, timeout=240, num_predict=350
        )
    except (VisionLocalError, OSError) as exc:
        if strict:
            if isinstance(exc, VisionLocalError):
                raise
            raise VisionLocalError(str(exc)) from exc
        return ""


__all__ = [
    "MODEL_DEFAULT",
    "VisionLocalError",
    "describe_image",
    "ensure_vision_available",
    "ensure_vision_ready",
    "response_needs_vision",
]

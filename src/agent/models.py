"""Daftar model NVIDIA yang tersedia + util untuk memilih model.

Semua model DI-HOST NVIDIA (integrate.api.nvidia.com), bukan lokal. Setiap
model di daftar ini sudah diverifikasi ADA di katalog `/v1/models` untuk key
ini dan mendukung alur kerja bagasAI (chat + tool-calling). Dipakai oleh
perintah /model & /effort di CLI dan oleh Agent.

Catatan penting: tiap keluarga model punya CARA mengaktifkan mode "thinking"
yang berbeda, jadi ModelSpec menyimpan `reasoning_style`:
  - "nemotron": kirim chat_template_kwargs.enable_thinking + reasoning_budget
                (keluarga NVIDIA Nemotron).
  - "gpt_oss" : kirim reasoning_effort = low/medium/high (OpenAI gpt-oss).
  - None      : model biasa / yang bernalar sendiri tanpa parameter khusus.
"""
from __future__ import annotations

from dataclasses import dataclass


# --- Level effort per gaya reasoning ---
# Nemotron: nama -> reasoning_budget (token). 0 = thinking dimatikan.
NEMOTRON_EFFORT: dict[str, int] = {
    "langsung": 0,
    "ringkas": 4096,
    "seimbang": 16384,
    "mendalam": 32768,
}
# gpt-oss: nama -> nilai reasoning_effort resmi OpenAI (low/medium/high).
GPTOSS_EFFORT: dict[str, str] = {
    "ringkas": "low",
    "seimbang": "medium",
    "mendalam": "high",
}

# Kosakata & penjelasan tiap level (untuk menu /effort) — (judul, deskripsi, ikon).
EFFORT_INFO: dict[str, tuple[str, str, str]] = {
    "langsung": ("Langsung", "tanpa mode berpikir — jawaban paling cepat", "⚡"),
    "ringkas": ("Ringkas", "berpikir singkat — gesit & hemat token", "🌤"),
    "seimbang": ("Seimbang", "nalar secukupnya — pas untuk kebanyakan tugas", "⚖"),
    "mendalam": ("Mendalam", "berpikir penuh — untuk soal kompleks (lebih lambat)", "🔬"),
}


@dataclass(frozen=True)
class ModelSpec:
    id: str  # ID model persis untuk dikirim ke API NVIDIA
    label: str  # nama tampilan
    multimodal: bool = False  # bisa memproses gambar/video
    reasoning_style: str | None = None  # None | "nemotron" | "gpt_oss"
    note: str = ""  # keterangan singkat (kecepatan/keunggulan)

    @property
    def reasoning(self) -> bool:
        """True bila model punya mode thinking yang bisa diatur via /effort."""
        return self.reasoning_style is not None

    def supports_effort(self) -> bool:
        return self.reasoning_style is not None

    def effort_options(self) -> dict[str, int | str]:
        if self.reasoning_style == "nemotron":
            return dict(NEMOTRON_EFFORT)
        if self.reasoning_style == "gpt_oss":
            return dict(GPTOSS_EFFORT)
        return {}

    def default_effort(self) -> str | None:
        if self.reasoning_style in ("nemotron", "gpt_oss"):
            return "seimbang"
        return None

    def effort_info(self) -> list[tuple[str, str, str, str]]:
        """Daftar (kunci, judul, deskripsi, ikon) untuk menu /effort — terurut."""
        out = []
        for key in self.effort_options():
            title, desc, icon = EFFORT_INFO.get(key, (key.capitalize(), "", "•"))
            out.append((key, title, desc, icon))
        return out

    def extra_body_for(self, effort: str | None) -> dict | None:
        """Parameter tambahan sesuai gaya reasoning & mode terpilih."""
        if self.reasoning_style == "nemotron":
            opts = NEMOTRON_EFFORT
            budget = opts.get(effort or "seimbang", 16384)
            if budget <= 0:
                return {"chat_template_kwargs": {"enable_thinking": False}}
            return {
                "chat_template_kwargs": {"enable_thinking": True},
                "reasoning_budget": budget,
            }
        if self.reasoning_style == "gpt_oss":
            level = GPTOSS_EFFORT.get(effort or "seimbang", "medium")
            return {"reasoning_effort": level}
        return None


# Alias pendek -> spesifikasi model. Urutan menentukan nomor pada /model.
# Semua entri di bawah sudah diuji: ADA di katalog & bisa tool-calling.
MODELS: dict[str, ModelSpec] = {
    # --- Serba-guna (default & cepat) ---
    "deepseek": ModelSpec(
        id="deepseek-ai/deepseek-v4-pro",
        label="DeepSeek-V4 Pro",
        note="Pilihan utama: paling pintar & serba-guna — tugas kompleks & coding berat",
    ),
    "deepseek-flash": ModelSpec(
        id="deepseek-ai/deepseek-v4-flash",
        label="DeepSeek-V4 Flash",
        note="Cepat & hemat — coding cepat & agent yang banyak iterasi",
    ),
    "llama33": ModelSpec(
        id="meta/llama-3.3-70b-instruct",
        label="Meta Llama-3.3 70B",
        note="Andal & seimbang — tugas umum sehari-hari",
    ),
    "llama4": ModelSpec(
        id="meta/llama-4-maverick-17b-128e-instruct",
        label="Meta Llama-4 Maverick",
        multimodal=True,
        note="Multimodal (teks+gambar) — analisis campuran teks & gambar",
    ),
    "llama31-70b": ModelSpec(
        id="meta/llama-3.1-70b-instruct",
        label="Meta Llama-3.1 70B",
        note="Stabil & matang — tugas umum",
    ),
    "llama31-8b": ModelSpec(
        id="meta/llama-3.1-8b-instruct",
        label="Meta Llama-3.1 8B",
        note="Sangat cepat & ringan — tanya-jawab singkat, hemat kuota",
    ),
    # --- Mistral ---
    "mistral-large": ModelSpec(
        id="mistralai/mistral-large-3-675b-instruct-2512",
        label="Mistral-Large-3 675B",
        note="Sangat pintar — penalaran mendalam & penulisan panjang",
    ),
    "mistral-medium": ModelSpec(
        id="mistralai/mistral-medium-3.5-128b",
        label="Mistral-Medium-3.5",
        note="Seimbang cepat & pintar — serba bisa",
    ),
    "mistral-small": ModelSpec(
        id="mistralai/mistral-small-4-119b-2603",
        label="Mistral-Small-4",
        note="Cepat — tugas ringan & respon kilat",
    ),
    "mistral-nemotron": ModelSpec(
        id="mistralai/mistral-nemotron",
        label="Mistral-Nemotron",
        note="Efisien untuk agent & pemakaian tool",
    ),
    # --- Qwen ---
    "qwen": ModelSpec(
        id="qwen/qwen3.5-122b-a10b",
        label="Qwen3.5 122B",
        note="Penalaran & multibahasa kuat — matematika/analisis",
    ),
    "qwen-next": ModelSpec(
        id="qwen/qwen3-next-80b-a3b-instruct",
        label="Qwen3-Next 80B",
        note="Efisien & konteks panjang — dokumen besar",
    ),
    # --- Agentic lain ---
    "glm": ModelSpec(
        id="z-ai/glm-5.2",
        label="GLM-5.2",
        note="Jago agentic & coding — otomasi banyak langkah",
    ),
    "minimax": ModelSpec(
        id="minimaxai/minimax-m3",
        label="MiniMax-M3",
        note="Penalaran panjang — perencanaan & pemecahan bertahap",
    ),
    # --- NVIDIA Nemotron (thinking via /effort) ---
    "nemotron-ultra": ModelSpec(
        id="nvidia/nemotron-3-ultra-550b-a55b",
        label="Nemotron-3 Ultra 550B",
        reasoning_style="nemotron",
        note="Reasoning flagship — soal tersulit (atur kedalaman via /effort)",
    ),
    "nemotron-super": ModelSpec(
        id="nvidia/nemotron-3-super-120b-a12b",
        label="Nemotron-3 Super 120B",
        reasoning_style="nemotron",
        note="Reasoning kuat & lebih ringan dari Ultra",
    ),
    "nemotron49": ModelSpec(
        id="nvidia/llama-3.3-nemotron-super-49b-v1.5",
        label="Llama-3.3 Nemotron Super 49B",
        reasoning_style="nemotron",
        note="Reasoning seimbang berbasis Llama — hemat & pintar",
    ),
    "nemotron-nano": ModelSpec(
        id="nvidia/nvidia-nemotron-nano-9b-v2",
        label="Nemotron Nano 9B",
        reasoning_style="nemotron",
        note="Reasoning ringan & cepat — berpikir tanpa lambat",
    ),
    # --- OpenAI gpt-oss (thinking via reasoning_effort) ---
    "gptoss120": ModelSpec(
        id="openai/gpt-oss-120b",
        label="GPT-OSS 120B",
        reasoning_style="gpt_oss",
        note="Reasoning gaya OpenAI — atur low/medium/high via /effort",
    ),
    "gptoss20": ModelSpec(
        id="openai/gpt-oss-20b",
        label="GPT-OSS 20B",
        reasoning_style="gpt_oss",
        note="Reasoning ringan & cepat",
    ),
    # --- Vision (analisis gambar; dipakai VISION_MODEL) ---
    "llama-vision": ModelSpec(
        id="meta/llama-3.2-90b-vision-instruct",
        label="Llama-3.2 90B Vision",
        multimodal=True,
        note="Khusus analisis gambar — deskripsi & tanya-jawab foto",
    ),
}

_ORDER = list(MODELS.keys())


def resolve(name: str) -> ModelSpec:
    """Cari ModelSpec dari alias, nomor (1..N), atau ID penuh.

    Jika ID penuh tidak dikenal, tetap dibuat ModelSpec generik agar pengguna
    bebas memakai model apa pun dari katalog NVIDIA.
    """
    key = name.strip().lower()

    # Alias langsung.
    if key in MODELS:
        return MODELS[key]

    # Nomor urut (1-based).
    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(_ORDER):
            return MODELS[_ORDER[idx]]

    # Cocokkan dengan ID penuh atau label.
    for spec in MODELS.values():
        if key == spec.id.lower() or key == spec.label.lower():
            return spec

    # ID penuh yang tidak terdaftar -> pakai apa adanya.
    if "/" in name:
        return ModelSpec(id=name.strip(), label=name.strip())

    raise ValueError(
        f"Model '{name}' tidak dikenal. Ketik /model untuk melihat daftar."
    )


def spec_for_id(model_id: str) -> ModelSpec:
    """Temukan ModelSpec berdasarkan ID (untuk model yang dipakai lewat .env)."""
    for spec in MODELS.values():
        if spec.id == model_id:
            return spec
    return ModelSpec(id=model_id, label=model_id)


def catalog() -> list[tuple[int, str, ModelSpec]]:
    """Daftar (nomor, alias, spec) terurut — untuk ditampilkan sebagai tabel."""
    return [(i, key, MODELS[key]) for i, key in enumerate(_ORDER, start=1)]


def list_text(current_id: str | None = None) -> str:
    """Daftar model siap tampil untuk perintah /model."""
    lines = ["Model tersedia (semua di-host NVIDIA):"]
    for i, key in enumerate(_ORDER, start=1):
        spec = MODELS[key]
        tags = []
        if spec.multimodal:
            tags.append("multimodal")
        if spec.reasoning:
            tags.append("thinking")
        if spec.note:
            tags.append(spec.note)
        tag = f"  [{', '.join(tags)}]" if tags else ""
        mark = "  <- aktif" if current_id and spec.id == current_id else ""
        lines.append(f"  {i:>2}. {key:16s} {spec.label} ({spec.id}){tag}{mark}")
    lines.append(
        "Pilih: /model <nama|nomor|id>   contoh: /model deepseek  atau  /model 2"
    )
    return "\n".join(lines)

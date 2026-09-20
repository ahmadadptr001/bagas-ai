"""Uji cepat: model mana yang MASIH benar-benar menjawab.

Daftar /v1/models hanya membuktikan sebuah nama TERDAFTAR, bukan bahwa ia
melayani. Skrip ini mengirim satu permintaan sekecil mungkin ke tiap kandidat
lalu melaporkan hasil terukurnya: waktu sampai balasan utuh, jumlah token,
dan teks galat apa adanya bila gagal.

Pakai:  python tools/uji_nvidia_models.py
        python tools/uji_nvidia_models.py z-ai/glm-5.3 openai/gpt-oss-20b

Bawaannya menembak jalur NVIDIA, tapi endpoint-nya bisa ditukar lewat env —
berguna untuk mengukur jalur lain dengan ukuran yang sama persis:
        UJI_BASE=https://opencode.ai/zen/v1 python tools/uji_nvidia_models.py ...
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from agent import config  # noqa: E402
from openai import OpenAI  # noqa: E402

# Batas waktu & jatah token keluaran bisa dinaikkan lewat env: model "berpikir"
# butuh jauh lebih lama sampai token pertama, dan jatah 16 token bisa HABIS
# dipakai nalarnya sehingga `content` pulang KOSONG — bukan gagal, hanya
# terpotong. Untuk menilai kualitas jawaban, pakai UJI_TOK=... yang lebih besar.
TIMEOUT = float(os.getenv("UJI_TIMEOUT", "90"))
MAKS_TOK = int(os.getenv("UJI_TOK", "16"))

# Endpoint & kredensial. Jalur opencode/* sengaja jalan TANPA key (anonim
# per-IP), jadi key kosong di sana bukan kesalahan — karena itu kliennya diberi
# string kosong, bukan ditolak lebih dulu.
BASE = os.getenv("UJI_BASE", config.NVIDIA_BASE_URL)
KEY = os.getenv("UJI_KEY", config.NVIDIA_API_KEY)

# Kandidat: dua entri yang sudah ada di /model + yang sekilas layak dipakai
# agent koding (chat-completion, bukan embedding/guard/vision-only).
KANDIDAT = [
    "nvidia/nemotron-3-ultra-550b-a55b",
    "meta/muse-glimmer-30b",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.6",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-nano-3-30b-a3b",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "openai/gpt-oss-20b",
    "poolside/laguna-xs-2.1",
    "google/gemma-4-31b-it",
    "mistralai/mistral-nemotron",
    "deepseek-ai/deepseek-v4-flash-0731",
    "nvidia/cosmos-reason2-8b",
]

klien = OpenAI(
    base_url=BASE,
    api_key=KEY,
    # Batas waktu & tanpa coba-ulang: yang diukur di sini "menjawab atau
    # tidak", dan klien bawaan akan menunggu SELAMANYA pada endpoint yang
    # menggantung (TERUKUR: uji pertama macet >10 menit tanpa satu pun baris).
    timeout=TIMEOUT,
    max_retries=0,
)


def uji(model: str) -> tuple[str, str]:
    t0 = time.time()
    try:
        r = klien.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Balas satu kata: oke"}],
            max_tokens=MAKS_TOK,
            temperature=0,
        )
        dt = time.time() - t0
        teks = (r.choices[0].message.content or "").strip().replace("\n", " ")[:40]
        pakai = r.usage.total_tokens if r.usage else "?"
        return model, f"OK    {dt:5.1f}s  {pakai:>5} tok  {teks!r}"
    except Exception as e:  # noqa: BLE001 — apa pun galatnya, itu hasil ujinya
        pesan = str(e).replace("\n", " ")[:160]
        return model, f"GAGAL {time.time() - t0:5.1f}s  {type(e).__name__}: {pesan}"


def main() -> int:
    # Key kosong hanya berbahaya di jalur yang MEMANG menuntut key; jalur
    # opencode/* justru dirancang tanpa key, jadi jangan dihentikan di sini.
    if not KEY and "opencode" not in BASE:
        print("NVIDIA_API_KEY kosong — tak ada yang bisa diuji.")
        return 2
    kandidat = sys.argv[1:] or KANDIDAT
    print(f"Menguji {len(kandidat)} model di {BASE}\n")
    with ThreadPoolExecutor(max_workers=4) as pool:
        for model, hasil in pool.map(uji, kandidat):
            print(f"{model:52s} {hasil}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

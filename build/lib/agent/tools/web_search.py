"""Tool pencarian web via DuckDuckGo (tanpa API key)."""
from __future__ import annotations

from .base import tool


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Cari informasi terkini di internet menggunakan DuckDuckGo. Gunakan untuk berita, fakta terbaru, harga, atau apa pun yang mungkin berubah.

    query: kata kunci pencarian.
    max_results: jumlah hasil (default 5).
    """
    try:
        from ddgs import DDGS
    except ImportError:  # nama paket lama
        from duckduckgo_search import DDGS  # type: ignore

    max_results = max(1, min(int(max_results), 10))
    results = []
    with DDGS() as ddgs:
        for i, r in enumerate(ddgs.text(query, max_results=max_results), start=1):
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            results.append(f"{i}. {title}\n   {body}\n   {href}")

    if not results:
        return f"Tidak ada hasil untuk '{query}'."
    return "\n\n".join(results)

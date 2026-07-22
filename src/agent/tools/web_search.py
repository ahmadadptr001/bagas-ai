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


@tool
def fetch_url(url: str, max_chars: int = 8000) -> str:
    """Ambil ISI sebuah halaman web / berkas teks dari URL dan kembalikan teksnya.

    web_search hanya memberi CUPLIKAN hasil pencarian; pakai fetch_url bila perlu
    membaca isi sebenarnya — dokumentasi, README, changelog, berkas JSON/CSV
    mentah, atau halaman yang alamatnya sudah diketahui.

    url: alamat lengkap (http/https).
    max_chars: batas panjang teks yang dikembalikan (default 8000).
    """
    import re as _re
    import requests

    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return "[error] url harus diawali http:// atau https://"
    try:
        r = requests.get(
            u, timeout=30, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (bagas-ai)"},
        )
    except requests.RequestException as e:
        return f"[error] gagal mengambil {u}: {e}"
    if r.status_code >= 400:
        return f"[error] HTTP {r.status_code} dari {u}"

    ctype = (r.headers.get("content-type") or "").lower()
    if not any(t in ctype for t in ("text", "json", "xml", "javascript",
                                    "html", "csv")):
        return (f"[error] isi bukan teks (content-type: {ctype or 'tidak ada'}). "
                "fetch_url hanya untuk halaman/berkas teks.")
    teks = r.text
    if "html" in ctype:
        # HTML dijadikan teks biasa: script/style dibuang lebih dulu karena
        # isinya bukan bacaan dan bisa jauh lebih panjang daripada artikelnya.
        teks = _re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", teks)
        teks = _re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", teks)
        teks = _re.sub(r"(?s)<[^>]+>", " ", teks)
        teks = (teks.replace("&nbsp;", " ").replace("&amp;", "&")
                    .replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&quot;", '"').replace("&#39;", "'"))
        teks = _re.sub(r"[ \t]+", " ", teks)
        teks = _re.sub(r"\n\s*\n\s*\n+", "\n\n", teks)
    teks = teks.strip()
    if not teks:
        return f"[error] {u} tak menghasilkan teks yang bisa dibaca."
    dipotong = len(teks) > max_chars
    if dipotong:
        teks = teks[:max_chars]
    kepala = f"[{u}] {len(r.content)} byte, {ctype.split(';')[0] or '?'}"
    if dipotong:
        kepala += f" — dipotong di {max_chars} karakter"
    return kepala + "\n\n" + teks

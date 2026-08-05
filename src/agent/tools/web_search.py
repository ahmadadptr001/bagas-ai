"""Tool pencarian web via DuckDuckGo (tanpa API key)."""
from __future__ import annotations

import re as _re

from .base import tool

_UA = {"User-Agent": "Mozilla/5.0 (bagas-ai)"}


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Cari di internet (DuckDuckGo). Query: 2-4 kata kunci konkret bhs Inggris (boleh operator site:), BUKAN kalimat panjang; kalau nihil ubah SATU kata, jangan susun query acak baru.

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


def _gambar_hidup(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """(hidup?, keterangan) untuk sebuah URL gambar.

    HEAD dulu karena jauh lebih murah; sebagian CDN menolak HEAD dengan 403/405
    padahal GET-nya baik-baik saja, jadi penolakan itu diuji ulang dengan GET
    yang hanya meminta beberapa byte pertama. Tanpa uji ulang itu, foto yang
    sebenarnya bisa dipakai ikut terbuang."""
    import requests

    def kabar(r) -> tuple[bool, str]:
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        if ctype and not ctype.startswith("image/"):
            return False, f"bukan gambar ({ctype})"
        ukuran = r.headers.get("content-length") or ""
        ket = ctype or "gambar"
        if ukuran.isdigit():
            ket += f", {int(ukuran) // 1024} KB"
        return True, ket

    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=_UA)
        if r.status_code not in (403, 405, 501):
            return kabar(r)
    except requests.RequestException:
        pass
    try:
        with requests.get(url, timeout=timeout, allow_redirects=True, stream=True,
                          headers={**_UA, "Range": "bytes=0-2047"}) as r:
            return kabar(r)
    except requests.RequestException as e:
        return False, f"{type(e).__name__}"


@tool
def web_search_image(query: str, max_results: int = 6,
                     verify: bool = True) -> str:
    """Cari GAMBAR di internet dan kembalikan URL gambar yang SUDAH DIPASTIKAN hidup (HTTP 200 + benar-benar gambar). Pakai ini sebelum memasang foto/ilustrasi ke halaman — jangan mengarang URL unsplash/pexels dari ingatan, alamat tebakan hampir selalu 404.

    query: kata kunci bahasa Inggris, 2-4 kata benda konkret (mis. 'village office building indonesia').
    max_results: jumlah hasil yang dikembalikan (default 6, maksimal 15).
    verify: periksa tiap URL benar-benar hidup sebelum dilaporkan (default true).
    """
    try:
        from ddgs import DDGS
    except ImportError:  # nama paket lama
        from duckduckgo_search import DDGS  # type: ignore

    q = (query or "").strip()
    if not q:
        return "[error] query kosong."
    max_results = max(1, min(int(max_results), 15))
    # Dicari lebih banyak daripada yang diminta: sebagian pasti mati, dan tanpa
    # cadangan hasilnya sering tinggal satu-dua setelah diperiksa.
    ambil = max_results * 3 if verify else max_results
    mentah: list[dict] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.images(q, max_results=ambil):
                u = (r.get("image") or "").strip()
                if u.lower().startswith(("http://", "https://")):
                    mentah.append(r)
    except Exception as e:  # noqa: BLE001 - layanan pencarian di luar kendali
        return f"[error] pencarian gambar gagal: {type(e).__name__}: {e}"

    if not mentah:
        return (f"Tidak ada gambar untuk '{q}'. Ubah SATU kata kunci yang "
                "paling menentukan, jangan menyusun query acak baru.")

    if not verify:
        hasil = mentah[:max_results]
        baris = [f"{i}. {r.get('title') or '(tanpa judul)'}\n   {r.get('image')}"
                 for i, r in enumerate(hasil, start=1)]
        return "\n".join(baris) + "\n\n(belum diperiksa — verify=false)"

    # Diperiksa BERBARENGAN. Berurutan, 18 alamat yang masing-masing bisa
    # menunggu 8 detik berarti tool ini sendiri yang jadi penyebab timeout.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        periksa = list(pool.map(lambda r: _gambar_hidup(r.get("image", "")),
                                mentah))

    hidup: list[str] = []
    mati = 0
    for r, (ok, ket) in zip(mentah, periksa):
        if not ok:
            mati += 1
            continue
        w, h = r.get("width"), r.get("height")
        ukuran = f"{w}x{h}" if w and h else "?"
        hidup.append(
            f"{len(hidup) + 1}. {r.get('title') or '(tanpa judul)'}\n"
            f"   {r.get('image')}\n"
            f"   {ukuran} · {ket} · sumber: {r.get('url') or '?'}")
        if len(hidup) >= max_results:
            break

    if not hidup:
        return (f"Semua {mati} kandidat gambar untuk '{q}' mati/bukan gambar. "
                "Coba kata kunci lain, atau pakai download_file dari sumber "
                "yang alamatnya sudah pasti.")
    ekor = f"\n\n({len(hidup)} hidup, {mati} dibuang karena mati/bukan gambar)"
    return "\n\n".join(hidup) + ekor


# Atribut HTML yang MEMUAT alamat gambar. `src` saja tak cukup: situs modern
# menunda pemuatan gambar, dan alamat aslinya dititipkan ke data-src/srcset
# sementara `src` cuma berisi placeholder abu-abu 1x1.
_ATRIBUT_GAMBAR = ("src", "data-src", "data-original", "data-lazy-src")


def _serap_gambar(html: str, dasar: str) -> list[str]:
    from urllib.parse import urljoin

    out: list[str] = []
    for m in _re.finditer(r"(?is)<img\s([^>]+)>", html):
        atr = m.group(1)
        alamat = ""
        for nama in _ATRIBUT_GAMBAR:
            a = _re.search(rf"""(?is)\b{nama}\s*=\s*["']([^"']+)["']""", atr)
            if a and not a.group(1).startswith("data:"):
                alamat = a.group(1).strip()
                break
        if not alamat:
            # srcset: "kecil.jpg 480w, besar.jpg 1200w" -> ambil yang terakhir
            # (paling besar), sebab yang dipakai orang biasanya versi penuh.
            s = _re.search(r"""(?is)\bsrcset\s*=\s*["']([^"']+)["']""", atr)
            if s:
                bagian = [b.strip().split(" ")[0] for b in s.group(1).split(",")]
                bagian = [b for b in bagian if b and not b.startswith("data:")]
                if bagian:
                    alamat = bagian[-1]
        if alamat:
            penuh = urljoin(dasar, alamat)
            if penuh not in out:
                out.append(penuh)
    for m in _re.finditer(
            r"""(?is)<meta[^>]+property\s*=\s*["']og:image["'][^>]*"""
            r"""content\s*=\s*["']([^"']+)["']""", html):
        penuh = urljoin(dasar, m.group(1).strip())
        if penuh not in out:
            out.insert(0, penuh)      # og:image = gambar utama halaman
    return out


@tool
def web_extractor(url: str, what: str = "all", max_chars: int = 6000) -> str:
    """Bongkar sebuah halaman web jadi bagian-bagian yang bisa dipakai langsung: judul, deskripsi, daftar heading, teks isi, URL semua GAMBAR, dan daftar tautan. Bedanya dengan fetch_url — fetch_url memberi teks mentah satu gumpalan, ini memberi bagian yang sudah dipisah, jadi dipakai saat butuh 'gambar apa saja yang ada di halaman ini' atau 'apa struktur isinya'.

    url: alamat lengkap (http/https).
    what: bagian yang diambil — "all" (bawaan), "text", "images", "links", atau "headings". Boleh digabung dengan koma.
    max_chars: batas panjang bagian teks (default 6000).
    """
    import requests

    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return "[error] url harus diawali http:// atau https://"
    minta = {b.strip().lower() for b in (what or "all").split(",") if b.strip()}
    if not minta or "all" in minta or "semua" in minta:
        minta = {"text", "images", "links", "headings"}

    try:
        r = requests.get(u, timeout=30, allow_redirects=True, headers=_UA)
    except requests.RequestException as e:
        return f"[error] gagal mengambil {u}: {e}"
    if r.status_code >= 400:
        return f"[error] HTTP {r.status_code} dari {u}"
    ctype = (r.headers.get("content-type") or "").lower()
    if "html" not in ctype:
        return (f"[error] {u} bukan halaman HTML (content-type: "
                f"{ctype.split(';')[0] or 'tidak ada'}). Untuk berkas teks "
                "biasa pakai fetch_url.")

    html = r.text
    dasar = r.url or u
    bagian: list[str] = [f"[{dasar}]"]

    judul = _re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if judul:
        bagian.append("JUDUL: " + _re.sub(r"\s+", " ", judul.group(1)).strip())
    desk = _re.search(
        r"""(?is)<meta[^>]+name\s*=\s*["']description["'][^>]*"""
        r"""content\s*=\s*["']([^"']*)["']""", html)
    if desk and desk.group(1).strip():
        bagian.append("DESKRIPSI: " + _re.sub(r"\s+", " ", desk.group(1)).strip())

    # Script/style dibuang SEKALI di awal, dipakai bersama oleh heading & teks.
    bersih = _re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>",
                     " ", html)

    if "headings" in minta:
        kepala = []
        for m in _re.finditer(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", bersih):
            t = _re.sub(r"\s+", " ", _re.sub(r"(?s)<[^>]+>", " ", m.group(2)))
            if t.strip():
                kepala.append("  " * (int(m.group(1)) - 1) + "- " + t.strip())
        if kepala:
            bagian.append("HEADING (" + str(len(kepala)) + "):\n"
                          + "\n".join(kepala[:60]))

    if "images" in minta:
        gambar = _serap_gambar(bersih, dasar)
        if gambar:
            bagian.append(f"GAMBAR ({len(gambar)}):\n"
                          + "\n".join("  " + g for g in gambar[:40]))
        else:
            bagian.append("GAMBAR: tak ada <img> di halaman ini.")

    if "links" in minta:
        from urllib.parse import urljoin
        tautan: list[str] = []
        terlihat: set[str] = set()
        for m in _re.finditer(
                r"""(?is)<a\s[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""",
                bersih):
            href = m.group(1).strip()
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            penuh = urljoin(dasar, href)
            if penuh in terlihat:
                continue
            terlihat.add(penuh)
            teks = _re.sub(r"\s+", " ",
                           _re.sub(r"(?s)<[^>]+>", " ", m.group(2))).strip()
            tautan.append(f"  {teks[:60] or '(tanpa teks)'} -> {penuh}")
        if tautan:
            bagian.append(f"TAUTAN ({len(tautan)}):\n" + "\n".join(tautan[:50]))

    if "text" in minta:
        t = _re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", bersih)
        t = _re.sub(r"(?s)<[^>]+>", " ", t)
        t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
              .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
        t = _re.sub(r"[ \t]+", " ", t)
        t = _re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()
        if len(t) > max_chars:
            t = t[:max_chars] + f"\n… (dipotong di {max_chars} karakter)"
        bagian.append("TEKS:\n" + (t or "(tak ada teks yang bisa dibaca)"))

    return "\n\n".join(bagian)


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

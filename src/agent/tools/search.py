"""Tool pencarian: cari BERKAS lewat pola nama, dan cari TEKS di dalam berkas.

Kenapa ini penting untuk agent yang jumlah gilirannya mahal (tiap giliran =
satu bolak-balik ke situs AI web): tanpa keduanya, satu-satunya cara menemukan
"di mana fungsi X didefinisikan" adalah list_dir lalu read_file berkali-kali —
belasan giliran habis hanya untuk mencari, sebelum pekerjaan sebenarnya dimulai.
Peta proyek (projectindex) memberi gambaran struktur, tapi tidak bisa menjawab
pertanyaan tentang ISI.

Karena mahalnya itu pula, keduanya dirancang supaya SATU panggilan sudah cukup:
  - hasilnya dikelompokkan per berkas dan barisnya diurutkan, bukan daftar datar;
  - baris yang tampak DEFINISI ditandai dan diangkat ke atas — 90% pertanyaan
    "di mana X" sebenarnya menanyakan itu;
  - saat kecocokannya sedikit, baris sekitarnya ikut ditampilkan otomatis,
    sehingga sering kali read_file susulan tak diperlukan lagi;
  - saat TIDAK ketemu, hasilnya tidak pernah buntu: lingkupnya dilebarkan
    sendiri, spasi dilonggarkan, dan nama yang mirip diusulkan.

Keduanya sengaja melewati folder yang tak pernah relevan (.git, node_modules,
venv, __pycache__, dist/build) — bukan sekadar demi kecepatan, tapi supaya
hasilnya tidak tenggelam oleh ribuan kecocokan di dependensi.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
from pathlib import Path

from .. import config
from .base import tool

ROOT = config.PROJECT_ROOT

# Folder yang TIDAK pernah ditelusuri.
_LEWATI = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".nuxt", ".cache", "target", "vendor", ".idea", ".vscode",
    "site-packages", ".tox", "coverage", ".gradle",
}
# Berkas biner tak ada gunanya digrep dan bikin hasil berantakan.
_EKS_BINER = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".ogg", ".flac",
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyd", ".class",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".db", ".sqlite",
}
_MAKS_BYTE = 2_000_000   # berkas raksasa (bundel/minified) dilewati
_MAKS_PER_BERKAS = 20    # supaya satu berkas tak menghabiskan seluruh jatah
_PANJANG_BARIS = 200


def _telusuri(akar: Path):
    """Semua berkas di bawah `akar`, melewati folder & berkas yang tak relevan."""
    for dirpath, dirnames, filenames in os.walk(akar):
        dirnames[:] = [d for d in dirnames
                       if d not in _LEWATI and not d.startswith(".")]
        for nama in filenames:
            p = Path(dirpath) / nama
            if p.suffix.lower() in _EKS_BINER:
                continue
            yield p


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _penyaring(pattern: str):
    """Predikat 'berkas ini masuk lingkup pattern' (pattern kosong = semua)."""
    pat = (pattern or "").strip().replace("\\", "/")
    if not pat:
        return lambda p: True
    hanya_nama = "/" not in pat

    def cocok(p: Path) -> bool:
        if hanya_nama:
            return fnmatch.fnmatch(p.name, pat)
        rel = _rel(p)
        return fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "**/" + pat)

    return cocok


def _isi(p: Path) -> str | None:
    """Isi berkas teks, atau None bila biner/kebesaran/tak terbaca."""
    try:
        if p.stat().st_size > _MAKS_BYTE:
            return None
        return p.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


# --- pengenalan DEFINISI ---------------------------------------------------
#
# Pertanyaan "di mana X" hampir selalu berarti "di mana X DIBUAT", bukan "di
# mana X dipakai". Tanpa penandaan, satu definisi tenggelam di antara 40 baris
# pemakaian dan model harus menebak-nebak berkas mana yang dibuka — satu giliran
# terbuang tiap tebakan. Polanya sengaja lintas-bahasa: satu pola untuk Python,
# JS/TS, Go, Rust, Java, C#, dan kerabatnya.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _pola_definisi(q: str) -> re.Pattern | None:
    """Pola 'baris ini MENDEFINISIKAN q', atau None bila q bukan identifier."""
    if not _IDENT.fullmatch(q or ""):
        return None
    n = re.escape(q)
    return re.compile(
        # def/class/function/interface/... NAMA
        r"(?:^|[^\w.])(?:def|class|function|interface|type|struct|enum|trait"
        r"|impl|fn|func|record|module|namespace|sub|method)\s+" + n + r"\b"
        # const/let/var NAMA   (JS/TS/Go)
        r"|(?:^|[^\w.])(?:const|let|var)\s+" + n + r"\b"
        # NAMA = fungsi/lambda/panah   (gaya JS & Python fungsional)
        r"|^\s*" + n + r"\s*[:=]\s*(?:async\s*)?(?:function\b|lambda\b|\()"
        # NAMA didefinisikan di tingkat modul (konstanta, tabel, konfigurasi)
        r"|^" + n + r"\s*[:=][^=]"
        # NAMA: tipe   (anotasi tingkat modul, field dataclass di kolom 0-8)
        r"|^\s{0,8}" + n + r"\s*:\s*[A-Za-z_\[\"']",
        re.MULTILINE,
    )


# Baris yang tampak DEFINISI apa pun namanya — dipakai read_file(outline=true).
# Kata kuncinya WAJIB diikuti spasi lalu sesuatu yang bukan '='/':' supaya
# `type = 3` (pemakaian biasa) tidak ikut terangkat jadi "definisi".
_DEF_BARIS = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:(?:public|private|protected|internal|static|final|abstract|async"
    r"|open|override|suspend|inline|pub|unsafe|extern)\s+)*"
    r"(?:def|class|function|interface|type|struct|enum|trait|impl|fn|func"
    r"|record|module|namespace|sub|package)\s+(?![=:])"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+[\w$]+\s*=\s*"
    r"(?:async\s*)?(?:\(|function\b|[\w$]+\s*=>)"
)
# Heading markdown. Sengaja DIPISAH: di berkas Python pola yang sama adalah
# komentar biasa, dan dulu itu membanjiri outline dengan baris '# --- ... ---'
# sampai definisi yang sesungguhnya tenggelam.
_HEADING_MD = re.compile(r"^#{1,6}\s+\S")


def _peta_definisi(teks: str, mulai: int = 1, batas: int = 60,
                   markdown: bool = False) -> list[tuple[int, str]]:
    """Daftar (nomor_baris, baris) yang tampak definisi — kerangka sebuah berkas."""
    keluar: list[tuple[int, str]] = []
    for ofs, baris in enumerate(teks.split("\n")):
        if len(baris) > 400 or not baris.strip():
            continue
        if _DEF_BARIS.match(baris) or (markdown and _HEADING_MD.match(baris)):
            potong = baris.rstrip()
            if len(potong) > _PANJANG_BARIS:
                potong = potong[:_PANJANG_BARIS] + "…"
            keluar.append((mulai + ofs, potong))
            if len(keluar) >= batas:
                break
    return keluar


@tool
def glob_files(pattern: str, max_results: int = 100) -> str:
    """Cari BERKAS berdasarkan pola nama, mis. '*.js', 'src/**/*.py', 'test_*'. Kalau tak ada yang cocok, hasilnya menyarankan nama berkas yang paling mirip — jadi salah tebak nama pun tetap terjawab dalam satu langkah.

    Pakai ini alih-alih menelusuri folder satu per satu dengan list_dir.

    pattern: pola nama berkas ('**' berarti sub-folder mana pun).
    max_results: batas jumlah hasil (default 100).
    """
    pat = (pattern or "").strip().replace("\\", "/")
    if not pat:
        return "[error] pattern kosong."
    hanya_nama = "/" not in pat
    hasil = []
    semua: list[str] = []
    for p in _telusuri(ROOT):
        rel = _rel(p)
        semua.append(rel)
        cocok = (fnmatch.fnmatch(p.name, pat) if hanya_nama
                 else fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "**/" + pat))
        if cocok and len(hasil) < max_results:
            hasil.append(rel)
    if hasil:
        hasil.sort()
        kepala = f"{len(hasil)} berkas cocok '{pattern}':"
        return kepala + "\n" + "\n".join("  " + h for h in hasil)

    # --- tak ketemu: JANGAN buntu -----------------------------------------
    # Penyebab paling sering cuma dua: polanya kurang '*', atau namanya salah
    # ingat sedikit. Dua-duanya bisa dijawab sekarang juga.
    inti = pat.strip("*/")
    longgar = [r for r in semua if inti and inti.lower() in r.lower()]
    if longgar:
        longgar.sort()
        return (f"Tidak ada yang cocok PERSIS dengan '{pattern}', tapi "
                f"{len(longgar)} berkas memuat '{inti}' di path-nya:\n"
                + "\n".join("  " + h for h in longgar[:max_results]))
    nama_saja = sorted({Path(r).name for r in semua})
    mirip = difflib.get_close_matches(Path(pat).name, nama_saja, n=6, cutoff=0.6)
    pesan = f"Tidak ada berkas yang cocok dengan '{pattern}'."
    if mirip:
        pesan += ("\nNama yang paling mirip: "
                  + ", ".join(mirip)
                  + "\nCoba salah satunya, atau pakai pola yang lebih longgar "
                    f"seperti '*{inti}*'.")
    else:
        pesan += (f"\nProyek ini berisi {len(semua)} berkas. Coba pola lebih "
                  "longgar (mis. '*.py'), atau cari ISI-nya dengan search_text.")
    return pesan


def _pindai(rx: re.Pattern, lolos, max_results: int,
            per_berkas: int = _MAKS_PER_BERKAS):
    """Pindai proyek; kembalikan (temuan, terpotong).

    temuan: list (rel, list[(nomor_baris, baris_mentah)], teks_berkas).
    """
    temuan: list[tuple[str, list[tuple[int, str]], str]] = []
    total = 0
    terpotong = False
    for p in _telusuri(ROOT):
        if not lolos(p):
            continue
        teks = _isi(p)
        if teks is None or not rx.search(teks):
            continue
        baris_cocok: list[tuple[int, str]] = []
        for i, baris in enumerate(teks.split("\n"), 1):
            if rx.search(baris):
                baris_cocok.append((i, baris))
                if len(baris_cocok) >= per_berkas:
                    terpotong = True
                    break
        if not baris_cocok:
            continue
        temuan.append((_rel(p), baris_cocok, teks))
        total += len(baris_cocok)
        if total >= max_results:
            terpotong = True
            break
    return temuan, terpotong


def _potong(baris: str) -> str:
    b = baris.rstrip()
    return b[:_PANJANG_BARIS] + "…" if len(b) > _PANJANG_BARIS else b


def _rentang(nomor: list[int], konteks: int, n_baris: int):
    """Gabungkan baris cocok + konteksnya jadi rentang yang tak tumpang tindih."""
    blok: list[list[int]] = []
    for i in sorted(nomor):
        a, b = max(1, i - konteks), min(n_baris, i + konteks)
        if blok and a <= blok[-1][1] + 1:
            blok[-1][1] = max(blok[-1][1], b)
        else:
            blok.append([a, b])
    return blok


def _usul_nama(q: str, lolos) -> str:
    """Saran identifier yang mirip `q` — dipanggil hanya saat hasil NOL."""
    if not _IDENT.fullmatch(q or ""):
        return ""
    kandidat: dict[str, str] = {}
    n_berkas = 0
    for p in _telusuri(ROOT):
        if not lolos(p):
            continue
        teks = _isi(p)
        if teks is None:
            continue
        n_berkas += 1
        if n_berkas > 600:
            break
        for m in _IDENT.finditer(teks):
            nama = m.group(0)
            if len(nama) >= 3 and nama not in kandidat:
                kandidat[nama] = _rel(p)
    mirip = difflib.get_close_matches(q, list(kandidat), n=6, cutoff=0.75)
    mirip = [m for m in mirip if m != q]
    if not mirip:
        return ""
    return ("\nNama yang mirip dan MEMANG ada di proyek ini: "
            + ", ".join(f"{m} ({kandidat[m]})" for m in mirip))


def _susun(temuan, konteks: int, pdef: re.Pattern | None, terpotong: bool,
           query: str, max_results: int) -> str:
    """Rakit hasil: berkas berisi definisi lebih dulu, baris diberi konteks."""
    n_cocok = sum(len(b) for _, b, _ in temuan)
    n_def = 0
    berkas: list[tuple[bool, str, list[tuple[int, str]], str]] = []
    for rel, baris_cocok, teks in temuan:
        ada_def = bool(pdef) and any(pdef.search(b) for _, b in baris_cocok)
        n_def += sum(1 for _, b in baris_cocok if pdef and pdef.search(b))
        berkas.append((ada_def, rel, baris_cocok, teks))
    # Berkas yang memuat definisi naik ke atas; sisanya urut abjad. Yang dicari
    # model hampir selalu ada di baris pertama hasil, bukan di baris ke-40.
    berkas.sort(key=lambda x: (not x[0], x[1]))

    kepala = f"{n_cocok} baris cocok '{query}' di {len(temuan)} berkas"
    if n_def:
        kepala += f" — {n_def} tampak DEFINISI (ditandai ▸)"
    baris_keluar = [kepala + ":"]

    for ada_def, rel, baris_cocok, teks in berkas:
        semua_baris = teks.split("\n")
        n_baris = len(semua_baris)
        baris_keluar.append("")
        baris_keluar.append(f"{rel}" + ("   [definisi di sini]" if ada_def else ""))
        peta = dict(baris_cocok)
        if konteks <= 0:
            for i, b in baris_cocok:
                tanda = "▸" if pdef and pdef.search(b) else " "
                baris_keluar.append(f"  {tanda}{i:>5}: {_potong(b)}")
            continue
        for ke, (a, z) in enumerate(
                _rentang([i for i, _ in baris_cocok], konteks, n_baris)):
            if ke:
                baris_keluar.append("    ⋯")
            for i in range(a, z + 1):
                isi_b = peta.get(i, semua_baris[i - 1])
                if i in peta:
                    tanda = "▸" if pdef and pdef.search(isi_b) else " "
                    baris_keluar.append(f"  {tanda}{i:>5}: {_potong(isi_b)}")
                else:
                    baris_keluar.append(f"   {i:>5}| {_potong(isi_b)}")

    if terpotong:
        baris_keluar.append("")
        baris_keluar.append(
            f"… (dipotong di sekitar {max_results} baris atau "
            f"{_MAKS_PER_BERKAS} baris per berkas). Persempit dengan pattern "
            "(mis. '*.py'), atau pakai kata yang lebih spesifik.")
    return "\n".join(baris_keluar)


@tool
def search_text(query: str, pattern: str = "", regex: bool = False,
                max_results: int = 60, context: int = -1) -> str:
    """Cari TEKS di seluruh berkas proyek. Hasilnya dikelompokkan per berkas, baris yang tampak DEFINISI ditandai ▸ dan diangkat ke atas, dan bila kecocokannya sedikit baris sekitarnya ikut ditampilkan — sering kali read_file susulan sudah tak perlu. Kalau tak ketemu, lingkupnya dilebarkan sendiri dan nama yang mirip diusulkan.

    Ini cara tercepat menjawab "di mana X didefinisikan/dipakai" tanpa membaca
    banyak berkas satu per satu.

    query: teks yang dicari (atau pola regex bila regex=true).
    pattern: batasi ke berkas tertentu, mis. '*.py' (kosong = semua).
    regex: true bila `query` adalah regex.
    max_results: batas jumlah baris hasil (default 60).
    context: berapa baris sekitar tiap kecocokan ikut ditampilkan. Default -1 =
        otomatis (2 baris bila kecocokannya sedikit, 0 bila banyak). Set 0 untuk
        memaksa ringkas.
    """
    q = query or ""
    if not q:
        return "[error] query kosong."
    try:
        rx = re.compile(q if regex else re.escape(q), re.IGNORECASE)
    except re.error as e:
        # Regex rusak bukan alasan memulangkan tangan kosong: coba anggap saja
        # teks biasa, dan bila itu justru ketemu, laporkan apa adanya.
        rx_lit = re.compile(re.escape(q), re.IGNORECASE)
        temuan, terpotong = _pindai(rx_lit, _penyaring(pattern), max_results)
        if temuan:
            return (f"[catatan] regex tidak sah ({e}) — dicari sebagai teks "
                    "biasa saja:\n\n"
                    + _susun(temuan, 0, _pola_definisi(q), terpotong, q,
                             max_results))
        return (f"[error] regex tidak sah: {e}\nSebagai teks biasa pun "
                f"'{query}' tidak ada di proyek ini.")

    lolos = _penyaring(pattern)
    temuan, terpotong = _pindai(rx, lolos, max_results)
    pdef = None if regex else _pola_definisi(q)

    if temuan:
        n_cocok = sum(len(b) for _, b, _ in temuan)
        konteks = context
        if konteks < 0:
            # Sedikit kecocokan -> hampir pasti pengguna butuh melihat
            # sekitarnya; banyak kecocokan -> daftar ringkas jauh lebih terbaca.
            konteks = 2 if n_cocok <= 12 else 0
        return _susun(temuan, konteks, pdef, terpotong, q, max_results)

    # --- NOL hasil: tiga jalan keluar, dicoba berurutan --------------------
    lingkup = f" pada berkas '{pattern}'" if pattern else ""
    pesan = [f"Tidak ditemukan '{query}'{lingkup}."]

    # 1) pattern-nya yang terlalu sempit?
    if pattern:
        temuan2, potong2 = _pindai(rx, lambda p: True, min(max_results, 30))
        if temuan2:
            n2 = sum(len(b) for _, b, _ in temuan2)
            return (f"Tidak ada di berkas '{pattern}', TAPI ada {n2} kecocokan "
                    "di berkas lain:\n\n"
                    + _susun(temuan2, 0, pdef, potong2, q, max_results))

    # 2) beda spasi/baris? ("def foo (" vs "def foo(")
    if not regex and re.search(r"\s", q):
        pola_longgar = r"\s*".join(re.escape(t) for t in q.split())
        try:
            rx3 = re.compile(pola_longgar, re.IGNORECASE)
        except re.error:
            rx3 = None
        if rx3 is not None:
            temuan3, potong3 = _pindai(rx3, lolos, max_results)
            if temuan3:
                return ("Tidak ada yang PERSIS begitu, tapi ketemu setelah "
                        "perbedaan spasi diabaikan:\n\n"
                        + _susun(temuan3, 0, pdef, potong3, q, max_results))

    # 3) salah ingat namanya?
    usul = _usul_nama(q, lolos)
    if usul:
        pesan.append(usul.lstrip("\n"))
    else:
        pesan.append("Sudah dicoba juga tanpa memandang huruf besar/kecil. "
                     "Coba potongan kata yang lebih pendek, atau cari nama "
                     "berkasnya dengan glob_files.")
    return "\n".join(pesan)

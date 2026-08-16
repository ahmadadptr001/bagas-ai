"""Server MCP khusus bagas-ai — sasaran tepat, tanpa baca ulang, tanpa buka file asal.

Dijalankan dengan `bagas-ai mcp` (transport stdio) lalu dihubungkan ke klien MCP
(Claude Desktop, Cursor, dsb). Server ini menegakkan tiga disiplin yang diminta:

  1. SASARAN TEPAT — tool `sasaran(tugas)` menetapkan daftar berkas yang relevan
     untuk tugas itu (dihitung dari peta proyek + kata kunci), lengkap dengan
     alasan. AI tahu persis berkas mana yang boleh disentuh — bukan menebak.

  2. TANPA BACA ULANG — tool `baca` mengingat sidik jari tiap berkas yang sudah
     dibaca dalam sesi ini; bila dipanggil lagi untuk berkas yang TIDAK berubah,
     ia menolak mengirim isi ulang dan menyuruh memakai yang sudah ada di konteks
     (atau force=True bila model sungguh-sungguh kehilangan isinya).

  3. TIDAK ASAL BUKA FILE — begitu `sasaran` dipanggil, `baca` MENOLAK berkas di
     luar sasaran (kecuali force=True). Perluasan cuma lewat `perluas_sasaran`
     dengan alasan — keputusan yang disengaja, bukan asal.

Server ini memakai mesin yang SUDAH dipakai agent: peta proyek (projectindex),
batas folder aman (workspace.allowed_roots), dan logika baca `read_file` yang
sama — jadi perilakunya konsisten dengan bagas-ai itu sendiri, bukan tiruan.

Modul `mcp` diimpor LAZY (di main) supaya paket bagas-ai tetap jalan normal
tanpa SDK MCP terpasang; hanya perintah `bagas-ai mcp` yang membutuhkannya.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config, projectindex, workspace
from .tools import search as _search
from .tools.files import read_file as _read_file

# --- kata tugas / pengisi yang BUKAN sasaran ---------------------------------
# Diambil dari kalimat tugas, lalu dicocokkan ke nama berkas & simbol. Kata di
# bawah ini hampir tak pernah menjadi target (kata kerja, kata sambung, isi
# umum), jadi membuangnya membuat peringkat jauh lebih tajam. Ditulis lengkap,
# bukan ditebak lewat stemming — bahasa Indonesia & Inggris sekaligus.
_KATA_TUGAS = frozenset("""
yang dan di ke dari untuk pada dengan agar supaya bisa dapat mau ingin harus
sudah belum tidak jangan ini itu ada adalah akan juga lalu maka atau jika kalau
karena tapi namun sementara saat ketika sehingga meski walau
buat membuat bikin perbaiki memperbaiki betulkan ubah mengubah ganti tambah
menambah hapus menghapus baca membaca tulis menulis cari mencari buka membuka
jalankan menjalankan kerjakan selesaikan tolong saya aku kami kita kamu anda gue
pakai memakai gunakan menggunakan lihat melihat tunjukkan kasih beri berikan
kode file berkas fungsi tool error gagal benar salah cek periksa uji tes test
fix add change remove update read write search open close make create delete
refactor improve please the and for with that this from have has not are was
were will would can could should your you our their what when where which why
how about into over after before between out up down off on in at by to of is
it its as or be been being do does did done need needs want wants
""".split())

# Sidik jari berkas: (mtime_ns, size). Cukup untuk "apakah isinya berubah" tanpa
# membaca ulang isi — dua angka ini berubah hampir pasti bila isi berubah.
_SIDIK = tuple[int, int]


class _Keadaan:
    """Negara bagian sesi MCP: sasaran aktif + cache berkas yang sudah dibaca.

    Satu server stdio melayani SATU klien, jadi negara bagian global per proses
    sudah tepat — tidak perlu per-sesi manual.
    """

    def __init__(self) -> None:
        # path relatif -> alasan mengapa masuk sasaran
        self.sasaran: dict[str, str] = {}
        # path relatif -> sidik jari saat terakhir dibaca
        self.cache: dict[str, _SIDIK] = {}

    def reset(self) -> None:
        self.sasaran = {}
        self.cache = {}


_KEADAAN = _Keadaan()


# --- path aman: sama dengan aturan tool file bagas-ai ------------------------
def _resolusi(path: str) -> tuple[Path | None, str]:
    """(Path, path_rel) untuk berkas yang sah; (None, pesan_error) bila tidak.

    Batasannya persis aturan bagas-ai: root project + folder konteks
    (workspace.allowed_roots). Di luar itu ditolak tanpa tawar-menawar — ini
    bagian dari "tidak asal buka file".
    """
    raw = (path or "").strip()
    if not raw:
        return None, "path kosong."
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = config.PROJECT_ROOT / p
    try:
        p = p.resolve()
    except OSError as e:
        return None, f"path tidak bisa diresolusi: {e}"
    if not p.is_file():
        return None, f"berkas tidak ditemukan: {raw}"
    for akar in workspace.allowed_roots():
        try:
            p.relative_to(akar)
            break
        except ValueError:
            continue
    else:
        return None, (f"di luar jangkauan bagas-ai (root: {config.PROJECT_ROOT} "
                      "+ folder konteks). Pilih berkas di dalamnya.")
    try:
        rel = str(p.relative_to(config.PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        rel = str(p)
    return p, rel


def _sidik(p: Path) -> _SIDIK:
    st = p.stat()
    return (st.st_mtime_ns, st.st_size)


# --- sasaran: menetapkan berkas yang relevan untuk satu tugas -----------------
def _kata_kunci(tugas: str) -> list[str]:
    """Token 3+ karakter dari kalimat tugas, minus kata tugas/isi umum."""
    toks = re.findall(r"[A-Za-z0-9_.-]+", (tugas or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _KATA_TUGAS]


def sasaran(tugas: str, max_hasil: int = 12) -> str:
    """TETAPKAN SASARAN untuk satu tugas: daftar berkas yang relevan beserta alasannya.

    Panggil ini DI AWAL setiap tugas sebelum membaca berkas apa pun. Hasilnya
    menjadi 'sasaran aktif': sesudah ini, `baca` MENOLAK berkas di luar daftar
    (kecuali force=True) — kalau ada berkas lain yang benar-benar diperlukan,
    gunakan `perluas_sasaran` dengan alasan, jangan memaksa.

    tugas: kalimat tugas dari pengguna, mis. 'perbaiki error di core.py saat
        menangani tool yang gagal'.
    max_hasil: batas jumlah berkas sasaran (default 12).
    """
    kunci = _kata_kunci(tugas)
    payload = projectindex.as_payload()
    berkas = payload.get("berkas") or {}

    skor: dict[str, int] = {}
    alasan: dict[str, list[str]] = {}
    for rel, syms in berkas.items():
        s = 0
        for k in kunci:
            if k in rel.lower():
                s += 3
                alasan.setdefault(rel, []).append(f"nama berkas memuat '{k}'")
            for sym in syms:
                if k in sym.lower():
                    s += 2
                    alasan.setdefault(rel, []).append(
                        f"simbol '{sym.strip()[:60]}' memuat '{k}'")
                    break
        if s:
            skor[rel] = s

    urut = sorted(skor, key=skor.get, reverse=True)[:max(1, int(max_hasil))]
    _KEADAAN.sasaran = {
        rel: "; ".join(dict.fromkeys(alasan[rel])) for rel in urut
    }

    baris = [f"Sasaran untuk tugas: {tugas.strip()[:140]}", ""]
    if not kunci:
        baris.append("(tidak ada kata kunci yang bisa dipakai — tugas terlalu "
                     "umum atau memakai kata tugas saja.)")
    if not urut:
        baris.append(
            "Tidak ada berkas kode yang cocok dengan kata kunci. Coba sebut "
            "nama berkas / fungsi / istilah yang lebih spesifik, atau gunakan "
            "`cari` untuk menemukan lokasinya lebih dulu.")
        baris.append("")
        baris.append("Sasaran aktif KOSONG — selama kosong, `baca` tidak "
                     "dibatasi. Tetapkan sasaran dulu supaya kerja tetap fokus.")
        return "\n".join(baris)

    baris.append(f"{len(urut)} berkas relevan (diurutkan dari yang paling "
                 f"cocok; kata kunci: {', '.join(kunci[:6])}):")
    for i, rel in enumerate(urut, 1):
        baris.append(f"{i}. {rel}")
        baris.append(f"   — {_KEADAAN.sasaran[rel]}")
    baris.append("")
    baris.append("DISIPLIN: mulai sekarang hanya berkas di daftar ini yang "
                 "boleh dibuka dengan `baca`. Berkas lain DITOLAK — kalau "
                 "benar-benar diperlukan, panggil `perluas_sasaran` dengan "
                 "alasan.")
    return "\n".join(baris)


def perluas_sasaran(paths: list[str], alasan: str = "") -> str:
    """PERLUAS sasaran aktif dengan berkas tambahan — keputusan yang disengaja.

    Satu-satunya jalan sah untuk membuka berkas di luar daftar `sasaran`
    (selain force=True). Wajib memberi alasan singkat supaya tidak jadi
    jalan pintas "asal buka file".

    paths: daftar path relatif terhadap root project.
    alasan: mengapa berkas ini diperlukan (wajib diisi).
    """
    alasan = (alasan or "").strip()
    if not alasan:
        return ("[error] alasan wajib diisi — perluas_sasaran tanpa alasan "
                "sama saja buka file asal.")
    masuk: list[str] = []
    gagal: list[str] = []
    for raw in paths or []:
        p, rel = _resolusi(str(raw))
        if p is None:
            gagal.append(str(raw))
            continue
        _KEADAAN.sasaran[rel] = f"(ditambahkan manual) {alasan}"
        masuk.append(rel)
    baris = []
    if masuk:
        baris.append(f"Ditambahkan ke sasaran ({len(masuk)}):")
        baris += [f"- {r}" for r in masuk]
    if gagal:
        baris.append(f"Gagal: {', '.join(gagal)}")
    if not masuk and not gagal:
        return "[error] paths kosong."
    baris.append("")
    baris.append(f"Sasaran aktif sekarang {len(_KEADAAN.sasaran)} berkas.")
    return "\n".join(baris)


def sasaran_aktif() -> str:
    """Tampilkan sasaran aktif saat ini (dan berapa berkas yang sudah dibaca)."""
    if not _KEADAAN.sasaran:
        return ("Sasaran aktif KOSONG — `baca` tidak dibatasi. Panggil "
                "`sasaran(tugas)` lebih dulu untuk menetapkan fokus.")
    baris = [f"Sasaran aktif ({len(_KEADAAN.sasaran)} berkas):"]
    for rel, alasan in _KEADAAN.sasaran.items():
        sudah = " ✓ sudah dibaca" if rel in _KEADAAN.cache else ""
        baris.append(f"- {rel}{sudah} — {alasan}")
    return "\n".join(baris)


def tutup_sasaran() -> str:
    """Bersihkan sasaran aktif & cache bacaan — mulai tugas baru dari nol."""
    n_s = len(_KEADAAN.sasaran)
    n_c = len(_KEADAAN.cache)
    _KEADAAN.reset()
    return (f"Sasaran & cache dibersihkan ({n_s} berkas sasaran, "
            f"{n_c} berkas ter-cache). Mulai tugas baru dengan `sasaran(tugas)`.")


# --- baca: guard + cache, lalu menyerahkan isi ke read_file yang sama ---------
def baca(path: str, start_line: int = 0, end_line: int = 0,
         outline: bool = False, force: bool = False) -> str:
    """Baca isi berkas — DENGAN PENJAGA: hanya berkas dalam sasaran aktif, dan TANPA BACA ULANG.

    Aturan main:
    - Bila sasaran aktif TIDAK kosong dan berkas ini TIDAK ada di dalamnya,
      bacaan DITOLAK — panggil `perluas_sasaran` dulu (dengan alasan).
    - Bila berkas ini sudah dibaca sesi ini dan isinya TIDAK berubah, isi
      TIDAK dikirim ulang — pakai yang sudah ada di konteks. force=True hanya
      bila isi sebelumnya benar-benar hilang dari konteks.
    - Isi yang dikirim memakai format `read_file` bagas-ai yang sama
      (potongan, kerangka, nomor baris 1-based), jadi konsisten dengan agent.

    path: relatif terhadap root project (atau absolut untuk folder konteks).
    start_line/end_line: rentang baris 1-based (0 = dari awal / sampai akhir).
    outline: true = peta definisi saja, bukan isi penuh.
    force: true = baca ulang walau sudah dibaca / di luar sasaran (keputusan sadar).
    """
    p, rel = _resolusi(path)
    if p is None:
        return f"[error] {rel}"

    # GUARD 1: di luar sasaran -> tolak (kecuali force).
    if not force and _KEADAAN.sasaran and rel not in _KEADAAN.sasaran:
        daftar = "\n".join(f"- {r}" for r in _KEADAAN.sasaran)
        return (f"[DITOLAK] '{rel}' TIDAK termasuk sasaran aktif.\n"
                f"Sasaran aktif ({len(_KEADAAN.sasaran)} berkas):\n{daftar}\n\n"
                "Kalau berkas ini memang diperlukan, panggil "
                f"perluas_sasaran(paths=['{rel}'], alasan='...') — jangan "
                "memaksakan baca.")

    # GUARD 2: sudah dibaca & tak berubah -> jangan kirim isi ulang.
    # KUNCI cache = (path, lingkup): outline != isi penuh, dan rentang baris
    # berbeda berarti bagian yang berbeda. Dulu kuncinya cuma path — outline
    # lalu start_line=1/end_line=20 yang BELUM pernah dibaca ikut ditolak
    # sebagai "sudah dibaca", padahal isinya tak pernah dilihat.
    sidik = _sidik(p)
    kunci = (rel, int(start_line or 0), int(end_line or 0), bool(outline))
    if not force and kunci in _KEADAAN.cache and _KEADAAN.cache[kunci] == sidik:
        return (f"[SUDAH DIBACA] '{rel}' (lingkup yang sama) tidak berubah "
                f"sejak terakhir dibaca sesi ini — pakai isi yang sudah ada di "
                "konteks, JANGAN baca ulang.\nBila isi sebelumnya benar-benar "
                "hilang dari konteks, baru panggil baca dengan force=True.")

    _KEADAAN.cache[kunci] = sidik
    return _read_file(path, start_line=int(start_line or 0),
                      end_line=int(end_line or 0), outline=bool(outline))


# --- cari: menemukan lokasi tanpa membuka berkas -------------------------------
def cari(query: str, pattern: str = "") -> str:
    """Cari TEKS di seluruh berkas proyek — untuk MENEMUKAN lokasi, tanpa membuka berkas.

    Hasil dikelompokkan per berkas; baris yang tampak definisi ditandai ▸ dan
    diangkat ke atas. Pakai ini SEBELUM `baca`, supaya tidak membuka berkas
    asal-asalan: cari dulu di mana sesuatu berada, baru baca berkas yang
    benar-benar relevan (dan pastikan ia masuk sasaran).

    query: teks yang dicari (mis. nama fungsi, pesan error).
    pattern: batasi ke berkas tertentu, mis. '*.py' (kosong = semua).
    """
    return _search.search_text(query, pattern=pattern)


# --- resource: peta proyek ------------------------------------------------------
def _peta_proyek() -> str:
    """Peta proyek (struktur + simbol kunci tiap berkas) — baca dulu ini untuk
    memahami proyek tanpa membuka berkas satu per satu."""
    return projectindex.ensure()


def _peta_sasaran() -> dict[str, Any]:
    """Sasaran aktif dalam bentuk data (path -> alasan)."""
    return dict(_KEADAAN.sasaran)


# --- main: pasang semuanya ke FastMCP -------------------------------------------
def main() -> None:
    """Jalankan server MCP (stdio). Import mcp dilakukan di sini supaya paket
    bagas-ai tetap jalan tanpa SDK MCP terpasang."""
    # API SDK MCP berubah besar di v2 (FastMCP -> MCPServer). Dicoba dua-duanya
    # supaya kode ini jalan di mcp 1.x maupun 2.x — keduanya memakai dekorator
    # .tool()/.resource() dengan bentuk yang sama.
    try:
        from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer as _Server  # mcp 2.x
        except ImportError as e:
            raise SystemExit(
                "SDK MCP belum terpasang. Jalankan: pip install 'mcp>=1.0'"
            ) from e

    mcp = _Server(
        "bagas-ai-sasaran",
        instructions=(
            "Server ini menegakkan tiga disiplin kerja: (1) panggil "
            "`sasaran(tugas)` dulu untuk menetapkan berkas yang "
            "relevan; (2) `baca` tidak mengirim ulang isi berkas yang "
            "sudah dibaca & tak berubah — pakai yang ada di konteks; "
            "(3) berkas di luar sasaran ditolak, perluas lewat "
            "`perluas_sasaran` dengan alasan. Gunakan `cari` untuk "
            "menemukan lokasi sebelum membaca, dan baca resource "
            "peta proyek untuk memahami struktur."),
    )

    # Tools
    mcp.tool()(sasaran)
    mcp.tool()(perluas_sasaran)
    mcp.tool()(sasaran_aktif)
    mcp.tool()(tutup_sasaran)
    mcp.tool()(baca)
    mcp.tool()(cari)

    # Resources
    mcp.resource("peta://proyek")(_peta_proyek)
    mcp.resource("peta://sasaran")(_peta_sasaran)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

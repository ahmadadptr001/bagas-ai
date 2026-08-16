"""Tool file: baca/tulis/daftar file di dalam ROOT PROJECT (folder terminal
saat `bagasai` dipanggil) DAN folder konteks tambahan (fitur add-dir).
Dibatasi agar tidak keluar dari folder-folder yang diizinkan."""
from __future__ import annotations

import difflib
import json as _json
import re
import shutil
import subprocess
from pathlib import Path

from .. import config, permissions, workspace
from .base import tool, tool_aktif
from .checkpoint import snapshot as _snapshot

ROOT = config.PROJECT_ROOT

# Cache anti-baca-ulang: (path, start, end, outline, bernomor) -> sidik (mtime_ns, size).
# Bila berkas dibaca lagi dengan lingkup yang sama dan isinya TIDAK berubah, isi
# TIDAK dikirim ulang — AI disuruh memakai yang sudah ada di konteks. Sidik jari
# mtime+size: mengubah file (write/edit) pasti mengubahnya, jadi bacaan sesudah
# edit tetap segar. Sama sekali tidak membatasi: force=True atau lingkup berbeda
# (start_line/end_line/outline lain) selalu membaca ulang.
_BACA_CACHE: dict[tuple, tuple[int, int]] = {}
_MAKS_CACHE = 400


def _sidik_baca(target: Path) -> tuple[int, int] | None:
    try:
        st = target.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _syntax_check(target: Path) -> str | None:
    """Cek sintaks RINGAN (hanya parsing, tak menjalankan) file kode yang baru ditulis.

    Return pesan status ('✓ ...' / '✗ ...') atau None bila jenis file tak dicek.
    Ini yang membuat bagas-ai SELALU memverifikasi hasil ngoding-nya secara cepat.
    """
    if not config.AUTO_SYNTAX_CHECK:
        return None
    ext = target.suffix.lower()
    try:
        if ext in (".py", ".pyw"):
            src = target.read_text(encoding="utf-8", errors="replace")
            try:
                compile(src, str(target), "exec")
                return "OK: sintaks Python valid"
            except SyntaxError as e:
                return f"GAGAL: SyntaxError baris {e.lineno}: {e.msg}"
        if ext == ".json":
            src = target.read_text(encoding="utf-8", errors="replace")
            try:
                _json.loads(src)
                return "OK: JSON valid"
            except ValueError as e:
                return f"GAGAL: JSON invalid: {e}"
        if ext in (".js", ".mjs", ".cjs"):
            node = shutil.which("node")
            if not node:
                return None
            r = subprocess.run(
                [node, "--check", str(target)],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                return "OK: sintaks JS valid"
            err = (r.stderr or r.stdout).strip().splitlines()
            detail = err[-1][:200] if err else "error"
            return f"GAGAL: error sintaks JS: {detail}"
    except Exception:
        return None
    return None


# Penanda "sisanya biarkan seperti semula" — tanda PALING jelas bahwa yang
# dikirim cuma potongan, bukan isi lengkap. Model menulisnya dengan sangat
# beragam bentuk, jadi polanya dibuat longgar tapi tetap menuntut kata kuncinya.
_ELIPSIS_RE = re.compile(
    r"^[ \t]*(?://|#|/\*|<!--|--|;)?[ \t]*"
    r"(?:\.\.\.|…)?[ \t]*"
    r"(?:rest of|sisa|sisanya|selebihnya|remaining|unchanged|tetap sama|"
    r"tidak berubah|tak berubah|kode lain|other code|existing code|"
    r"keep existing|biarkan|dan seterusnya|dst\.?)"
    r"[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Baris yang isinya HANYA elipsis (mis. "    ...") — juga khas potongan.
_ELIPSIS_POLOS_RE = re.compile(r"^[ \t]*(?://|#|/\*|<!--)?[ \t]*(?:\.\.\.|…)[ \t]*"
                               r"(?:\*/|-->)?[ \t]*$", re.MULTILINE)


def _tolak_penimpaan_merusak(target: Path, baru: str) -> str | None:
    """Pesan penolakan bila `baru` tampak POTONGAN, bukan isi lengkap file.

    Ini bug paling merusak yang bisa terjadi di sini: write_file mengganti
    SELURUH isi file, jadi ketika model hanya mengirim bagian yang ia ubah —
    kebiasaan yang sangat lazim, apalagi bila ia berpikir "ini yang berubah" —
    seluruh sisa file lenyap tanpa satu pun tanda. Kerusakannya senyap: sintaks
    bisa saja tetap valid, dan baru ketahuan jauh kemudian saat ada yang hilang.

    Dua sinyal dipakai, dan keduanya sengaja dipilih yang berpresisi tinggi
    supaya penulisan ulang yang SAH tidak ikut terhalang:
      1. penanda elipsis ("// ... sisanya tetap ...") — praktis tak pernah
         muncul di berkas yang benar-benar lengkap;
      2. penyusutan drastis pada berkas yang memang panjang.
    Bila model memang sengaja memangkas, ia bisa mengulang dengan
    allow_shrink=true — jadi ini menghambat kecelakaan, bukan niat.
    """
    try:
        lama = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not lama.strip():
        return None

    n_lama = len(lama.splitlines())
    n_baru = len(baru.splitlines())

    for pola, sebab in ((_ELIPSIS_RE, "penanda 'sisanya tetap'"),
                        (_ELIPSIS_POLOS_RE, "baris berisi '...' saja")):
        m = pola.search(baru)
        if m:
            return (
                f"[DITOLAK] Isi yang dikirim tampaknya CUMA POTONGAN — ada "
                f"{sebab}: {m.group(0).strip()[:70]!r}\n\n"
                f"write_file MENGGANTI SELURUH isi file. Kalau ini ditulis, "
                f"{n_lama} baris yang ada sekarang akan hilang dan diganti "
                f"{n_baru} baris.\n\n"
                "Pakai edit_file untuk mengubah bagian tertentu:\n"
                '  {"tool": "edit_file", "args": {"path": "...", '
                '"old_text": "potongan lama PERSIS", "new_text": "penggantinya"}}\n\n'
                "Atau kirim ulang lewat write_file dengan isi file yang "
                "BENAR-BENAR LENGKAP (tanpa penanda elipsis)."
            )

    # Penyusutan drastis. Ambang dipilih longgar supaya penulisan ulang yang sah
    # (refactor besar, file digantikan total) tetap lolos: hanya berkas yang
    # memang panjang, dan hanya bila isinya menyusut lebih dari separuh.
    if n_lama >= 30 and n_baru < n_lama * 0.5:
        return (
            f"[DITOLAK] Isi baru jauh lebih pendek dari isi sekarang: "
            f"{n_lama} baris -> {n_baru} baris (susut "
            f"{100 - n_baru * 100 // max(n_lama, 1)}%).\n\n"
            "Ini pola khas 'hanya mengirim bagian yang diubah', dan write_file "
            "akan MENGHAPUS sisanya.\n\n"
            "- Mau mengubah sebagian? Pakai edit_file (old_text/new_text).\n"
            "- Memang sengaja memangkas file sebanyak itu? Baca dulu isi "
            "lengkapnya dengan read_file, lalu ulangi write_file dengan "
            "allow_shrink=true."
        )

    # Tulis-ulang NYARIS SAMA: sebagian besar baris isi baru identik dengan
    # yang lama -> ini "perubahan kecil ditulis ulang seluruh file", padahal
    # seharusnya edit_file. Ditolak bukan demi gaya: menulis ulang file panjang
    # dari ingatan model rawan menghilangkan detail secara senyap, plus output
    # sepanjang itu rawan TERPOTONG situs sebelum sampai (JSON putus). Batas
    # 4000 baris menjaga SequenceMatcher tetap murah; file lebih besar dari itu
    # sudah pasti tak layak ditulis ulang utuh, tapi biarkan penjaga lain yang
    # bicara daripada menghitung rasio mahal di sini.
    if 60 <= n_lama <= 4000 and n_baru >= n_lama * 0.5:
        sm = difflib.SequenceMatcher(None, lama.splitlines(), baru.splitlines())
        if sm.quick_ratio() >= 0.8 and sm.ratio() >= 0.8:
            return (
                f"[DITOLAK] Isi baru ~{int(sm.ratio() * 100)}% identik dengan isi "
                f"sekarang ({n_lama} baris) — ini perubahan SEBAGIAN yang ditulis "
                "ulang sebagai seluruh file.\n\n"
                "Pakai edit_file per bagian yang berubah (old_text/new_text, "
                "boleh beberapa blok sekaligus) — lebih aman, bisa ditinjau, dan "
                "tak mungkin terpotong di tengah.\n\n"
                "Kalau memang SENGAJA menulis ulang total dan isinya sudah "
                "lengkap, ulangi write_file dengan allow_shrink=true."
            )
    return None


def _safe_path(path: str) -> Path:
    """Resolusikan `path` & pastikan boleh diakses.

    Root yang diizinkan = root project + semua folder yang ditambahkan lewat
    add-dir. Path relatif diresolusi terhadap root project; path ABSOLUT dipakai
    apa adanya (untuk mengakses folder konteks). Mencegah path traversal keluar.

    Path di LUAR itu tidak langsung ditolak lagi: pengguna DITANYA lebih dulu
    (lihat permissions.py), dan jawabannya diingat per folder. Dijalankan
    dengan `--skip-permissions`, seluruh pemeriksaan ini dilewati.

    Ini satu-satunya gerbang path untuk SEMUA tool berkas — files.py, extras.py,
    dan media.py semuanya lewat sini — jadi izinnya cukup ditegakkan di satu
    tempat.
    """
    p = Path(path).expanduser()
    target = p.resolve() if p.is_absolute() else (ROOT / p).resolve()
    for root in workspace.allowed_roots():
        r = root.resolve()
        if target == r or r in target.parents:
            return target
    if permissions.minta_izin(target, tool_aktif()):
        return target
    raise ValueError(permissions.alasan_ditolak(path))


def _display(target: Path) -> str:
    """Tampilkan path relatif terhadap root pemiliknya (atau absolut bila di luar)."""
    for root in workspace.allowed_roots():
        try:
            return str(target.relative_to(root.resolve()))
        except ValueError:
            continue
    return str(target)


_READ_CAP = 40000   # batas karakter per pembacaan (hemat konteks percakapan)


_EKS_MD = {".md", ".markdown", ".mdx"}


def _kerangka(teks: str, mulai: int = 1, batas: int = 60,
              markdown: bool = False) -> str:
    """Daftar baris definisi (class/def/function/…) beserta nomor barisnya.

    Indentasi aslinya dipertahankan supaya sarangnya terbaca sekilas."""
    from .search import _peta_definisi   # impor lokal: hindari siklus impor
    peta = _peta_definisi(teks, mulai, batas, markdown)
    if not peta:
        return ""
    return "\n".join(f"  {no:>5}| {baris}" for no, baris in peta)


def _sisa_kerangka(text: str, sudah: int, markdown: bool = False) -> str:
    """Kerangka bagian berkas yang BELUM terbaca, untuk ditempel saat dipotong.

    Kenapa: bacaan yang terpotong dulu berakhir buntu — model tak tahu apa yang
    ada di bawah sana, jadi ia menebak (lalu old_text edit_file-nya meleset)
    atau membaca lagi berpotong-potong, dan tiap potongan = satu giliran browser
    belasan detik. Dengan kerangkanya ikut terkirim, ia langsung tahu bagian
    mana yang perlu dibaca berikutnya — sering cukup SATU bacaan susulan."""
    baris = text.split("\n")[sudah:]
    if not baris:
        return ""
    peta = _kerangka("\n".join(baris), sudah + 1, batas=30, markdown=markdown)
    if not peta:
        return ""
    return ("\n\n[kerangka bagian yang BELUM terbaca — biar kamu tahu ada apa "
            "di bawah sana]\n" + peta)


@tool
def read_file(path: str, start_line: int = 0, end_line: int = 0,
              line_numbers: bool = False, outline: bool = False,
              force: bool = False) -> str:
    """Baca isi sebuah file teks di dalam root project atau folder konteks (add-dir). Untuk file BESAR, mulailah dengan outline=true (peta semua definisi + nomor barisnya, hemat sekali), lalu baca bagian yang kamu butuh saja lewat start_line/end_line. Bila bacaan terpotong, kerangka sisanya ikut dikirim supaya kamu tak perlu menebak. NOMOR BARIS SELALU 1-BASED (baris pertama = baris 1) — saat pengguna menyebut "baris ke-N", verifikasi dengan start_line/end_line atau line_numbers=true, JANGAN menghitung sendiri dari 0.

    ANTI BACA ULANG: bila berkas ini sudah dibaca sesi ini dengan lingkup yang
    sama dan isinya TIDAK berubah, isi TIDAK dikirim ulang — kamu disuruh
    memakai yang sudah ada di konteks (worklog `kerja_terakhir` juga mencatatnya).
    Bacaan dengan lingkup berbeda (start_line/end_line/outline lain), atau
    sesudah file diubah, selalu membaca ulang.

    path: relatif terhadap root project, atau path ABSOLUT untuk file di folder
    konteks tambahan.
    start_line: baris pertama yang dibaca (1-based; 0/kosong = dari awal).
    end_line: baris terakhir yang dibaca (1-based; 0/kosong = sampai akhir).
    line_numbers: true = tiap baris diberi awalan "N| " (1-based) supaya kamu
        tahu pasti nomor tiap baris. Awalan itu BUKAN isi file — saat menyalin
        old_text untuk edit_file, buang awalannya.
    outline: true = jangan kirim isinya, kirim PETA-nya saja (tiap class/def/
        function/heading beserta nomor barisnya). Pakai ini lebih dulu untuk
        berkas panjang yang belum kamu kenal.
    force: true = baca ulang WALAU sudah dibaca & tak berubah. Hanya bila isi
        sebelumnya benar-benar hilang dari konteks percakapan.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"File tidak ditemukan: {path}"

    # Anti baca ulang: lingkup yang sama + isi tak berubah -> jangan kirim ulang.
    sidik = _sidik_baca(target)
    if not force and sidik is not None:
        kunci = (str(target), int(start_line or 0), int(end_line or 0),
                 bool(outline), bool(line_numbers))
        if _BACA_CACHE.get(kunci) == sidik:
            return (f"[SUDAH DIBACA] '{path}' tidak berubah sejak terakhir "
                    "dibaca sesi ini — pakai isi yang sudah ada di konteks, "
                    "JANGAN baca ulang.\nBila isi sebelumnya benar-benar hilang "
                    "dari konteks, panggil read_file dengan force=True, atau "
                    "minta lingkup berbeda (start_line/end_line).")
    text = target.read_text(encoding="utf-8", errors="replace")
    # Rekam sidik jari untuk pembacaan BERIKUTNYA (anti baca ulang).
    if sidik is not None:
        kunci = (str(target), int(start_line or 0), int(end_line or 0),
                 bool(outline), bool(line_numbers))
        if len(_BACA_CACHE) >= _MAKS_CACHE:
            _BACA_CACHE.clear()
        _BACA_CACHE[kunci] = sidik
    try:
        s, e = int(start_line or 0), int(end_line or 0)
    except (TypeError, ValueError):
        s = e = 0

    md = target.suffix.lower() in _EKS_MD

    if outline:
        total = len(text.splitlines())
        lines = text.splitlines(keepends=True)
        a = max(1, s or 1)
        z = total if e <= 0 else min(e, total)
        potongan = "".join(lines[a - 1:z]) if (s or e) else text
        peta = _kerangka(potongan, a, batas=400, markdown=md)
        if not peta:
            return (f"[peta {_display(target)} — {total} baris] tidak ada "
                    "definisi yang terdeteksi (mungkin berkas data/teks biasa). "
                    "Baca isinya langsung dengan read_file biasa.")
        n = peta.count("\n") + 1
        lingkup = f" baris {a}-{z}" if (s or e) else ""
        return (f"[peta {_display(target)}{lingkup} — {total} baris, "
                f"{n} definisi]\n{peta}\n\n"
                "Baca bagian yang kamu perlukan saja: "
                f'read_file("{path}", start_line=…, end_line=…)')

    def _bernomor(potongan: str, mulai: int) -> str:
        """Awalan 'N| ' per baris, N dari `mulai` (1-based)."""
        out = []
        for ofs, ln in enumerate(potongan.splitlines()):
            out.append(f"{mulai + ofs}| {ln}")
        return "\n".join(out) + ("\n" if potongan.endswith("\n") else "")

    if s or e:
        lines = text.splitlines(keepends=True)
        n = len(lines)
        s = max(1, s or 1)
        e = n if e <= 0 else min(e, n)
        if s > n:
            return (f"[error] start_line={s} melebihi jumlah baris file "
                    f"({n} baris).")
        if e < s:
            return f"[error] end_line ({e}) lebih kecil dari start_line ({s})."
        cuplikan = "".join(lines[s - 1:e])
        if line_numbers:
            cuplikan = _bernomor(cuplikan, s)
        if len(cuplikan) > _READ_CAP:
            potong = cuplikan[:_READ_CAP]
            batas = potong.rfind("\n")
            if batas > 0:
                potong = potong[:batas + 1]
            terbaca = s - 1 + potong.count("\n")
            cuplikan = (potong + f"... [dipotong: baru baris {s}-{terbaca} "
                        f"dari rentang yang kamu minta. Lanjutkan dengan "
                        f"read_file(path, start_line={terbaca + 1}, "
                        f"end_line={e})]"
                        + _sisa_kerangka(text, terbaca, md))
        # Header di baris tersendiri: tanpa line_numbers, isi di bawahnya tetap
        # byte apa adanya — aman disalin persis sebagai old_text edit_file.
        return f"[{_display(target)} baris {s}-{e} dari {n}]\n" + cuplikan

    if line_numbers:
        bernomor = _bernomor(text, 1)
        if len(bernomor) > _READ_CAP:
            potong = bernomor[:_READ_CAP]
            batas = potong.rfind("\n")
            if batas > 0:
                potong = potong[:batas + 1]
            n_tampil = potong.count("\n")
            total = len(text.splitlines())
            return (potong + f"... [dipotong: baris 1-{n_tampil} dari {total}. "
                    f"Lanjutkan dengan read_file(path, start_line="
                    f"{n_tampil + 1}, line_numbers=true)]"
                    + _sisa_kerangka(text, n_tampil, md))
        return bernomor

    if len(text) > _READ_CAP:
        # Potong DI BATAS BARIS + beri petunjuk lanjut yang bisa langsung
        # dieksekusi — dulu cuma "[dipotong]" buntu, model jadi menebak sisanya
        # dan old_text edit_file-nya meleset.
        potong = text[:_READ_CAP]
        batas = potong.rfind("\n")
        if batas > 0:
            potong = potong[:batas + 1]
        n_tampil = potong.count("\n")
        total = len(text.splitlines())
        return (potong + f"... [dipotong: baru baris 1-{n_tampil} dari {total}. "
                f"Lanjutkan dengan read_file(path, start_line={n_tampil + 1})]"
                + _sisa_kerangka(text, n_tampil, md))
    return text


@tool
def write_file(path: str, content: str, allow_shrink: bool = False) -> str:
    """Tulis file BARU, atau timpa file lama dengan isi LENGKAP-nya. Untuk mengubah sebagian isi file yang sudah ada, pakai edit_file — bukan ini.

    PERINGATAN: tool ini MENGGANTI SELURUH isi file. Bila kamu hanya mengirim
    bagian yang kamu ubah, seluruh sisanya HILANG.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    allow_shrink: set true HANYA bila kamu memang SENGAJA memangkas besar-
        besaran / menulis ulang total file yang sudah ada, DAN sudah membaca
        isi lengkapnya lebih dulu. Tanpa ini, isi yang tampak potongan atau
        nyaris sama dengan isi lama DITOLAK (harusnya edit_file).
    content: isi file.
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    if existed and not allow_shrink:
        tolak = _tolak_penimpaan_merusak(target, content)
        if tolak:
            return tolak
    _snapshot(target)   # pre-image untuk undo_changes (file baru: undo = hapus)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    verb = "Ditimpa" if existed else "Dibuat"
    msg = f"{verb}: {_display(target)} ({len(content)} karakter)."
    # SELALU cek sintaks hasil ngoding (cepat). Bila ada '✗', bagas-ai wajib
    # memperbaikinya — jangan anggap selesai.
    chk = _syntax_check(target)
    if chk:
        msg += f"\n[cek sintaks] {chk}"
        if chk.startswith("GAGAL"):
            msg += "  -> PERBAIKI dulu sebelum lanjut; jangan anggap selesai."
    return msg


@tool
def delete_file(path: str) -> str:
    """Hapus sebuah file di root project atau folder konteks (add-dir). Pertimbangkan matang-matang karena sulit dibatalkan.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"File tidak ditemukan: {path}"
    _snapshot(target)   # pre-image untuk undo_changes (bisa dikembalikan)
    target.unlink()
    return f"Dihapus: {_display(target)} — bisa dikembalikan dengan undo_changes."


@tool
def list_dir(path: str = ".") -> str:
    """Daftar isi sebuah folder di root project atau folder konteks (add-dir). Berguna untuk cek apa yang sudah ada sebelum membuat sesuatu.

    path: relatif terhadap root project (default: root project), atau path ABSOLUT
    untuk folder konteks tambahan.
    """
    target = _safe_path(path)
    if not target.is_dir():
        return f"Folder tidak ditemukan: {path}"
    entries = []
    for p in sorted(target.iterdir()):
        kind = "dir " if p.is_dir() else "file"
        size = p.stat().st_size if p.is_file() else "-"
        entries.append(f"[{kind}] {p.name} ({size})")
    return "\n".join(entries) if entries else "(kosong)"


# --- pencocokan old_text yang TAHAN SALAH KETIK RINGAN ---------------------
#
# Kenapa ini ada: di bagas-ai, SATU panggilan tool = satu bolak-balik ke situs
# AI web (belasan detik). Jadi edit_file yang gagal karena beda satu spasi
# bukan sekadar menjengkelkan — ia membakar giliran, dan model lalu membaca
# ulang berkasnya (satu giliran lagi) hanya untuk menyalin potongan yang
# sebenarnya sudah benar isinya.
#
# Empat penyebab gagal yang TERUKUR paling sering, dan semuanya sepele:
#   1. sisa awalan "12| " dari read_file(line_numbers=true);
#   2. spasi di UJUNG baris ikut/tidak ikut tersalin;
#   3. "\r\n" terbawa di dalam JSON usulan model;
#   4. INDENTASI meleset — model menuliskan ulang potongannya rata kiri, atau
#      dengan kedalaman yang berbeda dari berkas aslinya.
# Tiga yang pertama tak mengubah makna kode sama sekali, dan yang keempat bisa
# diperbaiki otomatis dengan menggeser indentasi penggantinya.
_AWALAN_NOMOR = re.compile(r"^[ \t]*\d+\|[ ]?")


def _baris_normal(baris: str, buang_indentasi: bool = False) -> str:
    """Bentuk baku sebuah baris untuk dibandingkan (bukan untuk ditulis)."""
    b = _AWALAN_NOMOR.sub("", baris.replace("\r", ""))
    b = b.rstrip()
    return b.lstrip() if buang_indentasi else b


def _indentasi(baris: str) -> str:
    return baris[: len(baris) - len(baris.lstrip())]


def _geser_indentasi(teks: str, dari: str, ke: str) -> str:
    """Geser indentasi tiap baris `teks` dari kedalaman `dari` ke `ke`.

    Dipakai saat potongan lama ketemu dengan toleransi indentasi: penggantinya
    ikut digeser supaya mendarat pada kedalaman yang benar di berkas."""
    if dari == ke:
        return teks
    keluar = []
    for baris in teks.split("\n"):
        if not baris.strip():
            keluar.append(baris)
            continue
        if dari and baris.startswith(dari):
            keluar.append(ke + baris[len(dari):])
        elif len(ke) >= len(dari):
            keluar.append(ke[len(dari):] + baris)
        else:
            # Berkas lebih dangkal dari yang dikirim model: kupas selisihnya
            # sebanyak yang memang berupa spasi/tab.
            n = len(dari) - len(ke)
            kupas = 0
            while kupas < n and kupas < len(baris) and baris[kupas] in " \t":
                kupas += 1
            keluar.append(baris[kupas:])
    return "\n".join(keluar)


def _selaraskan_indentasi(pengganti: str, baris_old: list[str],
                          blok_berkas: list[str]) -> str:
    """Samakan indentasi `pengganti` dengan blok yang digantikannya di berkas.

    Dua aturan, dan yang pertama menutup kasus paling lazim: model MENGETIK
    ULANG blok yang sama rata kiri lalu mengubah satu barisnya. Di situ jumlah
    barisnya sama persis, jadi tiap baris pengganti mewarisi indentasi baris
    berkas pada posisi yang sama — hasilnya kembali rapi sampai ke dalam-dalam,
    bukan cuma baris pertamanya.

    Kalau jumlah barisnya BERBEDA (model memang menambah/mengurangi baris),
    tak ada pasangan yang bisa diwarisi; yang dilakukan cuma menggeser seluruh
    blok sebanyak selisih indentasi baris pertama, dan indentasi relatif yang
    dikirim model dipertahankan apa adanya.

    Hanya dipakai pada jalur yang MEMANG sudah longgar-indentasi — bila
    potongan model cocok persis, indentasinya tak pernah disentuh."""
    baris_baru = pengganti.split("\n")
    if len(baris_baru) == len(blok_berkas):
        return "\n".join(
            (_indentasi(f) + b.lstrip()) if b.strip() else b
            for b, f in zip(baris_baru, blok_berkas))
    return _geser_indentasi(pengganti, _indentasi(baris_old[0]),
                            _indentasi(blok_berkas[0]))


def _cari_longgar(baris_isi: list[str], baris_old: list[str],
                  buang_indentasi: bool) -> list[int]:
    """Indeks baris awal setiap kemunculan `baris_old` di `baris_isi`."""
    n, m = len(baris_isi), len(baris_old)
    if m == 0 or m > n:
        return []
    target = [_baris_normal(b, buang_indentasi) for b in baris_old]
    sumber = [_baris_normal(b, buang_indentasi) for b in baris_isi]
    return [i for i in range(n - m + 1) if sumber[i:i + m] == target]


def _tebak_terdekat(baris_isi: list[str], baris_old: list[str]) -> str:
    """Petunjuk saat potongan tak ketemu: tunjukkan bagian berkas yang PALING
    MIRIP beserta bedanya, supaya model bisa membetulkan dalam SATU giliran
    alih-alih membaca ulang seluruh berkas."""
    m = len(baris_old)
    if not m or len(baris_isi) > 20000:
        return ""
    jarum = "\n".join(_baris_normal(b, True) for b in baris_old)
    terbaik, skor_terbaik = -1, 0.0
    for i in range(max(1, len(baris_isi) - m + 1)):
        jerami = "\n".join(_baris_normal(b, True)
                           for b in baris_isi[i:i + m])
        skor = difflib.SequenceMatcher(None, jarum, jerami).ratio()
        if skor > skor_terbaik:
            terbaik, skor_terbaik = i, skor
    if terbaik < 0 or skor_terbaik < 0.5:
        return ""
    beda = difflib.unified_diff(
        [b.rstrip() + "\n" for b in baris_old],
        [b.rstrip() + "\n" for b in baris_isi[terbaik:terbaik + m]],
        fromfile="yang kamu kirim", tofile=f"isi berkas baris {terbaik + 1}",
        n=0, lineterm="\n")
    cuplik = "".join(list(beda)[:24])
    return (f"\nYang PALING MIRIP ada di baris {terbaik + 1} "
            f"(kemiripan {skor_terbaik * 100:.0f}%). Bedanya:\n{cuplik}")


@tool
def edit_file(path: str, old_text: str, new_text: str, count: int = 1) -> str:
    """Ubah SEBAGIAN isi file: ganti potongan lama dengan yang baru (bedah presisi, tanpa menulis ulang seluruh file). Pencocokannya TOLERAN: beda spasi ekor, sisa awalan "12| ", dan INDENTASI yang meleset tetap ketemu — indentasi penggantinya disesuaikan otomatis. Kalau tetap tak ketemu, hasilnya menunjukkan bagian berkas yang paling mirip beserta bedanya.

    Pakai ini untuk perubahan kecil pada file besar — jauh lebih hemat daripada
    write_file yang menuntut seluruh isi file. Perubahannya tetap tampil sebagai
    diff berwarna di terminal pengguna.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    old_text: potongan yang mau diganti. Usahakan persis, tapi tak apa bila
        spasi ekor/indentasinya sedikit meleset.
    new_text: penggantinya. Kosongkan untuk MENGHAPUS potongan itu.
    count: berapa kemunculan diganti (default 1; -1 = semua).
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"[error] file tidak ditemukan: {_display(target)}"
    if not old_text:
        return "[error] old_text kosong — sebutkan potongan yang mau diganti."
    isi = target.read_text(encoding="utf-8", errors="replace")

    # --- tingkat 1: cocok PERSIS (jalur cepat, tak mengubah apa pun) ---
    n = isi.count(old_text)
    catatan = ""
    if n:
        if n > 1 and count == 1:
            baris_ke = [isi[:m.start()].count("\n") + 1
                        for m in re.finditer(re.escape(old_text), isi)]
            return (f"[error] potongan itu muncul {n} kali di "
                    f"{_display(target)} — di baris "
                    f"{', '.join(str(b) for b in baris_ke[:12])}"
                    f"{'…' if n > 12 else ''}. Perpanjang old_text agar unik "
                    "(tambahkan baris di atas/bawahnya), atau set count=-1 "
                    "bila memang semua kemunculan harus diganti.")
        baru = isi.replace(old_text, new_text, n if count == -1 else count)
        diganti = n if count == -1 else min(count, n)
    else:
        # --- tingkat 2 & 3: toleran (spasi ekor/awalan nomor, lalu indentasi) ---
        baris_isi = isi.split("\n")
        baris_old = old_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # Baris kosong di ujung potongan yang dikirim model sering artefak
        # perenderan situs, bukan bagian sungguhan dari kodenya.
        while baris_old and not baris_old[-1].strip():
            baris_old.pop()
        while baris_old and not baris_old[0].strip():
            baris_old.pop(0)
        if not baris_old:
            return "[error] old_text kosong — sebutkan potongan yang mau diganti."

        posisi = _cari_longgar(baris_isi, baris_old, False)
        longgar_indentasi = False
        if not posisi:
            posisi = _cari_longgar(baris_isi, baris_old, True)
            longgar_indentasi = bool(posisi)
        if not posisi:
            return (f"[error] potongan itu TIDAK ADA di {_display(target)}, "
                    "bahkan setelah mengabaikan beda spasi & indentasi."
                    + _tebak_terdekat(baris_isi, baris_old)
                    + "\nBaca ulang bagian itu (read_file dengan start_line/"
                      "end_line) lalu kirim potongannya sesuai isi berkas.")
        if len(posisi) > 1 and count == 1:
            return (f"[error] potongan itu cocok di {len(posisi)} tempat di "
                    f"{_display(target)} — baris "
                    f"{', '.join(str(i + 1) for i in posisi[:12])}. "
                    "Perpanjang old_text agar unik, atau set count=-1.")

        m = len(baris_old)
        dipakai = posisi if count == -1 else posisi[:max(1, count)]
        pengganti_dasar = new_text.replace("\r\n", "\n").replace("\r", "\n")
        # Diterapkan dari BELAKANG supaya indeks baris di depannya tak bergeser.
        for i in sorted(dipakai, reverse=True):
            ganti = pengganti_dasar
            if longgar_indentasi:
                ganti = _selaraskan_indentasi(
                    ganti, baris_old, baris_isi[i:i + m])
            baris_isi[i:i + m] = ganti.split("\n") if ganti else []
        baru = "\n".join(baris_isi)
        diganti = len(dipakai)
        catatan = ("\n[dicocokkan longgar] indentasi & spasi disamakan dengan "
                   "isi berkas" if longgar_indentasi else
                   "\n[dicocokkan longgar] beda spasi ekor/awalan nomor diabaikan")

    if baru == isi:
        return "[error] tidak ada yang berubah."
    _snapshot(target)   # pre-image untuk undo_changes
    target.write_text(baru, encoding="utf-8")
    # DIBACA ULANG untuk memastikan perubahannya BENAR-BENAR mendarat.
    # Dilaporkan pengguna: "2 suntingan diterapkan" padahal katanya tak ada yang
    # berubah. Tanpa pemeriksaan ini, laporan berhasil cuma bersandar pada
    # write_text yang tak melempar — dan itu tak sama dengan berkasnya berubah
    # (berkas terkunci program lain, ditulis ulang proses lain, disk penuh,
    # tersinkron balik oleh OneDrive/Dropbox).
    try:
        sesudah = target.read_text(encoding="utf-8", errors="replace")
    except OSError as ex:
        return (f"[GAGAL] {_display(target)} ditulis tapi tak bisa dibaca "
                f"ulang untuk diperiksa: {ex}")
    if sesudah != baru:
        return (f"[GAGAL] tulisan ke {target} TIDAK mendarat: sesudah ditulis, "
                f"isinya {len(sesudah)} karakter (seharusnya {len(baru)}). "
                "Berkasnya kemungkinan dikunci/ditimpa program lain — periksa "
                "editor yang sedang membukanya atau folder yang tersinkron "
                "(OneDrive/Dropbox).")
    # Path LENGKAP disebut, bukan yang relatif. Saat pengguna merasa "tak ada
    # yang teredit", pertanyaan pertamanya selalu "berkas yang MANA" — dan path
    # relatif tak pernah bisa menjawabnya.
    msg = (f"Diubah: {target} ({diganti} kemunculan, "
           f"{len(isi)} -> {len(baru)} karakter).{catatan}")
    chk = _syntax_check(target)
    if chk:
        msg += f"\n[cek sintaks] {chk}"
        if chk.startswith("GAGAL"):
            msg += "  -> PERBAIKI dulu sebelum lanjut; jangan anggap selesai."
    return msg


@tool
def append_file(path: str, content: str) -> str:
    """Tambahkan teks di AKHIR file (dibuat bila belum ada) tanpa menimpa isi lama.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    content: teks yang ditambahkan.
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lama = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    _snapshot(target)   # pre-image untuk undo_changes
    with target.open("a", encoding="utf-8") as fh:
        fh.write(content)
    msg = (f"Ditambahkan ke {_display(target)} "
           f"({len(content)} karakter; total {len(lama) + len(content)}).")
    chk = _syntax_check(target)
    if chk:
        msg += f"\n[cek sintaks] {chk}"
    return msg


@tool
def move_file(source: str, dest: str) -> str:
    """Pindahkan atau ganti nama file/folder di dalam area yang diizinkan.

    source: file/folder asal. dest: tujuan (path baru, termasuk nama barunya).
    """
    a, b = _safe_path(source), _safe_path(dest)
    if not a.exists():
        return f"[error] tidak ditemukan: {_display(a)}"
    if b.exists():
        return f"[error] tujuan sudah ada: {_display(b)} — hapus dulu bila memang mau ditimpa."
    if a.is_file():
        # pre-image untuk undo_changes: sumber dikembalikan, tujuan dihapus.
        # Pemindahan FOLDER tidak dicadangkan (undo tak mencakupnya).
        _snapshot(a)
        _snapshot(b)
    b.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(a), str(b))
    return f"Dipindahkan: {_display(a)} -> {_display(b)}"


@tool
def copy_file(source: str, dest: str) -> str:
    """Salin file (atau seluruh folder) di dalam area yang diizinkan.

    source: file/folder asal. dest: tujuan salinan.
    """
    a, b = _safe_path(source), _safe_path(dest)
    if not a.exists():
        return f"[error] tidak ditemukan: {_display(a)}"
    if b.exists():
        return f"[error] tujuan sudah ada: {_display(b)}"
    _snapshot(b)   # tujuan belum ada -> undo_changes menghapus salinannya
    b.parent.mkdir(parents=True, exist_ok=True)
    if a.is_dir():
        shutil.copytree(str(a), str(b))
    else:
        shutil.copy2(str(a), str(b))
    return f"Disalin: {_display(a)} -> {_display(b)}"


@tool
def make_dir(path: str) -> str:
    """Buat folder (beserta folder induknya bila belum ada)."""
    target = _safe_path(path)
    if target.is_file():
        return f"[error] sudah ada FILE dengan nama itu: {_display(target)}"
    sudah = target.is_dir()
    target.mkdir(parents=True, exist_ok=True)
    return (f"Folder {'sudah ada' if sudah else 'dibuat'}: {_display(target)}")


# --- pemangkas bolak-balik --------------------------------------------------
#
# Satu panggilan tool = satu bolak-balik ke situs AI web (belasan detik). Jadi
# "baca 3 berkas" yang wajar-wajar saja di agent ber-API menjadi mahal di sini:
# tiga giliran, tiga kali menunggu. Dua tool di bawah menyatukan pekerjaan yang
# memang sudah pasti akan dilakukan berurutan.
#
# Batasnya dipatok ketat & disengaja: situs AI web MEMOTONG pesan panjang
# ("Output stopped"), dan blok yang terpotong tak bisa dieksekusi sama sekali —
# jadi menggabungkan terlalu banyak justru menghanguskan seluruh giliran, bukan
# menghematnya.
_MAKS_BERKAS_SEKALI = 5
_MAKS_EDIT_SEKALI = 8


@tool
def read_files(paths: str, outline: bool = False) -> str:
    """Baca BEBERAPA berkas sekaligus dalam satu panggilan (maksimal 5). Pakai ini menggantikan read_file berulang saat kamu sudah tahu berkas mana saja yang perlu dilihat — tiga read_file terpisah berarti tiga kali menunggu, satu read_files cukup sekali.

    paths: daftar path dipisah koma atau baris baru, mis. "src/a.py, src/b.py".
    outline: true = kirim PETA tiap berkas (definisi + nomor baris) alih-alih
        isinya. Jauh lebih hemat untuk berkas besar; baca bagian yang kamu
        butuh sesudahnya lewat read_file(start_line/end_line).
    """
    daftar = [p.strip() for p in (paths or "").replace("\n", ",").split(",")
              if p.strip()]
    if not daftar:
        return "[error] paths kosong — contoh: read_files('src/a.py, src/b.py')"
    kelebihan = daftar[_MAKS_BERKAS_SEKALI:]
    daftar = daftar[:_MAKS_BERKAS_SEKALI]
    bagian = []
    for p in daftar:
        try:
            isi = read_file(p, outline=outline)
        except Exception as e:  # noqa: BLE001 - satu berkas gagal != semua gagal
            isi = f"[error] {e}"
        bagian.append(f"===== {p} =====\n{isi}")
    if kelebihan:
        bagian.append(f"[catatan] {len(kelebihan)} path lain TIDAK dibaca "
                      f"(batas {_MAKS_BERKAS_SEKALI} berkas per panggilan): "
                      + ", ".join(kelebihan))
    return "\n\n".join(bagian)


@tool
def edit_files(edits: list) -> str:
    """Terapkan BEBERAPA suntingan kecil sekaligus (maksimal 8), boleh lintas berkas — pengganti edit_file berulang saat kamu sudah tahu persis semua titik yang perlu diubah. Tiap suntingan tetap ditampilkan sebagai diff berwarna ke pengguna, sama seperti edit_file satuan.

    Berhenti di suntingan pertama yang GAGAL (mis. old_text tak ketemu) dan
    melaporkan mana yang sudah terpakai — supaya kamu tak salah kira semuanya
    berhasil. Yang sudah terlanjur diterapkan TIDAK dibatalkan; perbaiki sisanya
    lalu lanjutkan.

    edits: daftar objek {"path": "...", "old_text": "...", "new_text": "...",
        "count": 1}. `count` opsional (default 1; -1 = semua kemunculan).
        Untuk MENGHAPUS potongan, isi new_text dengan string kosong.
    """
    if not isinstance(edits, list) or not edits:
        return ('[error] edits harus berupa daftar, mis. [{"path": "a.py", '
                '"old_text": "x", "new_text": "y"}]')
    if len(edits) > _MAKS_EDIT_SEKALI:
        return (f"[error] {len(edits)} suntingan melewati batas "
                f"{_MAKS_EDIT_SEKALI} per panggilan — pesan sepanjang itu "
                "berisiko dipotong situs dan hangus. Pecah jadi beberapa "
                "panggilan.")
    hasil = []
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return f"[error] suntingan #{i} bukan objek JSON."
        path = str(e.get("path", "")).strip()
        old = e.get("old_text", "")
        new = e.get("new_text", "")
        if not path or not old:
            return f"[error] suntingan #{i}: 'path' dan 'old_text' wajib diisi."
        try:
            r = edit_file(path, str(old), str(new), int(e.get("count", 1) or 1))
        except Exception as ex:  # noqa: BLE001
            r = f"[error] {ex}"
        if r.startswith("[error]") or r.startswith("[GAGAL"):
            terpakai = "\n".join(hasil) or "(belum ada yang diterapkan)"
            return (f"BERHENTI di suntingan #{i} ({path}) karena gagal:\n{r}\n\n"
                    f"Yang SUDAH diterapkan sebelum ini:\n{terpakai}\n\n"
                    "Perbaiki suntingan yang gagal itu saja, lalu kirim sisanya "
                    "— jangan mengulang yang sudah berhasil.")
        hasil.append(f"#{i} {path}: {r}")
    return f"{len(hasil)} suntingan diterapkan:\n" + "\n".join(hasil)

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

from .. import config, workspace
from .base import tool
from .checkpoint import snapshot as _snapshot

ROOT = config.PROJECT_ROOT


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
    """Resolusikan `path` & pastikan berada di dalam salah satu root yang diizinkan.

    Root yang diizinkan = root project + semua folder yang ditambahkan lewat
    add-dir. Path relatif diresolusi terhadap root project; path ABSOLUT dipakai
    apa adanya (untuk mengakses folder konteks). Mencegah path traversal keluar.
    """
    p = Path(path).expanduser()
    target = p.resolve() if p.is_absolute() else (ROOT / p).resolve()
    for root in workspace.allowed_roots():
        r = root.resolve()
        if target == r or r in target.parents:
            return target
    raise ValueError(
        f"Akses ditolak: '{path}' di luar folder yang diizinkan "
        f"(root project + folder add-dir). Untuk folder konteks, pakai path absolut."
    )


def _display(target: Path) -> str:
    """Tampilkan path relatif terhadap root pemiliknya (atau absolut bila di luar)."""
    for root in workspace.allowed_roots():
        try:
            return str(target.relative_to(root.resolve()))
        except ValueError:
            continue
    return str(target)


_READ_CAP = 20000   # batas karakter per pembacaan (hemat konteks percakapan)


@tool
def read_file(path: str, start_line: int = 0, end_line: int = 0,
              line_numbers: bool = False) -> str:
    """Baca isi sebuah file teks di dalam root project atau folder konteks (add-dir). Untuk file BESAR, baca per bagian dengan start_line/end_line. NOMOR BARIS SELALU 1-BASED (baris pertama = baris 1) — saat pengguna menyebut "baris ke-N", verifikasi dengan start_line/end_line atau line_numbers=true, JANGAN menghitung sendiri dari 0.

    path: relatif terhadap root project, atau path ABSOLUT untuk file di folder
    konteks tambahan.
    start_line: baris pertama yang dibaca (1-based; 0/kosong = dari awal).
    end_line: baris terakhir yang dibaca (1-based; 0/kosong = sampai akhir).
    line_numbers: true = tiap baris diberi awalan "N| " (1-based) supaya kamu
        tahu pasti nomor tiap baris. Awalan itu BUKAN isi file — saat menyalin
        old_text untuk edit_file, buang awalannya.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"File tidak ditemukan: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        s, e = int(start_line or 0), int(end_line or 0)
    except (TypeError, ValueError):
        s = e = 0

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
            cuplikan = cuplikan[:_READ_CAP] + \
                "\n... [dipotong — persempit rentang barisnya]"
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
                    f"{n_tampil + 1}, line_numbers=true)]")
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
                f"Lanjutkan dengan read_file(path, start_line={n_tampil + 1})]")
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


@tool
def edit_file(path: str, old_text: str, new_text: str, count: int = 1) -> str:
    """Ubah SEBAGIAN isi file: ganti potongan teks lama dengan yang baru (bedah presisi, tanpa menulis ulang seluruh file).

    Pakai ini untuk perubahan kecil pada file besar — jauh lebih hemat daripada
    write_file yang menuntut seluruh isi file. Perubahannya tetap tampil sebagai
    diff berwarna di terminal pengguna.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    old_text: potongan PERSIS yang mau diganti (termasuk spasi/indentasi).
    new_text: penggantinya. Kosongkan untuk MENGHAPUS potongan itu.
    count: berapa kemunculan diganti (default 1; -1 = semua).
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"[error] file tidak ditemukan: {_display(target)}"
    if not old_text:
        return "[error] old_text kosong — sebutkan potongan yang mau diganti."
    isi = target.read_text(encoding="utf-8", errors="replace")
    n = isi.count(old_text)
    if n == 0:
        return (f"[error] potongan itu TIDAK ADA di {_display(target)}. "
                "Baca dulu filenya (read_file) lalu salin potongannya persis "
                "— termasuk spasi & indentasi.")
    # Ambigu itu berbahaya: mengganti kemunculan yang salah merusak file diam-diam.
    if n > 1 and count == 1:
        return (f"[error] potongan itu muncul {n} kali di {_display(target)}. "
                "Perpanjang old_text agar unik, atau set count=-1 bila memang "
                "semua kemunculan harus diganti.")
    baru = isi.replace(old_text, new_text, n if count == -1 else count)
    if baru == isi:
        return "[error] tidak ada yang berubah."
    _snapshot(target)   # pre-image untuk undo_changes
    target.write_text(baru, encoding="utf-8")
    diganti = n if count == -1 else min(count, n)
    msg = (f"Diubah: {_display(target)} ({diganti} kemunculan, "
           f"{len(isi)} -> {len(baru)} karakter).")
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

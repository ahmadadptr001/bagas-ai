"""Tool file: baca/tulis/daftar file di dalam ROOT PROJECT (folder terminal
saat `bagasai` dipanggil) DAN folder konteks tambahan (fitur add-dir).
Dibatasi agar tidak keluar dari folder-folder yang diizinkan."""
from __future__ import annotations

import json as _json
import re
import shutil
import subprocess
from pathlib import Path

from .. import config, workspace
from .base import tool

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


@tool
def read_file(path: str) -> str:
    """Baca isi sebuah file teks di dalam root project atau folder konteks (add-dir).

    path: relatif terhadap root project, atau path ABSOLUT untuk file di folder
    konteks tambahan.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"File tidak ditemukan: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > 20000:
        text = text[:20000] + "\n... [dipotong]"
    return text


@tool
def write_file(path: str, content: str, allow_shrink: bool = False) -> str:
    """Tulis file BARU, atau timpa file lama dengan isi LENGKAP-nya. Untuk mengubah sebagian isi file yang sudah ada, pakai edit_file — bukan ini.

    PERINGATAN: tool ini MENGGANTI SELURUH isi file. Bila kamu hanya mengirim
    bagian yang kamu ubah, seluruh sisanya HILANG.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    allow_shrink: set true HANYA bila kamu memang sengaja memangkas file secara
        besar-besaran dan sudah membaca isi lengkapnya lebih dulu.
    content: isi file.
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    if existed and not allow_shrink:
        tolak = _tolak_penimpaan_merusak(target, content)
        if tolak:
            return tolak
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
    target.unlink()
    return f"Dihapus: {_display(target)}"


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

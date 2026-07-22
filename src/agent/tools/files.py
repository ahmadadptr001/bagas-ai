"""Tool file: baca/tulis/daftar file di dalam ROOT PROJECT (folder terminal
saat `bagasai` dipanggil) DAN folder konteks tambahan (fitur add-dir).
Dibatasi agar tidak keluar dari folder-folder yang diizinkan."""
from __future__ import annotations

import json as _json
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
def write_file(path: str, content: str) -> str:
    """Tulis (atau timpa) sebuah file teks di root project atau folder konteks (add-dir). Sebelum menulis, pertimbangkan cek dulu apakah file sudah ada (read_file/list_dir) agar tidak menimpa tanpa perlu.

    path: relatif terhadap root project, atau path ABSOLUT untuk folder konteks.
    content: isi file.
    """
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
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

"""Tool konteks kerja & pengujian — ingat tanpa baca ulang, target tepat, uji dengan Python.

Tiga keluhan yang dijawab di sini:

  1. AI terus BUKA ULANG file untuk memahami apa yang barusan dilakukannya.
     Solusi: worklog sesi. Setiap tool yang dijalankan (lewat base.execute)
     DIREKAM otomatis ke sini — nama, argumen, hasil ringkas — dan AI bisa
     bertanya `kerja_terakhir()` untuk mengingat kembali SEMUA yang barusan
     dilakukan tanpa membaca ulang satu berkas pun. Catatan sadar ditulis
     dengan `catat_kerja(...)` untuk hal yang tak tertangkap otomatis
     (mis. "penyebabnya ternyata X", "keputusan: pakai Y").

  2. AI membuka berkas ASAL-ASALAN. Solusi: `sasaran(tugas)` menghitung berkas
     yang paling relevan untuk tugas itu (dari peta proyek + kata kunci),
     lengkap dengan alasan — sehingga AI tahu tepat berkas mana yang perlu
     disentuh sebelum menjelajah. Dipasangkan dengan cache baca di read_file
     (files.py): berkas yang sudah dibaca & tak berubah tidak dikirim ulang.

  3. AI jarang MENGUJI hal kompleks dengan Python. Solusi: `test_function`
     menjalankan SATU fungsi/kelas dari berkas proyek dengan argumen contoh
     di subproses terisolasi — cara tercepat membuktikan "kode ini benar"
     tanpa menulis harness dari nol. `run_python` tetap ada untuk eksperimen
     bebas.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .. import config, projectindex, workspace
from .base import tool
from .shell import _execute

# Kata tugas/isi umum yang BUKAN sasaran — dipakai sasaran() untuk mempertajam
# peringkat. Disalin dari mcp_server supaya konsisten (satu sumber kebenaran
# untuk perilaku "tepat sasaran").
from ..mcp_server import _kata_kunci  # noqa: E402  (sama paket, lazy-aman)

# --- worklog sesi ------------------------------------------------------------
# Entri: {"ts": float, "nama": str, "args": str, "hasil": str}
_WORKLOG: list[dict[str, Any]] = []
_MAKS_ENTRI = 200

# Berkas yang DISENTUH sesi ini (ditulis/diedit/dihapus/dipindah). Diisi
# catat_otomatis untuk tool mutasi; dipakai validate_project agar validasi
# menyasar perubahan NYATA walau `paths` tidak dikirim — validasi yang tahu
# "apa yang baru saja berubah" jauh lebih tepat daripada validasi seluruh
# proyek setiap giliran.
_SENTUH: list[str] = []
_MAKS_SENTUH = 60

# Tool yang benar-benar mengubah berkas (nama + argumen yang memuat path).
_TOOL_MUTASI_PATH = {
    "write_file": ("path",), "edit_file": ("path",),
    "append_file": ("path",), "delete_file": ("path",),
    "move_file": ("source", "dest"), "copy_file": ("source", "dest"),
    "replace_in_files": ("pattern",), "undo_changes": ("path",),
}


def _catat_sentuh(name: str, arguments: dict[str, Any]) -> None:
    """Catat path yang diubah tool mutasi ke _SENTUH (relatif ke root proyek)."""
    kunci = _TOOL_MUTASI_PATH.get(name)
    if not kunci or not isinstance(arguments, dict):
        return
    kandidat: list[str] = []
    for k in kunci:
        v = arguments.get(k)
        if isinstance(v, str) and v:
            kandidat.append(v)
    if name == "edit_files":
        for e in (arguments.get("edits") or []):
            if isinstance(e, dict) and isinstance(e.get("path"), str):
                kandidat.append(e["path"])
    for raw in kandidat:
        raw = raw.strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = config.PROJECT_ROOT / p
        try:
            rel = str(p.resolve().relative_to(config.PROJECT_ROOT))
        except ValueError:
            continue
        if rel not in _SENTUH:
            _SENTUH.append(rel)
    if len(_SENTUH) > _MAKS_SENTUH:
        del _SENTUH[:-_MAKS_SENTUH]


def file_tersentuh() -> list[str]:
    """Daftar berkas yang diubah sesi ini (relatif ke root), terbaru dulu."""
    return list(reversed(_SENTUH))

# Tool yang TIDAK direkam: tool ini sendiri (sibuk) & tool pembaca daftar.
_SKIP_REKAM = {
    "catat_kerja", "kerja_terakhir", "sasaran", "sasaran_aktif",
    "tutup_sasaran", "list_tools", "list_scripts",
}


def catat_otomatis(name: str, arguments: dict[str, Any], result: str) -> None:
    """Rekam satu tool call ke worklog — dipanggil base.execute tiap tool selesai."""
    if not name or name in _SKIP_REKAM:
        return
    try:
        args_s = json.dumps(arguments, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        args_s = ""
    if len(args_s) > 90:
        args_s = args_s[:90] + "…"
    baris = (result or "").strip().splitlines()
    hasil = baris[0][:140] if baris else "(tanpa output)"
    _WORKLOG.append({
        "ts": time.time(), "nama": name,
        "args": args_s, "hasil": hasil,
    })
    if len(_WORKLOG) > _MAKS_ENTRI:
        del _WORKLOG[:len(_WORKLOG) - _MAKS_ENTRI]
    _catat_sentuh(name, arguments)


@tool
def catat_kerja(catatan: str) -> str:
    """SIMPAN catatan singkat tentang apa yang barusan kamu lakukan / putuskan / temukan — supaya bisa diingat lagi lewat `kerja_terakhir` TANPA membaca ulang berkas.

    Pakai setelah langkah penting: apa yang diubah & kenapa, error yang ditemukan
    beserta penyebabnya, keputusan desain, hasil uji yang belum jelas dari output
    tool. Isinya singkat & padat — itu yang akan kamu baca lagi nanti.

    catatan: 1-3 kalimat ringkas, mis. 'perbaiki bug X di core.py: penyebabnya
        old_text tak ketemu karena spasi ganda; sudah diganti edit_file'.
    """
    catatan = (catatan or "").strip()
    if not catatan:
        return "[error] catatan kosong."
    _WORKLOG.append({"ts": time.time(), "nama": "catat_kerja",
                     "args": "", "hasil": catatan[:300]})
    if len(_WORKLOG) > _MAKS_ENTRI:
        del _WORKLOG[:len(_WORKLOG) - _MAKS_ENTRI]
    return "✓ Tercatat. Ingat lagi dengan kerja_terakhir() kapan pun — tanpa membaca ulang berkas."


@tool
def kerja_terakhir(berapa: int = 10) -> str:
    """INGAT KEMBALI apa yang barusan dilakukan sesi ini — TANPA membaca ulang berkas.

    Menampilkan N entri kerja terakhir: tiap tool yang dijalankan (read_file,
    edit_file, run_python, dsb) + catatan `catat_kerja`, paling baru di atas.
    Ini pengganti kebiasaan buruk "buka ulang file untuk mengingat konteks" —
    hasil tool sudah diringkas di sini.

    berapa: jumlah entri yang ditampilkan (default 10, maksimal 50).
    """
    n = max(1, min(int(berapa or 10), 50))
    entri = _WORKLOG[-n:][::-1]
    if not entri:
        return ("Worklog kosong — belum ada tool yang dijalankan sesi ini. "
                "Setelah mengerjakan sesuatu, catat dengan catat_kerja(...) "
                "supaya konteksnya tak perlu dibaca ulang.")
    baris = [f"Kerja terakhir ({len(entri)} entri, terbaru di atas):"]
    for e in entri:
        jam = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        nama = e["nama"]
        if nama == "catat_kerja":
            baris.append(f"  [{jam}] 📝 {e['hasil']}")
            continue
        arg = f" {e['args']}" if e.get("args") else ""
        baris.append(f"  [{jam}] {nama}{arg}")
        baris.append(f"          → {e['hasil']}")
    baris.append("")
    baris.append("Pakai ini sebagai konteks — jangan baca ulang berkas yang "
                 "hasilnya sudah terlihat di atas, kecuali isinya berubah.")
    return "\n".join(baris)


# --- sasaran: berkas yang tepat untuk satu tugas ------------------------------
@tool
def sasaran(tugas: str, max_hasil: int = 10) -> str:
    """TETAPKAN BERKAS SASARAN untuk satu tugas: daftar berkas paling relevan beserta ALASANNYA.

    Panggil di awal tugas, sebelum menjelajah dengan list_dir/read_file. Dihitung
    dari peta proyek (struktur + simbol kunci tiap berkas) dan kata kunci tugas —
    jadi kamu langsung tahu berkas mana yang perlu disentuh, bukan membuka
    asal-asalan. Untuk lokasi BARIS yang persis, lanjutkan dengan search_text.

    tugas: kalimat tugas pengguna, mis. 'perbaiki error saat menangani tool yang gagal'.
    max_hasil: batas jumlah berkas (default 10).
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
                        f"simbol '{sym.strip()[:50]}' memuat '{k}'")
                    break
        if s:
            skor[rel] = s

    urut = sorted(skor, key=skor.get, reverse=True)[:max(1, int(max_hasil))]
    baris = [f"Berkas sasaran untuk: {tugas.strip()[:140]}", ""]
    if not kunci:
        baris.append("(tidak ada kata kunci yang bisa dipakai — tugas terlalu "
                     "umum atau memakai kata tugas saja.)")
    if not urut:
        baris.append("Tidak ada berkas kode yang cocok. Coba sebut nama "
                     "berkas/fungsi yang lebih spesifik, atau pakai search_text "
                     "untuk menemukan lokasinya lebih dulu.")
        return "\n".join(baris)
    baris.append(f"{len(urut)} berkas relevan (kata kunci: "
                 f"{', '.join(kunci[:6])}):")
    for i, rel in enumerate(urut, 1):
        baris.append(f"{i}. {rel}")
        baris.append(f"   — {'; '.join(dict.fromkeys(alasan[rel]))}")
    baris.append("")
    baris.append("Baca hanya berkas di atas (read_file/read_files). Berkas lain "
                 "buka hanya bila benar-benar diperlukan.")
    return "\n".join(baris)


# --- test_function: uji fungsi spesifik dengan Python --------------------------
def _resolusi_aman(path: str) -> tuple[Path | None, str]:
    """(Path, rel) untuk berkas dalam jangkauan; (None, pesan) bila tidak."""
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
        return None, (f"di luar jangkauan (root: {config.PROJECT_ROOT} + "
                      "folder konteks).")
    try:
        rel = str(p.relative_to(config.PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        rel = str(p)
    return p, rel


def _cari_modul(p: Path) -> tuple[str | None, str | None]:
    """(sys_path_dir, nama_modul) agar file bisa diimpor SEBAGAI MODUL PACKAGE.

    File di dalam package (punya __init__.py di atasnya) TIDAK bisa diimpor
    dengan spec_from_file_location — relative import-nya ('from .x import y')
    meledak. Untuk itu dicari nama modulnya: naik dari folder file selama ada
    __init__.py, dan folder paling atas yang BUKAN package dijadikan sys.path.
    Return (None, None) bila file bukan bagian package -> pakai jalur file biasa.
    """
    if p.suffix.lower() != ".py":
        return None, None
    folder = p.resolve().parent
    nama = [p.stem]
    while (folder / "__init__.py").is_file():
        nama.insert(0, folder.name)
        folder = folder.parent
    if len(nama) < 2:
        return None, None  # bukan package (tak ada __init__.py di atasnya)
    return str(folder), ".".join(nama)


@tool
def test_function(path: str, symbol: str, args: list | str = "[]") -> str:
    """UJI satu fungsi/kelas dari berkas proyek dengan argumen contoh — jalankan dan lihat hasil/errornya.

    Cara tercepat membuktikan kode benar: impor fungsi itu dari berkasnya
    (bukan menyalin-menyusun ulang), panggil dengan argumen contoh, dan lihat
    hasilnya atau traceback-nya. Berjalan di subproses terisolasi dengan
    timeout — aman & tidak menggantung. File di dalam package (pakai relative
    import) diimpor lewat nama modulnya, jadi tetap jalan. Untuk eksperimen
    bebas, pakai run_python; tool ini khusus menguji SATU simbol yang sudah ada.

    path: berkas proyek yang memuat fungsi (mis. 'src/agent/tools/files.py').
    symbol: nama atribut tingkat modul, mis. 'hitung' atau 'Hitung.jumlah'.
        Untuk fungsi di dalam kelas, tulis 'Kelas.fungsi'.
    args: argumen posisi sebagai JSON array, mis. '[2, 3]' atau '["a", 1]'.
        Argumen kata kunci belum didukung — tulis urut posisi saja.
    """
    p, rel = _resolusi_aman(path)
    if p is None:
        return f"[error] {rel}"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", symbol or ""):
        return ("[error] symbol harus nama atribut valid, mis. 'hitung' atau "
                "'Hitung.jumlah'.")
    # Model web kerap mengirim args sebagai LIST langsung (bentuk paling alami)
    # alih-alih string JSON. Dulu skema hanya menerima str, lalu base.py
    # menggabungkan list jadi '2\n3' yang bukan JSON valid — uji yang sebenarnya
    # valid malah ditolak dengan error menyesatkan. Terima keduanya.
    if isinstance(args, (list, tuple)):
        args_list = list(args)
    else:
        try:
            args_list = json.loads(args or "[]")
        except ValueError as e:
            return f"[error] args bukan JSON valid: {e}"
    if not isinstance(args_list, list):
        return "[error] args harus JSON array, mis. '[2, 3]'."
    if not config.ALLOW_CODE_EXEC:
        return ("[dinonaktifkan] Eksekusi kode dimatikan. Set "
                "ALLOW_CODE_EXEC=true di .env untuk mengaktifkan.")

    bagian = symbol.split(".")
    # Impor lewat NAMA MODUL bila file di dalam package (relative import tetap
    # jalan); fallback ke impor file-biasa bila bukan package.
    sys_dir, nama_modul = _cari_modul(p)
    if nama_modul:
        kode = (
            "import importlib, sys, traceback\n"
            f"sys.path.insert(0, {sys_dir!r})\n"
            "try:\n"
            f"    mod = importlib.import_module({nama_modul!r})\n"
            f"    obj = mod\n"
            f"    for _b in {bagian!r}:\n"
            "        obj = getattr(obj, _b)\n"
            f"    hasil = obj(*{args_list!r})\n"
            "    print('HASIL:', repr(hasil))\n"
            "except Exception:\n"
            "    print('GAGAL:', traceback.format_exc())\n"
        )
    else:
        kode = (
            "import importlib.util, sys, traceback\n"
            f"sys.path.insert(0, {str(config.PROJECT_ROOT)!r})\n"
            "try:\n"
            f"    spec = importlib.util.spec_from_file_location('_mod_uji', "
            f"{str(p)!r})\n"
            "    mod = importlib.util.module_from_spec(spec)\n"
            "    spec.loader.exec_module(mod)\n"
            f"    obj = mod\n"
            f"    for _b in {bagian!r}:\n"
            "        obj = getattr(obj, _b)\n"
            f"    hasil = obj(*{args_list!r})\n"
            "    print('HASIL:', repr(hasil))\n"
            "except Exception:\n"
            "    print('GAGAL:', traceback.format_exc())\n"
        )
    rc, out, timed_out = _execute(
        [sys.executable, "-c", kode], shell=False,
        timeout=config.CODE_EXEC_TIMEOUT,
    )
    if timed_out:
        return (f"[GAGAL/timeout] uji {symbol} di {rel} melebihi "
                f"{config.CODE_EXEC_TIMEOUT} detik — dihentikan. JANGAN anggap "
                f"berhasil.\n{out[-2000:]}")
    out = (out or "").strip()
    if "HASIL:" in out:
        hasil = out.split("HASIL:", 1)[1].strip()
        return (f"✓ {symbol} ({rel}) dengan args {args_list!r} BERHASIL:\n"
                f"  hasil = {hasil}\n\n"
                "Kalau hasilnya sesuai harapan, tandai selesai; kalau tidak, "
                "perbaiki kodenya lalu uji lagi.")
    if "GAGAL:" in out:
        tb = out.split("GAGAL:", 1)[1].strip()
        # Traceback bisa panjang — tampilkan ekor (tempat error sebenarnya).
        ekor = tb.splitlines()[-8:]
        return (f"✗ {symbol} ({rel}) dengan args {args_list!r} GAGAL:\n"
                + "\n".join(ekor)
                + "\n\nBaca error di atas, perbaiki kodenya, lalu uji lagi — "
                  "jangan anggap selesai.")
    if rc != 0:
        return (f"[GAGAL] proses uji berhenti dengan exit_code={rc} "
                f"(kemungkinan error impor).\n{out[-2000:]}")
    return f"[error] output uji tak terbaca:\n{out[-1000:]}"

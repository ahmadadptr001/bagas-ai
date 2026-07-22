"""Tool tambahan yang memperluas jangkauan bagas-ai di luar baca/tulis berkas.

Tema yang menyatukan berkas ini: hal-hal yang SEBELUMNYA hanya bisa dilakukan
lewat run_command/run_python — padahal jalur itu kini menolak penulisan berkas,
dan tetap saja tak bisa ditinjau pengguna. Masing-masing di sini punya alasan
tersendiri untuk ada, bukan sekadar pembungkus perintah shell:

  attach_file      AI web tak bisa membaca berkas di laptop; satu-satunya cara
                   ia benar-benar MELIHAT isinya adalah diunggah ke percakapan.
  http_request     fetch_url cuma GET; memanggil API butuh POST/PUT + header.
  replace_in_files ganti nama simbol di banyak berkas sekaligus — dengan
                   edit_file itu satu giliran per berkas, mahal sekali.
  diff_files       membandingkan dua berkas tanpa memuat keduanya ke percakapan.
  zip_create/extract  paket rilis & arsip, tanpa bergantung tar/7z ada di PATH.
  notify           tugas panjang selesai saat pengguna tak menonton terminal.
  clipboard_*      jembatan ke aplikasi lain yang tak punya berkas perantara.
  open_path        tunjukkan hasil ke pengguna (buka di aplikasi bawaannya).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from .. import config
from .base import tool
from .files import _display, _safe_path
from .screen import IMAGE_MARK


@tool
def attach_file(path: str, note: str = "") -> str:
    """UNGGAH sebuah berkas dari laptop ke percakapan AI ini supaya isinya benar-benar bisa dilihat/dibaca AI.

    Pakai untuk gambar, PDF, dokumen, CSV, atau berkas apa pun yang lebih mudah
    DILIHAT daripada dibacakan. Untuk berkas teks/kode biasa, read_file lebih
    hemat karena tak perlu unggahan.

    path: berkas di root project / folder konteks.
    note: keterangan singkat (opsional) yang menyertai lampiran.
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"[error] berkas tidak ditemukan: {_display(target)}"
    ukuran = target.stat().st_size
    if ukuran > 25 * 1024 * 1024:
        return (f"[error] {_display(target)} terlalu besar ({ukuran/1048576:.1f} MB). "
                "Batas unggah 25 MB — potong dulu atau kirim bagian yang relevan.")
    if ukuran == 0:
        return f"[error] {_display(target)} kosong (0 byte)."
    ket = f" — {note}" if note else ""
    # Penanda ini dibaca core (_take_image_marks) lalu berkasnya DILAMPIRKAN ke
    # pesan berikutnya; jadi lampirannya tiba bersama giliran ini, bukan sebagai
    # teks yang harus AI bayangkan sendiri.
    return (f"Melampirkan {_display(target)} ({ukuran} byte){ket}.\n"
            f"{IMAGE_MARK} {target}")


@tool
def http_request(url: str, method: str = "GET", body: str = "",
                 headers: str = "", max_chars: int = 6000) -> str:
    """Panggil HTTP apa pun (GET/POST/PUT/PATCH/DELETE) — untuk memakai API, bukan sekadar membaca halaman.

    url: alamat lengkap http/https.
    method: GET (default), POST, PUT, PATCH, DELETE, HEAD.
    body: isi permintaan; bila berupa JSON, Content-Type diisi otomatis.
    headers: header tambahan, format "Nama: nilai" dipisah baris baru.
    max_chars: batas panjang balasan yang dikembalikan.
    """
    import json as _json
    import requests

    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return "[error] url harus diawali http:// atau https://"
    m = (method or "GET").strip().upper()
    if m not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        return f"[error] method '{method}' tidak dikenal."

    h = {"User-Agent": "Mozilla/5.0 (bagas-ai)"}
    for baris in (headers or "").splitlines():
        if ":" in baris:
            k, v = baris.split(":", 1)
            h[k.strip()] = v.strip()
    data = None
    if body:
        data = body.encode("utf-8")
        if "content-type" not in {k.lower() for k in h}:
            try:
                _json.loads(body)
                h["Content-Type"] = "application/json"
            except ValueError:
                h["Content-Type"] = "text/plain; charset=utf-8"
    try:
        r = requests.request(m, u, data=data, headers=h, timeout=45,
                             allow_redirects=True)
    except requests.RequestException as e:
        return f"[error] {m} {u} gagal: {e}"

    kepala = f"HTTP {r.status_code} {r.reason} — {len(r.content)} byte"
    ctype = (r.headers.get("content-type") or "").split(";")[0]
    if ctype:
        kepala += f" ({ctype})"
    try:
        teks = r.text
    except Exception:  # noqa: BLE001
        return kepala + "\n(balasan biner — tak ditampilkan)"
    if len(teks) > max_chars:
        teks = teks[:max_chars] + f"\n… dipotong di {max_chars} karakter"
    return kepala + "\n\n" + teks.strip()


@tool
def replace_in_files(find: str, replace: str, pattern: str = "*",
                     dry_run: bool = True, max_files: int = 50) -> str:
    """Ganti teks di BANYAK berkas sekaligus — mis. mengganti nama fungsi/variabel di seluruh proyek.

    Dengan edit_file, pekerjaan ini butuh satu giliran per berkas. Ini
    menyelesaikannya sekali jalan.

    PENTING: jalankan dulu dengan dry_run=true untuk melihat DAFTAR berkas &
    jumlah kecocokan, baru ulangi dengan dry_run=false bila sudah yakin.

    find: teks persis yang dicari. replace: penggantinya.
    pattern: batasi jenis berkas, mis. '*.py' (default semua).
    dry_run: true = cuma laporkan, tak mengubah apa pun (default).
    max_files: pengaman, batas jumlah berkas yang disentuh.
    """
    from .search import _telusuri, _rel
    import fnmatch

    if not find:
        return "[error] `find` kosong."
    pat = (pattern or "*").strip().replace("\\", "/")
    hanya_nama = "/" not in pat
    kena: list[tuple[Path, int]] = []
    for p in _telusuri(config.PROJECT_ROOT):
        rel = _rel(p)
        if not (fnmatch.fnmatch(p.name, pat) if hanya_nama
                else fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, "**/" + pat)):
            continue
        try:
            isi = p.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        n = isi.count(find)
        if n:
            kena.append((p, n))
    if not kena:
        return f"Tidak ada berkas yang memuat '{find}' (pattern: {pattern})."
    if len(kena) > max_files:
        return (f"[error] {len(kena)} berkas memuat teks itu, melebihi batas "
                f"max_files={max_files}. Persempit `pattern`, atau naikkan "
                "max_files bila memang disengaja.")

    total = sum(n for _, n in kena)
    daftar = "\n".join(f"  {_rel(p)} ({n}x)" for p, n in kena)
    if dry_run:
        return (f"[SIMULASI] {total} kecocokan di {len(kena)} berkas — "
                f"BELUM ada yang diubah:\n{daftar}\n\n"
                "Ulangi dengan dry_run=false bila sudah benar.")
    for p, _ in kena:
        isi = p.read_text(encoding="utf-8", errors="strict")
        p.write_text(isi.replace(find, replace), encoding="utf-8")
    return f"Diganti {total} kecocokan di {len(kena)} berkas:\n{daftar}"


@tool
def diff_files(path_a: str, path_b: str, max_lines: int = 120) -> str:
    """Bandingkan dua berkas dan tampilkan bedanya saja (format unified diff).

    Jauh lebih hemat daripada membaca kedua berkas penuh ke percakapan.
    """
    import difflib

    a, b = _safe_path(path_a), _safe_path(path_b)
    for p in (a, b):
        if not p.is_file():
            return f"[error] bukan berkas: {_display(p)}"
    ta = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    tb = b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    beda = list(difflib.unified_diff(ta, tb, _display(a), _display(b), n=3))
    if not beda:
        return f"Isi {_display(a)} dan {_display(b)} SAMA PERSIS."
    potong = beda[:max_lines]
    keluar = "".join(potong).rstrip()
    if len(beda) > max_lines:
        keluar += f"\n… dipotong ({len(beda) - max_lines} baris diff lagi)"
    return keluar


@tool
def zip_create(paths: str, output_path: str) -> str:
    """Bungkus berkas/folder jadi satu arsip .zip (mis. untuk rilis atau kirim).

    paths: daftar berkas/folder dipisah koma.
    output_path: nama berkas .zip hasilnya.
    """
    keluar = _safe_path(output_path)
    sumber = [s.strip() for s in (paths or "").split(",") if s.strip()]
    if not sumber:
        return "[error] `paths` kosong."
    keluar.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(keluar, "w", zipfile.ZIP_DEFLATED) as z:
        for s in sumber:
            p = _safe_path(s)
            if not p.exists():
                return f"[error] tidak ditemukan: {_display(p)}"
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(p.parent))
                        n += 1
            else:
                z.write(p, p.name)
                n += 1
    return f"Arsip dibuat: {_display(keluar)} ({n} berkas, {keluar.stat().st_size} byte)."


@tool
def zip_extract(archive_path: str, dest_dir: str = ".") -> str:
    """Bongkar arsip .zip ke sebuah folder."""
    arsip = _safe_path(archive_path)
    tujuan = _safe_path(dest_dir)
    if not arsip.is_file():
        return f"[error] arsip tidak ditemukan: {_display(arsip)}"
    if not zipfile.is_zipfile(arsip):
        return f"[error] {_display(arsip)} bukan berkas zip yang sah."
    tujuan.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(arsip) as z:
        # Cegah zip-slip: entri ber-path '../' bisa menulis di luar folder tujuan.
        akar = tujuan.resolve()
        for nama in z.namelist():
            if (akar / nama).resolve().is_relative_to(akar) is False:
                return f"[error] arsip memuat jalur berbahaya: {nama}"
        z.extractall(tujuan)
        n = len(z.namelist())
    return f"Dibongkar: {n} entri -> {_display(tujuan)}"


@tool
def notify(message: str, title: str = "bagas-ai") -> str:
    """Kirim NOTIFIKASI desktop ke pengguna — untuk memberi tahu tugas panjang sudah selesai.

    Berguna saat pekerjaan makan waktu lama dan pengguna tak sedang menonton
    terminal.
    """
    msg = (message or "").strip()
    if not msg:
        return "[error] pesan kosong."
    try:
        if os.name == "nt":
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, "
                "Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::"
                "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]"
                "::ToastText02);"
                f"$t.GetElementsByTagName('text')[0].AppendChild("
                f"$t.CreateTextNode({title!r})) > $null;"
                f"$t.GetElementsByTagName('text')[1].AppendChild("
                f"$t.CreateTextNode({msg!r})) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::"
                "CreateToastNotifier('bagas-ai').Show("
                "[Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=20)
        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {msg!r} with title {title!r}'],
                capture_output=True, timeout=20)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, msg],
                           capture_output=True, timeout=20)
        else:
            return f"[info] notifikasi desktop tak tersedia di sistem ini: {msg}"
    except Exception as e:  # noqa: BLE001
        return f"[info] notifikasi gagal ({e}); pesan: {msg}"
    return f"Notifikasi dikirim: {title} — {msg}"


@tool
def clipboard_write(text: str) -> str:
    """Salin teks ke CLIPBOARD pengguna, siap ditempel ke aplikasi lain."""
    if not text:
        return "[error] teks kosong."
    try:
        if os.name == "nt":
            p = subprocess.run(["clip"], input=text.encode("utf-16-le"),
                               capture_output=True, timeout=20)
        elif sys.platform == "darwin":
            p = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                               capture_output=True, timeout=20)
        elif shutil.which("xclip"):
            p = subprocess.run(["xclip", "-selection", "clipboard"],
                               input=text.encode("utf-8"), capture_output=True,
                               timeout=20)
        else:
            return "[info] clipboard tak tersedia di sistem ini."
        if p.returncode != 0:
            return "[error] gagal menyalin ke clipboard."
    except Exception as e:  # noqa: BLE001
        return f"[error] clipboard gagal: {e}"
    return f"Disalin ke clipboard ({len(text)} karakter)."


@tool
def clipboard_read(max_chars: int = 4000) -> str:
    """Baca isi CLIPBOARD pengguna — mis. saat ia bilang 'ini yang barusan aku salin'."""
    try:
        if os.name == "nt":
            p = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, timeout=20)
        elif sys.platform == "darwin":
            p = subprocess.run(["pbpaste"], capture_output=True, text=True,
                               timeout=20)
        elif shutil.which("xclip"):
            p = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                               capture_output=True, text=True, timeout=20)
        else:
            return "[info] clipboard tak tersedia di sistem ini."
    except Exception as e:  # noqa: BLE001
        return f"[error] clipboard gagal: {e}"
    teks = (p.stdout or "").strip()
    if not teks:
        return "Clipboard kosong."
    if len(teks) > max_chars:
        teks = teks[:max_chars] + f"\n… dipotong di {max_chars} karakter"
    return teks


@tool
def open_path(path: str) -> str:
    """Buka berkas/folder/URL di aplikasi bawaan pengguna — untuk MENUNJUKKAN hasil.

    Mis. sesudah membuat halaman web, buka index.html-nya di browser supaya
    pengguna langsung melihat hasilnya tanpa mencari sendiri.
    """
    t = (path or "").strip()
    if not t:
        return "[error] path kosong."
    if t.lower().startswith(("http://", "https://")):
        sasaran = t
    else:
        p = _safe_path(t)
        if not p.exists():
            return f"[error] tidak ditemukan: {_display(p)}"
        sasaran = str(p)
    try:
        if os.name == "nt":
            os.startfile(sasaran)  # noqa: S606 - memang perilaku yang diminta
        elif sys.platform == "darwin":
            subprocess.run(["open", sasaran], capture_output=True, timeout=20)
        else:
            subprocess.run(["xdg-open", sasaran], capture_output=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        return f"[error] gagal membuka {sasaran}: {e}"
    return f"Dibuka di aplikasi bawaan: {sasaran}"


@tool
def analyze_image(path: str, question: str = "") -> str:
    """Analisis sebuah GAMBAR: lampirkan ke percakapan supaya kamu sendiri bisa MELIHATNYA, lalu jawab pertanyaannya.

    Dulu ada model vision terpisah yang mendeskripsikan gambar lewat API. Itu
    dihapus — dan hasilnya justru lebih baik: kamu (model AI web ini) memang
    bisa melihat gambar, dan melihatnya DI DALAM percakapan ini berarti kamu
    bisa mengaitkannya dengan tugas yang sedang berjalan lalu menindaklanjuti
    dengan tool, bukan sekadar mendeskripsikan sekali pakai.

    Pasangkan dengan take_screenshot untuk memeriksa tampilan aplikasi/web yang
    sedang dikerjakan.

    path: berkas gambar (png/jpg/webp/gif/bmp).
    question: yang ingin diketahui, mis. "kenapa tombolnya bertumpuk?".
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"[error] gambar tidak ditemukan: {_display(target)}"
    if target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif",
                                     ".bmp"):
        return (f"[error] {_display(target)} bukan berkas gambar. Untuk berkas "
                "lain pakai attach_file.")
    ukuran = target.stat().st_size
    if ukuran == 0:
        return f"[error] {_display(target)} kosong (0 byte)."
    q = (question or "").strip() or "Jelaskan isi gambar ini secara detail."
    return (f"Gambar {_display(target)} ({ukuran} byte) DILAMPIRKAN ke pesan "
            f"berikutnya — kamu akan melihatnya sendiri.\n"
            f"Pertanyaan: {q}\n"
            f"{IMAGE_MARK} {target}")


@tool
def download_file(url: str, dest_path: str, max_mb: int = 50) -> str:
    """UNDUH berkas dari internet ke proyek — gambar, sprite, suara, font, ikon, dataset, apa pun.

    Inilah cara mengambil ASET. web_search hanya memberi cuplikan hasil
    pencarian dan fetch_url hanya mengembalikan TEKS, jadi keduanya tak bisa
    membawa berkas biner masuk ke proyek. Alur yang biasa: web_search untuk
    menemukan sumbernya -> download_file untuk mengambilnya.

    JANGAN cuma memberi tautan supaya pengguna mengunduh sendiri — unduh di
    sini, lalu langsung pakai berkasnya di kode.

    url: alamat berkasnya (http/https, tautan LANGSUNG ke berkas).
    dest_path: tujuan simpan di proyek, mis. 'assets/img/mario.png'.
    max_mb: batas ukuran (default 50 MB).
    """
    import requests

    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return "[error] url harus diawali http:// atau https://"
    target = _safe_path(dest_path)
    if target.is_dir():
        return f"[error] {_display(target)} itu folder — sebutkan nama berkasnya."
    batas = max(1, int(max_mb)) * 1024 * 1024
    try:
        r = requests.get(u, timeout=90, stream=True, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (bagas-ai)"})
    except requests.RequestException as e:
        return f"[error] gagal mengunduh {u}: {e}"
    if r.status_code >= 400:
        return (f"[error] HTTP {r.status_code} dari {u} — tautannya mungkin "
                "bukan tautan langsung ke berkas, atau butuh login.")

    ctype = (r.headers.get("content-type") or "").split(";")[0]
    # Banyak "tautan gambar" hasil pencarian sebenarnya halaman HTML. Menyimpannya
    # sebagai .png menghasilkan berkas rusak yang baru ketahuan jauh kemudian.
    if "html" in ctype and target.suffix.lower() not in (".html", ".htm", ""):
        return (f"[error] {u} mengembalikan HALAMAN HTML, bukan berkas "
                f"{target.suffix}. Itu tautan halaman, bukan tautan langsung ke "
                "berkas. Buka halamannya dengan fetch_url untuk menemukan URL "
                "berkas aslinya.")

    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with target.open("wb") as fh:
            for bagian in r.iter_content(chunk_size=65536):
                if not bagian:
                    continue
                total += len(bagian)
                if total > batas:
                    fh.close()
                    target.unlink(missing_ok=True)
                    return (f"[error] berkas melebihi {max_mb} MB — dibatalkan "
                            "supaya proyek tak membengkak. Naikkan max_mb bila "
                            "memang disengaja.")
                fh.write(bagian)
    except (OSError, requests.RequestException) as e:
        target.unlink(missing_ok=True)
        return f"[error] gagal menyimpan: {e}"
    if total == 0:
        target.unlink(missing_ok=True)
        return f"[error] {u} mengembalikan berkas kosong."
    return (f"Diunduh: {_display(target)} ({total} byte"
            + (f", {ctype}" if ctype else "") + ").")

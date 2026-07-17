"""Tool kelola VIDEO & AUDIO via ffmpeg/ffprobe — profesional & aman.

Semua perintah dijalankan NON-INTERAKTIF (-nostdin, -y), dibatasi timeout yang
membunuh seluruh pohon proses, dan path divalidasi agar tetap di dalam folder
yang diizinkan. Bila ffmpeg belum terpasang, tool memberi instruksi pemasangan
yang jelas, bukan error misterius.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil

from .. import config
from .base import tool
from .files import _display, _safe_path
from .shell import _execute, _guard

# Transcode video bisa lama — beri anggaran lebih longgar dari perintah biasa.
MEDIA_TIMEOUT = int(os.getenv("MEDIA_TIMEOUT", "600"))


def _need(binary: str) -> str | None:
    """None bila `binary` tersedia; kalau tidak, pesan cara memasangnya."""
    if shutil.which(binary):
        return None
    return (
        f"[error] '{binary}' tidak ditemukan di PATH. Pasang dulu:\n"
        f"  - Windows : winget install Gyan.FFmpeg   (lalu buka terminal baru)\n"
        f"  - macOS   : brew install ffmpeg\n"
        f"  - Linux   : sudo apt install ffmpeg\n"
        f"ffmpeg & ffprobe terpasang bersamaan dalam satu paket."
    )


def _run_ffmpeg(args: list[str]) -> str:
    """Jalankan ffmpeg dengan flag aman + timeout; kembalikan laporan untuk LLM."""
    blocked = _guard()
    if blocked:
        return blocked
    missing = _need("ffmpeg")
    if missing:
        return missing
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           *args]
    rc, out, timed_out = _execute(cmd, shell=False, timeout=MEDIA_TIMEOUT)
    if timed_out:
        return (f"[GAGAL/timeout] ffmpeg melewati {MEDIA_TIMEOUT} detik dan "
                f"dihentikan. Output:\n{out.strip()}")
    if rc != 0:
        return f"[GAGAL] ffmpeg exit={rc}.\n{out.strip() or '(tanpa pesan)'}"
    return out


def _size(p) -> str:
    try:
        n = p.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return "?"


def _done(out_path, extra: str = "") -> str:
    return (f"[OK] tersimpan: {_display(out_path)} ({_size(out_path)})"
            + (f" — {extra}" if extra else ""))


def _secs(t: str) -> float:
    """'HH:MM:SS(.ms)' / 'MM:SS' / '90' -> detik. ValueError bila tak valid."""
    parts = [float(p) for p in str(t).strip().split(":")]
    if not 1 <= len(parts) <= 3:
        raise ValueError(t)
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


@tool
def media_info(path: str) -> str:
    """Baca info video/audio via ffprobe: durasi, codec, resolusi, fps, bitrate, channel audio. Selalu pakai ini dulu sebelum mengolah file media.

    path: file video/audio yang diperiksa (relatif thd root project).
    """
    missing = _need("ffprobe")
    if missing:
        return missing
    src = _safe_path(path)
    if not src.is_file():
        return f"[error] file tidak ditemukan: {path}"
    rc, out, timed_out = _execute(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(src)],
        shell=False, timeout=60,
    )
    if timed_out or rc != 0:
        return f"[GAGAL] ffprobe exit={rc}.\n{out.strip()}"
    try:
        data = json.loads(out)
    except ValueError:
        return f"[GAGAL] output ffprobe tak terbaca:\n{out[:2000]}"
    fmt = data.get("format", {})
    lines = [f"File   : {_display(src)} ({_size(src)})",
             f"Format : {fmt.get('format_long_name', fmt.get('format_name', '?'))}"]
    try:
        dur = float(fmt.get("duration", 0))
        lines.append(f"Durasi : {int(dur // 60)}m {dur % 60:.1f}s ({dur:.2f}s)")
    except (TypeError, ValueError):
        pass
    if fmt.get("bit_rate"):
        lines.append(f"Bitrate: {int(fmt['bit_rate']) // 1000} kb/s")
    for s in data.get("streams", []):
        kind = s.get("codec_type", "?")
        if kind == "video":
            fps = s.get("avg_frame_rate", "0/1")
            try:
                a, b = fps.split("/")
                fps = f"{float(a) / float(b):.2f}" if float(b) else "?"
            except (ValueError, ZeroDivisionError):
                fps = "?"
            lines.append(
                f"Video  : {s.get('codec_name')} {s.get('width')}x{s.get('height')}"
                f" @ {fps} fps")
        elif kind == "audio":
            lines.append(
                f"Audio  : {s.get('codec_name')} {s.get('sample_rate')} Hz, "
                f"{s.get('channels')} channel")
        elif kind == "subtitle":
            lines.append(f"Subtitle: {s.get('codec_name')} "
                         f"({s.get('tags', {}).get('language', '?')})")
    return "\n".join(lines)


@tool
def media_convert(input_path: str, output_path: str, options: str = "") -> str:
    """Konversi/olah video-audio dengan ffmpeg; format keluaran mengikuti ekstensi output (mp4, mkv, mp3, wav, gif, webm, dll).

    input_path: file sumber.
    output_path: file hasil — ekstensinya menentukan format.
    options: (opsional) argumen ffmpeg ekstra di antara input & output, mis.
        "-vf scale=1280:-2", "-an" (buang audio), "-r 30", "-b:a 192k".
    """
    src = _safe_path(input_path)
    dst = _safe_path(output_path)
    if not src.is_file():
        return f"[error] file tidak ditemukan: {input_path}"
    extra = shlex.split(options) if options else []
    out = _run_ffmpeg(["-i", str(src), *extra, str(dst)])
    return out if out.startswith("[") and not out.startswith("[OK") else _done(dst)


@tool
def media_trim(input_path: str, output_path: str, start: str,
               end: str = "", reencode: bool = False) -> str:
    """Potong bagian video/audio dari `start` sampai `end` (format waktu HH:MM:SS atau detik).

    input_path: file sumber.
    output_path: file hasil potongan.
    start: waktu mulai, mis. "00:01:30" atau "90".
    end: (opsional) waktu akhir; kosong = sampai habis.
    reencode: False = cepat tanpa encode ulang (potongan mengikuti keyframe,
        bisa meleset ~1-2 detik); True = akurat ke frame tapi lebih lama.
    """
    src = _safe_path(input_path)
    dst = _safe_path(output_path)
    if not src.is_file():
        return f"[error] file tidak ditemukan: {input_path}"
    args = ["-ss", start, "-i", str(src)]
    if end:
        # -ss di depan -i me-RESET timestamp, jadi `end` mentah akan salah arti
        # ("N detik setelah titik mulai"). Kirim DURASI eksplisit (-t) agar
        # potongan benar-benar start..end seperti yang diminta pengguna.
        try:
            dur = _secs(end) - _secs(start)
        except ValueError:
            return f"[error] format waktu tak valid: start='{start}' end='{end}'"
        if dur <= 0:
            return f"[error] end ({end}) harus setelah start ({start})."
        args += ["-t", f"{dur:.3f}"]
    if not reencode:
        args += ["-c", "copy"]
    args.append(str(dst))
    out = _run_ffmpeg(args)
    mode = "akurat (encode ulang)" if reencode else "cepat (tanpa encode ulang)"
    return out if out.startswith("[") and not out.startswith("[OK") else _done(dst, mode)


@tool
def media_extract_audio(input_path: str, output_path: str) -> str:
    """Ambil audio saja dari sebuah video; format hasil mengikuti ekstensi output (mp3/wav/m4a/opus).

    input_path: file video sumber.
    output_path: file audio hasil, mis. "musik.mp3".
    """
    src = _safe_path(input_path)
    dst = _safe_path(output_path)
    if not src.is_file():
        return f"[error] file tidak ditemukan: {input_path}"
    out = _run_ffmpeg(["-i", str(src), "-vn", str(dst)])
    return out if out.startswith("[") and not out.startswith("[OK") else _done(dst)


@tool
def media_compress(input_path: str, output_path: str, crf: int = 28,
                   height: int = 0) -> str:
    """Kompres video agar ukurannya jauh lebih kecil (H.264 + CRF), opsional turunkan resolusi.

    input_path: file video sumber.
    output_path: file hasil (sebaiknya .mp4).
    crf: kualitas 18(bagus/besar) - 32(kecil); default 28 seimbang.
    height: (opsional) tinggi target, mis. 720 -> di-scale ke 720p; 0 = tetap.
    """
    src = _safe_path(input_path)
    dst = _safe_path(output_path)
    if not src.is_file():
        return f"[error] file tidak ditemukan: {input_path}"
    args = ["-i", str(src), "-c:v", "libx264", "-crf", str(crf),
            "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k"]
    if height:
        args += ["-vf", f"scale=-2:{height}"]
    args.append(str(dst))
    out = _run_ffmpeg(args)
    if out.startswith("[") and not out.startswith("[OK"):
        return out
    return _done(dst, f"dari {_size(src)}")


@tool
def media_thumbnail(input_path: str, output_path: str,
                    at: str = "00:00:01") -> str:
    """Ambil satu frame video sebagai gambar (jpg/png).

    input_path: file video sumber.
    output_path: file gambar hasil, mis. "cover.jpg".
    at: waktu frame yang diambil (HH:MM:SS atau detik), default detik ke-1.
    """
    src = _safe_path(input_path)
    dst = _safe_path(output_path)
    if not src.is_file():
        return f"[error] file tidak ditemukan: {input_path}"
    out = _run_ffmpeg(["-ss", at, "-i", str(src), "-frames:v", "1", str(dst)])
    return out if out.startswith("[") and not out.startswith("[OK") else _done(dst)


@tool
def media_merge(input_paths: list, output_path: str) -> str:
    """Gabungkan beberapa video/audio berurutan menjadi satu file (concat).

    input_paths: daftar file sumber SEJENIS (codec/resolusi sama -> cepat tanpa
        encode ulang; kalau beda, tool otomatis encode ulang agar tetap berhasil).
    output_path: file hasil gabungan.
    """
    if not input_paths or len(input_paths) < 2:
        return "[error] beri minimal 2 file untuk digabung."
    srcs = []
    for p in input_paths:
        sp = _safe_path(str(p))
        if not sp.is_file():
            return f"[error] file tidak ditemukan: {p}"
        srcs.append(sp)
    dst = _safe_path(output_path)
    # Jalur cepat: concat demuxer tanpa encode ulang (butuh codec seragam).
    lst = dst.with_name(dst.stem + ".concat.txt")
    lst.write_text(
        "\n".join("file '" + str(s).replace("'", "'\\''") + "'" for s in srcs),
        encoding="utf-8",
    )
    try:
        out = _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst),
                           "-c", "copy", str(dst)])
        if not (out.startswith("[") and not out.startswith("[OK")):
            return _done(dst, f"{len(srcs)} file digabung tanpa encode ulang")
        # Jalur aman: encode ulang via filter concat (codec/resolusi boleh beda).
        args = []
        for s in srcs:
            args += ["-i", str(s)]
        n = len(srcs)
        flt = ("".join(f"[{i}:v][{i}:a]" for i in range(n))
               + f"concat=n={n}:v=1:a=1[v][a]")
        out2 = _run_ffmpeg([*args, "-filter_complex", flt,
                            "-map", "[v]", "-map", "[a]", str(dst)])
        if out2.startswith("[") and not out2.startswith("[OK"):
            return f"{out}\n--- encode ulang juga gagal ---\n{out2}"
        return _done(dst, f"{len(srcs)} file digabung (encode ulang)")
    finally:
        try:
            lst.unlink()
        except OSError:
            pass

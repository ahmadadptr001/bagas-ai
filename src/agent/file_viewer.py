"""Pratinjau lokal hanya-baca untuk file nonkode di sidebar."""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from rich.style import Style
from rich.text import Text

from .editor import MAX_EDITOR_BYTES, HTML_SUFFIXES, EditorFileError, _project_file, open_html_file

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico", ".avif"}
OFFICE = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
MEDIA = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".mp4", ".webm", ".mov", ".mkv", ".avi"}
EXTERNAL = IMAGES | OFFICE | MEDIA | HTML_SUFFIXES | {".pdf", ".doc", ".xls", ".ppt", ".rtf"}
MAX_TEXT = 120_000


@dataclass
class FilePreview:
    content: Text
    kind: str
    detail: str = ""
    page: int = 0
    pages: int = 1
    visual: bool = False
    has_text: bool = True


def file_kind(path: Path | str) -> str:
    target = _project_file(path)
    suffix = target.suffix.lower()
    with target.open("rb") as stream:
        head = stream.read(8192)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if suffix in IMAGES:
        return "image"
    if suffix in OFFICE:
        return "office"
    if suffix in MEDIA:
        return "media"
    if suffix in {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz"}:
        return "archive"
    if b"\x00" not in head:
        try:
            # Bagian terakhir boleh terpotong di tengah karakter UTF-8.
            import codecs
            codecs.getincrementaldecoder("utf-8-sig")().decode(head, final=False)
            return "text" if target.stat().st_size <= MAX_EDITOR_BYTES else "document"
        except UnicodeDecodeError:
            pass
    return "binary"


def image_cells(image, width: int, height: int, background: str = "#10121c") -> Text:
    """Dua piksel per sel, proporsi asli, transparansi mengikuti latar editor."""
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(image).convert("RGBA")
    width, height = max(1, min(width, 160)), max(1, min(height, 48))
    ratio = min(width / image.width, (height * 2) / image.height)
    size = max(1, round(image.width * ratio)), max(1, round(image.height * ratio))
    image = image.resize(size, Image.Resampling.LANCZOS)
    base = Image.new("RGBA", size, background)
    base.alpha_composite(image)
    pixels = base.convert("RGB")
    result = Text(no_wrap=True)
    for y in range(0, size[1], 2):
        if y:
            result.append("\n")
        for x in range(size[0]):
            top = "#%02x%02x%02x" % pixels.getpixel((x, y))
            bottom = "#%02x%02x%02x" % pixels.getpixel((x, min(y + 1, size[1] - 1)))
            result.append("▀", Style(color=top, bgcolor=bottom))
    return result


def _office_text(path: Path) -> str:
    import re
    chunks, total = [], 0
    with zipfile.ZipFile(path) as archive:
        def wanted(name):
            return name in {"word/document.xml", "xl/sharedStrings.xml", "content.xml"} or bool(
                re.fullmatch(r"ppt/slides/slide\d+\.xml|xl/worksheets/sheet\d+\.xml", name))

        files = sorted((i for i in archive.infolist() if wanted(i.filename)),
                       key=lambda i: [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", i.filename)])
        shared = []
        for info in files[:100]:
            total += info.file_size
            if info.file_size > 8_000_000 or total > 20_000_000:
                chunks.append("… Dokumen terlalu besar; buka di aplikasi asal untuk isi lengkap.")
                break
            root = ElementTree.fromstring(archive.read(info))
            if info.filename == "xl/sharedStrings.xml":
                shared = ["".join(node.itertext()) for node in root]
                continue
            if info.filename.startswith("xl/worksheets/"):
                rows = []
                for row in root.iter():
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    cells = []
                    for cell in row:
                        value = "".join(cell.itertext())
                        if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                        cells.append(value)
                    rows.append("  |  ".join(cells))
                chunks.append(info.filename.rsplit("/", 1)[-1] + "\n" + "\n".join(rows))
            else:
                # Pertahankan batas paragraf, jangan mengulang semua ancestor.
                paras = ["".join(n.itertext()) for n in root.iter()
                         if n.tag.rsplit("}", 1)[-1] in {"p", "h"}]
                chunks.append("\n".join(paras))
            if sum(map(len, chunks)) > MAX_TEXT:
                break
    return "\n\n".join(chunks)[:MAX_TEXT] or "Dokumen tidak berisi teks yang bisa dipratinjau."


def render_file(path: Path | str, width: int, height: int, page: int = 0,
                visual: bool = False, background: str = "#10121c") -> FilePreview:
    target = _project_file(path)
    kind = file_kind(target)
    size = target.stat().st_size
    detail = f"{size / 1024:.1f} KB" if size < 1_048_576 else f"{size / 1_048_576:.1f} MB"
    if kind == "image":
        from PIL import Image
        with Image.open(target) as image:
            dimensions = f"{image.width} × {image.height}"
            content = image_cells(image, width, height, background)
        return FilePreview(content, kind, f"{dimensions} · {detail}", visual=True)
    if kind == "pdf":
        try:
            import pymupdf
        except ImportError as exc:
            raise EditorFileError("Pratinjau PDF memerlukan PyMuPDF. Jalankan installer untuk melengkapinya.") from exc
        from PIL import Image
        with pymupdf.open(target) as document:
            if document.needs_pass:
                return FilePreview(Text("PDF dilindungi kata sandi. Gunakan Buka untuk membaca di aplikasi PDF."), kind, detail, has_text=False)
            pages = len(document)
            if not pages:
                return FilePreview(Text("PDF tidak berisi halaman."), kind, detail)
            page = max(0, min(page, pages - 1))
            sheet = document[page]
            text = sheet.get_text(sort=True).strip()
            visual = visual or not text
            if visual:
                scale = min(max(1, width) / max(1, sheet.rect.width),
                            max(2, height * 2) / max(1, sheet.rect.height), 2)
                pix = sheet.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                content = image_cells(Image.frombytes("RGB", (pix.width, pix.height), pix.samples),
                                      width, height, background)
            else:
                content = Text(text[:MAX_TEXT])
            return FilePreview(content, kind, detail, page, pages, visual, bool(text))
    if kind == "office":
        return FilePreview(Text(_office_text(target)), kind, detail)
    if kind == "archive":
        rows = []
        if zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as archive:
                entries = archive.infolist()
                for item in entries[:300]:
                    rows.append(f"{item.filename}  · {item.file_size:,} B")
                count = len(entries)
        elif tarfile.is_tarfile(target):
            count = 0
            with tarfile.open(target) as archive:
                for item in archive:
                    count += 1
                    if count > 300:
                        break
                    rows.append(f"{item.name}  · {item.size:,} B")
        else:
            return FilePreview(Text("Format arsip belum mendukung pratinjau."), kind, detail)
        if count > 300:
            rows.append("… Menampilkan 300 entri pertama.")
        return FilePreview(Text("\n".join(rows) or "Arsip kosong."), kind, detail)
    if kind == "media":
        import av
        rows = []
        with av.open(str(target)) as media:
            if media.duration is not None:
                seconds = int(media.duration / av.time_base)
                rows.append(f"Durasi  {seconds // 60}:{seconds % 60:02d}")
            for stream in media.streams:
                codec = stream.codec_context
                if stream.type == "video":
                    rows.append(f"Video   {codec.name} · {codec.width} × {codec.height}")
                elif stream.type == "audio":
                    rows.append(f"Audio   {codec.name} · {codec.sample_rate:,} Hz")
        rows.append("\nGunakan Buka untuk memutar di aplikasi media.")
        return FilePreview(Text("\n".join(rows)), kind, detail)
    with target.open("rb") as stream:
        sample = stream.read(MAX_TEXT * 2)
    for encoding in ("utf-8-sig", "utf-16" if sample.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"):
        try:
            import codecs
            text = codecs.getincrementaldecoder(encoding)().decode(sample, final=size <= len(sample))
            if "\x00" not in text:
                suffix = "\n\n… Pratinjau dipotong." if size > len(sample) or len(text) > MAX_TEXT else ""
                return FilePreview(Text(text[:MAX_TEXT] + suffix), "document", detail)
        except UnicodeError:
            pass
    rows = ["Berkas biner · pratinjau 256 byte pertama", ""]
    for offset in range(0, min(256, len(sample)), 16):
        part = sample[offset:offset + 16]
        rows.append(f"{offset:08x}  {part.hex(' '):47}  " + "".join(chr(c) if 32 <= c < 127 else "." for c in part))
    return FilePreview(Text("\n".join(rows)), "binary", detail)


def can_open_external(path: Path) -> bool:
    return path.suffix.lower() in EXTERNAL


def open_external(path: Path | str) -> None:
    target = _project_file(path)
    if not can_open_external(target):
        raise EditorFileError("Format ini hanya tersedia sebagai pratinjau.")
    if target.suffix.lower() in HTML_SUFFIXES:
        open_html_file(target)
        return
    if sys.platform == "win32":
        os.startfile(str(target))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

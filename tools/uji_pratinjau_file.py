"""Regresi syntax/editor dan pratinjau file nyata, tanpa mengubah proyek pengguna."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock, patch
import wave
import zipfile

temporary = tempfile.TemporaryDirectory(prefix="uji_pratinjau_", ignore_cleanup_errors=True)
root = Path(temporary.name)
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
os.environ.pop("NO_COLOR", None)
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))

from PIL import Image, ImageDraw
import pymupdf
from rich.text import Text
from textual.widgets import TextArea
from agent.editor import EditorFileError
from agent.file_viewer import file_kind, image_cells, render_file, open_external
from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import BukaBerkas, FileEditorScreen, FilePreviewScreen, MessageList
from agent.ui import tema
from agent.ui.syntax import code_highlights


def fixtures():
    image = Image.new("RGBA", (480, 280), "#17223b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((40, 40, 440, 240), radius=24, fill="#4677ed")
    draw.ellipse((180, 70, 300, 190), fill="#ffd27c")
    image.save(root / "foto.png")
    image.convert("RGB").save(root / "foto.jpg")
    with pymupdf.open() as pdf:
        for n in (1, 2):
            page = pdf.new_page(width=400, height=500)
            page.insert_text((40, 65), f"Halaman {n}", fontsize=24)
            page.insert_text((40, 100), "Pratinjau dokumen di terminal", fontsize=12)
            page.draw_rect((40, 130, 360, 260), color=(.2, .4, .9), fill=(.2, .4, .9))
        pdf.save(root / "dokumen.pdf")
    with pymupdf.open() as pdf:
        pdf.new_page().insert_image(pymupdf.Rect(0, 0, 480, 280), filename=str(root / "foto.png"))
        pdf.save(root / "scan.pdf")
    with zipfile.ZipFile(root / "arsip.zip", "w") as archive:
        archive.writestr("../../jangan-ekstrak.txt", "tidak boleh diekstrak")
        archive.writestr("src/main.py", "print(1)")
    with zipfile.ZipFile(root / "surat.docx", "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="word"><w:body><w:p><w:r><w:t>Surat uji</w:t></w:r></w:p><w:p><w:r><w:t>Paragraf kedua</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(root / "data.xlsx", "w") as archive:
        archive.writestr("xl/sharedStrings.xml", '<sst><si><t>Nama</t></si></sst>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c></row></sheetData></worksheet>')
    with wave.open(str(root / "suara.wav"), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", ""))
        audio.writeframes(b"\x00\x00" * 1600)
    (root / "program.exe").write_bytes(b"MZ\x00\x01\x02" * 100)
    (root / "besar.txt").write_text("dokumen panjang\n" * 160000, encoding="utf-8")
    (root / "install.ps1").write_text('$Nama = "Bagas"\n# Komentar\nif ($Nama) { Write-Output 42 }\n', encoding="utf-8")


def backend():
    samples = {
        "contoh.py": '# héllo\ndef fungsi():\n    return "teks" + str(42)\n',
        "contoh.ps1": '$Nama = "Bagas"\n# komentar\nif ($true) { Write-Output 42 }',
        "contoh.tsx": 'export function App() { return <div id="main">Hi</div>; }',
        "contoh.json": '{"judul": "isi", "angka": 42, "aktif": true}',
        "contoh.html": '<div class="utama"><style>body { color: red }</style></div>',
        "contoh.css": '.utama { color: #ff0000; width: 42px; }',
        "multiline.py": 'nilai = """你好\nbaris kedua\nselesai"""\nreturn 42',
    }
    for name, text in samples.items():
        spans = code_highlights(text, Path(name))
        colors = {str(style.color) for row in spans.values() for _, _, style in row}
        assert len(colors) >= 3, (name, colors)
        for row, items in spans.items():
            for start, end, style in items:
                assert 0 <= start < end <= len(text.split("\n")[row]), (name, row, start, end)
        assert spans != code_highlights(text, Path(name), light=True)
    assert code_highlights("hello", Path("unknown.extension")) == {}
    assert code_highlights(samples["contoh.py"], Path("c.py"), cancelled=lambda: True) == {}
    transparent = image_cells(Image.new("RGBA", (2, 2), (0, 0, 0, 0)), 1, 1, "#112233")
    assert transparent.spans[0].style.color.get_truecolor() == (17, 34, 51)
    assert transparent.spans[0].style.bgcolor.get_truecolor() == (17, 34, 51)
    for name in ("foto.png", "foto.jpg"):
        result = render_file(root / name, 30, 10)
        assert result.kind == "image" and result.visual
        assert len(result.content.plain.splitlines()) <= 10
        assert max(map(len, result.content.plain.splitlines())) <= 30
    pdf = render_file(root / "dokumen.pdf", 70, 20, page=1)
    assert pdf.page == 1 and pdf.pages == 2 and "Halaman 2" in pdf.content.plain
    assert render_file(root / "dokumen.pdf", 30, 10, visual=True).visual
    scan = render_file(root / "scan.pdf", 30, 10)
    assert scan.visual and not scan.has_text
    assert "src/main.py" in render_file(root / "arsip.zip", 70, 20).content.plain
    assert not (root / "jangan-ekstrak.txt").exists()
    assert render_file(root / "surat.docx", 70, 20).content.plain == "Surat uji\nParagraf kedua"
    assert "Nama  |  42" in render_file(root / "data.xlsx", 70, 20).content.plain
    assert "16000" in render_file(root / "suara.wav", 70, 20).content.plain.replace(",", "")
    assert file_kind(root / "besar.txt") == "document"
    assert "dipotong" in render_file(root / "besar.txt", 70, 20).content.plain
    assert render_file(root / "program.exe", 70, 20).kind == "binary"
    with patch("agent.file_viewer.os.startfile", create=True) as launch:
        open_external(root / "foto.png")
        launch.assert_called_once_with(str(root / "foto.png"))
        try:
            open_external(root / "program.exe")
        except EditorFileError:
            pass
        else:
            raise AssertionError("File executable tidak boleh dijalankan dari pratinjau")
    with tempfile.TemporaryDirectory() as outside:
        path = Path(outside) / "luar.png"
        Image.new("RGB", (2, 2)).save(path)
        for reader in (file_kind, lambda p: render_file(p, 30, 10), open_external):
            try:
                reader(path)
            except EditorFileError:
                pass
            else:
                raise AssertionError("Path di luar proyek tidak ditolak")
    print("OK: warna 7 bahasa, Unicode/multiline, transparansi, PDF, Office, arsip, media, batas dan path guard")


async def wait(pilot, condition):
    for _ in range(150):
        await pilot.pause(.03)
        if condition():
            return
    raise AssertionError("Pekerjaan pratinjau/syntax tidak selesai")


async def ui():
    agent = MagicMock()
    agent.model_spec = MagicMock(label="Model Uji", is_web=False)
    agent.memory.messages = []
    app = BagasAIApp(agent=agent)
    async with app.run_test(size=(120, 36)) as pilot:
        app.post_message(BukaBerkas(root / "install.ps1"))
        await wait(pilot, lambda: isinstance(app.screen, FileEditorScreen))
        editor = app.screen
        area = editor.query_one("#file-editor-area", TextArea)
        await wait(pilot, lambda: bool(area._syntax_spans))
        assert len(area.get_line(0).spans) >= 2
        assert any(s.style and s.style.color and s.style.color.name == "#ce9178"
                   for s in area.render_line(0)), "Warna belum mencapai baris terminal"
        if "--preview" in sys.argv:
            output = repo / "screenshots/ui-reference"
            output.mkdir(parents=True, exist_ok=True)
            app.save_screenshot("syntax-dark.svg", path=str(output))
        area.load_text('function Test {\n    return "Baru"\n}\n')
        await wait(pilot, lambda: 1 in area._syntax_spans and area.get_line(1).plain == '    return "Baru"')
        assert len(area.get_line(1).spans) >= 2
        await pilot.press("escape", "escape")
        for size in ((120, 36), (80, 24), (40, 20)):
            await pilot.resize_terminal(*size)
            app.post_message(BukaBerkas(root / "dokumen.pdf"))
            await wait(pilot, lambda: isinstance(app.screen, FilePreviewScreen) and app.screen.preview is not None)
            preview = app.screen
            body = preview.query_one("#file-preview-body", MessageList)
            assert body.size.height >= 5
            for selector in ("#file-preview-close", "#file-preview-prev", "#file-preview-next", "#file-preview-mode", "#file-preview-open"):
                control = preview.query_one(selector)
                assert 0 <= control.region.x < control.region.right <= size[0], (size, selector, control.region)
                assert control.region.bottom <= size[1]
                assert control.content_region.height == 1
            await pilot.click("#file-preview-next")
            await wait(pilot, lambda: preview.preview.page == 1)
            assert "Halaman 2" in preview.preview.content.plain
            assert len(body._items) == 1, "Halaman lama tertinggal di riwayat"
            await pilot.press("left")
            await wait(pilot, lambda: preview.preview.page == 0)
            await pilot.click("#file-preview-mode")
            await wait(pilot, lambda: preview.preview.visual)
            await pilot.press("ctrl+c", "ctrl+l")
            assert app.screen is preview
            if "--preview" in sys.argv and size == (120, 36):
                output = repo / "screenshots/ui-reference"
                output.mkdir(parents=True, exist_ok=True)
                app.save_screenshot("pdf-preview.svg", path=str(output))
            await pilot.press("escape")
            assert app.screen is not preview
        await pilot.resize_terminal(120, 36)
        for name in ("foto.png", "surat.docx", "arsip.zip", "suara.wav", "program.exe"):
            app.post_message(BukaBerkas(root / name))
            await wait(pilot, lambda: isinstance(app.screen, FilePreviewScreen) and app.screen.preview is not None)
            preview = app.screen
            assert preview.preview.kind != "error", preview.preview.content.plain
            if name == "foto.png":
                assert preview.preview.visual
                await pilot.resize_terminal(70, 22)
                await wait(pilot, lambda: preview._requested_size[0] < 70)
                await pilot.resize_terminal(120, 36)
                await wait(pilot, lambda: preview._requested_size[0] > 70)
                if "--preview" in sys.argv:
                    app.save_screenshot("image-preview.svg", path=str(output))
            await pilot.press("escape")
        # Hasil worker yang datang setelah Esc tidak boleh mengubah layar lain.
        import threading
        gate = threading.Event()
        def delayed(*args, **kwargs):
            gate.wait(3)
            return render_file(*args, **kwargs)
        with patch("agent.interfaces.textual_widgets.file_preview.render_file", side_effect=delayed):
            preview = FilePreviewScreen(root / "foto.png")
            app.push_screen(preview)
            await pilot.pause(.05)
            await pilot.press("escape")
            gate.set()
            await pilot.pause(.2)
            assert app.screen is not preview
        assert tema.set_tema("vslight")
        app.refresh_css()
        editor = FileEditorScreen(root / "install.ps1", '$n = "Halo"\n', lambda *_: (True, ""))
        app.push_screen(editor)
        await pilot.pause()
        area = editor.query_one("#file-editor-area", TextArea)
        await wait(pilot, lambda: bool(area._syntax_spans))
        assert any(s.style and s.style.color and s.style.color.name == "#a31515"
                   for s in area.render_line(0)), "Warna tema terang belum tampil"
        actual_bg = editor.query_one("#file-editor-btn-close").styles.background.hex.lower()
        assert actual_bg == tema.p("menu_bg").lower(), (actual_bg, tema.p("menu_bg"))
        if "--preview" in sys.argv:
            app.save_screenshot("syntax-light.svg", path=str(repo / "screenshots/ui-reference"))
        await pilot.press("escape")
    print("OK: routing tree, syntax sesudah edit, PDF 3 ukuran, navigasi, visual, resize gambar, Esc, callback terlambat")


if __name__ == "__main__":
    fixtures()
    fingerprints = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}
    backend()
    asyncio.run(ui())
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in fingerprints.items())
    os.chdir(repo)
    temporary.cleanup()

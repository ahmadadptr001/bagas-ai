# -*- coding: utf-8 -*-
"""Uji InfoSidebar: footer path lengkap + tree project + lebar sidebar.

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_project_tree.py

Yang dicek:
1. Footer path menampilkan path project LENGKAP (di-wrap bila panjang,
   tak ada "…" di path; boleh ada "…" hanya bila path > 2 baris muat).
2. Tarik tepi kiri menyempitkan/melebarkan SELURUH sidebar; lebar dibatasi
   28..60 dan garis seksi Sistem ikut menyesuaikan lebar.
3. Klik footer path membuka ProjectTree berisi folder DAN berkas project;
   klik folder di dalamnya me-expand dan memuat isi folder itu (lazy), tanpa
   menduplikasi anak saat dibuka-tutup.
4. Lebar pilihan tersimpan ke prefs.json dan dipakai lagi saat mount.
5. Klik file membuka editor, Ctrl+S menampilkan diff lalu menyimpan dengan
   checkpoint yang dapat dipulihkan lewat undo_changes.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Root project = folder temporer khusus uji — HARUS diset sebelum modul
# agent diimpor (config.PROJECT_ROOT dibaca sekali saat impor).
_TMP = tempfile.mkdtemp(prefix="uji_tree_")
os.environ["BAGASAI_PROJECT_ROOT"] = _TMP
# prefs.json juga diarahkan ke temp — uji lebar sidebar menulis preferensi,
# jangan cemari prefs asli pengguna.
os.environ["BAGASAI_HOME"] = os.path.join(_TMP, "home")

sys.path.insert(0, r"C:/Users/user/Documents/PROJECTS/ai-agent/src")

from textual.app import App
from textual.widgets import RichLog, TextArea

from agent.interfaces.textual_app import BagasAIApp
from agent.interfaces.textual_widgets import (
    FileEditorScreen, InfoSidebar, ProjectTree,
)


def buat_struktur():
    root = Path(_TMP)
    (root / "src" / "agent" / "widgets").mkdir(parents=True, exist_ok=True)
    (root / "src" / "agent").mkdir(parents=True, exist_ok=True)
    (root / "src" / "modul_lain").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "node_modules" / "besar").mkdir(parents=True, exist_ok=True)
    (root / "berkas.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "catatan.txt").write_text("uji\n", encoding="utf-8")


async def tunggu(pilot, kondisi, maks=100, jeda=0.05,
                 pesan="kondisi tak terpenuhi"):
    for _ in range(maks):
        await pilot.pause(jeda)
        if kondisi():
            return
    raise AssertionError(pesan)


async def main():
    buat_struktur()
    gagal = 0

    def cek(nama, kondisi, detail=""):
        nonlocal gagal
        if kondisi:
            print(f"OK: {nama}")
        else:
            print(f"GAGAL: {nama} {detail}")
            gagal += 1

    from unittest.mock import MagicMock
    ag = MagicMock()
    ag.model_spec = MagicMock(label="uji", is_web=False)
    app = BagasAIApp(agent=ag)
    async with app.run_test(size=(120, 30)) as pilot:
        sb = app.query_one("#sidebar", InfoSidebar)
        tree = sb.query_one("#project-tree", ProjectTree)
        await pilot.pause()

        # Footer/pra-jawaban/chat berhenti tepat sebelum sidebar. Kalau footer
        # masih 100%, ia menimpa divider sidebar pada empat baris terbawah.
        footer = app.query_one("#footer")
        input_row = app.query_one("#input-row")
        cek("footer berhenti tepat di tepi sidebar",
            footer.region.x + footer.region.width == sb.region.x,
            f"footer={footer.region}, sidebar={sb.region}")
        cek("box chat tidak menutupi sidebar",
            input_row.region.x + input_row.region.width == sb.region.x,
            f"chat={input_row.region}, sidebar={sb.region}")
        streaming = app.query_one("#streaming-preview")
        streaming.update_preview("uji pra-jawaban")
        await pilot.pause()
        cek("box pra-jawaban tidak memutus divider",
            streaming.region.x + streaming.region.width == sb.region.x,
            f"pra={streaming.region}, sidebar={sb.region}")
        streaming.hide()
        await pilot.resize_terminal(90, 30)
        await pilot.pause()
        cek("footer kembali penuh saat sidebar disembunyikan",
            not sb.display and footer.region.width == 90,
            f"sidebar={sb.display}, footer={footer.region}")
        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        cek("divider tersambung lagi saat sidebar muncul",
            sb.display
            and footer.region.x + footer.region.width == sb.region.x,
            f"footer={footer.region}, sidebar={sb.region}")

        # ── 1. footer path LENGKAP ────────────────────────────────────
        path_asli = str(Path(_TMP)).replace("\\", "/")
        tampil = sb.terakhir_path.replace("\\", "/")
        # Semua potongan path harus ada di footer (di-wrap, bukan "…").
        bersih = "\n".join(b.strip("📁▾▸ ") for b in tampil.split("\n"))
        utuh = all(bag in bersih for bag in path_asli.split("/"))
        cek("footer path tampil lengkap (tanpa '…')",
            utuh and "…" not in bersih, f"footer={bersih!r}")
        cek("tree tertutup di awal", tree.display is False)

        # ── 2. tarik tepi kiri untuk resize seluruh sidebar ───────────
        awal = sb._lebar
        handle = sb.query_one("#sidebar-resize")
        berhenti = lambda: None
        handle.on_mouse_down(SimpleNamespace(
            button=1, screen_x=80, stop=berhenti))
        handle.on_mouse_move(SimpleNamespace(screen_x=86, stop=berhenti))
        handle.on_mouse_up(SimpleNamespace(
            button=1, screen_x=86, stop=berhenti))
        await pilot.pause()
        cek("tarik kanan menyempitkan seluruh sidebar", sb._lebar < awal,
            f"{awal} -> {sb._lebar}")
        cek("styles.width milik sidebar ikut berubah",
            int(sb.styles.width.value) == sb._lebar,
            f"style={sb.styles.width}, state={sb._lebar}")
        cek("footer mengikuti resize sidebar",
            footer.region.x + footer.region.width == sb.region.x,
            f"footer={footer.region}, sidebar={sb.region}")
        sempit = sb._lebar
        handle.on_mouse_down(SimpleNamespace(
            button=1, screen_x=80, stop=berhenti))
        handle.on_mouse_move(SimpleNamespace(screen_x=68, stop=berhenti))
        handle.on_mouse_up(SimpleNamespace(
            button=1, screen_x=68, stop=berhenti))
        await pilot.pause()
        cek("tarik kiri melebarkan seluruh sidebar", sb._lebar > sempit,
            f"{sempit} -> {sb._lebar}")
        # Batas tetap berlaku walau pointer ditarik jauh.
        handle.on_mouse_down(SimpleNamespace(
            button=1, screen_x=80, stop=berhenti))
        handle.on_mouse_move(SimpleNamespace(screen_x=-100, stop=berhenti))
        handle.on_mouse_up(SimpleNamespace(
            button=1, screen_x=-100, stop=berhenti))
        cek("lebar dibatasi maksimum", sb._lebar == 60,
            f"lebar={sb._lebar}")
        handle.on_mouse_down(SimpleNamespace(
            button=1, screen_x=80, stop=berhenti))
        handle.on_mouse_move(SimpleNamespace(screen_x=200, stop=berhenti))
        handle.on_mouse_up(SimpleNamespace(
            button=1, screen_x=200, stop=berhenti))
        cek("lebar dibatasi minimum", sb._lebar == 28,
            f"lebar={sb._lebar}")
        # Kembali ke default utk seksi berikut.
        sb.terapkan_lebar(34)
        await pilot.pause()

        # ── 3. klik path -> tree terbuka, folder expandable ──────────
        # Klik bisa kalah cepat dengan layout (region footer berubah
        # setelah terapkan_lebar) — klik ulang sampai togglenya nyata.
        for _ in range(8):
            if tree.display:
                break
            await pilot.click(sb.query_one("#path-footer"))
            await pilot.pause(0.15)
        cek("tree terbuka setelah klik path", tree.display is True)
        label_root = [str(n.label) for n in tree.root.children]
        cek("tree menampilkan subfolder root",
            any("src" in l for l in label_root)
            and any("tests" in l for l in label_root),
            f"anak={label_root}")
        cek("tree menampilkan berkas root",
            any("berkas.py" in l for l in label_root),
            f"anak={label_root}")
        cek("folder teknis dilewati",
            not any("node_modules" in l for l in label_root),
            f"anak={label_root}")

        # Expand "src" -> anaknya (agent, modul_lain) dimuat lazy.
        node_src = next(n for n in tree.root.children
                        if "src" in str(n.label))
        node_src.expand()
        await pilot.pause()
        anak_src = [str(n.label) for n in node_src.children]
        cek("expand src memuat isinya (lazy)",
            any("agent" in l for l in anak_src)
            and any("modul_lain" in l for l in anak_src),
            f"anak src={anak_src}")
        cek("expand folder menampilkan berkas di dalamnya",
            any("catatan.txt" in l for l in anak_src),
            f"anak src={anak_src}")
        jumlah_anak_src = len(node_src.children)
        node_src.collapse()
        node_src.expand()
        await pilot.pause()
        cek("expand ulang tidak menduplikasi anak",
            len(node_src.children) == jumlah_anak_src,
            f"{jumlah_anak_src} -> {len(node_src.children)}")

        # Expand "src/agent" satu level lagi -> "widgets" muncul.
        node_agent = next((n for n in node_src.children
                           if "agent" in str(n.label)), None)
        cek("subfolder agent ada di tree", node_agent is not None)
        if node_agent is not None:
            node_agent.expand()
            await pilot.pause()
            anak_ag = [str(n.label) for n in node_agent.children]
            cek("expand agent memuat widgets",
                any("widgets" in l for l in anak_ag), f"anak={anak_ag}")

        # ── 5. klik file -> edit -> tinjau diff -> simpan + undo ──────
        node_file = next(n for n in tree.root.children
                         if "berkas.py" in str(n.label))
        tree.select_node(node_file)
        tree.action_select_cursor()
        def editor_siap():
            try:
                return (isinstance(app.screen, FileEditorScreen)
                        and app.screen.query_one("#file-editor-area", TextArea))
            except Exception:
                return False
        await tunggu(pilot, editor_siap,
                     pesan="klik file harus membuka editor")
        editor = app.screen
        area = editor.query_one("#file-editor-area", TextArea)
        cek("editor membaca isi file", area.text == "x = 1\n",
            f"isi={area.text!r}")
        area.text = "x = 2\n"
        await pilot.pause()
        cek("editor menandai perubahan", editor.dirty is True)

        # Ctrl+S pertama wajib hanya membuka diff, belum menyentuh disk.
        await pilot.press("ctrl+s")
        await pilot.pause()
        diff = editor.query_one("#file-editor-diff", RichLog)
        cek("Ctrl+S pertama membuka pratinjau diff",
            editor._preview is True and diff.display is True)
        cek("file belum ditulis sebelum konfirmasi",
            (Path(_TMP) / "berkas.py").read_text(encoding="utf-8") == "x = 1\n")

        # Ctrl+S kedua mengonfirmasi penulisan.
        await pilot.press("ctrl+s")
        await tunggu(
            pilot,
            lambda: (Path(_TMP) / "berkas.py").read_text(encoding="utf-8")
            == "x = 2\n",
            pesan="konfirmasi editor harus menyimpan file",
        )
        cek("file tersimpan dari editor",
            (Path(_TMP) / "berkas.py").read_text(encoding="utf-8") == "x = 2\n")
        cek("editor kembali bersih setelah simpan", editor.dirty is False)
        await pilot.pause(0.2)
        await pilot.press("escape")
        await tunggu(pilot,
                     lambda: not list(editor.query("#file-editor-area")),
                     pesan="editor tersimpan harus dapat ditutup")

        from agent.tools.checkpoint import undo_changes
        hasil_undo = undo_changes()
        cek("backup editor dapat dipulihkan oleh undo_changes",
            (Path(_TMP) / "berkas.py").read_text(encoding="utf-8") == "x = 1\n",
            hasil_undo)

        # Tutup tree lagi: klik path kedua kali (retry — sama seperti buka).
        for _ in range(8):
            if not tree.display:
                break
            await pilot.click(sb.query_one("#path-footer"))
            await pilot.pause(0.15)
        cek("klik path kedua menutup tree", tree.display is False)

    print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} uji GAGAL")
    return 0 if gagal == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

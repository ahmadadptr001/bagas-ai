"""ProjectTree — tree folder project yang bisa di-expand di InfoSidebar.

Dua kegunaan sekaligus:
1. FOOTER collapsible: baris "📁 <path lengkap>" (di-wrap, tak dipotong)
   di bagian bawah sidebar — klik untuk membuka tree.
2. TREE LAZY: tiap folder di-expand baru membaca isi disknya (satu level),
   jadi project raksasa (node_modules, .venv, dst.) tak pernah dibaca
   sekaligus saat membuka tree.

Folder yang disorot memberi umpan balik visual (gaya teks terang).

Pemakai: InfoSidebar (info_sidebar.py) — footer & lebarnya diatur sana;
widget ini cuma soal isi tree.
"""
from __future__ import annotations

from pathlib import Path

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from ... import config
from ...ui import tema

# Folder yang tak berguna dijalan di tree — teknis, isinya ribuan, dan tak
# pernah jadi konteks percakapan. Dotfolder (kecuali .bagas) juga dilewati
# demi keringkasan.
_ABAIKAN_DIR = {
    "node_modules", "__pycache__", ".git", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".idea", ".vscode", "target", "site-packages", "__pypackages__",
}
_MAX_ANAK = 500  # batas anak per folder — di atasnya beri penanda "…"
_PENANDA_LEBIH = "… ({sisa} lagi)"


def _abai(path: Path) -> bool:
    nama = path.name
    if nama.startswith(".") and nama != ".bagas":
        return True
    return nama in _ABAIKAN_DIR


class ProjectTree(Tree):
    """Tree project — lazy: isi folder dibaca saat folder di-expand."""

    COMPONENT_CLASSES = {"project-tree--folder"}

    DEFAULT_CSS = """
    ProjectTree {
        height: auto;
        max-height: 100%;
        background: transparent;
        padding: 0;
        border: none;
        scrollbar-size-vertical: 1;
    }
    ProjectTree > .project-tree--folder {
        color: $t-teks;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", id="project-tree", **kwargs)
        # Batas huruf label node (diperbarui InfoSidebar mengikuti lebar).
        self._lebar_maks = 26
        # Event ``NodeExpanded`` juga dikirim lagi setelah node ditutup lalu
        # dibuka. Ingat node yang sudah dimuat agar anaknya tidak terduplikasi.
        self._node_sudah_dimuat: set[int] = set()
        self.root.expand()

    # --- API untuk InfoSidebar -----------------------------------------

    def set_lebar_karakter(self, n: int) -> None:
        """Batas huruf per baris node — lebar sidebar dikurangi indentasi
        tree & ikon (dipanggil InfoSidebar tiap lebar berubah)."""
        self._lebar_maks = max(8, n)

    def _potong(self, nama: str) -> str:
        """Nama folder yang lebih panjang dari sidebar dipotong "…" di
        ujung — tree Textual tak membungkus label."""
        if len(nama) <= self._lebar_maks:
            return nama
        return nama[:self._lebar_maks - 1] + "…"

    def buka_root(self) -> None:
        """Bangun (atau reset) tree dari root project — dipanggil saat
        footer path diklik pertama kali."""
        root = config.PROJECT_ROOT
        self.clear()
        self._node_sudah_dimuat.clear()
        self.root.data = root
        self.root.set_label("📁 " + self._potong(str(root)))
        self._isi_anak(self.root, root)
        self.root.expand()
        self.scroll_to(0, animate=False)

    # --- Lazy loading ---------------------------------------------------

    def _isi_anak(self, node: TreeNode, folder: Path) -> None:
        """Isi ``node`` dengan isi ``folder`` (satu level baca disk).

        Subfolder diberi placeholder agar bisa di-expand secara lazy; berkas
        ditampilkan sebagai leaf. Folder yang benar-benar kosong tetap diberi
        penanda agar tidak tampak sebagai folder mati.
        """
        try:
            isi = [path for path in folder.iterdir() if not _abai(path)]
        except OSError:
            return
        isi.sort(key=lambda path: (not path.is_dir(), path.name.lower()))
        if not isi:
            node.add_leaf("· kosong")
            return
        for path in isi[:_MAX_ANAK]:
            if path.is_dir():
                anak = node.add("📁 " + self._potong(path.name), data=path,
                                expand=False)
                # Placeholder memastikan chevron muncul untuk pemuatan lazy.
                anak.add_leaf("")
            else:
                node.add_leaf("📄 " + self._potong(path.name), data=path)
        if len(isi) > _MAX_ANAK:
            node.add(_PENANDA_LEBIH.format(sisa=len(isi) - _MAX_ANAK),
                     expand=False, allow_expand=False)

    def _node_path(self, node: TreeNode) -> Path | None:
        """Path folder yang diwakili ``node``.

        Path disimpan sebagai data node, bukan dirunut dari label yang mungkin
        dipotong mengikuti lebar sidebar.
        """
        return node.data if isinstance(node.data, Path) else None

    # --- Event ----------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Folder di-expand (klik pengguna): isi anaknya sekarang — baca
        disk satu level saja. Placeholder kosong dibuang begitu anak
        sungguhan datang."""
        node = event.node
        if node is self.root or not node.allow_expand:
            return
        if id(node) in self._node_sudah_dimuat:
            return
        # Placeholder "▸" dari _isi_anak — buang sebelum mengisi.
        if len(node.children) == 1 and not str(node.children[0].label):
            node.children[0].remove()
        folder = self._node_path(node)
        if folder is not None:
            self._isi_anak(node, folder)
            self._node_sudah_dimuat.add(id(node))

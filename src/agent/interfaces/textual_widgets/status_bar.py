"""StatusBar widget — baris status permanen di dasar layar.

Menampilkan: merek, model, cabang git, jumlah berkas berubah, perintah.
Responsif: segmen dilepas satu per satu dari kanan saat terminal menyempit.

CATATAN BUG YANG SUDAH DIPERBAIKI (jangan diulang):

1. Warna dulu diambil dari ``app.get_css_variables()`` dengan kunci
   ``footer-bg``/``footer-cmd``/``footer-sep``/``footer-exit``/``footer-muted``
   yang TIDAK PERNAH ADA, jadi selalu jatuh ke nilai cadangan bertema terang.
   Di tema gelap hasilnya baris terang-di-atas-terang alias tak terbaca.
   Sekarang warna diambil dari ``tema.p()`` — sumber yang sama dengan sisa UI.
2. Segmen kiri (merek/model/git) dulu ditulis TANPA warna depan, hanya
   ``bar.style = "on <bg>"``, jadi ikut warna depan bawaan terminal.
   Sekarang setiap segmen punya warnanya sendiri.
3. ``_git_info()`` menjalankan DUA ``subprocess.run`` (timeout 2 detik) di
   thread UI setiap kali render. Di repo besar seluruh antarmuka membeku.
   Sekarang git dibaca di thread pekerja dan hasilnya di-cache.
4. ``self.parent.size.width`` dipakai sebagai lebar — itu lebar INDUK, bukan
   lebar baris ini. Sekarang pakai ``self.size.width``.
"""
from __future__ import annotations

import subprocess
import time
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual.app import RenderResult
from textual.reactive import reactive
from textual.widget import Widget

from ...ui import tema

if TYPE_CHECKING:
    from ...core import Agent

# Urutan pelepasan segmen saat ruang habis (paling depan dilepas dulu).
_URUTAN_LEPAS = ("ctrlc", "perintah", "ubah", "git")

# Cache git: dibaca di thread pekerja, dibaca-tulis dari UI.
_git_cache: dict = {"t": 0.0, "v": ("", 0)}
_GIT_TTL = 5.0


def _baca_git() -> tuple[str, int]:
    """Baca (cabang, jumlah_berubah). PANGGIL DARI THREAD PEKERJA SAJA."""
    now = time.time()
    if now - _git_cache["t"] < _GIT_TTL:
        return _git_cache["v"]
    hasil = ("", 0)
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        branch = r.stdout.strip() if r.returncode == 0 else ""
        if branch:
            out = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            hasil = (branch, len(out.splitlines()) if out else 0)
    except (OSError, subprocess.SubprocessError):
        hasil = ("", 0)
    _git_cache["v"] = hasil
    _git_cache["t"] = now
    return hasil


class StatusBar(Widget):
    """Baris status responsif setinggi satu baris."""

    model_label: reactive[str] = reactive("")
    is_web: reactive[bool] = reactive(False)
    live_screen: reactive[bool] = reactive(False)
    voice_state: reactive[str] = reactive("")

    def __init__(self, agent: "Agent | None" = None, **kwargs):
        super().__init__(**kwargs)
        self._agent = agent

    # Tanpa compose() — StatusBar memakai render() langsung.

    def on_mount(self) -> None:
        self._muat_git()
        self.set_interval(5.0, self._muat_git)

    def _muat_git(self) -> None:
        """Segarkan info git di thread pekerja, lalu gambar ulang."""
        def kerja() -> None:
            sebelum = _git_cache["v"]
            sesudah = _baca_git()
            if sesudah != sebelum:
                try:
                    self.app.call_from_thread(self.refresh)
                except Exception:  # noqa: BLE001 — app sudah tutup
                    pass

        try:
            self.run_worker(kerja, thread=True, group="statusbar-git",
                            exclusive=True, exit_on_error=False)
        except Exception:  # noqa: BLE001 — di luar konteks app
            pass

    def _label_model(self) -> tuple[str, bool]:
        """Kembalikan (label, apakah_model_web) dengan aman."""
        if self.model_label:
            return self.model_label, bool(self.is_web)
        try:
            spec = self._agent.model_spec  # type: ignore[union-attr]
            return spec.label, bool(spec.is_web)
        except Exception:  # noqa: BLE001 — agent belum siap
            return "—", False

    def render(self) -> RenderResult:
        lebar = self.size.width or 80
        label, is_web = self._label_model()
        sep = "  │  "
        branch, changed = _git_cache["v"]

        segmen: dict[str, str] = {
            "merek": " ⬢ bagas-ai",
            "model": f"{sep}{'🌐' if is_web else '🤖'} {label}",
            "live": f"{sep}📹 layar" if self.live_screen else "",
            "voice": (f"{sep}● merekam" if self.voice_state == "merekam"
                      else f"{sep}🎙 dengar" if self.voice_state else ""),
            "git": f"{sep}🌿 {branch}" if branch else "",
            "ubah": f"{sep}📝 {changed}" if changed else "",
            "perintah": f"{sep}/help · ",
            "exit": "/exit",
            "ctrlc": " atau ctrl+c",
        }
        warna: dict[str, str] = {
            "merek": tema.p("merek_footer"),
            "model": tema.p("model_footer"),
            "live": tema.p("aksen"),
            "voice": tema.p("aksen"),
            "git": tema.p("git_footer"),
            "ubah": tema.p("ubah_footer"),
            "perintah": tema.p("cmd_footer"),
            "exit": tema.p("exit_footer"),
            "ctrlc": tema.p("muted_footer"),
        }
        bg = tema.p("bg_footer")
        warna_sep = tema.p("sep_footer")

        kiri = [s for s in ("merek", "model", "live", "voice", "git", "ubah")
                if segmen[s]]
        kanan = [s for s in ("perintah", "exit", "ctrlc") if segmen[s]]

        # Lepas segmen opsional sampai muat. Merek/model/exit selalu tampil.
        def total() -> int:
            return sum(cell_len(segmen[s]) for s in kiri + kanan) + 1

        for kandidat in _URUTAN_LEPAS:
            if total() <= lebar:
                break
            if kandidat in kiri:
                kiri.remove(kandidat)
            elif kandidat in kanan:
                kanan.remove(kandidat)

        bar = Text(style=f"on {bg}", no_wrap=True, overflow="crop")

        for s in kiri:
            teks = segmen[s]
            # Pemisah "│" diwarnai berbeda dari isi segmennya.
            if teks.startswith(sep):
                bar.append(sep, style=f"{warna_sep} on {bg}")
                teks = teks[len(sep):]
            bar.append(teks, style=f"{warna[s]} on {bg}")

        lebar_kanan = sum(cell_len(segmen[s]) for s in kanan)
        bar.append(" " * max(0, lebar - bar.cell_len - lebar_kanan - 1),
                   style=f"on {bg}")

        for s in kanan:
            teks = segmen[s]
            if teks.startswith(sep):
                bar.append(sep, style=f"{warna_sep} on {bg}")
                teks = teks[len(sep):]
            if s == "perintah":
                # "/help · " -> perintah berwarna, titik pemisah diredupkan
                nama, _, sisa = teks.partition("·")
                bar.append(nama, style=f"{warna[s]} on {bg}")
                if sisa or _:
                    bar.append("·" + sisa, style=f"{warna_sep} on {bg}")
            else:
                bar.append(teks, style=f"{warna[s]} on {bg}")

        bar.append(" ", style=f"on {bg}")
        return bar

    def update_model(self, label: str, is_web: bool = False) -> None:
        """Ganti label model (memicu gambar ulang lewat reactive)."""
        self.model_label = label
        self.is_web = is_web

    def update_live_screen(self, aktif: bool) -> None:
        """Tampilkan indikator privasi selama screenshot otomatis aktif."""
        self.live_screen = bool(aktif)

    def update_voice_state(self, keadaan: str = "") -> None:
        """Tampilkan indikator privasi mikrofon: dengar atau merekam."""
        self.voice_state = keadaan if keadaan in ("dengar", "merekam") else ""

    def refresh_theme(self) -> None:
        """Gambar ulang dengan warna tema baru."""
        self.refresh()

"""Antarmuka CLI bagas-ai (sinkron & bersih).

Desain: rich memegang terminal penuh (warna/emoji/panel mulus, tanpa bocor kode
ANSI). Animasi loading realtime (spinner + token + waktu) NEMPEL inline pada tiap
task via rich Live. Input pakai prompt_toolkit (hanya saat idle) supaya
Ctrl+Backspace bisa hapus per-kata.

Kotak chat SATU-SATUNYA: bentuk & tempatnya (menempel di atas status bar) sama
persis saat idle maupun saat AI bekerja. Mengetik selagi AI belum selesai tetap
"mengirim pesan" — pesannya cuma dikerjakan setelah giliran ini beres, tanpa
satu pun teks di layar yang membahas antrean.
"""
from __future__ import annotations

from pathlib import Path
import difflib
from collections import deque
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata

try:  # keyboard non-blocking (Windows): ketikan-selama-giliran & Ctrl+C
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows
    _msvcrt = None

# Penanda byte Ctrl+Backspace = hapus kata.
# msvcrt & prompt_toolkit memakai API input BERBEDA — byte yang diterima
# untuk tombol sama bisa lain. prompt_toolkit (ReadConsoleInputW) di ConPTY
# melaporkan Backspace polos='\x7f' & Ctrl+BS='\x08', tapi msvcrt (getwch)
# di console klasik melaporkan kebalikannya: polos='\x08' & Ctrl='\x7f'.
# Deteksi lewat GetConsoleMode: ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200)
# menyala di ConPTY/VT, mati di console klasik.
if _msvcrt is not None:
    try:
        import ctypes as _ctypes
        _con_mode = _ctypes.wintypes.DWORD()
        _ctypes.windll.kernel32.GetConsoleMode(
            _ctypes.windll.kernel32.GetStdHandle(-10),
            _ctypes.byref(_con_mode),
        )
        # ConPTY/VT aktif? '\x08' = Ctrl+Backspace. Klasik? '\x7f' = Ctrl+BS.
        _DATA_KATA_MSVCRT = "\x08" if _con_mode.value & 0x0200 else "\x7f"
    except Exception:  # noqa: BLE001
        _DATA_KATA_MSVCRT = "\x7f"  # gagal deteksi -> anggap klasik
else:
    _DATA_KATA_MSVCRT = "\x08"   # VT (Linux/mac): '\x08' = Ctrl+Backspace

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from prompt_toolkit.application import Application  # noqa: E402
from prompt_toolkit.buffer import Buffer  # noqa: E402
from prompt_toolkit.completion import Completer, Completion  # noqa: E402
from prompt_toolkit.document import Document  # noqa: E402
from prompt_toolkit.filters import has_completions  # noqa: E402
from prompt_toolkit.formatted_text import HTML  # noqa: E402
from prompt_toolkit.history import InMemoryHistory  # noqa: E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: E402
from prompt_toolkit.keys import Keys  # noqa: E402
from prompt_toolkit.layout import Layout  # noqa: E402
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit, Window  # noqa: E402
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl  # noqa: E402
from prompt_toolkit.layout.dimension import Dimension as D  # noqa: E402
from prompt_toolkit.layout.menus import CompletionsMenu  # noqa: E402
from prompt_toolkit.patch_stdout import patch_stdout  # noqa: E402
from prompt_toolkit.styles import Style as PTStyle  # noqa: E402
from prompt_toolkit.utils import get_cwidth  # noqa: E402
from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.markup import escape as _esc  # noqa: E402
from rich.padding import Padding  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.syntax import Syntax  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402
from rich.theme import Theme  # noqa: E402

try:
    from pyfiglet import Figlet  # noqa: E402
except Exception:  # pragma: no cover
    Figlet = None  # type: ignore

from .. import config, interaction, llm, longmem, models, osinfo, permissions, prefs, projectindex, scripts, telegram_perms, updater, workspace  # noqa: E402
from .. import dengar as _dengar  # noqa: E402
from .. import session as session_mod  # noqa: E402
from .. import tanda as _tanda  # noqa: E402
from .. import suara as _suara  # noqa: E402
from .. import tempelan as _tempelan  # noqa: E402
from ..ui.ascii_art import (  # noqa: E402
    BLOCK_H, BLOCK_W, image_dimensions,
    image_to_blocks_pixels, is_image_path, is_video_path,
)
from ..tools.screen import IMAGE_MARK  # noqa: E402
from ..core import Agent  # noqa: E402
from ..session import Session  # noqa: E402
# Prompt interaktif MILIK SENDIRI (dulu InquirerPy) — lihat ui/menu.py.
from ..ui.menu import Choice, inquirer  # noqa: E402
from ..ui import tema  # noqa: E402


def _TM(markup: str) -> Text:
    """Text.from_markup yang SADAR-TEMA: hex warisan emas/oranye di dalam
    markup ditukar menjadi warna tema aktif (lihat ui/tema.py). Seluruh
    panggilan lama dialihkan ke sini sehingga berganti tema benar-benar
    mengganti wajah UI, bukan cuma footer & kotak chat."""
    return Text.from_markup(tema.terjemah(markup))

# Tema Markdown selaras palet "zenitsu" (kuning-oranye) agar jawaban AI
# (heading, list, kutipan,
# kode, tautan) serasi dengan seluruh UI — bukan warna default rich yang kontras.
# Warna diambil dari TEMA AKTIF saat impor (tersimpan di prefs, dipilih /theme),
# jadi markdown jawaban ikut tema sejak startup.
_MD_THEME = Theme({
    "markdown.h1": f"bold {tema.p('aksen')}",
    "markdown.h1.border": tema.p("aksen"),
    "markdown.h2": f"bold {tema.p('aksen2')}",
    "markdown.h3": f"bold {tema.p('aksen_terang')}",
    "markdown.h4": "bold #9fc93c",
    "markdown.h5": f"bold {tema.p('aksen_terang')}",
    "markdown.h6": f"bold {tema.p('aksen2')}",
    "markdown.item.bullet": f"bold {tema.p('aksen')}",
    "markdown.item.number": f"bold {tema.p('aksen2')}",
    "markdown.code": f"{tema.p('aksen_terang')} on #3a2a1a",       # `inline code`
    "markdown.link": f"{tema.p('aksen2')} underline",
    "markdown.link_url": f"dim {tema.p('aksen_terang')}",
    "markdown.block_quote": f"italic {tema.p('aksen_terang')}",
    "markdown.block_quote_border": tema.p("tepi"),
    "markdown.hr": tema.p("tepi_redup"),
    "markdown.strong": f"bold {tema.p('teks')}",
    "markdown.emph": f"italic {tema.p('teks')}",
    "markdown.text": tema.p("teks"),
})
console = Console(theme=_MD_THEME, _environ={
    # COLUMNS/LINES warisan shell (di sebagian profil diekspor otomatis)
    # MEMBAKU ukuran rich selamanya: nilai warisan itu menimpa hasil query
    # terminal sungguhan, jadi memperbesar/mengecilkan jendela tak lagi
    # berefek sedikit pun. Dibuang dari pandangan rich; ukuran dibaca
    # langsung dari terminal di tiap render.
    k: v for k, v in os.environ.items() if k not in ("COLUMNS", "LINES")
})  # auto-detect VT -> warna/emoji mulus

# PRINT SADAR-TEMA: seluruh markup yang lewat console.print diterjemahkan
# dulu lewat tema.terjemah(), jadi hex warisan emas/oranye pada teks status,
# pemberitahuan, dan judul panel ikut tema aktif TANPA harus ditulis ulang
# satu per satu — berlaku juga untuk cetakan baru di masa depan.
_cetak_asli = console.print


def _cetak_tematik(*args, **kwargs):
    args = tuple(tema.terjemah(a) if isinstance(a, str) else a
                 for a in args)
    return _cetak_asli(*args, **kwargs)


console.print = _cetak_tematik

# Tema penyorotan sintaks blok kode ```lang``` — 'gruvbox-dark' dipilih karena
# palet dasarnya memang hangat (kuning/oranye/cokelat), jadi kode di dalam
# jawaban tak lagi memercikkan ungu-pink ke tengah tema kuning-oranye.
# Fallback aman bila versi pygments-nya belum punya.
try:  # pragma: no cover - bergantung versi pygments
    from pygments.styles import get_style_by_name as _gsbn
    _gsbn("gruvbox-dark")
    _CODE_THEME = "gruvbox-dark"
except Exception:  # pragma: no cover
    _CODE_THEME = "monokai"


# Garis penghubung gambar pohon direktori (box-drawing, BUKAN ASCII '|').
# Sengaja tak memuat '│' saja: banyak teks biasa memakainya sebagai pemisah.
_SAMBUNG_POHON = ("├", "└", "┣", "┗")
_GARIS_POHON = _SAMBUNG_POHON + ("│", "─", "┃", "━")
_PAGAR = re.compile(r"^\s*(```|~~~)")


def _pagari_pohon(text: str) -> str:
    """Bungkus gambar pohon direktori dengan pagar kode sebelum di-Markdown-kan.

    Kenapa perlu: Markdown menganggap baris-baris berurutan sebagai SATU
    paragraf lalu membungkusnya ulang mengikuti lebar terminal. Untuk kalimat
    biasa itu benar, tapi gambar pohon ("├── agent/") jadi hancur — semua
    barisnya ditempel berderet jadi satu paragraf dan bentuk pohonnya lenyap.
    Model menuliskan pohon TANPA pagar kode jauh lebih sering daripada dengan,
    jadi memagarinya di sini adalah satu-satunya cara agar tampilannya selamat.

    Yang sudah berada di dalam pagar kode dibiarkan apa adanya.
    """
    if not any(c in text for c in _SAMBUNG_POHON):
        return text                      # jalur cepat: tak ada pohon sama sekali
    keluar: list[str] = []
    blok: list[str] = []
    dalam_pagar = False

    def _tutup() -> None:
        """Pindahkan blok yang tertampung ke keluaran, dipagari bila layak."""
        if not blok:
            return
        # Pagari hanya bila benar-benar gambar pohon: minimal dua baris DAN ada
        # sambungan sungguhan (├/└). Satu baris berhias garis cuma pemanis.
        layak = len(blok) >= 2 and any(
            any(c in b for c in _SAMBUNG_POHON) for b in blok)
        keluar.extend(["```text", *blok, "```"] if layak else blok)
        blok.clear()

    for baris in text.splitlines():
        if _PAGAR.match(baris):
            _tutup()
            dalam_pagar = not dalam_pagar
            keluar.append(baris)
            continue
        if dalam_pagar:
            keluar.append(baris)
            continue
        if any(c in baris for c in _GARIS_POHON):
            blok.append(baris)
            continue
        # Baris akar pohon ("src/", "proyek/") berada TEPAT di atas cabang
        # pertamanya dan tak punya garis apa pun — tanpa ikut dipagari, ia
        # tertinggal sebagai paragraf yatim di atas kotak kode.
        if not blok and baris.strip().endswith(("/", "\\")) and \
                not baris.lstrip().startswith(("#", ">", "-", "*", "+")):
            blok.append(baris)
            continue
        _tutup()
        keluar.append(baris)
    _tutup()
    return "\n".join(keluar)


def _md(text: str) -> Markdown:
    """Markdown bertema zenitsu (inline code pakai style `markdown.code`,
    blok kode ```lang``` disorot tema `gruvbox-dark`).

    Escape ANSI dibuang DI SINI karena inilah satu-satunya pintu yang dilewati
    SEMUA teks model menuju layar: narasi antar-langkah dan jawaban akhir. Dulu
    penyaringnya hanya dipasang di region live, padahal jalur ini justru lebih
    berbahaya — region live ditimpa tiap frame, sedangkan riwayat terminal
    PERMANEN: satu \x1b[2J dari log yang disalin model menghapus scrollback
    giliran sebelumnya, dan \x1b[31m tanpa reset mewarnai semua teks sesudahnya
    sampai terminal di-reset manual."""
    return Markdown(_pagari_pohon(_bersih_kendali(text)), code_theme=_CODE_THEME)

# Padding tepi supaya konten tidak mepet ke pinggir terminal (kiri/kanan/bawah).
_LPAD = 2

# Perintah slash + deskripsi singkat (dipakai autocomplete "/..." & bantuan).
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("menu", "menu interaktif"),
    ("model", "pilih model + saran"),
    ("effort", "mode berpikir"),
    ("mode", "mode kerja situs: buat gambar/video, dll"),
    ("theme", "tema warna antarmuka"),
    ("tim", "24 spesialis yang meninjau pekerjaan secara pasif"),
    ("mic", "suara: kabar AI dibacakan pengeras suara (on/off/tes)"),
    ("voice", "mikrofon: sebut \"bagas ai …\" lalu diam sejenak "
              "(on/off/tes/jangkau/dekat|normal|jauh)"),
    ("compact", "simpan riwayat percakapan ke berkas memory"),
    ("send-compact", "kirim berkas memory terakhir ke percakapan sekarang"),
    ("add-dir", "tambah folder konteks"),
    ("dirs", "folder konteks aktif"),
    ("rm-dir", "hapus folder konteks"),
    ("new", "mulai sesi baru"),
    ("delete", "hapus sesi"),
    ("reset", "kosongkan riwayat"),
    ("clear", "bersihkan layar"),
    ("web", "kelola sesi AI web (hapus chat menumpuk / logout)"),
    ("browser", "ganti browser (brave/chrome/dll)"),
    ("bot", "hidup/matikan bot Telegram di sesi ini"),
    ("permissions-bot", "atur izin siapa yang boleh kontrol via Telegram"),
    ("review", "cari bug & kesalahan sistem di seluruh proyek"),
    ("scan", "pindai ulang & segarkan peta proyek"),
    ("live", "hidup/matikan tampilan mengalir dengan footer status"),
    ("memory", "memory jangka panjang"),
    ("scripts", "script memory"),
    ("update", "cek pembaruan"),
    ("help", "bantuan"),
    ("exit", "keluar"),
]

# Instruksi untuk /review — audit bug & kesalahan sistem menyeluruh.
_REVIEW_PROMPT = (
    "Lakukan REVIEW/AUDIT menyeluruh pada proyek ini KHUSUS untuk menemukan BUG dan "
    "KESALAHAN SISTEM. Manfaatkan Peta Proyek yang sudah kamu punya untuk menentukan "
    "file paling berisiko lebih dulu, lalu baca file-file itu seperlunya (jangan baca "
    "semua kalau tak perlu). Telusuri terutama:\n"
    "- Bug logika & kasus tepi: off-by-one, None/null/undefined, pembagian nol, "
    "kondisi salah, loop tak berhenti, race condition, error/exception tak tertangani.\n"
    "- Kesalahan sistem/konfigurasi: import/modul salah, path/berkas salah, dependency "
    "hilang atau versi bentrok, variabel env yang belum diset, entry-point rusak.\n"
    "- Referensi rusak: fungsi/variabel/atribut yang dipanggil tapi tak ada, salah tipe, "
    "signature tak cocok.\n"
    "- Keamanan: kredensial/secret bocor, injeksi (SQL/shell), path traversal, input "
    "tak divalidasi.\n"
    "Untuk SETIAP temuan sebutkan: `file:baris`, tingkat keparahan (KRITIS/TINGGI/"
    "SEDANG/RENDAH), penjelasan singkat kenapa itu bug, dan saran perbaikan. URUTKAN "
    "dari paling parah. Kalau tak ada masalah serius, katakan terus terang. PENTING: "
    "ini fase pelaporan — JANGAN mengubah kode apa pun kecuali aku memintanya."
)


class SlashCompleter(Completer):
    """Sugesti perintah saat mengetik '/': '/ef' -> '/effort', dst.

    Hanya aktif untuk token perintah di awal baris (sebelum spasi), jadi tidak
    mengganggu saat mengetik pesan biasa atau argumen (mis. '/model llama').
    """

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Hanya untuk perintah slash di awal baris & sebelum ada spasi/argumen.
        if not text.startswith("/") or " " in text:
            return
        prefix = text[1:].lower()
        for name, desc in SLASH_COMMANDS:
            if name.startswith(prefix):
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display=HTML(f"<b>/{name}</b>"),
                    display_meta=desc,
                )


def _perintah(teks: str) -> bool:
    """Teks ini PERINTAH bagas-ai, bukan pesan untuk AI?

    Aturannya sengaja dibuat sama persis dengan gelung utama (diawali "/"),
    supaya satu baris tak pernah dinilai berbeda oleh dua bagian program: yang
    dianggap perintah saat diketik selagi AI menjawab harus juga dianggap
    perintah saat dikerjakan sesudahnya."""
    return teks.strip().startswith("/")


def pout(renderable, *, bottom: int = 1) -> None:
    """Cetak renderable dengan padding kiri/kanan (+bawah) yang konsisten."""
    console.print(Padding(renderable, (0, _LPAD, bottom, _LPAD)))


# Gradasi ungu -> biru (magenta neon) untuk teks shadow.
# Gradasi wordmark: emas terang -> oranye -> cokelat bara, arah
# yang sama dengan cahaya di latar tema ini.
# Gradien logo mengikuti TEMA AKTIF (7 titik; lihat ui/tema.py). Dibaca via
# fungsi supaya pergantian tema di tengah sesi langsung terlihat.
def _grad() -> list[str]:
    return list(tema.p("grad"))


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _fmt_elapsed(sec: float) -> str:
    """Format durasi bertingkat: <60s -> '12.3s', lalu 'm s', 'h m', 'd h'."""
    if sec < 60:
        return f"{sec:.1f}s"
    total = int(sec)
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


# Kosakata fase di samping animasi loading. SENGAJA cuma segelintir kata, dan
# semuanya bicara soal MODEL — bukan soal cara bagas-ai memakainya.
#
# Dulu status mentah connector diteruskan apa adanya ("menyiapkan jendela
# Chrome…", "membuka percakapan baru…", "tab Kimi mati, mengulang…"). Itu
# membocorkan jeroan ke layar: pengguna sedang menunggu jawaban, bukan sedang
# mengurus browser, dan bagi dia "jendela Chrome" terbaca seperti ada yang
# salah. Kalimat sedetail itu tempatnya di log, bukan di baris status.
_FASE_SIAP = "menyiapkan model"
_FASE_TUNGGU = "menunggu model"
_FASE_PIKIR = "berpikir"


def _fase_status(msg: str) -> str:
    """Ringkas status jadi SATU kata fase untuk baris status.

    Dipakai KEDUA jalur, karena itu namanya bukan lagi _web_phase: jalur API
    (nvidia/*) mengirim statusnya lewat pintu yang sama, dan dulu status
    "sedang memproses"-nya jatuh ke cabang terakhir lalu tampil sebagai
    "menyiapkan model" — keliru, dan keliru ke arah yang paling merugikan:
    permintaannya SUDAH terkirim dan model sudah bekerja, tapi pengguna
    dibilangi modelnya belum siap. Pada model yang butuh menit-menitan sebelum
    kata pertama, itu tampak persis seperti bagas-ai yang tak mengirim apa pun.
    """
    m = (msg or "").lower()
    # "menjawab" ikut jadi "berpikir": bagi yang menunggu, keduanya sama saja —
    # model belum selesai. Membedakannya cuma menambah kata yang berkedip.
    if "menjawab" in m or "berpikir" in m:
        return _FASE_PIKIR
    if "login" in m or "sign-in" in m or "sign in" in m:
        return "menunggu login"
    if ("mengetik" in m or "mengirim" in m or "mengunggah" in m
            or "lampiran" in m):
        return "analisis pesan"
    # Jalur API: permintaan sudah di jalan, potongan pertama belum kembali.
    # Diperiksa SESUDAH "berpikir"/"menjawab" supaya fase yang lebih pasti
    # tetap menang, dan SEBELUM cabang terakhir supaya tak jadi "menyiapkan".
    if "memproses" in m:
        return _FASE_TUNGGU
    # Sisanya — meluncurkan browser, memuat halaman, membuka percakapan baru,
    # menyambung ulang tab yang mati, mengantre giliran browser — semuanya satu
    # hal yang sama di mata pengguna: modelnya belum siap dipakai.
    return _FASE_SIAP


# Escape ANSI (CSI/OSC/dua-karakter) + karakter kendali lain. Keluaran tool nyata
# penuh dengannya — pip, npm, git, dan hampir semua CLI modern mewarnai
# keluarannya, dan model web pun kadang menyalin log berwarna ke jawabannya.
# Rich memperlakukan isi Text sebagai teks BIASA: byte ESC diteruskan apa adanya
# ke terminal, lalu terminal mengeksekusinya. Akibatnya warna region live berubah
# sendiri, kursor melompat, bahkan layar terhapus — persis "tampilan kacau" yang
# sulit ditelusuri karena sumbernya keluaran perintah, bukan kode UI.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI (warna, gerak kursor, hapus layar)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (judul jendela, hyperlink)
    r"|\x1b[()*+-./][0-9A-Za-z]"           # penanda charset (mis. \x1b(B)
    r"|\x1b[=><~]"                         # mode keypad/terminal
    r"|\x1b[@-Z\\-_]"                      # escape dua karakter
)
_KENDALI_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _bersih_kendali(s: str) -> str:
    """Buang escape ANSI & karakter kendali; CR jadi baris baru, TAB jadi spasi.

    CR ditangani TERSENDIRI, bukan lewat _KENDALI_RE: \\x0d jatuh persis di celah
    antara \\x0c dan \\x0e sehingga dulu lolos diam-diam. Ia karakter kendali
    sungguhan — memindahkan kursor ke kolom 0 — dan seluruh progress bar
    pip/npm/docker dibangun darinya, jadi ini bukan kasus langka. Diubah jadi
    baris baru (bukan dibuang) supaya tiap pembaruan progres tetap terbaca
    sebagai barisnya sendiri alih-alih menyambung jadi satu baris panjang.

    TAB ikut diganti karena lebar tampilannya ditentukan terminal (biasanya 8),
    sehingga perhitungan lebar Rich meleset dan kolom jadi tak sejajar."""
    if not s:
        return s
    s = _ANSI_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\t", "    ")
    return _KENDALI_RE.sub("", s)


def _oneline(t: Text) -> Text:
    """Baris untuk region live: JANGAN pernah wrap — wrap membuat tinggi region
    berubah antar-frame sehingga rich.Live menggambar ulang kacau (kedip/baris
    hantu, terutama di terminal sempit). Kelebihan dipotong dengan elipsis."""
    t.no_wrap = True
    t.overflow = "ellipsis"
    return t


# Gema prompt di transkrip — GARIS VERTIKAL LURUS agak tebal (▌) di tiap
# baris, dibalut strip gelap netral. Warnanya ikut TEMA AKTIF (ui/tema.py),
# dibaca saat dipanggil supaya /theme langsung terasa tanpa mulai ulang.
def _gema_prompt(teks: str, prefix: str = "") -> Text:
    """Gema pesan pengguna di transkrip — penanda batas antar-giliran.

    Bentuknya garis vertikal LURUS & agak tebal (▌) yang memanjang di tiap
    baris (termasuk lipatan), seluruh baris dibalut strip gelap — kontras
    tinggi tapi netral, mudah ditemukan sekali lihat saat menggulung riwayat.

    Lipatan dihitung SENDIRI (bukan Text.wrap: ia tak memecah tanpa justify)
    memakai lebar sel tampilan rich, supaya emoji/CJK tak menggeser garis."""
    from rich.cells import cell_len

    bersih = _bersih_kendali(teks).strip()
    if not bersih:
        return _KOSONG
    gema_bg = tema.p("gema_bg")
    garis = tema.p("gema_garis")
    teks_gaya = f"bold {tema.p('gema_teks')} on {gema_bg}"
    lebar_isi = max(16, console.width - 2 * 2 - 2 - cell_len(prefix))
    baris: list[str] = []
    for paraf in bersih.split("\n"):
        kata, kini = [], ""
        for k in paraf.split(" "):
            calon = f"{kini} {k}".strip() if kini else k
            if kini and cell_len(calon) > lebar_isi:
                baris.append(kini)
                kini = k
            else:
                kini = calon
        baris.append(kini)

    hasil = Text(no_wrap=True, overflow="ellipsis")
    for i, b in enumerate(baris):
        if i:
            hasil.append("\n  ")
        else:
            hasil.append("  ")
        # Garis ikut berlatar strip supaya menyambung mulus dengan isinya.
        hasil.append("▌ ", style=f"bold {garis} on {gema_bg}")
        if i == 0 and prefix:
            hasil.append(prefix, style=f"bold {garis} on {gema_bg}")
        hasil.append(b, style=teks_gaya)
    return hasil


# Warna gaya editor (GitHub-like): teks kalem di atas bg gelap hijau/merah.
#
# Sengaja DIREDAM, bukan warna hijau/merah pekat: diff sering menutupi layar
# berbaris-baris, dan warna cerah pada seluruh baris itu melelahkan sekaligus
# menenggelamkan sorotan sintaks yang justru dicari mata (lihat _warna_kode).
# Latarnya cukup untuk menandai baris mana yang berubah, tak lebih.
_ADD = "#a3ccad on #0d2312"
_DEL = "#cca3a3 on #230d0d"
_CTX = "grey50"
_GUT_A = "#4f8f5c on #08170b"
_GUT_D = "#96565a on #170808"


# --- pewarnaan sintaks DI DALAM diff ---------------------------------------
#
# Kenapa perlu: latar hijau/merah cuma memberi tahu baris mana yang berubah, dan
# di situ seluruh kode tampil satu warna rata. Padahal yang dibaca pengguna
# justru KODE-nya — `return`, `def`, `console.log`, string, angka. Tanpa warna
# token, meninjau perubahan di terminal jauh lebih lambat daripada di editor,
# dan pratinjau diff inilah satu-satunya kesempatan meninjau sebelum berkas
# disentuh.
#
# Caranya: rich.Syntax dipakai HANYA sebagai penghasil Text berwarna
# (.highlight), bukan sebagai renderable. Warna latar dari temanya dibuang dan
# diganti latar diff kita, sementara warna DEPAN tiap token dibiarkan menimpa —
# jadi kode tetap berwarna di atas bg hijau/merah, persis seperti diff editor.
_TEMA_KODE = "gruvbox-dark"  # hangat & terbaca di atas bg gelap hijau/merah
_MAKS_WARNAI = 400_000      # berkas raksasa: lewati saja, tak sebanding biayanya


def _tanpa_latar(t: Text) -> Text:
    """Salinan `t` yang mempertahankan warna DEPAN token tapi membuang LATARnya.

    Wajib: rich.Syntax menempelkan latar temanya (mis. #263238) pada SETIAP
    span token, bukan cuma sebagai gaya dasar. Kalau dibiarkan, tiap potongan
    kode membawa latar abu-abunya sendiri dan pita hijau/merah diff jadi
    belang-belang — persis hal yang membuat baris berubah gampang dikenali
    justru hilang."""
    out = Text(t.plain, no_wrap=True)
    for span in t.spans:
        st = span.style
        if not isinstance(st, Style):
            continue
        out.stylize(Style(color=st.color, bold=st.bold, italic=st.italic,
                          underline=st.underline), span.start, span.end)
    return out


def _pewarna(path: str, kode: str):
    """Daftar Text per baris hasil pewarnaan `kode`, atau None bila tak bisa.

    Seluruh isi diwarnai SEKALI lalu dipecah per baris — bukan baris demi baris
    — supaya konteks lintas-baris (docstring, string multi-baris, komentar blok)
    tidak salah warna."""
    if not kode or len(kode) > _MAKS_WARNAI:
        return None
    try:
        lexer = Syntax.guess_lexer(path, code=kode)
        if not lexer or lexer in ("text", "default"):
            return None
        syn = Syntax("", lexer, theme=_TEMA_KODE)
        return [_tanpa_latar(b) for b in syn.highlight(kode).split("\n")]
    except Exception:  # noqa: BLE001 - pewarnaan gagal != diff gagal
        return None


def _ambil(baris_warna, i: int):
    """Baris ke-i (1-based) dari hasil pewarnaan, atau None bila di luar batas."""
    if not baris_warna or i < 1 or i > len(baris_warna):
        return None
    return baris_warna[i - 1]


def _row(lineno: str, sign: str, text, style: str) -> Text:
    """Satu baris gaya editor '123 + kode' dengan bg + margin tepi.

    `text` boleh str biasa ATAU Text yang sudah diwarnai sintaks. Untuk yang
    kedua, `style` dipasang sebagai gaya DASAR (latar hijau/merah + warna depan
    cadangan) lalu span token milik pygments ditempel di atasnya — karena span
    yang belakangan hanya menyetel warna depan, latarnya lolos utuh.

    MENGEMBALIKAN Text, tidak mencetak sendiri: seluruh diff dirakit dulu lalu
    dicetak SEKALI (atomik). Dulu tiap baris dicetak terpisah — selagi region
    live me-refresh ~12x/detik, footer animasi bisa menyela DI ANTARA dua baris
    diff dan tampilan diff tampak tertimpa/terpotong kotak animasi."""
    # Selebar terminal (dikurangi margin kiri), TANPA batas atas: diff adalah
    # satu-satunya kesempatan meninjau perubahan sebelum berkas disentuh, jadi
    # memotongnya di kolom 108 pada terminal 160 kolom berarti membuang 50
    # kolom kode yang sebenarnya muat — dan yang terpotong justru ujung baris,
    # tempat perubahan JSX/atribut panjang biasanya berada.
    inner = max(20, console.width - _LPAD)
    line = Text(" " * _LPAD)  # margin kiri tanpa background
    line.append(f" {lineno:>4} {sign} ", style=style)
    if isinstance(text, Text):
        body = text.copy()
        body.expand_tabs(4)
        body.style = style          # gaya dasar; token menimpa warna depannya
        line.append_text(body)
    else:
        line.append(f"{text}".replace("\t", "    "), style=style)
    # Baris panjang DIPOTONG (bukan wrap): wrap membuat background hijau/merah
    # meluber tak beraturan ke baris berikutnya.
    line.truncate(_LPAD + inner, overflow="ellipsis")
    pad = (_LPAD + inner) - line.cell_len  # isi bg sampai batas kanan
    if pad > 0:
        line.append(" " * pad, style=style)
    line.no_wrap = True
    return line


# --- kotak chat: SATU komponen, selalu di tempat yang sama -----------------
#
# Selebar terminal, menempel langsung di atas bar status, dan keduanya dipaku di
# DASAR LAYAR — saat kamu mengetik maupun saat AI masih bekerja. Bentuk dan
# posisinya tak pernah berubah, jadi tempat mengetik selalu satu dan itu-itu
# saja: tak perlu dicari ulang tiap kali layar berubah.
# Tepi kotak chat & panel gambar mengikuti TEMA AKTIF — dibaca per-render
# lewat fungsi supaya /theme langsung terlihat di mode mengalir juga.
def _garis_kotak() -> str:
    return tema.p("tepi")


_TAG_HTML_RE = re.compile(r"<[^>]+>")


def _panjang_tampak(html: str) -> int:
    """Berapa KOLOM yang benar-benar dipakai sepotong markup HTML prompt_toolkit.

    Tag gaya (`<brand>…</brand>`) tak memakan ruang di layar, jadi len() atas
    teks mentahnya jauh melebihi lebar sesungguhnya — dan perataan yang
    dihitung darinya meleset puluhan kolom. get_cwidth dipakai agar ukurannya
    persis sama dengan cara prompt_toolkit merender teks."""
    tampak = _TAG_HTML_RE.sub("", html)
    tampak = tampak.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return sum(get_cwidth(ch) for ch in tampak)


def _ukuran_pt() -> tuple[int, int] | None:
    """(lebar_gambar, tinggi) menurut OUTPUT prompt_toolkit yang sedang
    menggambar — None bila tak ada app prompt_toolkit yang berjalan.

    Di dalam kotak idle, inilah SATU-SATU sumber yang boleh dipakai: rich
    bisa melaporkan lebar yang BERBEDA (legacy_windows memotong satu kolom;
    COLUMNS/LINES warisan shell menimpa nilai segar), dan penggambar
    prompt_toolkit melipat baris yang lebih lebar dari terminalnya. Selisih
    SATU kolom saja sudah cukup membuat tepi kotak patah — persis kerusakan
    yang muncul tiap kali ukuran terminal diubah. Dengan bertanya langsung
    ke output yang menggambar, bingkai selalu pas berapa pun terminalnya
    berubah, karena prompt_toolkit menggambar ulang pada tiap resize.

    Lebar yang DIKEMBALIKAN sudah dipotong satu kolom: renderer PT memang
    tak pernah melukis kolom terakhir terminal (menghindari auto-wrap), jadi
    elemen selebar "lebar_gambar" ini TEPAT habis tergambar — termasuk
    sudut ╮/╯ dan rel kanan yang sebelumnya selalu hilang."""
    try:
        from prompt_toolkit.application.current import get_app_or_none
        app = get_app_or_none()
        if app is not None and app.is_running:
            uk = app.output.get_size()
            if uk.columns > 1:
                return uk.columns - 1, uk.rows
    except Exception:  # noqa: BLE001
        pass
    return None


def _lebar_kotak() -> int:
    """Lebar kotak chat = lebar terminal dikurangi SATU kolom — di kedua
    penyaji (idle: prompt_toolkit; giliran: rich).

    Saat idle, renderer prompt_toolkit memang tak pernah melukis kolom
    terakhir terminal (anti auto-wrap), jadi W-1 adalah lebar yang BENAR-BENAR
    tergambar (lihat _ukuran_pt). Saat giliran, rich sanggup melukis kolom
    terakhir — tapi memilih W-1 juga membuat kotak & bar status SAMA PERSIS
    lebarnya dengan saat idle, sehingga peralihan idle↔giliran tak pernah
    menggeser tepi satu kolom pun, sekaligus menghindari "pending wrap"
    baris selebar-terminal di console lama.

    Bukan dibatasi _KOTAK_MAKS lagi: kotak chat dan bar status adalah satu
    kesatuan yang menempel di dasar layar, dan bar status memang selebar
    terminal. Kotak yang lebih sempit membuat keduanya tampak tak sejajar."""
    pt = _ukuran_pt()
    return max(20, pt[0] if pt else console.width - 1)


def _tinggi_terminal() -> int:
    """Tinggi terminal versi penggambar aktif (PT saat idle, rich saat giliran).

    Dipakai memutuskan apakah panel rencana runtuh jadi satu baris; sumber
    yang keliru membuat panel runtuh/tegak di saat yang salah."""
    pt = _ukuran_pt()
    return pt[1] if pt else console.height


# Baris kosong pemberi napas DI ATAS kotak chat saja. Di BAWAHNYA sengaja tak
# ada: kotak dan bar status satu kesatuan yang menempel di dasar layar, jadi
# celah di antara keduanya justru memutus kesatuan itu.
_KOSONG = Text("")


# --- konten percakapan: tampung dulu, CETAK oleh thread utama -------------
#
# SEMUA konten percakapan (logo pembuka, gema perintah, narasi, langkah,
# jawaban) menuju scrollback terminal biasa — sekali, permanen, dan bisa
# digulir ke atas. Tapi TIDAK dicetak oleh pemanggilnya langsung: renderables
# ditampung di buffer _KONTEN, dan thread UTAMA yang mencetaknya ke scrollback
# (via _flush_konten) di titik-titik aman.
#
# Kenapa tidak langsung console.print dari worker? Rich Live menggambar ulang
# region bawah tiap ~83ms lewat thread refresh-nya sendiri. Kalau worker
# mencetak ke console di tengah-tengah itu, baris-baris region lama (kotak
# chat, bar status) bisa tertinggal di scrollback — "jejak" yang terlihat
# saat menggulir ke atas. Menampung dulu lalu mencetak dari thread utama
# (yang tahu kapan Live sedang tidak menggambar) menghilangkan tabrakan itu.
#
# Batas baris (_KONTEN_MAKS) mengunci memori; baris tertua di-flush duluan
# oleh _flush_konten sehingga jarang tercapai.
#
# _LIVE adalah referensi Live yang sedang aktif — diisi/dikosongkan oleh
# process_stream/process_classic. "paused" menandai Live yang di-stop
# SEMENTARA (menu ask_user tampil): saat itu TIDAK ada yang boleh mencetak
# ke console (menu inquirer akan rusak), jadi buffer dibiarkan menumpuk.
_LIVE: dict = {"live": None, "paused": False}
_KONTEN: deque[list[Text]] = deque()
_KONTEN_MAKS = 4000
_KONTEN_KUNCI = threading.Lock()
# Jeda minimum antar-flush saat Live AKTIF (≤5x/dtk). Tiap flush memicu
# redraw region penuh (render hook rich) — kalau menyala secepat loop
# (30ms), redraw menumpuk di atas refresh berkala dan memperparah kedip.
# Saat Live sudah tutup, flush langsung tanpa jeda.
_FLUSH_JEDA = 0.2
_FLUSH_TERAKHIR = 0.0


def _tambah_konten(renderables) -> None:
    """Render `renderables` SEKALI; cetak langsung atau tampung.

    Dipanggil dari thread worker maupun thread utama (karena itu di bawah
    kunci). Saat Live TIDAK aktif (startup / idle / sesudah giliran) langsung
    dicetak ke scrollback — aman, tak ada region yang sedang digambar. Saat
    Live AKTIF, renderables DITAMPUNG dan _flush_konten (thread utama) yang
    mencetak di titik aman. Render dilakukan di SINI supaya blok tersimpan
    sebagai baris Text siap-pakai."""
    if not renderables:
        return
    try:
        baris = console.render_lines(Group(*renderables))
    except Exception:  # noqa: BLE001 - render gagal: jangan jatuhkan app
        return
    blok = []
    for seg_baris in baris:
        t = Text()
        for seg in seg_baris:
            if seg.text:
                t.append(seg.text, style=seg.style)
        t.no_wrap = True
        t.overflow = "ellipsis"
        blok.append(t)
    if not blok:
        return
    if _LIVE["live"] is None:
        # Live TIDAK aktif (startup / idle / sesudah giliran): cetak langsung
        # ke scrollback — aman, tidak ada region yang sedang digambar ulang.
        try:
            console.print(Group(*blok))
        except Exception:  # noqa: BLE001 - print gagal: jangan jatuhkan app
            pass
        return
    with _KONTEN_KUNCI:
        _KONTEN.append(blok)
        total = sum(len(b) for b in _KONTEN)
        while total > _KONTEN_MAKS:
            total -= len(_KONTEN.popleft())


def _flush_konten() -> None:
    """Cetak SEMUA konten tertampung ke scrollback, SATU KALI (atomik).

    HANYA dipanggil dari thread utama, di titik aman: di dalam loop Live
    (antar frame), sesudah Live tutup, dan sebelum/ sesudah kotak idle
    bertanya. Menghabiskan buffer di bawah kunci yang sama supaya worker
    yang sedang menambah tidak terpotong di tengah blok."""
    with _KONTEN_KUNCI:
        if not _KONTEN:
            return
        # Live di-stop sementara (menu ask_user tampil): jangan cetak apa pun
        # ke console — menu inquirer akan rusak. Biarkan tertampung; akan
        # ter-flush begitu Live jalan lagi (atau tertutup).
        if _LIVE["live"] is not None and _LIVE["paused"]:
            return
        if _LIVE["live"] is not None:
            global _FLUSH_TERAKHIR
            kini = time.time()
            if kini - _FLUSH_TERAKHIR < _FLUSH_JEDA:
                return
            _FLUSH_TERAKHIR = kini
        antri = list(_KONTEN)
        _KONTEN.clear()
    if not antri:
        return
    # Satu Group besar agar tercetak atomik: dicetak satu-satu memberi celah
    # bagi refresh live menyela di antara dua blok.
    try:
        console.print(Group(*[t for b in antri for t in b]))
    except Exception:  # noqa: BLE001 - print gagal: jangan jatuhkan app
        pass


def _pt_gaya(st) -> str:
    """rich Style -> string gaya prompt_toolkit (untuk jendela idle)."""
    bag = []
    if st.bold:
        bag.append("bold")
    if st.italic:
        bag.append("italic")
    if st.dim:
        bag.append("dim")
    if st.underline:
        bag.append("underline")
    if st.strike:
        bag.append("strike")
    if st.blink:
        bag.append("blink")
    if st.reverse:
        bag.append("reverse")
    for awalan, w in (("fg", st.color), ("bg", st.bgcolor)):
        if w is None or w.is_default:
            continue
        tc = w.get_truecolor()
        if tc is not None:
            bag.append(f"{awalan}:#{tc.red:02x}{tc.green:02x}{tc.blue:02x}")
        elif w.name and w.name != "default":
            bag.append(f"{awalan}:{w.name}")
    return " ".join(bag)


def _pt_dari_teks(t: Text) -> list[tuple[str, str]]:
    """Satu baris rich Text -> formatted text prompt_toolkit."""
    out = []
    for seg in t.render(console):
        if not seg.text:
            continue
        st = seg.style or Style.null()
        out.append((_pt_gaya(st), seg.text))
    return out


# --- panel gambar Minecraft (nempel di bawah kotak chat) ------------------
#
# Saat user me-drop / mengetik path gambar yang valid, blok warna kecil
# ditampilkan tepat di bawah kotak input (bagian dari layout prompt_toolkit).
# Bila path/url dihapus atau tidak valid (mis. huruf dihapus), panel langsung
# hilang seketika. Bila diperbaiki lagi menjadi path valid, panel muncul kembali.
_gambar_state: dict = {}  # pixels: list[list[(r,g,b)]], title: str, path: str
_pending_gambar: dict = {}  # path: str


def _ekstrak_path_gambar(text: str, pending_path: str = "") -> str | None:
    """Cari path file GAMBAR/VIDEO yang valid di dalam teks (atau dari [foto]).

    Video ikut dikenali sejak dukungan analisis video di model API vision —
    pratinjaunya tanpa pixel (lihat _perbarui_pratinjau_gambar), tapi
    mekanisme pending & pelampirannya sama persis."""
    if not text:
        return None

    # 1. Jika ada [foto] dan pending_path masih valid di disk
    if "[foto]" in text and pending_path and \
            (is_image_path(pending_path) or is_video_path(pending_path)):
        return pending_path

    def _media(bersih: str) -> str | None:
        if is_image_path(bersih) or is_video_path(bersih):
            try:
                return str(Path(bersih).resolve())
            except Exception:
                return bersih
        return None

    # 2. Cek apakah seluruh text adalah path (dengan atau tanpa tanda petik / file://)
    bersih = text.strip().strip('"').strip("'").strip()
    ketemu = _media(bersih)
    if ketemu:
        return ketemu

    # 3. Cari quoted paths di dalam text: "..." atau '...'
    quoted = re.findall(r'["\']([^"\']+)["\']', text)
    for q in quoted:
        ketemu = _media(q.strip())
        if ketemu:
            return ketemu

    # 4. Cari kata-kata / token yang merupakan path media valid
    tokens = text.split()
    for tok in tokens:
        tok_b = tok.strip().strip('"').strip("'").strip("(),;[]<>")
        ketemu = _media(tok_b)
        if ketemu:
            return ketemu

    return None


def _isi_state_media(p: str) -> bool:
    """Isi _gambar_state utk satu path media. False bila tak bisa dipratinjau.

    Gambar -> pixel blok warna; VIDEO -> tanpa pixel (renderer menampilkan
    baris judul saja). State tanpa isi membuat panel tak muncul sama sekali,
    jadi kegagalan muat gambar tetap ditolak di sini."""
    if is_video_path(p):
        _gambar_state.clear()
        _gambar_state["title"] = Path(p).name
        _gambar_state["path"] = p
        return True
    px = image_to_blocks_pixels(p)
    if not px:
        return False
    _gambar_state.clear()
    _gambar_state["pixels"] = px[0]
    _gambar_state["title"] = Path(p).name
    _gambar_state["path"] = p
    return True


def _perbarui_pratinjau_gambar(
    text: str,
    app: Application | None = None,
    buf: Buffer | None = None,
) -> None:
    """Perbarui _gambar_state secara reaktif berdasarkan teks saat ini.

    Bila teks mengandung path gambar/video yang valid (bukan sudah diganti
    [foto]):
      • Muat pixel → tampilkan pratinjau Minecraft di bawah kotak (gambar);
        video tampil sebagai baris judul saja.
      • Ganti teks di buffer dengan [foto] supaya pesan yang terkirim konsisten.
    Bila path tidak valid → hapus pratinjau seketika.
    """
    # Hindari loop: on_text_changed dipanggil lagi saat kita SET buffer.text.
    # Kalau teks sudah berisi [foto] dan pending_path masih ada, tinggal
    # periksa pending_path-nya saja — tidak perlu mencari path lagi di teks.
    if "[foto]" in text and _pending_gambar.get("path"):
        # Pratinjau sudah ada & pending ok → pastikan _gambar_state masih terisi.
        if not _gambar_state.get("pixels") and not _gambar_state.get("title"):
            p = _pending_gambar["path"]
            _isi_state_media(p)
            if app is not None:
                try:
                    app.invalidate()
                except Exception:
                    pass
        return

    p = _ekstrak_path_gambar(text, _pending_gambar.get("path", ""))
    berubah = False
    if p:
        if _gambar_state.get("path") != p:
            if _isi_state_media(p):
                _pending_gambar["path"] = p
                berubah = True
                # Ganti teks buffer dengan [foto] (hanya bila teks bukan [foto]).
                if buf is not None and text != "[foto]":
                    try:
                        buf.set_document(
                            Document("[foto]", cursor_position=6),
                            bypass_readonly=True,
                        )
                    except Exception:
                        pass
            elif _gambar_state:
                _gambar_state.clear()
                berubah = True
    else:
        if _gambar_state:
            _gambar_state.clear()
            berubah = True
        # Bila [foto] ada tapi pending tidak valid lagi → bersihkan juga.
        if _pending_gambar:
            _pending_gambar.clear()
    if berubah and app is not None:
        try:
            app.invalidate()
        except Exception:
            pass


def _kembangkan_foto(teks: str) -> str:
    """Tukar penanda [foto] dengan penanda lampiran `[GAMBAR] <path>`.

    [foto] CUMA bentuk tampilan di kotak ketik & gema riwayat — yang dikirim
    ke model wajib penanda [GAMBAR], sebab hanya itu yang dikenali core dan
    dipisah jadi LAMPIRAN sungguhan (Agent.run). Dulu [foto] ditukar jadi
    path telanjang di tengah kalimat; tak ada yang membacanya sebagai
    lampiran, jadi model menerima teks path dan fotonya tak pernah terkirim.
    Baris-baris baru di sekelilingnya sengaja ada: penandanya berlaku per
    BARIS, dan tanpa itu "[foto] analisis ini" melahirkan path+prompt dalam
    SATU baris yang tak tertangkap regex.

    Gambar pending TANPA [foto] di teks tetap dilampirkan ke pesan ini —
    perilaku "drop lalu langsung Enter" yang sudah biasa. Pending dibersihkan
    sesudah dipakai: satu gambar, satu pesan."""
    p = _pending_gambar.get("path")
    if not p:
        return teks
    if "[foto]" in teks:
        teks = teks.replace("[foto]", f"\n{IMAGE_MARK} {p}\n")
    elif teks.strip():
        teks = f"{teks}\n{IMAGE_MARK} {p}"
    else:
        teks = f"{IMAGE_MARK} {p}"
    _pending_gambar.clear()
    return teks

def _gambar_idle_pt() -> list[tuple[str, str]]:
    """Render blok warna gambar untuk jendela idle (prompt_toolkit).

    Mengembalikan formatted text lines bergaya kotak kecil dengan blok
    warna per-pixel. Kosong bila tak ada gambar.
    """
    rows = _gambar_state.get("pixels")
    title = _gambar_state.get("title", "")
    bdr = "class:garis"
    if not rows:
        # VIDEO / media tanpa pratinjau pixel: satu kotak judul mini, supaya
        # pengguna tetap TAHU lampirannya menempel (bukan hilang diam-diam).
        if title:
            return [
                (bdr, "╭─ "), ("class:tanda", f"🎞 {title}"), (bdr, " ─╮"),
                ("", "\n"),
                (bdr, "╰" + "─" * (get_cwidth(title) + 8) + "╯"),
            ]
        return []
    n_cols = max(len(r) for r in rows) if rows else BLOCK_W
    # Judul di tengah tepi atas (dipotong bila panjang).
    avail = max(0, n_cols - 2)
    t = title[:avail] if title else ""
    tw = sum(get_cwidth(ch) for ch in t)
    pad_l = max(0, (n_cols - tw) // 2)
    pad_r = max(0, n_cols - tw - pad_l)
    out: list[tuple[str, str]] = []
    def _add_line(frags):
        out.extend(frags)
        out.append(("", "\n"))
    # ╭── judul ──╮
    _add_line([(bdr, "╭"), (bdr, "─" * pad_l), ("class:tanda", t),
               (bdr, "─" * pad_r), (bdr, "╮")])
    # │ pixels  │
    for row in rows:
        frags = [(bdr, "│")]
        for r, g, b in row:
            frags.append((f"bg:#{r:02x}{g:02x}{b:02x}", " "))
        sisa = n_cols - len(row)
        if sisa > 0:
            frags.append(("", " " * sisa))
        frags.append((bdr, "│"))
        _add_line(frags)
    # ╰──────────╯
    _add_line([(bdr, "╰"), (bdr, "─" * n_cols), (bdr, "╯")])
    # Buang newline terakhir (tidak perlu di akhir).
    if out and out[-1] == ("", "\n"):
        out.pop()
    return out

def _panel_gambar_rich() -> list[Text]:
    """Kembar RICH dari _gambar_idle_pt: blok pratinjau gambar sebagai baris
    Text. Dipakai region live saat giliran berjalan supaya urutan tumpukan
    bawah sama persis dengan saat idle (status · gambar · kotak · rencana)."""
    rows = _gambar_state.get("pixels")
    title = _gambar_state.get("title", "")
    if not rows:
        # Kembaran fallback video _gambar_idle_pt (versi rich).
        if title:
            baris = Text("╭─ ", style=_garis_kotak())
            baris.append(f"🎞 {title}", style=f"bold {tema.p('aksen')}")
            baris.append(" ─╮", style=_garis_kotak())
            bawah = Text("╰" + "─" * (get_cwidth(title) + 8) + "╯",
                         style=_garis_kotak())
            return [_oneline(baris), _oneline(bawah)]
        return []
    n_cols = max(len(r) for r in rows)
    avail = max(0, n_cols - 2)
    t = title[:avail]
    tw = sum(get_cwidth(ch) for ch in t)
    pad_l = max(0, (n_cols - tw) // 2)
    pad_r = max(0, n_cols - tw - pad_l)
    out: list[Text] = []
    atas = Text("╭", style=_garis_kotak())
    atas.append("─" * pad_l, style=_garis_kotak())
    atas.append(t, style=f"bold {tema.p('aksen')}")
    atas.append("─" * pad_r, style=_garis_kotak())
    atas.append("╮", style=_garis_kotak())
    out.append(atas)
    for row in rows:
        baris = Text("│", style=_garis_kotak())
        for r, g, b in row:
            baris.append(" ", style=f"on rgb({r},{g},{b})")
        sisa = n_cols - len(row)
        if sisa > 0:
            baris.append(" " * sisa)
        baris.append("│", style=_garis_kotak())
        out.append(baris)
    bawah = Text("╰" + "─" * n_cols + "╯", style=_garis_kotak())
    out.append(bawah)
    return [_oneline(t) for t in out]


def _plan_idle_pt() -> list[tuple[str, str]]:
    """Baris panel rencana untuk jendela idle (kosong bila tak ada rencana).

    Jendela terpisah dari konten supaya panel rencana menempel LANGSUNG di
    atas kotak chat — sama persis seperti region rich saat giliran berjalan
    (bingkai ╰─╯ dan ╭─╮ bersentuhan)."""
    try:
        plan = _panel_plan()
        if not plan:
            return []
        out: list[tuple[str, str]] = []
        for i, t in enumerate(plan):
            if i:
                out.append(("", "\n"))
            out.extend(_pt_dari_teks(t))
        return out
    except Exception:  # noqa: BLE001 - render gagal: jangan jatuhkan kotak
        return []


# --- menu pilihan untuk ask_user -------------------------------------------
#
# Dua hal yang dulu tak ada dan membuat menunya terasa buntu:
#
#   1. TAK ADA JALAN KELUAR. Pilihan yang disodorkan AI tak selalu memuat yang
#      sebenarnya diinginkan pengguna, dan satu-satunya cara menyimpang adalah
#      Esc — yang terbaca sebagai "dibatalkan", bukan sebagai jawaban. Kini tiap
#      menu selalu punya entri isian bebas.
#   2. PADA MODE BANYAK-PILIHAN, tak jelas kapan jawabannya terkirim. Enter
#      memang mengirim, tapi tak ada apa pun di layar yang mengatakannya. Kini
#      footernya menyebut "⏎ KIRIM JAWABAN" dengan tegas.
_OPSI_TULIS = "✎ Tulis jawaban sendiri…"


def _tanya_pilihan(question: str, options: list[str], multiple: bool) -> str:
    """Tampilkan menu pilihan dari ask_user & kembalikan jawabannya sebagai teks.

    `multiple` menentukan bentuk menunya: satu jawaban (pilih lalu Enter) atau
    banyak jawaban (spasi menandai, Enter mengirim). Keduanya sama-sama diberi
    entri isian bebas di urutan terakhir."""
    pilihan = list(options) + [_OPSI_TULIS]

    def _tulis_sendiri(judul: str) -> str:
        jawab = (inquirer.text(message=judul).execute() or "").strip()
        return jawab

    if not multiple:
        dipilih = inquirer.select(message=question, choices=pilihan).execute()
        if dipilih != _OPSI_TULIS:
            return dipilih
        # Isian kosong bukan jawaban — kembalikan ke menunya daripada
        # menyerahkan string kosong yang tak berarti apa-apa ke AI.
        while True:
            jawab = _tulis_sendiri("Jawabanmu")
            if jawab:
                return jawab
            dipilih = inquirer.select(
                message=question + "  (isian kosong — pilih lagi)",
                choices=pilihan).execute()
            if dipilih != _OPSI_TULIS:
                return dipilih

    hasil = inquirer.checkbox(
        message=question, choices=pilihan,
        instruction="spasi tandai  ·  a semua  ·  ⏎ KIRIM JAWABAN  ·  esc batal",
    ).execute()
    hasil = list(hasil or [])
    if _OPSI_TULIS in hasil:
        hasil.remove(_OPSI_TULIS)
        tambahan = _tulis_sendiri("Jawaban tambahanmu")
        if tambahan:
            hasil.append(tambahan)
    if not hasil:
        return "(tidak memilih apa pun)"
    # Dinomori supaya AI tak salah membaca jawaban majemuk sebagai satu kalimat
    # panjang — terutama bila salah satunya isian bebas yang memuat koma.
    if len(hasil) == 1:
        return hasil[0]
    return "; ".join(f"({i}) {j}" for i, j in enumerate(hasil, 1))


def _kotak_chat(isi: str = "", pos: int | None = None) -> list:
    """Tiga baris kotak chat selebar terminal: tepi atas, baris isi, tepi bawah.

    Kembarannya saat idle adalah KotakChat (prompt_toolkit) — bentuk, lebar,
    dan warna "❯"-nya sengaja dibuat sama persis supaya kotak chat terasa satu
    benda yang sama, bukan dua tampilan yang mirip.

    `pos` = letak kursor di dalam `isi`. None berarti di ujung."""
    lebar = _lebar_kotak()
    atas = Text()
    atas.append("╭" + "─" * (lebar - 2) + "╮", style=_garis_kotak())
    baris = Text()
    baris.append("│ ", style=_garis_kotak())
    baris.append("❯ ", style=f"bold {tema.p('aksen')}")
    if isi:
        n = len(isi)
        k = n if pos is None else max(0, min(int(pos), n))
        muat = max(8, lebar - 6)
        # Jendela geser yang selalu MEMUAT KURSOR. Dulu yang ditampilkan selalu
        # ekor teks — benar selama mengetik di ujung, tapi begitu kursor bisa
        # digeser, membetulkan awal kalimat panjang jadi mustahil: yang terlihat
        # tetap ekornya sementara kursornya entah di mana.
        mulai = 0 if n <= muat else max(0, min(k - muat // 2, n - muat))
        tampak = isi[mulai:mulai + muat]
        rel = k - mulai
        baris.append(tampak[:rel], style=tema.p("teks"))
        baris.append("▌", style=tema.p("aksen"))
        baris.append(tampak[rel:], style=tema.p("teks"))
    # Potong dulu, baru ratakan sampai tepi kanan — kalau tidak, isi yang
    # kepanjangan mendorong tepi kanannya keluar layar dan kotaknya patah.
    baris.truncate(lebar - 1, overflow="ellipsis")
    pad = (lebar - 1) - baris.cell_len
    if pad > 0:
        baris.append(" " * pad)
    baris.append("│", style=_garis_kotak())
    bawah = Text()
    bawah.append("╰" + "─" * (lebar - 2) + "╯", style=_garis_kotak())
    return [_oneline(atas), _oneline(baris), _oneline(bawah)]


# Tinggi maksimum daftar sugesti "/..." di dalam kotak chat.
_MENU_MAKS = 8

# --- hasil sebuah langkah: LANGSUNG di terminal, di tempatnya --------------
#
# Sejarah singkat supaya tak berputar lagi ke jalan yang sudah buntu:
#
#   1. `/expand N` — harus diketik, dan memaksa tiap langkah memamerkan nomor
#      yang tak berguna untuk hal lain. Dibuang.
#   2. Berkas .html + tautan yang dibuka dengan Ctrl-klik — bisa diklik, tapi
#      menaburkan berkas di disk dan membuka jendela lain. Dibuang.
#   3. Menangkap KLIK di baris langkahnya sendiri — TIDAK BISA, dan bukan soal
#      kemauan: (a) klik hanya sampai ke aplikasi bila mouse capture menyala,
#      dan itu menelan scroll wheel; (b) begitu tercetak, baris langkah jadi
#      scrollback milik terminal — aplikasi tak punya cara tahu baris apa yang
#      ada di koordinat yang diklik, apalagi menulis ulang di sana.
#
# Maka hasilnya ditampilkan saja SEKARANG, di tempatnya, dengan latar abu-abu
# gelap sebagai pembeda — dibatasi beberapa baris supaya keluaran 400 baris tak
# menenggelamkan riwayat.
_PRATINJAU_BARIS = 8
# Latar panel hasil (mode mengalir) mengikuti tema — dibaca per-render.
def _BG_HASIL() -> str:
    return tema.p("menu_bg")


def _pratinjau_hasil(lines: list[str], *, gagal: bool = False) -> list:
    """Blok pratinjau keluaran sebuah langkah, berlatar abu-abu gelap.

    Baris kosong di ujung dibuang lebih dulu: keluaran perintah hampir selalu
    berakhir dengan newline, dan tanpa ini blok abu-abunya punya baris kosong
    menggantung yang membuatnya terlihat seperti salah render."""
    isi = [ln.rstrip() for ln in lines]
    while isi and not isi[-1].strip():
        isi.pop()
    while isi and not isi[0].strip():
        isi.pop(0)
    if not isi:
        return []
    sisa = len(isi) - _PRATINJAU_BARIS
    tampil = isi[:_PRATINJAU_BARIS]
    if sisa > 0:
        tampil.append(f"… {sisa} baris lagi")
    lebar = max(20, console.width - 5)
    gaya = f"on {_BG_HASIL()}"
    tepi = "#f0603c" if gagal else tema.p("tepi")
    out = []
    for i, ln in enumerate(tampil):
        baris = Text("     ")
        # Garis tepi kiri: penanda "ini satu blok", jauh lebih murah daripada
        # bingkai penuh yang bakal beradu dengan kotak chat satu-satunya.
        baris.append("▏", style=f"{tepi} {gaya}")
        redup = sisa > 0 and i == len(tampil) - 1
        baris.append(" " + ln, style=f"{tema.p('redup') + ' italic' if redup else tema.p('teks')} {gaya}")
        baris.truncate(lebar, overflow="ellipsis")
        pad = lebar - baris.cell_len
        if pad > 0:
            baris.append(" " * pad, style=gaya)
        out.append(_oneline(baris))
    return out


def _panel_plan() -> list:
    """Panel rencana tugas yang dipasang tetap di atas kotak chat.

    Hanya tampil saat ada rencana aktif (plan() sudah dipanggil, belum di-reset
    oleh giliran berikutnya). Dibaca via plan_tool.get_state() tiap frame
    Live (~100ms), jadi selalu sinkron: centang otomatis muncul begitu
    plan_step() mengubah flag completed[i] dari False ke True,
    tanpa perlu mekanisme notifikasi.

    Dirender sebagai blok berbingkai tipis dengan lebar sama persis kotak chat
    (_lebar_kotak), supaya keduanya terasa satu kolom yang padu. Bila layar
    terlalu pendek untuk memuatnya, panel runtuh jadi SATU baris ringkas —
    kotak chat & bar status tak boleh terdorong keluar layar (render rich.Live
    kacau bila regionnya lebih tinggi dari layar)."""
    from ..tools import plan_tool
    snap = plan_tool.get_state()
    steps = snap["steps"]
    cur = snap["current"]
    completed = snap["completed"]
    if not steps:
        return []
    lebar = _lebar_kotak()
    if lebar < 24:
        return []  # terminal terlalu sempit

    n = len(steps)
    selesai = sum(completed)

    # Layar terlalu pendek: ringkas jadi satu baris. Anggaran 10 baris =
    # footer spinner + tips + dua baris kosong + kotak chat + bar status.
    # Tanpa ambang minimum: region live TAK BOLEH lebih tinggi dari layar,
    # berapa pun kecilnya layar (render rich.Live kacau bila itu terjadi).
    if n + 4 > _tinggi_terminal() - 10:
        ringkas = Text()
        ringkas.append("  ◈ ", style=f"bold {tema.p('aksen')}")
        ringkas.append(f"rencana {selesai}/{n}", style=tema.p("aksen_terang"))
        if 1 <= cur <= n:
            ringkas.append(f"  ▸ {steps[cur - 1]}", style=tema.p("teks"))
        return [_oneline(ringkas)]

    tepi = tema.p("tepi_redup")

    def dibingkai(isi: Text) -> Text:
        """Tepi kiri/kanan + padding kanan sehingga baris selebar `lebar` persis.
        Isi dipotong lebih dulu — tanpa ini, judul yang panjang mendorong tepi
        kanan keluar layar dan bingkainya patah."""
        isi.truncate(lebar - 4, overflow="ellipsis")
        b = Text()
        b.append("│ ", style=tepi)
        b.append(isi)
        pad = (lebar - 1) - b.cell_len
        if pad > 0:
            b.append(" " * pad)
        b.append("│", style=tepi)
        return _oneline(b)

    out = [_oneline(Text("╭" + "─" * (lebar - 2) + "╮", style=tepi))]

    # Header: judul + counter langkah selesai. Pakai "◈" alih-alih emoji —
    # lebarnya pasti 1 kolom di semua terminal, jadi tepi kanan bingkai kotak
    # ini tetap lurus (lebar emoji bisa tak sepakat antara rich & terminal).
    header = Text()
    header.append("◈ ", style=f"bold {tema.p('aksen')}")
    header.append("rencana", style=f"bold {tema.p('teks')}")
    header.append(f"  ·  {selesai}/{n} selesai", style=f"dim {tema.p('redup')}")
    out.append(dibingkai(header))

    # Separator tipis
    out.append(_oneline(Text("├" + "─" * (lebar - 2) + "┤", style=tepi)))

    # Body: satu baris per langkah, dengan ikon status
    for i, s in enumerate(steps, 1):
        isi = Text()
        if completed[i - 1]:
            # Selesai: centang hijau + teks redup (flag completed=True).
            isi.append("✓ ", style="bold #9fc93c")
            isi.append(s, style=tema.p("redup"))
        elif i == cur:
            # Sedang dikerjakan: panah kuning + teks terang.
            isi.append("▸ ", style=f"bold {tema.p('aksen')}")
            isi.append(s, style=tema.p("teks"))
        else:
            # Belum: titik redup + teks redup.
            isi.append("· ", style=tema.p("tepi"))
            isi.append(s, style=tema.p("tepi"))
        out.append(dibingkai(isi))

    # Garis bawah
    out.append(_oneline(Text("╰" + "─" * (lebar - 2) + "╯", style=tepi)))
    return out


# --- memaku tampilan ke DASAR LAYAR ----------------------------------------
#
# Saat idle tak ada yang perlu dilakukan: prompt_toolkit selalu memberi aplikasi
# non-fullscreen setinggi sisa baris di bawah kursor, dan Window pendorong di
# KotakChat._rakit mendorong kotak + bar mentok ke bawah.
#
# rich.Live tak punya mekanisme itu — ia menggambar di posisi kursor apa adanya,
# jadi kalau layar masih lowong regionnya menggantung di tengah. Karena itu
# kursornya didorong dulu ke baris terakhir SEBELUM region mulai menggambar.
# Sesudah menyentuh dasar, ia tinggal di sana sendiri: tiap baris baru yang
# tercetak di atasnya menggulung layar, bukan menggeser regionnya turun.
_KELUARAN_PT: dict = {"out": None, "menyerah": False}


def _sisa_baris_bawah() -> int | None:
    """Jumlah baris dari kursor sampai baris terakhir layar (kursornya ikut).

    Dipinjam dari prompt_toolkit karena ia sudah menyediakannya per-platform
    (di Windows lewat GetConsoleScreenBufferInfo). None = terminalnya tak bisa
    ditanya; itu bukan kegagalan, cuma berarti tampilan tak bisa dipaku ke dasar
    di sana — semua yang lain tetap jalan seperti biasa."""
    if _KELUARAN_PT["menyerah"]:
        return None
    try:
        if _KELUARAN_PT["out"] is None:
            from prompt_toolkit.output.defaults import create_output
            _KELUARAN_PT["out"] = create_output(stdout=sys.stdout)
        return _KELUARAN_PT["out"].get_rows_below_cursor_position()
    except Exception:  # noqa: BLE001 - sekali gagal, jangan dicoba tiap giliran
        _KELUARAN_PT["menyerah"] = True
        return None


def _ke_dasar_layar() -> None:
    """RAPATKAN layar sebelum kotak/region baru digambar: kursor ke kolom 0,
    lalu HAPUS semua baris di bawahnya (erase-down, TANPA menggulir).

    Tiga tahap sudah dicoba, dan jejak dua yang pertama masih terlihat di
    transkrip pengguna sebagai 'jarak antar prompt & jawaban yang berjauhan'
    (banding: --resume yang mencetak transkrip rapat tampil normal):
      1. banjir '\\r\\n' sebanyak sisa baris — tiap barisnya MASUK SCROLLBACK;
      2. Cursor Down (turun tanpa menggulir) — tak menambah baris, tapi kursor
         DITELEPORT ke dasar, sehingga baris kosong di antara konten & dasar
         tergulir menjadi celah permanen begitu giliran berikutnya menggulir
         layar;
      3. kini: erase-down + render di POSISI KURSOR. Region live yang mulai
         tepat di bawah konten akan menyentuh dasar SENDIRI saat isinya
         memenuhi layar (mekanisme sisip rich), jadi transkrip tetap rapat
         seperti --resume dan tak ada baris basi/ghost yang tertinggal.

    Kolom dikembalikan ke 0 dengan \\r karena aplikasi inline prompt_toolkit
    (menu ask_user) menggambar baris pertamanya PERSIS di posisi kursor."""
    try:
        console.file.write("\r")
        console.file.flush()
    except Exception:  # noqa: BLE001 - terminal tak bisa ditulis
        return
    try:
        if _KELUARAN_PT["out"] is None:
            from prompt_toolkit.output.defaults import create_output
            _KELUARAN_PT["out"] = create_output(stdout=sys.stdout)
        _KELUARAN_PT["out"].erase_down()
        _KELUARAN_PT["out"].flush()
    except Exception:  # noqa: BLE001 - erase-down tak didukung: diam saja
        _KELUARAN_PT["menyerah"] = True


def _retempel_live(live) -> None:
    """Setelah ukuran terminal berubah: tempelkan lagi region live ke DASAR
    layar, lalu segarkan.

    rich menggambar ulang regionnya pada baris yang SAMA saat di-refresh;
    bila terminalnya baru saja DIPERBESAR tingginya, baris itu kini ada di
    tengah layar dan region tampak menggantung dengan ruang kosong di
    bawahnya. Mencetak baris kosong lewat console (rich menyelipkannya di
    ATAS region yang aktif lalu menggeser region ke bawah) menurunkan
    region baris demi baris sampai menempel dasar lagi — mekanisme yang
    sama dengan cetak konten biasa selama giliran berjalan."""
    sisa = _sisa_baris_bawah() or 0
    for _ in range(min(sisa, console.height)):
        console.print("")
    live.refresh()


# --- bar status permanen ---------------------------------------------------
#
# Bar ini PASANGAN TETAP kotak chat: kotak selalu menempel persis di atasnya,
# dan keduanya tak pernah menghilang — baik saat kamu mengetik maupun saat AI
# masih menyusun jawabannya. Saat idle ia digambar prompt_toolkit (lihat
# KotakChat), saat giliran berjalan ia digambar rich dari sini; isinya dijaga
# sama persis supaya perpindahan antar keduanya tak terlihat.
#
# Seluruh warnanya milik TEMA AKTIF (dibaca per-render di _bar_status &
# _buat_pt_style) — pada tema "default" footer berlatar PUTIH dengan teks
# gelap, sesuai permintaan pengguna; tema lain membawa keluarga warnannya
# sendiri yang kontrasnya dijaga.
def _BG_BAR() -> str:
    return tema.p("bg_footer")

# Urutan PENGORBANAN saat terminal menyempit, dari isi yang paling bisa
# dilepas. Bar status dipatok di dasar layar, jadi ia tak boleh terlipat: satu
# baris yang meluber merusak seluruh susunan kotak chat di atasnya.
#
# Daftar ini dipakai BERSAMA oleh kedua bar — versi prompt_toolkit (saat idle)
# dan versi rich (saat giliran berjalan). Keduanya tampil bergantian di tempat
# yang sama persis, jadi aturan yang berbeda akan terlihat sebagai layar yang
# melompat tiap kali giliran mulai atau selesai.
_TINGKAT_BAR: tuple[tuple[str, ...], ...] = (
    ("merek", "model", "git", "ubah", "perintah", "ctrlc"),
    ("merek", "model", "git", "ubah", "perintah"),
    ("merek", "model", "git",  "perintah", "ctrlc"),
    ("merek", "model", "git",  "perintah"),
    ("merek", "model", "git", "perintah"),
    ("merek", "model", "perintah"),
    ("model", "perintah"),
    ("model", "exit"),
)


def _bagian_bar(lebar: int, ukur) -> tuple[str, ...]:
    """Bagian mana yang muat di lebar ini. `ukur(nama)` -> jumlah kolomnya.

    Sisa dua kolom disediakan sebagai jarak minimal kiri-kanan: menempel rapat
    membuat keterangan dan perintah terbaca sebagai satu deretan, padahal
    justru pemisahannya yang jadi maksud susunan ini."""
    for tingkat in _TINGKAT_BAR:
        if sum(ukur(b) for b in tingkat) + 2 <= lebar:
            return tingkat
    return _TINGKAT_BAR[-1]


def _git_info() -> tuple[str, int]:
    """(branch, berkas_berubah) dari repo di folder proyek — di-cache 5 detik.

    Dipanggil tiap frame render, jadi harus murah: git dijalankan paling
    sering sekali per 5 detik, sisanya dibaca dari cache."""
    now = time.time()
    if now - _git_info._t < 5.0:          # cache masih segar
        return _git_info._v              # type: ignore[attr-defined]
    _git_info._t = now
    try:
        r = config.PROJECT_ROOT
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=r,
        ).stdout.strip()
        if not branch:
            _git_info._v = ("", 0)
            return _git_info._v
        count = 0
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=r,
        ).stdout.strip()
        if out:
            count = len(out.splitlines())
        # Staged tapi belum commit juga dihitung.
        out2 = subprocess.run(
            ["git", "diff", "--staged", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=r,
        ).stdout.strip()
        if out2:
            staged = set(out2.splitlines()) - set(out.splitlines())
            count += len(staged)
        # Untracked files.
        out3 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=r,
        ).stdout.strip()
        if out3:
            count += len(out3.splitlines())
        _git_info._v = (branch, count)
    except (OSError, FileNotFoundError):
        _git_info._v = ("", 0)
    return _git_info._v


_git_info._t = 0.0     # type: ignore[attr-defined]
_git_info._v = ("", 0) # type: ignore[attr-defined]


def _bar_status(agent: Agent, total: int) -> Text:
    """Satu baris: ⬢ bagas-ai · model · token sesi ……… /menu · /exit.

    `total` tetap diterima meski tak lagi ditampilkan: penghitungannya jalan
    terus dan pemanggilnya tak perlu ikut berubah — yang dibuang cuma
    tampilannya (lihat status_bar di main untuk alasan yang sama).
    """
    s = agent.tokens_session
    spec = agent.model_spec
    SEP = "  │  "
    teks = {
        "merek": " ⬢ bagas-ai",
        "model": f"{SEP}{'🌐' if spec.is_web else '🤖'} {spec.label}",
        "perintah": "/menu · /exit",
        "ctrlc": " atau ctrl+c",
        "exit": "/exit",
    }
    git_branch, git_changed = _git_info()
    teks["git"] = f"{SEP}🌿 {git_branch}" if git_branch else ""
    teks["ubah"] = f"{SEP}📝 {git_changed}" if git_changed else ""
    ada = _bagian_bar(_lebar_kotak(), lambda b: Text(teks[b]).cell_len + 1)

    bar = Text(style=f"on {_BG_BAR()}")
    if "merek" in ada:
        bar.append(" ⬢ bagas-ai", style=f"bold {tema.p('merek_footer')}")
    if "model" in ada:
        if "merek" in ada:
            bar.append(SEP, style=tema.p("sep_footer"))
        else:
            bar.append(" ")
        bar.append(f"{'🌐' if spec.is_web else '🤖'} ")
        bar.append(spec.label, style=f"bold {tema.p('model_footer')}")
    if "git" in ada and git_branch:
        bar.append(SEP, style=tema.p("sep_footer"))
        bar.append("🌿 ", style=tema.p("git_footer"))
        bar.append(git_branch, style=tema.p("git_footer"))
    if "ubah" in ada and git_changed:
        bar.append(SEP, style=tema.p("sep_footer"))
        bar.append(f"📝 {git_changed}", style=tema.p("ubah_footer"))
    # Segmen "sesi" DIHAPUS atas permintaan pengguna: token sesi sudah
    # ditampilkan di baris status live (⚡ X sesi), tak perlu duplikat di footer.

    # Perintah didorong ke tepi KANAN, bukan disambung dengan pemisah:
    # perintah bukan bagian dari deretan keterangan di kiri — ia hal lain, dan
    # memisahkannya secara ruang membuat keduanya terbaca sekali lihat.
    kanan = Text(style=f"on {_BG_BAR()}")
    if "perintah" in ada:
        kanan.append("/menu", style=tema.p("cmd_footer"))
        kanan.append(" · ", style=tema.p("sep_footer"))
    kanan.append("/exit", style=tema.p("exit_footer"))
    if "ctrlc" in ada:
        kanan.append(" atau ctrl+c", style=tema.p("muted_footer"))
    kanan.append(" ")

    # Sisanya diisi spasi supaya latarnya jadi PITA penuh, bukan potongan
    # pendek yang menggantung di tengah baris. Lebarnya _lebar_kotak()
    # (terminal - 1) supaya sama persis dengan bar versi idle.
    antara = _lebar_kotak() - bar.cell_len - kanan.cell_len
    if antara > 0:
        bar.append(" " * antara)
    bar.append_text(kanan)
    return _oneline(bar)


class KotakChat:
    """Kotak chat saat idle: satu Application prompt_toolkit, tiga baris.

    Kenapa merakit Application sendiri dan bukan memakai PromptSession:
    PromptSession tak sanggup MENUTUP kotak di sekeliling baris ketikan, dan
    dua akalannya sama-sama patah — terbukti pada tampilan rusak yang
    dilaporkan:

      1. Tepi kanan hanya bisa dititipkan ke `rprompt`, tapi prompt_toolkit
         menempelkan rprompt di baris PERTAMA blok input. Karena tepi ATAS
         kotak juga tinggal di sana (prompt berisi "\\n"), tepi kanan itu
         mendarat di baris yang salah sekaligus di ujung terminal — bukan di
         tepi kotak yang lebarnya dibatasi _lebar_kotak().
      2. Tepi bawah hanya bisa dititipkan ke `bottom_toolbar`, padahal
         PromptSession MEMESAN ruang menu autocomplete (reserve_space_for_menu,
         bawaan 8 baris) tepat di ANTARA baris ketikan dan toolbar selama
         complete_while_typing hidup. Ruang kosong itulah yang merobek kotak
         jadi belasan baris menganga.

    Di sini kotaknya container biasa: tepi atas/bawah satu Window teks, tepi
    kiri/kanan Window ber-`char`. Jadi keempat sisinya rapat mengelilingi
    ketikan berapa pun barisnya (teks yang membungkus ikut berbingkai), dan
    daftar sugesti "/..." TUMBUH DI DALAM kotak alih-alih jadi lubang kosong.
    """

    def __init__(self, *, status, key_bindings, style, completer=None):
        self._status = status

        def _on_buf_change(_buf: Buffer) -> None:
            _perbarui_pratinjau_gambar(
                _buf.text,
                getattr(self, "app", None),
                _buf,
            )

        self.buffer = Buffer(
            multiline=False,
            completer=completer,
            complete_while_typing=True,
            history=InMemoryHistory(),
            accept_handler=self._terima,
            on_text_changed=_on_buf_change,
        )
        self._isi = Window(
            BufferControl(self.buffer),
            get_line_prefix=self._awalan_baris,
            wrap_lines=True,
            height=D(min=1),
            dont_extend_height=True,
        )
        self.app: Application = Application(
            layout=Layout(self._rakit(), focused_element=self._isi),
            key_bindings=key_bindings,
            style=style,
            # Kotaknya HILANG begitu Enter ditekan; yang tertinggal di riwayat
            # cuma gema "❯ pesan" yang dicetak pemanggil. Tanpa ini scrollback
            # penuh potongan kotak setengah jadi (tepi atas + baris ketikan
            # tanpa tepi bawah) — sisa render terakhir prompt_toolkit.
            erase_when_done=True,
        )

    # --- potongan tampilan ---------------------------------------------------
    @staticmethod
    def _awalan_baris(nomor: int, lipat: int):
        """"❯ " hanya di baris pertama; baris lipatan diberi lekuk selebar itu
        supaya hurufnya tetap lurus di bawah huruf pertama."""
        if nomor == 0 and lipat == 0:
            return [("class:tanda", "❯ ")]
        return [("", "  ")]

    @staticmethod
    def _tepi(kiri: str, kanan: str) -> Window:
        def teks():
            return [("class:garis", kiri + "─" * (_lebar_kotak() - 2) + kanan)]
        return Window(FormattedTextControl(teks), height=1, dont_extend_height=True)

    @staticmethod
    def _sisi() -> Window:
        return Window(width=1, char="│", style="class:garis")

    def _tinggi_menu(self) -> D:
        """Tinggi daftar sugesti = JUMLAH sugestinya (dibatasi _MENU_MAKS).

        Dipatok tepat, bukan diserahkan ke tinggi pilihan menu: kalau tidak,
        selalu tersisa satu baris kosong di antara sugesti terakhir dan tepi
        bawah — kotak yang terlihat bocor lagi, walau cuma sebaris."""
        st = self.buffer.complete_state
        n = len(st.completions) if st else 0
        return D.exact(max(1, min(n, _MENU_MAKS)))

    def _rakit(self) -> HSplit:
        # SPACER KANAN: renderer prompt_toolkit tak pernah melukis kolom
        # TERAKHIR terminal (anti auto-wrap), jadi VSplit yang berakhir
        # persis di tepi kehilangan rel "│" kanannya. Spacer selebar satu
        # kolom menggeser seluruh baris ke dalam area yang benar-benar
        # dilukis, dan lebarnya mengikuti _lebar_kotak() yang sudah
        # terpotong satu kolom itu (lihat _ukuran_pt).
        kotak = HSplit(
            [
                self._tepi("╭", "╮"),
                VSplit([self._sisi(), Window(width=1), self._isi,
                        Window(width=1), self._sisi(), Window(width=1)]),
                ConditionalContainer(
                    VSplit([self._sisi(), Window(width=1),
                            CompletionsMenu(max_height=_MENU_MAKS),
                            Window(), self._sisi(), Window(width=1)],
                           height=self._tinggi_menu),
                    filter=has_completions),
                self._tepi("╰", "╯"),
            ],
        )   # tanpa `width`: kotak mengisi LEBAR PENUH terminal (lihat
            # _lebar_kotak), sejajar dengan bar status di bawahnya.
        # --- susun elemen dasar (selalu ada) -----------------------------------
        elemen = [
            # PENDORONG. Inilah yang memakukan kotak + bar ke DASAR LAYAR:
            # prompt_toolkit memberi aplikasi non-fullscreen setinggi sisa
            # baris di bawah kursor dan menyegarkannya lagi tiap resize.
            Window(),
            # Satu baris napas di atas kotak — kembaran _KOSONG di sisi rich,
            # supaya jaraknya tak berubah saat giliran selesai.
            Window(height=1),
            # Panel rencana (bila ada) — menempel di atas kotak chat.
            Window(FormattedTextControl(_plan_idle_pt),
                   dont_extend_height=True, wrap_lines=False),
            kotak,
            # Panel gambar Minecraft — nempel di bawah kotak, hilang bila kosong.
            Window(FormattedTextControl(_gambar_idle_pt),
                   dont_extend_height=True, wrap_lines=False),
            # TANPA baris kosong di sini: bar status menempel langsung.
            Window(FormattedTextControl(self._status), height=1,
                   dont_extend_height=True,
                   style="class:bottom-toolbar"),
        ]
        return HSplit(elemen)

    # --- pemakaian -----------------------------------------------------------
    def tanya(self, default: str = "", posisi: int | None = None,
              *, push: bool = True) -> str:
        """Tampilkan kotak sampai Enter, kembalikan teksnya.

        `default` = sisa ketikan dari giliran sebelumnya, `posisi` = di mana
        kursornya tadi berada. Keduanya dibawa utuh supaya penyerahan dari
        kotak-saat-sibuk ke kotak idle tak terasa: kursor tak melompat ke ujung
        di tengah kalimat yang sedang dibetulkan. KeyboardInterrupt/EOFError
        diteruskan ke pemanggil.

        `push` = apakah cursor harus didorong ke dasar layar sebelum aplikasi
        mulai. Default True agar setiap idle kembali menempel di dasar. Dipasang
        False hanya pada startup pertama — di situ konten sudah ditempelkan
        manual ke dasar (lihat main()), jadi push hanya menciptakan celah kosong
        yang memotong tampilan logo."""
        n = len(default)
        kursor = n if posisi is None else max(0, min(int(posisi), n))
        self.buffer.reset(Document(default, kursor))
        # Sinkronkan pratinjau gambar ke teks yang dibawa dari giliran sebelumnya.
        _perbarui_pratinjau_gambar(default, self.app, self.buffer)
        if push:
            _ke_dasar_layar()
        return self.app.run()

    # Sebab kegagalan terakhir kirim_dari_luar, untuk ditampilkan ke pengguna.
    # Ada karena satu-satunya gejala jalur ini rusak adalah KESUNYIAN: perintah
    # terbaca di layar, lalu tak terjadi apa-apa.
    galat_kirim = ""

    def kirim_dari_luar(self, teks: str) -> bool:
        """Kirim `teks` seolah diketik lalu di-Enter, DARI THREAD LAIN.

        Dipakai perintah suara (/voice): saat kotak idle sedang menunggu, ia
        memblokir thread utama, jadi menaruh perintah di antrean saja takkan
        pernah terbaca sampai pengguna menekan Enter — persis hal yang tak
        mungkin ia lakukan kalau sedang bicara, bukan mengetik.

        Return False bila kotaknya memang tidak sedang menunggu (giliran sedang
        berjalan); pemanggil lalu memakai antrean biasa."""
        app = self.app
        try:
            if not app.is_running:
                return False          # giliran sedang jalan -> pakai antrean
            loop = app.loop
        except Exception as exc:  # noqa: BLE001 - aplikasi sedang dibongkar
            self.galat_kirim = f"kotak tak bisa dibaca: {exc}"
            return False
        if loop is None:
            self.galat_kirim = "kotak sedang menunggu tapi belum punya loop"
            return False

        def _kirim() -> None:
            # KEGAGALAN DI SINI TAK BOLEH SENYAP. Ia berjalan di dalam loop
            # prompt_toolkit, jadi lemparannya tak sampai ke pemanggil —
            # gejalanya persis "perintahnya terbaca tapi tak terjadi apa-apa".
            try:
                self.buffer.reset(Document(teks, len(teks)))
                app.exit(result=teks)
            except Exception as exc:  # noqa: BLE001
                self.galat_kirim = f"kotak menolak isian: {exc}"
                log.debug("kirim_dari_luar gagal di dalam loop", exc_info=True)

        try:
            loop.call_soon_threadsafe(_kirim)
        except Exception as exc:  # noqa: BLE001
            self.galat_kirim = f"loop kotak menolak: {exc}"
            return False
        return True

    def sisa(self) -> str:
        """Teks yang masih ada di kotak — dibaca SESUDAH tanya() terputus.

        Dipakai untuk memutuskan arti Ctrl+C: kotak kosong berarti tak ada yang
        hilang kalau program ditutup, kotak berisi berarti ketukan itu hampir
        pasti dimaksudkan untuk membuang ketikan."""
        try:
            return self.buffer.text
        except Exception:  # noqa: BLE001 - aplikasi sudah dibongkar
            return ""

    def _terima(self, buf: Buffer) -> bool:
        self.app.exit(result=buf.text)
        return False   # buffer dikosongkan & teksnya masuk riwayat ↑/↓


# Tool yang MENGUBAH ISI file -> perubahannya ditampilkan sebagai diff berwarna
# SEBELUM file disentuh. Dulu hanya write_file, sehingga perubahan lewat
# edit_file/append_file lolos tanpa bisa ditinjau — padahal justru edit_file yang
# dianjurkan untuk file besar, jadi tanpa ini kebanyakan perubahan jadi tak terlihat.
_TOOL_DIFF = ("write_file", "edit_file", "edit_files", "append_file")

# SEMUA tool yang mengubah isi disk. Dipakai ringkasan giliran ("N file") dan
# pemicu penyegaran peta proyek. Dulu hanya write_file/delete_file — giliran
# penuh edit_file diringkas "0 file", dan peta proyek DIAM-DIAM basi sesudah
# edit sehingga giliran berikutnya bekerja dari peta lama.
_TOOL_UBAH_FILE = {
    "write_file", "edit_file", "append_file", "delete_file", "move_file",
    "copy_file", "replace_in_files", "download_file", "zip_extract",
    "undo_changes",
}


def _teks_unified(old: str, new: str, limit: int = 400) -> str:
    """Teks unified-diff (tanpa header ---/+++) untuk DISIMPAN ke memory.

    Dipangkas `limit` baris supaya write_file file raksasa tak menggembungkan
    berkas sesi — replay tetap menampilkan paling penting di awal."""
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                  lineterm="", n=2))
    if len(d) >= 2 and d[0].startswith("---"):
        d = d[2:]
    if len(d) > limit:
        d = d[:limit] + ["… (diff tersimpan dipangkas)"]
    return "\n".join(d)


def _isi_sebelum_sesudah(name: str, path: str, args: dict):
    """(isi_lama, isi_baru, file_sudah_ada) untuk merender diff sebuah langkah.

    Isi barunya DIHITUNG dari argumen — untuk edit_file/append_file hasil akhir
    tak ada di args, jadi harus disimulasikan persis seperti yang akan dilakukan
    tool-nya (lihat tools/files.py)."""
    full = config.PROJECT_ROOT / path
    exists = full.exists()
    old = full.read_text(encoding="utf-8", errors="replace") if exists else ""
    a = args if isinstance(args, dict) else {}
    if name == "write_file":
        baru = a.get("content", "") or ""
        # Prediksi dengan GUARD YANG SAMA seperti tool-nya: write_file berisi
        # potongan (penanda "...sisanya tetap..." / menyusut drastis) akan
        # DITOLAK oleh _tolak_penimpaan_merusak — jadi diff "seluruh file merah,
        # potongan hijau" itu pratinjau penulisan yang TAK PERNAH terjadi.
        # Mencetaknya justru membuat pengguna yakin kodenya dihapus & ditulis
        # ulang. Bila akan ditolak: tanpa diff; pesan [DITOLAK] dari tool-lah
        # yang tampil sebagai hasil langkah.
        if exists and not a.get("allow_shrink"):
            try:
                from ..tools.files import _tolak_penimpaan_merusak
                if _tolak_penimpaan_merusak(full, baru):
                    return old, old, exists
            except Exception:  # noqa: BLE001 - prediksi gagal: tampilkan apa adanya
                pass
        return old, baru, exists
    if name == "append_file":
        return old, old + (a.get("content", "") or ""), exists
    if name == "edit_file":
        lama = a.get("old_text", "") or ""
        baru = a.get("new_text", "") or ""
        if not lama or lama not in old:
            # Tool-nya akan menolak; jangan tampilkan diff yang menyesatkan.
            return old, old, exists
        jml = a.get("count", 1)
        try:
            jml = int(jml)
        except (TypeError, ValueError):
            jml = 1
        n = old.count(lama) if jml == -1 else jml
        return old, old.replace(lama, baru, n), exists
    return old, old, exists


def _print_diff(path: str, old: str, new: str, is_new: bool, limit: int = 200,
                ke_konten: bool = False) -> None:
    """Tampilan editor: header status + line-numbered diff (bg hijau/merah).

    Seluruh diff dicetak SEKALI sebagai satu Group (statis, atomik) — tak
    pernah disela/ditimpa footer live. `ke_konten=True` mengarahkannya ke
    tumpukan bawah (mode mengalir); tanpa itu ia mengalir ke scrollback
    (mode klasik)."""
    icon, label = ("✨", "dibuat") if is_new else ("📝", "diubah")
    rows: list = [_TM(
        f"\n  [bold]{icon} [cyan]{_esc(path)}[/cyan][/bold] [dim]({label})[/dim]")]
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     lineterm="", n=2))
    body = diff[2:] if len(diff) >= 2 and diff[0].startswith("---") else diff
    # Kedua sisi diwarnai dari isi LENGKAPNYA masing-masing: baris yang dihapus
    # harus diwarnai menurut berkas LAMA, yang ditambah menurut berkas BARU.
    warna_lama = _pewarna(path, old)
    warna_baru = _pewarna(path, new)
    old_ln = new_ln = 0
    shown = 0
    for line in body:
        if shown >= limit:
            rows.append(Text("  ... (diff dipotong)", style="dim"))
            break
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
            if shown:
                rows.append(Text("  ⋮", style="dim"))
            continue
        tag, content = line[:1], line[1:]
        if tag == "+":
            rows.append(_row(str(new_ln), "+",
                             _ambil(warna_baru, new_ln) or content, _ADD))
            new_ln += 1
        elif tag == "-":
            rows.append(_row(str(old_ln), "-",
                             _ambil(warna_lama, old_ln) or content, _DEL))
            old_ln += 1
        else:
            rows.append(_row(str(new_ln), " ", content, _CTX))
            old_ln += 1
            new_ln += 1
        shown += 1
    if ke_konten:
        _tambah_konten(rows)
    else:
        console.print(Group(*rows))


def _print_delete(path: str, content: str, limit: int = 80,
                  ke_konten: bool = False) -> None:
    rows: list = [_TM(
        f"\n  [bold]🗑 [cyan]{_esc(path)}[/cyan][/bold] [dim](dihapus)[/dim]")]
    warna = _pewarna(path, content)
    for i, line in enumerate(content.splitlines(), start=1):
        if i > limit:
            rows.append(Text("  ... (dipotong)", style="dim"))
            break
        rows.append(_row(str(i), "-", _ambil(warna, i) or line, _DEL))
    if ke_konten:
        _tambah_konten(rows)
    else:
        console.print(Group(*rows))


def _replay_diff(rec: dict) -> Group:
    """Render ulang record diff tersimpan (role 'diff') untuk transkrip --resume.

    Tanpa pewarnaan sintaks per-baris — isi lengkap file lamanya memang tak
    disimpan (cuma teks unified yang sudah terpangkas), tapi pita hijau/merah,
    nomor baris, dan header statusnya sama seperti saat diff itu pertama
    tampil, jadi potongan kode tak lagi lenyap saat sesi dibuka kembali."""
    path = str(rec.get("path") or "?")
    if rec.get("deleted"):
        icon, label = "🗑", "dihapus"
    else:
        icon, label = ("✨", "dibuat") if rec.get("is_new") else ("📝", "diubah")
    rows = [_TM(f"\n  [bold]{icon} [cyan]{_esc(path)}[/cyan][/bold] "
                f"[dim]({label})[/dim]")]
    ln_old = ln_new = 0
    for line in str(rec.get("diff") or "").split("\n"):
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if m:
                ln_old, ln_new = int(m.group(1)), int(m.group(2))
            if len(rows) > 1:
                rows.append(Text("  ⋮", style="dim"))
            continue
        tag, isi = line[:1], line[1:]
        if rec.get("deleted"):
            ln_old += 1
            rows.append(_row(str(ln_old), "-", isi, _DEL))
        elif tag == "+":
            ln_new += 1
            rows.append(_row(str(ln_new), "+", isi, _ADD))
        elif tag == "-":
            ln_old += 1
            rows.append(_row(str(ln_old), "-", isi, _DEL))
        else:
            ln_old += 1
            ln_new += 1
            rows.append(_row(str(ln_new), " ", isi, _CTX))
    return Group(*rows)


# Font logo berjenjang dari yang paling besar. Terminal yang menyempit
# turun ke font berikutnya alih-alih loncat langsung ke teks polos, jadi
# perubahan tampilannya berangsur — bukan jurang antara mural dan coretan.
_FONT_LOGO = ("ansi_shadow", "slant", "small", "digital")


def _pusatkan(t: Text, lebar: int) -> Text:
    """Baris `t` didorong ke tengah `lebar` — perataan manual, TANPA Align.

    Align.center mengukur isi lebih dulu; teks no-wrap yang lebih panjang
    dari ruangnya membuat ukurannya meleset sehingga offset perataannya
    nol dan seluruh isi menempel kiri — itulah banner miring yang tampak
    di terminal sempit. Perataan manual tak menyisakan ruang salah ukur."""
    w = t.cell_len
    if w >= lebar:
        return t
    out = Text(" " * ((lebar - w) // 2), no_wrap=True)
    out.append_text(t)
    return out


def _logo_content(lebar: int | None = None) -> Group:
    """Figlet bergradasi + garis aksen + tagline, semuanya dipusatkan pada
    `lebar` (ruang isi panel penampungnya). Dipakai oleh _banner()."""
    if lebar is None:
        lebar = max(20, console.width - 12)
    lebar = max(24, lebar)
    lines: list[str] = []
    if Figlet is not None:
        for font in _FONT_LOGO:
            try:
                kandidat = Figlet(font=font).renderText("bagas-ai").split("\n")
                # Buang baris kosong di akhir (figlet sering mengemit
                # trailing \n\n yang jadi "jeda" tembak di dalam panel).
                while kandidat and not kandidat[-1].strip():
                    kandidat.pop()
                if kandidat and max(len(ln) for ln in kandidat) <= lebar:
                    lines = kandidat
                    break
            except Exception:  # noqa: BLE001 - font tak ada -> cek yang berikut
                continue
    if not lines:
        lines = ["b a g a s - a i"]

    # Offset pemusatan dihitung SEKALI dari baris terpanjang, bukan per
    # baris: baris figlet yang panjangnya tak sama akan digeser beda-beda
    # dan hurufnya tak lagi lurus antar baris bila tiap baris dipusatkan
    # sendiri-sendiri.
    w_art = max(Text(ln).cell_len for ln in lines)
    geser = max(0, (lebar - w_art) // 2)

    logo_lines = []
    grad = _grad()
    for i, ln in enumerate(lines):
        t = Text(" " * geser + ln.rstrip(),
                 style=f"bold {grad[min(i, len(grad) - 1)]}", no_wrap=True)
        logo_lines.append(t)
    # Garis aksen gradasi mengikuti LEBAR LOGONYA, bukan lebar panel —
    # aksen yang lebih panjang dari logonya membuat blok terasa miring.
    seg = min(56, max(12, w_art))
    per = max(1, seg // len(grad))
    bar = Text(no_wrap=True)
    for col in grad:
        bar.append("━" * per, style=col)
    logo_lines.append(_pusatkan(bar, lebar))
    # Tagline: versi panjang hanya bila benar-benar muat. Tanpa syarat ini
    # ia diukur 57 kolom dan menyeret perataan seluruh blok (lihat _pusatkan).
    sub = Text()
    sub.append("AI agent serbaguna", style=f"bold {tema.p('teks')}")
    if 18 + len("  ·  terminal · telegram · multitasking") <= lebar:
        sub.append("  ·  terminal · telegram · multitasking", style="dim")
    logo_lines.append(_pusatkan(sub, lebar))
    return Group(*logo_lines)


def show_logo() -> None:
    """Feedback cepat saat startup — wordmark singkat.
    Figlet penuh + info akan tampil di Panel _banner() setelah setup selesai."""
    console.print()
    t = Text(" " * _LPAD + "⬢ bagas-ai", style=f"bold {tema.p('aksen')}")
    console.print(t)


# ---------------------------------------------------------------------------
# Bar kemajuan PIL bergaris putus-putus — dipakai saat ingatan dipadatkan
# ---------------------------------------------------------------------------
# Bentuk pilnya dibuat dari dua setengah-blok sebagai tutup ujung (▐ … ▌).
# Sengaja BUKAN glif powerline ( ) yang bulat sempurna: glif itu cuma ada di
# font yang sudah ditambal, dan di terminal biasa ia jadi kotak kosong — bar
# yang rusak lebih buruk daripada bar yang sudutnya kurang bulat. Setengah-blok
# ada di hampir semua font monospace.
#
# Isinya PUTUS-PUTUS (▰ / ╌) dan ruas terangnya BERGERAK tiap frame. Gerak itu
# ada gunanya: pemadatan bisa berhenti sejenak di satu tahap (peta proyek yang
# dibangun ulang), dan bar yang diam di angka yang sama tak bisa dibedakan dari
# bar yang macet.
_PIL_TUTUP_KIRI, _PIL_TUTUP_KANAN = "▐", "▌"
_PIL_ISI, _PIL_KOSONG = "▰", "╌"
def _pil_warna():
    return (f"bold {tema.p('aksen')}", tema.p("aksen2"), tema.p("tepi_redup"))


def _bar_pil(frac: float, lebar: int = 20, fase: int = 0) -> Text:
    """Bar pil putus-putus. `fase` menggeser ruas terangnya (animasi)."""
    frac = max(0.0, min(1.0, frac))
    isi = int(round(frac * lebar))
    t = Text()
    t.append(_PIL_TUTUP_KIRI, style=_PIL_TERANG if isi else _PIL_SISA)
    for i in range(lebar):
        if i < isi:
            # Ruas 2 terang - 2 redup yang merayap: pola putus-putus yang hidup.
            terang = ((i - fase) % 4) < 2
            t.append(_PIL_ISI, style=_PIL_TERANG if terang else _PIL_REDUP)
        else:
            t.append(_PIL_KOSONG, style=_PIL_SISA)
    t.append(_PIL_TUTUP_KANAN, style=_PIL_TERANG if isi >= lebar else _PIL_SISA)
    return t


# Berapa lama bar pemadatan MINIMAL tampak di layar. Menyimpan ingatan biasanya
# selesai dalam sepersekian detik — tanpa jeda ini, satu-satunya jejak kejadian
# penting itu cuma kedipan yang tak sempat terbaca. Jedanya JEDA TAMPILAN, bukan
# pekerjaan yang dibuat-buat: barnya sudah 100% dan tertulis "tersimpan".
_PADAT_MIN_TAMPIL = 1.4


def _jeda_padat(view: Any):
    """Callback on_padat: gerakkan bar, lalu TAHAN sebentar di akhir.

    Penahanannya berjalan di thread worker — thread yang sama yang menjalankan
    giliran — jadi gilirannya benar-benar berhenti selama bar terlihat, bukan
    cuma tampak berhenti. Itu yang diminta: pemadatan menjeda semua aktivitas."""
    def lapor(frac: float, ket: str) -> None:
        # Sudah selesai & barnya sudah dilepas? Laporan 100% yang datang
        # menyusul TIDAK boleh memulai penantian kedua — itu membuat jedanya
        # dua kali lipat dari yang dijanjikan.
        if frac >= 1.0 and view.padat is None:
            return
        view.note_padat(frac, ket)
        if frac < 1.0:
            return
        mulai = view.padat[2] if view.padat else time.time()
        sisa = _PADAT_MIN_TAMPIL - (time.time() - mulai)
        if sisa > 0:
            time.sleep(sisa)
        view.padat = None          # kembali ke baris status biasa
    return lapor


def _baris_padat(frac: float, ket: str, sejak: float, el: float) -> Text:
    """Satu baris status pemadatan: pil + persen + perkiraan sisa waktu."""
    jalan = max(time.time() - sejak, 0.001)
    t = Text()
    t.append("  ⏸ ", style=f"bold {tema.p('aksen')}")
    t.append("memadatkan ingatan", style=tema.p("aksen"))
    t.append("  ")
    # Panjang pil MENYESUAIKAN lebar terminal: persen & keterangan lebih
    # penting daripada pilnya, dan baris ini anti-lipat — di terminal sempit
    # pil 20 ruas justru mendorong persennya terpotong dari layar. Perkiraan
    # sisa waktu ikut disembunyikan bila ruangnya tak cukup.
    pil = max(6, min(20, (console.width - 62) // 2))
    t.append_text(_bar_pil(frac, pil, fase=int(el * 12)))
    t.append(f" {int(round(frac * 100)):>3}%", style=f"bold {tema.p('aksen2')}")
    # ETA dari kemajuan NYATA, bukan tebakan: waktu yang sudah lewat dibagi
    # bagian yang sudah selesai. Di bawah 8% angkanya masih liar (satu tahap
    # cepat bisa menghasilkan perkiraan sepersepuluh detik), jadi ditahan dulu.
    # Ini juga alasannya bar ini boleh ada sementara bar ETA untuk jawaban AI
    # dulu dibuang: yang di sini mengukur pekerjaan LOKAL yang tahapannya
    # diketahui, bukan menebak kapan situs selesai menjawab.
    if 0.08 <= frac < 1.0 and console.width >= 72:
        t.append(f"  ~{jalan / frac - jalan:.1f}s", style=tema.p("aksen_terang"))
    t.append("  ·  ")
    t.append(ket, style="dim")
    t.append("  ·  dijeda", style="dim italic")
    return _oneline(t)


# ---------------------------------------------------------------------------
# Indikator "berpikir" realtime (rich Live) — nempel inline pada task
# ---------------------------------------------------------------------------
# Kata FASE per-tool: bikin indikator status menjelaskan APA yang sedang
# dikerjakan (bukan cuma "berpikir"). Tanpa tool aktif -> "berpikir".
_PHASE = {
    "write_file": "menulis",
    "edit_file": "mengedit",
    "append_file": "menambah",
    "delete_file": "menghapus",
    "move_file": "memindah",
    "copy_file": "menyalin",
    "make_dir": "membuat folder",
    "read_file": "membaca",
    "list_dir": "menelusuri",
    "glob_files": "mencari",
    "search_text": "mencari",
    "web_search": "mencari",
    "fetch_url": "membuka",
    "http_request": "memanggil API",
    "download_file": "mengunduh",
    "replace_in_files": "mengganti",
    "diff_files": "membandingkan",
    "zip_create": "mengarsip",
    "zip_extract": "membongkar",
    "take_screenshot": "memotret",
    "analyze_image": "menganalisis",
    "attach_file": "mengunggah",
    "validate_project": "memvalidasi",
    "run_tests": "menguji",
    "web_preview": "meninjau",
    "undo_changes": "memulihkan",
    "bg_send": "mengetik",
    "run_command": "menjalankan",
    "run_python": "menjalankan",
    "run_command_bg": "menjalankan",
    "bg_output": "memantau",
    "run_script": "menjalankan",
    "save_script": "menyimpan",
    "remember": "mengingat",
}


# Blok "pikiran model" di region hidup: TERTUTUP satu baris, TERBUKA
# beberapa baris ekor. Tab membuka & menutupnya (lihat _ketik).
#
# Kenapa di region hidup dan bukan dicetak ke scrollback: pikiran model itu
# nalar MENTAH — panjang, berbahasa Inggris walau jawabannya Indonesia, dan
# separuhnya berisi pertimbangan yang ia tolak sendiri beberapa kalimat
# kemudian. Ditumpahkan ke transkrip, ia menenggelamkan jawaban & langkah yang
# justru dicari orang. Di sini ia tetap TERLIHAT ADA (dan menghitung, jadi
# terbukti hidup) tapi hanya terbuka kalau memang mau dibaca.
#
# Tingginya DIBATASI _PIKIR_BARIS: region hidup tak boleh melebihi layar, dan
# aturan itu tak boleh ditawar cuma karena bloknya sedang terbuka.
_PIKIR_BARIS = 8


def _tinggi_pikir() -> int:
    """Berapa baris isi pikiran yang boleh tampil saat bloknya dibuka.

    Anggaran 15 baris untuk sisa region hidup (kotak chat 3 + bar status 1 +
    tips 2 + napas + panel rencana bila ada) plus sedikit kelonggaran. Batas
    ini ada supaya membuka pikiran tak pernah MENDORONG kotak chat keluar
    layar: begitu kotaknya lenyap, orang menyangka ketikannya hilang — mahal
    sekali untuk sesuatu yang cuma keterangan tambahan. Di layar pendek
    bloknya menyusut, bukan region yang melebar."""
    return max(3, min(_PIKIR_BARIS, console.height - 15))


def _blok_pikir(teks: str, buka: bool, *, bisa_buka: bool = True) -> list:
    """Baris-baris blok pikiran. [] bila model ini memang tak mengirim pikiran.

    bisa_buka=False membuang tanda buka/tutupnya dan menyisakan penghitungnya
    saja. Itu yang dipakai mode klasik, yang tak punya gelung pembaca tombol:
    memasang tanda "bisa dipencet" pada yang tak bisa dipencet lebih buruk
    daripada tak memasang tanda sama sekali.

    Yang ditampilkan saat terbuka adalah EKORNYA, bukan awalnya: yang sedang
    dipikirkan sekarang lebih berguna daripada pembukaan yang sudah lewat.
    Spasi & baris baru dirapikan jadi satu aliran supaya tinggi bloknya bisa
    dipastikan — nalar mentah kerap memberi baris kosong tiap dua kalimat, dan
    menghormatinya berarti tinggi region berubah-ubah tiap frame."""
    if not teks:
        return []
    tanda = ('▾ ' if buka else '▸ ') if bisa_buka else ''
    # Petunjuk tombolnya DITULIS: dulu blok ini cuma memasang tanda ▸ yang
    # menyerupai bisa-diklik, dan tak satu pun teks di layar menyebut Tab —
    # pengguna menekan/mengklik apa saja lalu menyimpulkan fiturnya rusak.
    petunjuk = "" if (buka or not bisa_buka) else " · Tab buka/tutup"
    kepala = _oneline(_TM(
        f"  [dim]{tanda}💭 pikiran {_fmt(len(teks))} karakter{petunjuk}[/]"))
    if not buka:
        return [kepala]
    # Isinya menjorok ke kolom 4 — kolom yang SAMA dengan tulisan fase di baris
    # status, sementara tanda ▾ menggantung di kolom 2 bersama gasing. Jadi
    # tanda itu membuka blok yang duduk persis di bawah keterangannya.
    lebar = max(24, console.size.width - 8)
    baris = textwrap.wrap(" ".join(teks.split()), lebar)[-_tinggi_pikir():]
    return [kepala] + [_oneline(Text(f"    {b}", style="dim italic"))
                       for b in baris]


class Status:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, agent: Agent, total=None) -> None:
        self.agent = agent
        # total() -> token global sepanjang masa; dipakai bar status permanen.
        self.total = total or (lambda: agent.tokens_session.total)
        self.start = time.time()
        self.tool: str | None = None
        self.phase = "berpikir"
        self.step = 0
        self.disp = float(agent.tokens_session.total)
        self.retry_until = 0.0
        self.retry_msg = ""
        self.cancelling = False
        # Pemadatan ingatan yang sedang berjalan: (pecahan, keterangan, mulai).
        # None = tak sedang memadatkan.
        self.padat: tuple[float, str, float] | None = None
        # Pikiran model yang sudah masuk giliran ini (lihat note_pikir).
        self.pikir = ""

    def note_pikir(self, piece: str) -> None:
        """Satu potongan aliran "pikiran" model — ditumpuk, tidak dicetak.

        Di mode klasik yang tampil cuma penghitungnya, dan itu memang seluruh
        gunanya: pada model yang butuh sekitar 150 detik sebelum kata pertama
        (TERUKUR: deepseek-v4-flash), angka yang merambat naik adalah
        satu-satunya bukti ia bekerja alih-alih menggantung. Isinya tak bisa
        dibuka di sini — mode klasik tak membaca tombol selagi giliran jalan."""
        if piece:
            self.pikir += piece

    def note_padat(self, frac: float, ket: str) -> None:
        """Kemajuan pemadatan ingatan (menggantikan baris status selagi jalan)."""
        mulai = self.padat[2] if self.padat else time.time()
        self.padat = (frac, ket, mulai)

    def note_retry(self, wait: float, msg: str) -> None:
        """Tandai bahwa bagas-ai sedang menunggu rate limit lalu melanjutkan."""
        self.retry_until = time.time() + wait
        self.retry_msg = msg

    def note_cancelling(self) -> None:
        """Tandai bahwa pembatalan (Ctrl+C) sedang diproses di latar belakang."""
        self.cancelling = True

    def note_step(self, name: str) -> None:
        """Mulai satu langkah tool: set fase sesuai jenis tool & naikkan nomor."""
        self.tool = name
        self.phase = _PHASE.get(name, "bekerja")
        self.step += 1

    def note_thinking(self) -> None:
        """Kembali ke fase 'berpikir' (tak ada tool aktif)."""
        self.tool = None
        self.phase = "berpikir"

    def note_phase(self, text: str) -> None:
        """Set fase status langsung (dipakai connector web: 'menjawab', dsb)."""
        if self.tool is None and text:
            self.phase = text

    def __rich__(self):
        """Baris kerja · kotak chat · bar status — susunan yang SAMA PERSIS
        seperti mode mengalir dan seperti saat idle.

        Kotak chat dan bar status ikut hadir di sini justru supaya keduanya tak
        pernah hilang: mode klasik dipakai saat /live dimatikan DAN sebagai
        cadangan bila jalur mengalir gagal, jadi tanpa ini kolom chat lenyap
        persis di saat tampilannya sedang paling tidak menentu.

        Rupa kotaknya sama persis dengan kotak idle — kosong, tanpa teks ajakan
        apa pun. Kotak chat cuma SATU, jadi ia tak boleh berganti wajah hanya
        karena AI sedang sibuk."""
        # Susunan SAMA PERSIS dengan mode mengalir: napas · baris status ·
        # rencana? · kotak · gambar? · bar. (Dulu cabang tanpa rencana
        # menambah SATU baris kosong lagi — stack melompat sebaris tiap kali
        # rencana muncul/hilang atau mode berganti.)
        rows = [_KOSONG, self._baris_status()]
        # Menempel LANGSUNG di bawah baris status, tanpa napas: keduanya satu
        # keterangan tentang hal yang sama (apa yang sedang dikerjakan model).
        rows.extend(_blok_pikir(self.pikir, False, bisa_buka=False))
        plan_rows = _panel_plan()
        if plan_rows:
            # Panel plan menempel langsung ke kotak chat
            # — bingkai ╰─╯ dan ╭─╮ bersentuhan, tak perlu napas.
            rows.extend(plan_rows)
        rows.extend(_kotak_chat())
        # Pratinjau gambar di bawah kotak — kembaran layout idle.
        rows.extend(_panel_gambar_rich())
        rows.append(_bar_status(self.agent, self.total()))
        return Group(*rows)

    def _baris_status(self) -> Text:
        el = time.time() - self.start
        now = time.time()
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]

        dot = f"[{tema.p('tepi_redup')}]•[/]"

        # Ingatan sedang dipadatkan: SELURUH giliran berhenti di sini, jadi
        # baris status biasa (fase/token/alat) tak lagi menggambarkan apa pun
        # yang sedang terjadi — diganti utuh oleh bar pemadatan.
        if self.padat is not None:
            return _baris_padat(*self.padat, el)

        # Mode membatalkan: Ctrl+C ditekan, menunggu langkah aman berhenti.
        if self.cancelling:
            t = Text()
            t.append(f"  {frame} ", style="bold #f0603c")
            t.append("membatalkan — menunggu langkah aman berhenti", style="#f0603c")
            t.append("     Ctrl+C lagi = paksa", style="dim italic")
            return _oneline(t)

        # Mode menunggu rate limit: tampilkan hitung mundur + jaminan lanjut.
        if now < self.retry_until:
            left = self.retry_until - now
            t = Text()
            t.append(f"  {frame} ", style=f"bold {tema.p('aksen_terang')}")
            t.append("layanan sibuk — menunggu lalu melanjutkan", style=tema.p("aksen_terang"))
            t.append(f"  {left:.0f}s", style=f"bold {tema.p('aksen2')}")
            if self.retry_msg:
                t.append(f"  ·  {self.retry_msg}", style=f"dim {tema.p('aksen_terang')}")
            t.append("     Ctrl+C batal", style="dim italic")
            return _oneline(t)

        target = float(self.agent.tokens_session.total + self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30  # easing -> angka mengalir
        if abs(target - self.disp) < 1:
            self.disp = target
        t = Text()
        t.append(f"  {frame} ", style=f"bold {tema.p('aksen')}")
        t.append(self.phase, style=tema.p("aksen"))
        t.append(f"  {_fmt_elapsed(el)}", style=f"bold {tema.p('aksen2')}")
        t.append("   ")
        t.append_text(_TM(dot))
        t.append(f"  ⚡ {_fmt(int(self.disp))}", style=tema.p("aksen_terang"))
        t.append(" sesi", style="dim")
        if self.tool:
            t.append("   ")
            t.append_text(_TM(dot))
            t.append(f"  🔧 {self.tool}", style=tema.p("aksen_terang"))
        if self.step:
            t.append("   ")
            t.append_text(_TM(dot))
            t.append(f"  langkah {self.step}", style=f"dim {tema.p('aksen_terang')}")
        t.append("     Ctrl+C batal", style="dim italic")
        return _oneline(t)


# Tips singkat yang BERGANTIAN muncul di bawah status selama AI bekerja
# (seperti Claude CLI) — biar waktu menunggu tetap informatif.
#
# ATURAN ISI: tiap tips WAJIB menggambarkan bagas-ai yang SEKARANG. Tips yang
# menjanjikan hal usang lebih merugikan daripada tak ada tips sama sekali —
# pengguna mencoba, gagal, lalu berhenti percaya pada seluruh barisan ini. Dua
# yang sudah pernah basi dan kini diperbaiki: "/effort mengatur kedalaman
# berpikir" (artinya BEDA per jalur — di model (web) ia MENGKLIK pemilih mode di
# situsnya, di model (API) ia mengirim parameter permintaan; tipsnya harus benar
# untuk keduanya) dan "naik kelas otomatis" (yang tersisa hanya kenaikan EFFORT
# di jalur API + penjaga anti-macet — bagas-ai tak pernah berpindah model
# sendiri, lihat catatan _escalate di core.py).
_TIPS = (
    "ketik pesan berikutnya kapan saja — Enter mengirimnya, dikerjakan sesudah ini",
    "/model ganti model kapan saja — login cukup sekali, sesudah itu langsung jalan",
    "/add-dir menambah folder lain ke konteks · /dirs melihat daftarnya",
    "/scan menyegarkan peta proyek · /review memburu bug di seluruh proyek",
    "perintah menetap (mis. npm run dev) otomatis jalan di latar — terminal tetap bebas",
    "Ctrl+C sekali = batalkan dengan aman; ketik 'lanjutkan' untuk meneruskan",
    "bagas-ai --resume melanjutkan percakapan terakhir di folder ini",
    "keluaran tiap langkah langsung tampil di bawahnya, dipotong biar riwayat tetap terbaca",
    "tiap perubahan file ditampilkan sebagai diff dulu, sebelum berkasnya disentuh",
    "/memory menyimpan fakta yang harus diingat lintas sesi",
    "/effort mengatur mode berpikir — diklik di situsnya (web) atau dikirim sebagai parameter (API)",
    "kalau model mengulang langkah yang sama, bagas-ai menyetopnya lalu cari jalan lain",
    "/web merapikan chat yang menumpuk di situs model · sekalian logout kalau perlu",
    "/bot menyalakan kontrol lewat Telegram — perintahkan bagas-ai dari HP",
    "/scripts menyimpan perintah panjang jadi satu nama pendek",
    "/live mengalihkan tampilan mengalir ↔ klasik bila terminalmu bermasalah",
    "💭 aliran pikiran model: tekan Tab selagi ia berpikir untuk buka/tutup",
)


class TurnView:
    """Tampilan SATU GILIRAN yang MENGALIR seperti terminal biasa.

    SEMUA konten (narasi, langkah, diff, jawaban) dicetak STATIS ke scrollback
    begitu tersedia — tidak ada yang dirender di region tetap yang
    menimpa/menutupi apa pun. Yang hidup (rich.Live) cuma baris-baris di paling
    bawah: spinner status, tips, PANEL RENCANA (bila ada), KOTAK CHAT, lalu BAR
    STATUS. Tingginya kecil & tetap (tiap baris _oneline anti-wrap; panel
    rencana runtuh jadi satu baris bila layar tak cukup), jadi ia tak pernah
    lebih tinggi dari layar dan tak pernah menutupi ketikan pengguna maupun
    diff — akar bug
    "kotak animasi menimpa input" pada desain lama yang menaruh langkah +
    pratinjau di region live.

    Yang DITAMPILKAN dijaga tetap sedikit dan pasti: fase yang sedang berjalan,
    alat yang sedang dipakai, narasi AI, hasil tiap langkah, dan jawaban akhir.
    Tak ada bar perkiraan waktu (cuma menebak sisa waktu) dan tak ada pratinjau
    kalimat yang sedang ditulis (isinya berubah tiap frame, lalu tercetak lagi
    ke scrollback beberapa detik kemudian)."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, agent: Agent, commit=None, total=None) -> None:
        self.agent = agent
        self.commit = commit          # commit(renderables) -> cetak ke riwayat
        # total() -> token global sepanjang masa; dipakai bar status permanen.
        self.total = total or (lambda: agent.tokens_session.total)
        self.start = time.time()
        self._lock = threading.Lock()
        self.all_steps: list[dict] = []            # SEMUA langkah (untuk ringkasan)
        self._said = False                         # header "🤖" sekali per giliran
        self.answer: str | None = None
        self.done = False
        self.cancelling = False
        self.retry_until = 0.0
        self.retry_msg = ""
        self.phase = "berpikir"
        self.tool: str | None = None
        self.tool_label = ""           # label langkah berjalan (tampil di footer)
        self.disp = float(agent.tokens_session.total)
        self.phase_since = self.start   # kapan fase SEKARANG mulai
        # BAR ETA & PRATINJAU ALIRAN JAWABAN DIHAPUS (permintaan pengguna).
        #
        # Keduanya menampilkan hal yang belum jadi: bar ETA menebak sisa waktu,
        # dan pratinjau mengalirkan teks MENTAH dari situs — termasuk isi blok
        # [[TOOL]] beserta JSON-nya, yaitu potongan yang justru tak boleh
        # dilihat pengguna (itu percakapan mesin, bukan jawaban). Yang tampil
        # sekarang hanya yang benar-benar sudah jadi: fase yang sedang berjalan,
        # alat yang sedang dipakai, narasi AI, dan jawaban akhir — semuanya
        # sudah bersih dari penanda tool sejak di core.
        #
        # Perekaman durasi di connector (web_timing.record) sengaja DIBIARKAN:
        # ia tak menampilkan apa pun, cuma menabung statistik.
        #
        # Pratinjau kalimat yang sedang ditulis AI (satu baris abu-abu di atas
        # spinner) IKUT DIHAPUS atas permintaan pengguna. Ia memang mengabarkan
        # giliran panjang belum mati, tapi harganya satu baris yang isinya
        # berubah tiap frame tepat di sebelah kotak chat — dan yang sama akan
        # tercetak rapi ke scrollback beberapa detik kemudian. Waktu berjalan
        # + fase di baris spinner sudah cukup jadi tanda kehidupan.
        # Ketikan pengguna SELAMA giliran berjalan, tampil di kotak chat yang
        # sama seperti saat idle. Enter TIDAK membatalkan apa pun — prompt
        # dikerjakan setelah giliran ini selesai. Itu urusan di balik layar:
        # tak ada satu pun teks di layar yang menyebut-nyebut antrean.
        self.typing = ""
        self.typing_pos = 0
        # Pemadatan ingatan yang sedang berjalan (lihat note_padat & _footer).
        self.padat: tuple[float, str, float] | None = None
        # Pikiran model yang sudah masuk giliran ini + apakah bloknya
        # sedang terbuka (lihat note_pikir & _blok_pikir).
        self.pikir = ""
        self.pikir_buka = False

    # --- mutasi (dipanggil dari worker) ---
    def note_padat(self, frac: float, ket: str) -> None:
        """Kemajuan pemadatan ingatan. Selama ini hidup, seluruh baris status
        diganti bar pemadatan — giliran memang sedang berhenti di situ."""
        mulai = self.padat[2] if self.padat else time.time()
        self.padat = (frac, ket, mulai)

    def add_narasi(self, text: str) -> None:
        """Narasi langsung DIBEKUKAN ke riwayat (bisa panjang) -> region live tetap
        pendek & tak berkedip."""
        if not (text and text.strip()):
            return
        if self.commit:
            out = []
            # Header "🤖 bagas-ai" dan kalimatnya adalah SATU blok, jadi
            # jarak di atas cuma diberi sekali: oleh header bila ia
            # muncul, oleh kalimatnya bila tidak. Kalau keduanya sama-sama
            # memberi jarak, headernya tampak melayang terpisah dari
            # ucapan yang ia perkenalkan.
            baru_bicara = not self._said
            if baru_bicara:
                out.append(Padding(Text("🤖 bagas-ai", style=f"bold {tema.p('aksen2')}"),
                                   (1, 0, 0, 2)))
                self._said = True
            out.append(Padding(_md(text.strip()),
                               (0 if baru_bicara else 1, 3, 0, 3)))
            self.commit(out)

    def note_pikir(self, piece: str) -> None:
        """Satu potongan aliran "pikiran" model — ditumpuk, tidak dicetak.

        Yang menampilkannya _footer lewat _blok_pikir, dan bawaannya TERTUTUP:
        cukup satu baris yang angkanya bertambah. Itu jawaban untuk keluhan
        "diprompt 'hai' tapi tak dibalas" — pada model yang butuh ±150 detik
        sebelum kata pertama (TERUKUR: deepseek-v4-flash), angka yang merambat
        naik adalah satu-satunya bukti ia bekerja, bukan menggantung.

        HANYA jalur API yang sampai ke sini; di jalur web blok berpikir sudah
        dibuang saat serialisasi, jadi tak ada yang bisa diteruskan."""
        if piece:
            self.pikir += piece

    def toggle_pikir(self) -> None:
        """Buka/tutup blok pikiran (Tab). Tak melakukan apa pun bila model ini
        memang tak mengirim pikiran — tombol yang membuka kekosongan hanya
        membuat orang menyangka tombolnya rusak."""
        if self.pikir:
            self.pikir_buka = not self.pikir_buka

    def start_step(self, n: int, name: str, label: str) -> dict:
        rec = {"n": n, "name": name, "label": label, "result": "",
               "failed": False, "running": True}
        with self._lock:
            self.all_steps.append(rec)
        self.tool = name
        self.tool_label = label or ""
        self.phase = _PHASE.get(name, "bekerja")
        self.phase_since = time.time()
        return rec

    def end_step(self, rec: dict, result: str, failed: bool) -> None:
        rec["result"] = result or ""
        rec["failed"] = failed
        rec["running"] = False
        text = re.sub(r"^exit_code=\S+\n?", "", (result or "").strip())
        # Keluaran perintah nyaris selalu berwarna (pip/npm/git) -> escape-nya
        # WAJIB dibuang sebelum dicetak, kalau tidak terminal mengeksekusinya
        # dan tampilan berantakan.
        rec["_lines"] = _bersih_kendali(text).splitlines()
        rec["_nlines"] = sum(1 for ln in rec["_lines"] if ln.strip())
        # Langkah selesai LANGSUNG dicetak statis ke scrollback — bukan ditahan
        # di region live. Inilah inti desain mengalir: riwayat langkah menjadi
        # scrollback biasa yang bisa digulung dan tak pernah ditimpa animasi.
        if self.commit:
            self.commit(self._render_step(rec))
        self.tool = None
        self.tool_label = ""
        self.phase = "berpikir"
        self.phase_since = time.time()

    def note_retry(self, wait: float, msg: str) -> None:
        self.retry_until = time.time() + wait
        self.retry_msg = msg

    def note_phase(self, text: str) -> None:
        """Set fase status langsung (dipakai connector web: 'berpikir', dsb).
        Diabaikan saat ada tool berjalan supaya fase tool tak tertimpa."""
        if self.tool is None and text and text != self.phase:
            self.phase = text
            self.phase_since = time.time()

    def _kotak_ketikan(self) -> list:
        """Kotak chat yang SAMA PERSIS seperti saat idle — termasuk kosongnya.

        Tak ada ajakan "ketik untuk mengantre" atau semacamnya: kotak chat cuma
        SATU dan rupanya tak boleh berubah-ubah menurut sibuk/tidaknya AI.
        Mengetik di sini tetap "mengetik pesan"; kalau AI kebetulan belum
        selesai, pesannya dikerjakan setelah giliran ini — diam-diam, tanpa
        perlu diumumkan lewat teks di layar."""
        return _kotak_chat(self.typing, self.typing_pos)

    # --- render satu langkah (dipanggil SEKALI saat langkah selesai) ---
    def _render_step(self, rec: dict) -> list:
        """Blok statis sebuah langkah yang SELESAI, untuk dicetak ke riwayat:
        kepala (✓/✗ + label) lalu ringkasan "N baris" yang BISA DIKLIK.

        Nomor langkah tak lagi ditampilkan: satu-satunya gunanya dulu adalah
        diketik ulang sebagai `/expand N`, dan hasilnya kini tampil langsung
        di bawah barisnya sendiri."""
        label = rec["label"] or ""
        if len(label) > 64:
            label = label[:61] + "…"
        failed = rec["failed"]
        icon = "[#f0603c]✗[/]" if failed else "[#9fc93c]✓[/]"
        phase = _PHASE.get(rec["name"], "langkah")
        out = [_KOSONG, _oneline(_TM(
            f"  {icon} [{tema.p('teks')}]{phase}[/]  [white]{_esc(label)}[/]"))]
        lines = rec.get("_lines") or []
        if lines:
            out.extend(_pratinjau_hasil(lines, gagal=failed))
        return out

    def __rich__(self):
        """Region live, dari atas ke bawah: kalimat yang sedang ditulis AI ·
        spinner + tips · PANEL RENCANA (bila ada) · KOTAK CHAT · BAR STATUS.

        Tiga yang terakhir urutannya HARAM ditukar atau dilewati: panel rencana
        (bila aktif) menempel tepat di atas kotak chat, dan kotak chat menempel
        persis di atas bar status, sama seperti saat idle. Itulah yang membuat
        tempat mengetik terasa satu benda yang tak pernah pindah — segala yang
        hidup (spinner, tips, kalimat AI, panel rencana) tumbuh ke ATAS, bukan
        menyelip di antara keduanya.

        Panel rencana dibaca via plan_tool.get_state() tiap frame, jadi
        centang otomatis muncul begitu plan_step() mengubah flag completed[i]
        dari False ke True, tanpa perlu mekanisme notifikasi. Tinggi region bertambah saat plan aktif
        (maks ~16 baris: 12 langkah + bingkai), tetap aman di layar standar.

        Semua baris _oneline anti-wrap -> lebar stabil, tak pernah menimpa
        konten yang sudah tercetak. Saat done, region dikosongkan: semuanya
        sudah berada di scrollback."""
        if self.done:
            return Text("")
        rows = []
        footer = self._footer()
        if isinstance(footer, Group):
            footer_rows = list(footer.renderables)
        else:
            footer_rows = [footer]
        # Urutan dari atas ke bawah: BARIS STATUS (kegiatan/waktu/token/tool +
        # tips) -> PANEL RENCANA -> KOTAK -> BAR. Konten percakapan TIDAK
        # dirender di sini — ia dicetak ke scrollback oleh _tambah_konten,
        # jadi region ini tetap kecil (tak berkedip) dan riwayat bisa digulir
        # ke atas. Baris status WAJIB di atas kotak chat, bahkan di atas
        # panel rencana; semua bagian menempel ke dasar karena region live
        # di-render ulang.
        plan_rows = _panel_plan()
        rows.append(_KOSONG)
        rows.extend(footer_rows)
        if plan_rows:
            # Panel plan menempel langsung ke kotak ketikan
            # — bingkai ╰─╯ dan ╭─╮ bersentuhan, tak perlu napas.
            rows.extend(plan_rows)
        rows.extend(self._kotak_ketikan())
        # Pratinjau gambar menempel DI BAWAH kotak — kembaran layout idle,
        # urutannya haram berbeda: status · gambar · kotak · rencana.
        rows.extend(_panel_gambar_rich())
        rows.append(_bar_status(self.agent, self.total()))
        return Group(*rows)

    def _footer(self):
        el = time.time() - self.start
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]
        now = time.time()
        if self.padat is not None:
            return _baris_padat(*self.padat, el)
        if self.cancelling:
            return _oneline(_TM(
                f"  [bold #f0603c]{frame}[/] [#f0603c]membatalkan — "
                f"menunggu langkah aman berhenti[/]   [dim italic]Ctrl+C lagi = paksa[/]"))
        if now < self.retry_until:
            left = self.retry_until - now
            return _oneline(_TM(
                f"  [bold {tema.p('aksen_terang')}]{frame}[/] [{tema.p('aksen_terang')}]layanan sibuk — menunggu lalu "
                f"melanjutkan[/] [bold {tema.p('aksen2')}]{left:.0f}s[/]   [dim italic]Ctrl+C batal[/]"))
        target = float(self.agent.tokens_session.total + self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30
        if abs(target - self.disp) < 1:
            self.disp = target
        tok = _fmt(int(self.disp))
        extra = ""
        if self.tool:
            lbl = self.tool_label
            if len(lbl) > 40:
                lbl = lbl[:37] + "…"
            lbl = f" [dim]{_esc(lbl)}[/]" if lbl else ""
            extra = f"   [dim]·[/]   [{tema.p('aksen_terang')}]🔧 {_esc(self.tool)}[/]{lbl}"
        # Segmen "◇ effort" HANYA untuk model (API): di sanalah mode berpikir
        # jadi parameter yang benar-benar ikut tiap permintaan. Untuk model (web)
        # agent.effort selalu None — mode berpikirnya tombol di situs, bukan
        # keadaan yang kita pegang, jadi menampilkannya di sini berarti mengaku
        # tahu sesuatu yang tak kita ketahui.
        if self.agent.effort:
            _lbl = models.EFFORT_INFO.get(
                self.agent.effort, (self.agent.effort,))[0]
            extra += (f"   [dim]·[/]   [{tema.p('aksen2')}]◇ {_esc(_lbl)}[/]")
        status = _oneline(_TM(
            f"  [bold {tema.p('aksen')}]{frame}[/] [{tema.p('aksen')}]{_esc(self.phase)}[/]   [dim]·[/]   "
            f"[{tema.p('aksen2')}]{_fmt_elapsed(el)}[/]   [dim]·[/]   [{tema.p('aksen_terang')}]⚡ {tok}[/] "
            f"[dim]sesi[/]{extra}"
            f"   [dim italic]Ctrl+C batal[/]"))
        rows = [status]
        # Blok pikiran menempel LANGSUNG di bawah baris status, tanpa napas:
        # keduanya satu keterangan tentang hal yang sama (apa yang sedang
        # dikerjakan model), sedangkan tips di bawah memang catatan terpisah
        # dan karena itu ia yang diberi baris kosong.
        rows.extend(_blok_pikir(self.pikir, self.pikir_buka))
        # Ketikannya sendiri tampil di KOTAK CHAT (lihat _kotak_ketikan), bukan
        # di sini — supaya bentuk & tempatnya sama persis dengan prompt idle.
        # Tak ada baris tambahan soal antrean: kotak chat satu-satunya UI chat.

        # Tips bergantian (tiap 10 dtk) — baru muncul setelah beberapa detik agar
        # giliran singkat tak sempat kedip-kedip tips. Diberi baris kosong di
        # atasnya supaya ia terbaca sebagai catatan tersendiri, bukan sambungan
        # baris status di atasnya.
        if el > 4:
            tip = _TIPS[int(el / 10) % len(_TIPS)]
            rows.append(_KOSONG)
            rows.append(_oneline(Text(f"  ✦ tips: {tip}", style="dim italic")))
        return rows[0] if len(rows) == 1 else Group(*rows)



# ---------------------------------------------------------------------------
# Komponen tampilan
# ---------------------------------------------------------------------------
def _hint_banner(lebar: int) -> list[Text]:
    """Hint perintah pembuka, 1 baris yang selalu muat di `lebar`.

    Terminal selebar apa pun mendapat versi yang utuh; terminal sempit
    berangsur turun ke versi ringkas, bukan hint yang terlipat ke dua
    baris tak beraturan."""
    penuh = Text()
    penuh.append("ketik pesan untuk mengobrol", style="dim")
    penuh.append("   ")
    penuh.append("/menu", style=tema.p("aksen_terang"))
    penuh.append(" menu   ", style="dim")
    penuh.append("/model", style=tema.p("aksen_terang"))
    penuh.append(" ganti model   ", style="dim")
    penuh.append("/exit", style="#f0603c")
    penuh.append(" keluar", style="dim")
    ringkas = Text()
    ringkas.append("ketik pesan untuk mengobrol", style="dim")
    ringkas.append("   ")
    ringkas.append("/menu /model /exit", style=tema.p("aksen_terang"))
    for t in (penuh, ringkas):
        if t.cell_len <= lebar:
            return [_pusatkan(t, lebar)]
    return [_pusatkan(Text("/menu /model /exit", style=tema.p("aksen_terang")), lebar)]


def _banner(agent: Agent, resumed: bool) -> Panel:
    """Panel pembuka: logo di tengah + hint singkat."""
    # Ruang isi panel dihitung EKSPLISIT: total - padding luar (_LPAD kiri
    # & kanan) - tepi panel (2) - padding panel (2+2). Logo dan hint dipusat
    # kan terhadap ruang yang SUNGGUH tersedia, bukan lebar terminal.
    isi = max(24, console.width - 2 * _LPAD - 2 - 4)
    body = Group(_logo_content(isi), Rule(style=tema.p("tepi_redup")),
                 *_hint_banner(isi))
    return Panel(body, border_style=tema.p("aksen"), box=box.ROUNDED, padding=(1, 2))


# Tinggi minim kotak chat: napas(1) + kotak(3) + status(1). Dipakai oleh
# _dorong_ke_bawah untuk menyisakan ruang di dasar layar.
_KOTAK_TINGGI_MIN = 5


def _dorong_ke_bawah(renderables: list) -> None:
    """Cetak renderables DI POSISI KURSOR — tanpa baris kosong pengisi.

    Dipanggil di startup dan saat /clear & /new. Dulu ia mengisi sisa layar
    dengan baris baru supaya konten MENEMPEL di dasar; pengisi itu masuk
    scrollback dan menjadi celah permanen antar giliran (persis keluhan
    'jarak antar prompt & jawaban berjauhan' — sementara --resume yang mencetak
    rapat tampil normal). Kotak chat & region live kini digambar mengikuti
    kursor (lihat _ke_dasar_layar) dan menyentuh dasar sendiri saat konten
    memenuhi layar, jadi pengisi tak lagi diperlukan."""
    _tambah_konten(renderables)


# ---------------------------------------------------------------------------
# Loop utama
# ---------------------------------------------------------------------------
def main(resume: bool = False) -> None:
    console.clear()
    # Persiapan cepat (deteksi OS, baca sesi, peta proyek). Bar loading BERTAHAP
    # sudah ditampilkan saat impor pustaka (di __main__._preload_with_bar) — fase
    # yang benar-benar lama. Sisa kerja di sini ringan; untuk pemindaian proyek
    # BESAR yang butuh baca banyak file, tampilkan bar tersendiri.
    os_status = osinfo.sync_to_memory()

    resumed = False
    if resume:
        session = session_mod.latest()
        if session:
            resumed = True
        else:
            session = Session.create()
    else:
        session = Session.create()

    # Peta proyek: JANGAN memblokir startup — pengguna harus bisa LANGSUNG
    # mengetik. Pakai cache disk apa adanya (instan, mungkin sedikit basi), lalu
    # periksa kesegaran & bangun ulang DI THREAD LATAR; system prompt disegarkan
    # otomatis begitu peta terbaru siap.
    #
    # DIBUNGKUS dengan sengaja: instalasi PARSIAL/basi (update tak tuntas -> modul
    # tak lagi cocok satu sama lain, mis. cli.py memanggil fungsi yang belum ada
    # di projectindex.py versi lama) TAK boleh membuat bagas-ai gagal start dengan
    # traceback mentah. Petanya toh dibangun ulang di latar; cukup lanjut & beri
    # tahu sekali supaya penyebabnya (perlu reinstall bersih) jelas.
    try:
        _primed_map = projectindex.prime(config.PROJECT_ROOT)
    except Exception as _prime_exc:  # noqa: BLE001
        _primed_map = ""
        _tambah_konten([_TM(
            "  [yellow]⚠ peta proyek dilewati saat start[/] "
            f"[dim](instalasi tampaknya belum tuntas: {type(_prime_exc).__name__}). "
            "Tutup bagas-ai lalu reinstall/`bagasai update` bila ini berulang.)[/]")])

    agent = Agent(session=session)   # instan: pakai peta cache / tanpa peta dulu

    def _bg_build_map() -> None:
        try:
            # Spek laptop: deteksi LOKAL sekali saja (tanpa LLM) -> memory.
            hw_status = osinfo.sync_hardware_to_memory()
            fresh = projectindex.refresh(config.PROJECT_ROOT)
            if fresh != _primed_map or hw_status == "added":
                agent.refresh_system_prompt()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_bg_build_map, daemon=True).start()
    # Kumpulkan SEMUA konten startup (logo, info sesi, OS, peta) dalam satu batch,
    # baru dorong ke DASAR LAYAR sebelum mencetak — bukan dicetak di puncak layar
    # yang kosong dan diturunkan ke bawah oleh _ke_dasar_layar(). Dengan
    # mendorong manual, logo menempel tepat di atas kotak chat tanpa celah kosong
    # yang memotong tampilannya.
    _konten_startup: list = []
    _konten_startup.append(Padding(_banner(agent, resumed), (0, _LPAD, 0, _LPAD)))
    if resumed:
        _konten_startup.append(Padding(Rule("[dim]percakapan sebelumnya[/dim]",
                                            style=tema.p("tepi_redup")), (1, 0, 0, 0)))
        for m in agent.memory.messages:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                # WAJIB dibersihkan dari kendali & ANSI: teks pengguna yang
                # memuat '[i]' / '[red]' (lazim di kode, mis. arr[i]) akan
                # ditafsirkan rich sebagai markup — teks berubah gaya &
                # kurungnya hilang saat replay. _gema_prompt menghindarinya
                # karena memakai Text.append, bukan from_markup.
                _konten_startup.extend([Text("\n"), _gema_prompt(content)])
            elif role == "diff":
                # Potongan kode langkah tulis/ubah/hapus direplay juga —
                # tanpa ini transkrip --resume kehilangan seluruh kerja
                # kodenya (diff dulu hanya tampil di layar sesaat).
                _konten_startup.append(Padding(_replay_diff(m),
                                               (0, 3, 0, 3)))
            elif role == "assistant" and content:
                _konten_startup.append(_TM(
                    "\n  [bold {tema.p('aksen2')}]🤖 bagas-ai[/]"))
                # Rapat ke headernya (top=0) — sama seperti add_narasi:
                # header dan ucapannya satu blok, jaraknya satu baris di
                # atas header, bukan di antara keduanya.
                _konten_startup.append(Padding(_md(content), (0, 3, 0, 3)))
        _konten_startup.append(Padding(Rule("[dim]lanjut di bawah[/dim]",
                                            style=tema.p("tepi_redup")), (1, 0, 0, 0)))
    if os_status in ("added", "updated"):
        verb = "terdeteksi & disimpan" if os_status == "added" else "diperbarui"
        _konten_startup.append(Padding(_TM(
            f"[dim]🖥  OS {verb}: {osinfo.summary()} — perintah terminal akan "
            f"disesuaikan.[/dim]"), (0, _LPAD, 0, _LPAD)))
    # Peta proyek: dari cache instan (disegarkan di latar), atau sedang dibangun
    # pertama kali di latar — dua-duanya TANPA menunda prompt.
    _pn = _primed_map.count("\n- ")
    if _pn:
        _konten_startup.append(Padding(_TM(
            f"[dim]🗺  peta proyek siap (~{_pn} file) — disegarkan di latar; "
            f"ketik [/][{tema.p('aksen_terang')}]/scan[/][dim] untuk paksa pindai ulang.[/]"),
            (0, _LPAD, 0, _LPAD)))
    else:
        _konten_startup.append(Padding(_TM(
            "[dim]🗺  peta proyek dibangun di latar — langsung ngetik aja, "
            "tak perlu menunggu.[/dim]"), (0, _LPAD, 0, _LPAD)))

    # Dorong semua konten ke dasar layar sebelum cetak, supaya logo menempel
    # tepat di atas kotak chat (lihat _dorong_ke_bawah).
    _dorong_ke_bawah(_konten_startup)

    # State Live global (modul): diisi saat giliran berjalan (process_stream /
    # process_classic) & menu ask_user; dipakai _tambah_konten/_flush_konten
    # untuk memutuskan cetak-langsung vs tampung.
    _LIVE["live"] = None
    _LIVE["paused"] = False
    tg_service: dict = {"svc": None}   # layanan bot Telegram di dalam sesi ini
    # Pendengar mikrofon (/voice). SELALU mulai dari mati & tak disimpan ke
    # preferensi: mikrofon yang menyala sendiri di sesi berikutnya adalah
    # kejutan yang tak seorang pun minta.
    voice_state: dict = {"pendengar": None}
    # Total token PERSISTEN lintas semua sesi ("dimanapun").
    # "sesi" (agent.tokens_session) kini persisten per-sesi (ikut saat --resume),
    # dan sudah termasuk di total global. Agar tidak dobel saat resume, base =
    # total global dikurangi token sesi yang sudah dihitung.
    grand = {"base": prefs.get_total_tokens() - agent.tokens_session.total}

    def _save_total() -> None:
        prefs.set_total_tokens(grand["base"] + agent.tokens_session.total)

    def _total_global() -> int:
        """Angka "🔋 total" di bar status — sama persis saat idle & saat sibuk.

        tokens_live ditambahkan supaya bar status juga mutlak real-time selama
        giliran berjalan, bukan hanya melompat di akhir giliran."""
        return grand["base"] + agent.tokens_session.total + agent.tokens_live

    status_obj = Status(agent, total=_total_global)

    # --- Jejak langkah -------------------------------------------------------
    # Tiap pemanggilan tool = satu "langkah" bernomor. Nomornya tak pernah
    # tampil di layar lagi — ia cuma urutan internal. `steps` menyimpan
    # ringkasannya untuk ringkasan giliran & pemicu indeks ulang; `cur_step`
    # menjembatani on_tool -> on_tool_result.
    steps: dict[int, dict] = {}
    step_ctr = {"n": 0}
    cur_step: dict = {}
    # --- Antrean prompt: mengetik SELAGI giliran berjalan --------------------
    # Enter saat giliran masih jalan TIDAK membatalkan apa pun — teksnya masuk
    # antrean dan dikerjakan berurutan setelah giliran selesai. `typing_state`
    # menyimpan ketikan yang belum di-Enter; sisa ketikan saat giliran selesai
    # dibawa ke prompt berikutnya sebagai isi awal (tak ada ketikan yang hilang).
    prompt_queue: list[str] = []
    # Kunci antrean: `_ketik` mengisi dari thread utama sementara `_ambil_sisipan`
    # mengambil dari thread worker. Dulu tak perlu karena keduanya cuma memakai
    # operasi list atomik; sekarang pengambilnya MENYARING (perintah / dibiarkan
    # di antrean), dan penyaringan bukan operasi tunggal — tanpa kunci, pesan
    # yang datang di tengahnya bisa terhapus tanpa pernah dikerjakan.
    antre_lock = threading.Lock()
    # `pos` = letak kursor di dalam `buf`. Tanpa ini ketikan cuma bisa
    # tumbuh & disusut dari ujung — salah ketik di tengah kalimat panjang
    # berarti menghapus semuanya dulu.
    typing_state = {"buf": "", "pos": 0}
    # Mode tampilan giliran: True = tampilan mengalir dengan footer status hidup
    # (langkah/diff/jawaban di tumpukan bawah yang menempel ke dasar layar);
    # False = tampilan klasik (semua ke scrollback).
    tui_mode = {"on": True}

    def _step_label(name: str, args: dict) -> str:
        a = args if isinstance(args, dict) else {}
        val = ""
        if name == "run_command":
            val = a.get("command", "") or "perintah"
        elif name == "run_python":
            val = "kode Python"
        elif name == "run_script":
            val = f"skrip {a.get('name', '')}"
        elif name == "read_file":
            val = a.get("path", "")
        elif name == "list_dir":
            val = a.get("path", ".") or "."
        elif name == "web_search":
            val = a.get("query", "")
        elif name == "write_file":
            val = a.get("path", "")
        elif name == "delete_file":
            val = a.get("path", "")
        elif name == "save_script":
            val = a.get("name", "")
        elif name == "remember":
            val = a.get("fact", "") or "fakta"
        else:
            for k in ("path", "source", "dest_path", "url", "query", "pattern",
                      "bg_id"):
                v = a.get(k)
                if isinstance(v, str) and v:
                    val = v
                    break
            if not val:
                val = name
        return " ".join(str(val).split())

    # Saat prompt pilihan (ask_user) aktif, POLLER keyboard di loop giliran
    # (msvcrt) HARUS berhenti membaca — kalau tidak, ketikan user DICURI poller
    # dan dropdown inquirer rusak (keduanya membaca console yang sama).
    input_paused = {"on": False}
    # Event Telegram yang datang saat console "dipinjam" menu — dicetak nanti.
    _tg_pending: list[tuple[str, str]] = []

    def _stack_rich() -> list:
        """Baris tumpukan bawah versi rich: rencana? · kotak chat · gambar? ·
        bar status — urutan yang sama persis dengan layout idle. Dipakai
        _kaki_menu (dikonversi ke prompt_toolkit) dan oleh Live singkat di
        luar giliran, supaya kotak chat tak pernah hilang hanya karena
        program sedang menunggu sesuatu (login browser, unggah ingatan…)."""
        return _panel_plan() + _kotak_chat() + _panel_gambar_rich() \
            + [_bar_status(agent, _total_global())]

    def _region_stack(baris) -> Group:
        """Satu region Live: baris status sementara di ATAS tumpukan bawah."""
        return Group(baris, *(_stack_rich()))

    def _kaki_menu() -> list[tuple[str, str]] | None:
        """Tumpukan bawah untuk dipasang di kaki menu — lihat _stack_rich.
        Kotak chat TAK BOLEH hilang hanya karena menu terbuka; layar
        terlalu pendek untuk memuat menu + tumpukan adalah satu-satunya
        alasan melepaskannya."""
        baris = _stack_rich()
        kaki: list[tuple[str, str]] = []
        for i, t in enumerate(baris):
            if i:
                kaki.append(("", "\n"))
            kaki.extend(_pt_dari_teks(t))
        # Anggaran 16 baris untuk kotak menu termasuk judul & petunjuknya.
        if len(baris) + 16 > console.height:
            return None
        return kaki

    def _with_console(fn, *a, **k):
        """Jalankan aksi ber-dropdown (inquirer) dengan console dipinjam penuh:
        poller input berhenti & pesan thread lain (Telegram) ditahan dulu —
        mencegah menu rusak oleh cetakan yang menyela. Tumpukan bawah
        (kotak chat + bar status) dipasang di KAKI tiap menu supaya tak pernah
        tertimpa app menu yang mengisi hingga dasar layar."""
        from ..ui import menu as ui_menu
        ui_menu.kaki_aktif = _kaki_menu()
        input_paused["on"] = True
        try:
            return fn(*a, **k)
        finally:
            ui_menu.kaki_aktif = None
            input_paused["on"] = False
            _tg_flush()

    def choice_handler(question: str, options: list[str], multiple: bool) -> str:
        """Pertanyaan dari dalam giliran: klarifikasi ask_user, dan permintaan
        IZIN akses folder luar (permissions.py).

        Pertanyaan & jawabannya tak dicetak terpisah lagi: menu bagas-ai sudah
        menampilkan pertanyaannya di dalam kotak lalu meninggalkan satu baris
        ringkasan "✓ pertanyaan · jawaban" sesudah kotaknya hilang."""
        from ..ui import menu as ui_menu
        input_paused["on"] = True
        live = _LIVE.get("live")
        _transient_lama = None
        if live:
            # Tandai PAUSED dulu (sebelum stop): _flush_konten di loop Live
            # harus tahu bahwa Live berhenti sementara, supaya tidak mencetak
            # ke console saat menu inquirer sedang menggambar.
            _LIVE["paused"] = True
            # Frame region DIHAPUS saat stop (transient sesaat). Dibiarkan
            # berarti kotak chat & bar status tampil DOBEL selama menu
            # terbuka — sekali sebagai frame beku di atas menu, sekali lagi
            # sebagai kaki di bawahnya. Nilai aslinya disimpan karena mode
            # klasik memang ber-transient True sejak awal.
            _transient_lama = live.transient
            live.transient = True
            live.stop()
        # Menu & isian bebas (ask_user) digambar mulai di posisi kursor —
        # tepat di bawah konten terakhir, karena frame region barusan
        # dihapus saat stop. _ke_dasar_layar() membuang sisa gambaran apa pun
        # di bawah kursor supaya menu tidak menumpuk di atasnya; sisanya
        # (scrollback di atas) tak disentuh sama sekali.
        _ke_dasar_layar()
        ui_menu.kaki_aktif = _kaki_menu()
        try:
            answer = _tanya_pilihan(question, list(options), bool(multiple))
        except (KeyboardInterrupt, EOFError):
            answer = "(dibatalkan)"
        finally:
            ui_menu.kaki_aktif = None
            input_paused["on"] = False
            _tg_flush()
        if live:
            # TANPA console.clear(): itu menghapus SELURUH layar terlihat —
            # transkrip yang semula kelihatan ikut lenyap, dan yang tersisa
            # cuma region kecil di atas plus kekosongan panjang di bawahnya
            # sampai konten baru mengalir ("nge-space banyak kosong").
            # Cukup erase-down dari kursor: erase_when_done milik
            # prompt_toolkit sudah mengembalikan kursor ke baris PERTAMA
            # areanya, dan segala sesudahnya hanyalah gambaran menunya.
            _ke_dasar_layar()
            live.start()
            live.transient = _transient_lama
            # PAUSED dilepas SETELAH start: antara start() dan pelepasannya
            # _flush_konten tetap menahan — print saat Live belum aktif lagi
            # akan mendarat di posisi kursor dan merusak region.
            _LIVE["paused"] = False
            # Ringkasan "✓ pertanyaan · jawaban" dicetak lewat jalur
            # tertampung, bukan langsung: jejak tanya-jawab ini justru inti
            # dari modali ini, maka dicetak ulang lewat jalur tertampung
            # (aman dari tabrakan refresh Live, dan ikut menempel ke dasar).
            q = question if len(question) <= 46 else question[:45] + "…"
            _tambah_konten([_oneline(_TM(
                f"  [#9fc93c]✓[/] [{tema.p('teks')}]{_esc(q)}[/]"
                f"  [dim]·[/] [{tema.p('teks')}]{_esc(str(answer))}[/]"))])
        return answer

    interaction.set_choice_handler(choice_handler)

    # Tool yang hasilnya berupa teks substansial & layak di-expand penuh.
    _EXPANDABLE = {"run_command", "run_python", "run_script",
                   "read_file", "list_dir", "web_search",
                   "search_text", "glob_files", "fetch_url", "http_request",
                   "validate_project", "diff_files", "bg_output", "media_info"}

    def on_tool(name: str, args: dict) -> None:
        """Mulai satu langkah: set fase + timer, dan untuk tulis/hapus tampilkan diff."""
        status_obj.note_step(name)
        step_ctr["n"] += 1
        cur_step.clear()
        cur_step.update(n=step_ctr["n"], name=name, args=args, start=time.time())
        p = args.get("path") if isinstance(args, dict) else None
        # Diff/preview substantif ditampilkan SEBELUM aksi (konten inti perubahan).
        if name == "edit_files":
            # edit_files membawa DAFTAR suntingan — tampilkan diff tiap
            # suntingan berurutan sesuai file-nya, sama seperti edit_file satuan.
            for e in (args.get("edits") or []):
                if not isinstance(e, dict):
                    continue
                ep = e.get("path", "")
                if not ep:
                    continue
                old, new, exists = _isi_sebelum_sesudah("edit_file", ep, e)
                if not (exists and old == new):
                    _print_diff(ep, old, new, is_new=not exists)
                    agent.memory.add_diff(ep, _teks_unified(old, new),
                                          is_new=not exists)
        elif name in _TOOL_DIFF and p:
            old, new, exists = _isi_sebelum_sesudah(name, p, args)
            # old == new pada file yang sudah ada = tulisan itu akan DITOLAK
            # tool-nya (potongan/penyusutan drastis) atau memang tanpa efek —
            # jangan cetak header diff yang menyesatkan.
            if not (exists and old == new):
                _print_diff(p, old, new, is_new=not exists)
                agent.memory.add_diff(p, _teks_unified(old, new),
                                      is_new=not exists)
        elif name == "delete_file" and p:
            full = config.PROJECT_ROOT / p
            content = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
            _print_delete(p, content)
            agent.memory.add_diff(
                p, "\n".join(content.splitlines()[:200]), is_new=False,
                deleted=True)

    def finish_step(name: str, result: str) -> None:
        """Selesaikan langkah: cetak baris jejak + siapkan hasil penuhnya.

        Baris jejak = ceklis ringkas (ikon, fase, target, durasi). Hasil yang
        substansial diringkas jadi "N baris" yang BISA DI-CTRL+KLIK untuk
        membuka isi penuhnya; hasil gagal ditandai."""
        n = cur_step.get("n", step_ctr["n"])
        args = cur_step.get("args", {})
        dur = time.time() - cur_step.get("start", time.time())
        text = (result or "").strip()
        failed = text.startswith("[GAGAL") or text.startswith("[error]")

        # Catatan langkah — bukan lagi untuk menampilkan ulang hasilnya (itu
        # kini urusan berkas langkah-N.html), melainkan untuk ringkasan giliran
        # dan pemicu indeks-ulang peta proyek. Karena itu isinya cuma ringkasan;
        # teks hasilnya tak ikut disimpan.
        steps[n] = {"name": name, "label": _step_label(name, args),
                    "failed": failed, "dur": dur}
        if len(steps) > 200:            # batasi memori sesi panjang
            for old_n in sorted(steps)[:-200]:
                steps.pop(old_n, None)

        label = _step_label(name, args)
        if len(label) > 64:
            label = label[:61] + "…"
        icon = "[#f0603c]✗[/]" if failed else "[#9fc93c]✓[/]"
        phase = _PHASE.get(name, "selesai")
        dur_s = f"{dur:.1f}s" if dur >= 0.05 else ""
        console.print()          # satu baris kosong di ATAS blok (aturan irama)
        console.print(f"  {icon} [#f2e3cc]{phase}[/]  [white]{_esc(label)}[/]"
                      f"   [dim]{dur_s}[/]")

        # Di bawahnya: pratinjau keluarannya, berlatar abu-abu gelap. Langkah
        # GAGAL pun ikut ditampilkan — justru di situlah isinya paling
        # dibutuhkan (pesan galat, jejak tumpukan, keluaran perintah yang
        # error); menyembunyikannya di balik satu kata "gagal" memaksa pengguna
        # menyuruh AI mengulang cuma untuk melihat sebabnya.
        body = re.sub(r"^exit_code=\S+\n?", "", text)
        bersih = _bersih_kendali(body)
        if failed or (name in _EXPANDABLE and bersih.strip()):
            for baris in _pratinjau_hasil(bersih.splitlines(), gagal=failed):
                console.print(baris)
        elif name in _TOOL_DIFF:
            # Tampilkan status cek sintaks bila ada di hasil — edit_file &
            # append_file juga mengembalikannya, bukan cuma write_file.
            m = re.search(r"\[cek sintaks\]\s*(.+)", text)
            if m:
                ok = m.group(1).startswith("OK")
                col = "#9fc93c" if ok else "#f0603c"
                console.print(f"     [{col}]{_esc(m.group(1).strip())}[/]")
        # Langkah tool selesai -> kembali ke fase "berpikir" untuk generasi berikut.
        status_obj.note_thinking()

    # Berapa lama giliran web yang dibatalkan boleh "bernapas" sebelum sesi
    # browsernya dianggap benar-benar macet. Sengaja jauh lebih longgar dari
    # sebelumnya (2 detik): pembatalan diperiksa tiap ~300 ms DI ANTARA
    # panggilan Playwright, jadi Ctrl+C yang jatuh di tengah panggilan panjang
    # yang SEHAT — meluncurkan Chrome, memuat halaman — memang baru terasa
    # belasan detik kemudian. Dengan tenggat 2 detik, pembatalan normal pun
    # dihukum reset: jendela browser dimatikan, lalu pesan berikutnya harus
    # menyalakan Chrome dari nol (dan itulah yang kadang nyangkut).
    _TENGGAT_LEPAS = 20.0

    def _reset_web_hub_if_stuck(wt: threading.Thread) -> None:
        """Pasca-Ctrl+C pada giliran web: pastikan sesi browser tak tertinggal
        macet — TANPA menahan antarmuka.

        Dijalankan di thread sendiri. Pembersihannya memanggil PowerShell
        (mencari & menunggu proses Chrome mati) yang TERUKUR ~0,8 detik sekali
        jalan, dan dulu itu dikerjakan di thread yang sama dengan tampilan —
        sehingga seluruh UI membeku persis di saat pengguna baru saja menekan
        Ctrl+C dan paling ingin melihat responsnya."""
        if not wt.is_alive():
            return
        svc = agent.model_spec.connector or None

        def bereskan() -> None:
            wt.join(timeout=_TENGGAT_LEPAS)
            if not wt.is_alive():
                return          # lepas sendiri — browser & sesi login dibiarkan
            try:
                from ..connectors import browser as _br
                _br.reset_hub(svc)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=bereskan, daemon=True,
                         name="bagasai-reset-hub").start()

    def _connect_web(prev_model_id: str) -> None:
        """Alur CONNECT saat model web DIPILIH (bukan saat pesan pertama):
        belum pernah login -> diarahkan ke Chrome untuk login SEKALI; sudah
        pernah -> langsung tersambung ke sesi chat. Gagal/dibatalkan -> kembali
        ke model sebelumnya supaya pengguna tak terjebak di model yang mati."""
        spec = agent.model_spec
        try:
            from .. import connectors
        except Exception:  # noqa: BLE001
            connectors = None
        if connectors is None or not connectors.playwright_available():
            console.print(
                "  [yellow]⚠ Connector butuh Playwright:[/] [bold]pip install "
                "playwright[/] lalu [bold]playwright install chromium[/]\n")
            _revert_model(prev_model_id)
            return

        state = {"status": f"menghubungkan ke {spec.label}…"}
        cancel_event = threading.Event()
        result: dict = {"login": None, "error": None}

        def worker() -> None:
            try:
                result["login"] = connectors.get_connector(spec.connector).connect(
                    on_status=lambda m: state.__setitem__("status", m),
                    # Permintaan sign-in DICETAK, bukan cuma dikedipkan di
                    # baris status: Live di bawah bersifat transient, jadi
                    # apa pun yang cuma lewat status akan lenyap tanpa bekas
                    # begitu penantiannya selesai. console.print saat Live
                    # aktif mendarat di atas region hidup itu — permanen.
                    on_notice=lambda m: console.print(
                        f"\n  [bold {tema.p('aksen')}]{_esc(m)}[/]\n"),
                    cancel_event=cancel_event,
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        def render():
            return _TM(
                f"  [{tema.p('aksen_terang')}]◐[/] [dim]{_esc(state['status'])}[/]")

        wt = threading.Thread(target=worker, daemon=True)
        interrupted = False
        try:
            # Region dipaku ke dasar dulu supaya tumpukan bawahnya (kotak
            # chat + bar status) menempel seperti saat idle — login browser
            # bisa bermenit-menit, kotak tak boleh menghilang selama itu.
            _ke_dasar_layar()
            with Live(_region_stack(render()), console=console, refresh_per_second=8,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    try:
                        live.update(_region_stack(render()))
                        wt.join(timeout=0.1)
                    except KeyboardInterrupt:
                        if not interrupted:
                            interrupted = True
                            cancel_event.set()
                            state["status"] = "membatalkan…"
                        else:
                            break
        except KeyboardInterrupt:
            interrupted = True
            cancel_event.set()

        if result["login"] is not None:
            # TANPA menu pilihan sesi — sesi bagasai SELALU mengikuti percakapan
            # browser yang terkait dengannya otomatis: layanan yang sudah punya
            # kaitan chat (dari sesi ini / --resume) disambung kembali begitu
            # pesan dikirim, dan layanan yang belum langsung membuat chat BARU
            # yang diawali konteks penuh — termasuk ringkasan percakapan dari
            # model sebelumnya (lihat _sync_web_state & build_transcript_digest).
            # Menu pilih-chat lama justru memutus aturan 'satu sesi terminal =
            # satu percakapan browser': pengguna bisa memilih chat yang tak
            # berhubungan dengan pekerjaan yang sedang berjalan.
            if getattr(agent, "_web_chat_id", ""):
                console.print(
                    f"  [#9fc93c]✓ terhubung ke[/] [bold]{_esc(spec.label)}[/bold]"
                    " [dim]— pesan berikutnya melanjutkan percakapan web yang "
                    "sudah terkait sesi ini.[/]\n")
            else:
                console.print(
                    f"  [#9fc93c]✓ terhubung ke[/] [bold]{_esc(spec.label)}[/bold]"
                    " [dim]— percakapan baru dibuka di pesan pertama, lengkap "
                    "dengan konteks proyek & riwayat dari model "
                    "sebelumnya.[/]\n")
            return
        err = result["error"]
        why = ("dibatalkan" if interrupted or isinstance(err, llm.Cancelled)
               else f"gagal: {err}")
        console.print(f"  [yellow]⚠ koneksi {_esc(spec.label)} {_esc(str(why))}[/]")
        _revert_model(prev_model_id)

    def _delete_web_chats_of(sessions_deleted: list) -> None:
        """Hapus percakapan AI web milik sesi terminal yang baru saja dihapus
        (satu sesi terminal = satu percakapan browser)."""
        pairs: dict[str, list[str]] = {}
        for s in sessions_deleted:
            for svc, cid in (getattr(s, "web_chats", None) or {}).items():
                if cid:
                    pairs.setdefault(svc, []).append(cid)
        if not pairs:
            return
        try:
            from .. import connectors
            if not connectors.playwright_available():
                return
        except Exception:  # noqa: BLE001
            return
        total = sum(len(v) for v in pairs.values())

        def _do() -> int:
            n = 0
            for svc, ids in pairs.items():
                try:
                    conn = connectors.get_connector(svc)
                    if conn.supports_chat_admin():
                        n += conn.delete_chats(ids)
                        conn.forget_chats(set(ids))
                except Exception:  # noqa: BLE001 - lanjut ke service berikutnya
                    pass
            return n

        n, err = _web_busy(f"menghapus {total} percakapan web terkait…", _do)
        if err is None and n:
            console.print(f"  [dim]🌐 {n} percakapan di AI web ikut dihapus.[/dim]")

    def _revert_model(prev_model_id: str) -> None:
        # Seluruh model kini berbasis browser, jadi tak ada lagi model "pasti
        # jalan tanpa koneksi" untuk dijadikan pelabuhan. Yang masuk akal adalah
        # kembali ke model SEBELUMNYA apa adanya; bila ID-nya sudah tak dikenal
        # (mis. peninggalan katalog lama), spec_for_id memetakannya ke bawaan.
        if not models.is_known_id(prev_model_id):
            prev_model_id = config.CHAT_MODEL
        try:
            console.print(
                f"  [dim]kembali ke model: {agent.set_model(prev_model_id)}[/dim]\n")
        except ValueError:
            console.print()

    def process(text: str) -> None:
        """Jalankan satu giliran INLINE (tanpa layar-penuh, tetap di alur terminal
        biasa). Semua konten (langkah, diff, jawaban) MENGALIR statis ke
        scrollback; yang hidup hanya footer status kecil di baris paling bawah.
        Keluaran tiap langkah tampil langsung di bawah barisnya. Ctrl+C
        membatalkan. Bila gagal, jatuh ke process_classic.

        Model CONNECTOR web (Kimi/Qwen web) memakai jalur yang SAMA: ia kini
        bisa memanggil tool (edit file, jalankan perintah, dll) lewat protokol
        teks, jadi langkah-langkahnya tampil rapi di terminal."""
        steps.clear()
        step_ctr["n"] = 0
        cur_step.clear()
        turn_start = time.time()
        if not tui_mode["on"]:
            process_classic(text)
            return

        # Saklar hidup callback: worker daemon yang DITINGGAL (Ctrl+C dua kali)
        # tidak boleh lagi mencetak ke terminal setelah kita kembali ke prompt.
        cbs_alive = {"on": True}

        def _commit(renderables) -> None:
            """Cetak konten ke scrollback (via _tambah_konten) — bisa digulir.

            Konten TIDAK dirender di region live: region bawah hanya berisi
            baris status + panel rencana + kotak + bar, jadi ia tetap kecil
            (tak berkedip) dan riwayat percakapan bisa digulir ke atas."""
            if not cbs_alive["on"]:
                return
            if renderables:
                _tambah_konten(renderables)

        view = TurnView(agent, commit=_commit, total=_total_global)
        ctr = {"n": 0}

        def _on_tool(name: str, args: dict) -> None:
            if not cbs_alive["on"]:
                return
            ctr["n"] += 1
            n = ctr["n"]
            label = _step_label(name, args)
            rec = view.start_step(n, name, label)
            cur_step.clear()
            cur_step["rec"] = rec
            cur_step["n"] = n
            # Diff tulis/hapus dicetak (otomatis di ATAS region live) sbg konteks
            # perubahan, lalu menjadi bagian riwayat terminal.
            p = args.get("path") if isinstance(args, dict) else None
            if name == "edit_files":
                # edit_files membawa DAFTAR suntingan — tampilkan diff tiap
                # suntingan berurutan sesuai file-nya.
                for e in (args.get("edits") or []):
                    if not isinstance(e, dict):
                        continue
                    ep = e.get("path", "")
                    if not ep:
                        continue
                    old, new, exists = _isi_sebelum_sesudah("edit_file", ep, e)
                    if not (exists and old == new):
                        _print_diff(ep, old, new, is_new=not exists, ke_konten=True)
                        agent.memory.add_diff(ep, _teks_unified(old, new),
                                              is_new=not exists)
            elif name in _TOOL_DIFF and p:
                # WAJIB lewat _isi_sebelum_sesudah: untuk edit_file/append_file
                # isi barunya BUKAN args["content"] (edit_file pakai old_text/
                # new_text). Menyalin content mentah membuat `new` kosong pada
                # edit_file, sehingga diff menampilkan SELURUH file sebagai
                # terhapus — seakan kode dihapus lalu ditulis ulang.
                old, new, exists = _isi_sebelum_sesudah(name, p, args)
                # old == new pada file yang sudah ada = akan DITOLAK / tanpa
                # efek — jangan cetak header diff yang menyesatkan.
                if not (exists and old == new):
                    _print_diff(p, old, new, is_new=not exists, ke_konten=True)
                    agent.memory.add_diff(p, _teks_unified(old, new),
                                          is_new=not exists)
            elif name == "delete_file" and p:
                full = config.PROJECT_ROOT / p
                content = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
                _print_delete(p, content, ke_konten=True)
                agent.memory.add_diff(
                    p, "\n".join(content.splitlines()[:200]), is_new=False,
                    deleted=True)

        def _on_result(name: str, result: str) -> None:
            if not cbs_alive["on"]:
                return
            rec = cur_step.get("rec")
            n = cur_step.get("n", ctr["n"])
            failed = (result or "").strip().startswith(("[GAGAL", "[error]"))
            if rec is not None:
                view.end_step(rec, result, failed)
            steps[n] = {"name": name, "label": _step_label(name, {}),
                        "failed": failed, "dur": 0.0}
            if rec is not None:
                steps[n]["label"] = rec["label"]
            step_ctr["n"] = n

        def _on_msg(content: str) -> None:
            if cbs_alive["on"]:
                view.add_narasi(content)
                # Kabar yang sama juga DIBACAKAN, supaya terdengar walau
                # jendela terminalnya sedang tertutup jendela lain — keadaan
                # yang justru paling sering terjadi selagi menunggu AI bekerja.
                if prefs.load().get("suara", True):
                    _suara.ucap(content)

        def _on_retry(attempt: int, wait: float, exc: Exception) -> None:
            if cbs_alive["on"]:
                view.note_retry(wait, f"percobaan ke-{attempt}")

        def _on_status(msg: str) -> None:
            """Status model (menyiapkan sesi / memproses / berpikir / menjawab)."""
            if cbs_alive["on"]:
                view.note_phase(_fase_status(msg))

        def _on_pikir(piece: str) -> None:
            """Pikiran model, potongan demi potongan (hanya jalur API)."""
            if cbs_alive["on"]:
                view.note_pikir(piece)

        def _on_tim(nama: list[str]) -> None:
            """Rekan satu tim yang ikut meninjau langkah barusan (lihat tim.py).

            Ditampilkan sebagai satu baris redup, bukan panel: ini peristiwa
            latar yang menemani langkah — bukan hasil yang perlu direnungkan.
            Tanpa baris ini, sudut pandang tambahan itu bekerja tanpa jejak dan
            pengguna tak punya cara tahu siapa yang sedang menemani."""
            if not cbs_alive["on"] or not nama:
                return
            _commit([_oneline(_TM(
                f"  [dim]‧ ikut meninjau: {_esc(', '.join(nama))}[/dim]"))])

        def _on_notice(msg: str) -> None:
            """Kabar dari mesin giliran: pesan susulan disisipkan, naik kelas,
            atau tindakan anti-macet.

            Labelnya dipilih dari ISI pesannya. Penyisipan diperiksa LEBIH DULU
            dan tanpa embel-embel "konteks dipertahankan": kalimat itu benar
            untuk pemulihan anti-macet, tapi jadi membingungkan pada peristiwa
            yang sama sekali bukan pemulihan."""
            # CAPTCHA: pengguna harus BERTINDAK, dan jendela browser akan
            # melompat ke depan beberapa detik lagi. Ditampilkan menonjol —
            # kabar sebaris redup di tengah giliran yang sibuk terlalu mudah
            # terlewat, dan yang terlewat di sini berakhir jadi giliran yang
            # menggantung tanpa sebab yang kelihatan.
            if "VERIFIKASI KEAMANAN" in msg or "captcha" in msg.lower():
                _commit([_KOSONG, _TM(
                    f"  [bold #f0603c]🔒 {_esc(msg)}[/]"), _KOSONG])
                return
            # LOGIN: sekelas captcha — giliran berhenti sampai ada tangan
            # manusia di jendela browser. Tanpa cabang sendiri ia jatuh ke
            # label "naik kelas otomatis"/"anti-macet" di bawah, yang sama
            # sekali bukan artinya.
            if "belum login" in msg.lower() or "sign-in" in msg.lower():
                _commit([_KOSONG, _TM(
                    f"  [bold {tema.p('aksen')}]{_esc(msg)}[/]"), _KOSONG])
                return
            if "disisipkan" in msg:
                _commit([_TM(
                    f"  [{tema.p('aksen2')}]✉ {_esc(msg)}[/] "
                    f"[dim]— bagas-ai yang menentukan urutannya[/]")])
                return
            if "langkah" in msg and "batas" in msg:
                # Batas langkah bukan kegagalan maupun pemulihan — ia keputusan
                # yang bisa ditindaklanjuti pengguna, jadi ditandai tersendiri
                # supaya tak terbaca sebagai error.
                _commit([_TM(f"  [#f7d488]⏱ {_esc(msg)}[/]")])
                return
            label = ("⚡ naik kelas otomatis:" if "→" in msg
                     else "🛟 anti-macet:")
            _commit([_TM(
                f"  [{tema.p('aksen_terang')}]{label}[/] [dim]{_esc(msg)} "
                f"— konteks dipertahankan[/]")])

        cancel_event = threading.Event()
        result: dict = {"answer": None, "error": None}

        def _ambil_sisipan() -> list[str]:
            """Ambil pesan yang diketik selagi giliran berjalan — TANPA perintah.

            Perintah bagas-ai (/model, /mode, …) sengaja DITINGGAL di antrean.
            Perintah itu ditujukan ke programnya, bukan ke AI: menyisipkannya ke
            giliran berarti AI membaca "/model" sebagai kalimat pengguna, lalu
            menanggapinya sebagai permintaan — dan perintahnya sendiri tak pernah
            dijalankan. Yang tertinggal dikerjakan gelung utama begitu giliran
            ini benar-benar selesai.

            Dipanggil dari THREAD WORKER sementara `_ketik` mengisi dari thread
            utama, karena itu di bawah kunci: penyaringan menyentuh antrean dua
            kali (baca lalu buang), dan tanpa kunci pesan yang datang di antara
            keduanya ikut terbuang tanpa pernah dikerjakan."""
            with antre_lock:
                if not prompt_queue:
                    return []
                diambil = [t for t in prompt_queue if not _perintah(t)]
                if diambil:
                    prompt_queue[:] = [t for t in prompt_queue if _perintah(t)]
                # Pesan sisipan tak lewat gelung utama, jadi penanda tempelannya
                # harus dikembangkan di sini juga — kalau tidak, AI menerima
                # tulisan "[tempelan #1 · 312 baris · 14,2 KB]" alih-alih log
                # yang sebenarnya ingin ditunjukkan pengguna. [foto] + gambar
                # pending ikut ditukarkan: sisipan adalah jalur masuk yang sama
                # sahnya dengan kotak idle, lampirannya harus sama pula.
                return [_kembangkan_foto(_tempelan.simpanan().kembangkan(t))
                        for t in diambil]

        _padat = _jeda_padat(view)

        def worker() -> None:
            try:
                result["answer"] = agent.run(
                    text, on_tool=_on_tool, on_message=_on_msg,
                    on_retry=_on_retry, cancel_event=cancel_event,
                    on_tool_result=_on_result, on_notice=_on_notice,
                    on_status=_on_status, ambil_sisipan=_ambil_sisipan,
                    on_tim=_on_tim, on_padat=_padat,
                    # on_reasoning DITERUSKAN walau on_token tidak (lihat
                    # catatan di bawah): keduanya aliran yang berbeda.
                    # Jawaban toh tercetak utuh ke scrollback sesudahnya,
                    # sedangkan pikiran tak muncul di mana pun kecuali di
                    # sini — dan justru selama pikiran itulah model yang
                    # lambat memulai tampak seperti model yang mati.
                    on_reasoning=_on_pikir,
                    # on_token SENGAJA tak diteruskan: pratinjau kalimat yang
                    # sedang ditulis sudah dihapus dari layar, jadi tak ada lagi
                    # yang memakainya. Efek sampingnya justru menguntungkan —
                    # connector hanya boleh memulihkan diri dari tab yang mati
                    # selama BELUM ada teks yang mengalir keluar (kalau sudah,
                    # mengulang berarti mencetak jawaban dua kali). Tanpa
                    # aliran, pemulihan itu selalu aman.
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        # Mouse capture SENGAJA tak dipakai lagi: dulu ia ada untuk klik-buka
        # langkah di region live, tapi ia MENELAN event scroll wheel (terminal
        # tak bisa digulung) — dan kini keluaran tiap langkah sudah tercetak
        # statis ke scrollback, jadi tak ada lagi yang perlu diklik sama sekali.
        # Scroll & seleksi teks tetap 100% native.
        worker_thread = threading.Thread(target=worker, daemon=True)
        interrupted = False

        # --- penyunting baris mini untuk ketikan SELAMA giliran berjalan ---
        #
        # Di sini prompt_toolkit tak bisa dipakai: ia menguasai terminal, dan
        # terminalnya sedang dipegang rich.Live yang menggambar footer. Jadi
        # ketikan dibaca mentah lewat msvcrt, dan seluruh penyuntingan baris
        # harus disediakan sendiri. Sebelumnya yang ada cuma "tambah di ujung"
        # dan "hapus di ujung" — kursor tak bisa digeser sama sekali, sehingga
        # salah ketik di tengah kalimat panjang berarti menghapus semuanya.
        #
        # Kodenya sengaja tetap kecil: hanya perpindahan yang benar-benar
        # dipakai orang saat membetulkan satu kalimat (huruf, kata, ujung).
        def _sinkron() -> None:
            """Kirim ketikan + posisi kursor ke kotak chat di footer —
            termasuk pratinjau gambar: path gambar yang diketik/di-drop di
            tengah giliran harus langsung memunculkan blok warnanya,
            sama seperti saat idle."""
            view.typing = typing_state["buf"]
            view.typing_pos = typing_state["pos"]
            _perbarui_pratinjau_gambar(typing_state["buf"])

        def _batas_kata(mundur: bool) -> int:
            """Posisi awal kata sebelumnya / awal kata berikutnya."""
            buf, p = typing_state["buf"], typing_state["pos"]
            if mundur:
                i = p
                while i > 0 and buf[i - 1].isspace():
                    i -= 1
                while i > 0 and not buf[i - 1].isspace():
                    i -= 1
                return i
            i = p
            while i < len(buf) and not buf[i].isspace():
                i += 1
            while i < len(buf) and buf[i].isspace():
                i += 1
            return i

        def _hapus_sebelum() -> None:
            p = typing_state["pos"]
            if p <= 0:
                return
            buf = typing_state["buf"]
            typing_state["buf"] = buf[:p - 1] + buf[p:]
            typing_state["pos"] = p - 1
            _sinkron()

        def _hapus_kata() -> None:
            awal = _batas_kata(True)
            p = typing_state["pos"]
            if awal >= p:
                return
            buf = typing_state["buf"]
            typing_state["buf"] = buf[:awal] + buf[p:]
            typing_state["pos"] = awal
            _sinkron()

        # Scancode msvcrt untuk tombol yang datang setelah prefix \x00/\xe0.
        _NAV = {
            "K": "kiri", "M": "kanan", "G": "awal", "O": "akhir",
            "S": "hapus_depan",
            "s": "kata_kiri", "t": "kata_kanan",     # Ctrl+kiri / Ctrl+kanan
            "\x93": "hapus_kata_depan",              # Ctrl+Delete
        }

        def _navigasi(kode: str) -> None:
            aksi = _NAV.get(kode)
            if aksi is None:
                return                                # tombol yang tak dipakai
            buf = typing_state["buf"]
            p = typing_state["pos"]
            if aksi == "kiri":
                typing_state["pos"] = max(0, p - 1)
            elif aksi == "kanan":
                typing_state["pos"] = min(len(buf), p + 1)
            elif aksi == "awal":
                typing_state["pos"] = 0
            elif aksi == "akhir":
                typing_state["pos"] = len(buf)
            elif aksi == "kata_kiri":
                typing_state["pos"] = _batas_kata(True)
            elif aksi == "kata_kanan":
                typing_state["pos"] = _batas_kata(False)
            elif aksi == "hapus_depan":
                if p < len(buf):
                    typing_state["buf"] = buf[:p] + buf[p + 1:]
            elif aksi == "hapus_kata_depan":
                akhir = _batas_kata(False)
                if akhir > p:
                    typing_state["buf"] = buf[:p] + buf[akhir:]
            _sinkron()

        def _ketik(ch: str) -> bool:
            """Tangani SATU karakter ketikan selama giliran berjalan.

            Enter = kirim pesannya (BUKAN membatalkan — Ctrl+C tetap
            satu-satunya jalan membatalkan). Pesan itu DISISIPKAN ke giliran
            yang sedang berjalan pada batas langkah berikutnya, dan AI sendiri
            yang memutuskan mana didahulukan; kalau gilirannya keburu selesai,
            ia jadi giliran berikutnya.

            PERINTAH (/model, /mode, …) diperlakukan lain: ia tak pernah
            disisipkan ke giliran — perintah ditujukan ke program, bukan ke AI —
            melainkan menunggu sampai giliran benar-benar selesai. Return True
            bila karakter sudah ditangani di sini (pemanggil tak perlu
            memprosesnya lagi)."""
            if ch in ("\r", "\n"):
                teks = typing_state["buf"].strip()
                typing_state["buf"] = ""
                typing_state["pos"] = 0
                view.typing = ""
                view.typing_pos = 0
                if teks:
                    with antre_lock:
                        prompt_queue.append(teks)
                    # Gema pesannya SEKARANG, sama persis seperti pesan yang
                    # dikirim dari kotak idle — pengguna cuma perlu tahu
                    # pesannya terkirim, bukan bagaimana ia disalurkan.
                    # (Gema kedua saat prompt ini benar-benar dikerjakan
                    # sengaja tak ada — lihat gelung utama.)
                    # Baris kosong DI ATAS gema: tanpa itu, gema menempel ke
                    # konten sebelumnya (jawaban/baris langkah terakhir).
                    _commit([_KOSONG, _gema_prompt(teks)])
                    # Perintah menunggu; tanpa keterangan ini ia tampak
                    # "terkirim tapi tak terjadi apa-apa" sampai giliran usai.
                    if _perintah(teks):
                        _commit([_oneline(_TM(
                            f"  [dim]dijalankan setelah {_esc(agent.model_spec.label)} "
                            f"selesai menjawab[/dim]"))])
                return True
            if ch in ("\x08", "\x7f"):             # Backspace / Ctrl+Backspace
                # _DATA_KATA_MSVCRT ditentukan lewat GetConsoleMode di atas:
                # ConPTY/VT -> '\x08' = Ctrl+Backspace, klasik -> '\x7f'.
                if ch == _DATA_KATA_MSVCRT:
                    _hapus_kata()
                else:
                    _hapus_sebelum()
                return True
            if ch in ("\x00", "\xe0"):             # prefix tombol khusus msvcrt
                # Tombol navigasi datang BERPASANGAN: prefix ini lalu scancode.
                # Dulu scancode-nya cuma dibuang, sehingga panah kiri/kanan,
                # Home/End, dan Delete tak berfungsi sama sekali selama giliran
                # berjalan — ketikan hanya bisa tumbuh & disusut dari ujung.
                if _msvcrt is None:
                    return True
                try:
                    kode = _msvcrt.getwch()
                except Exception:  # noqa: BLE001
                    return True
                _navigasi(kode)
                return True
            if ch == "\t":                          # Tab -> buka/tutup pikiran
                # Tab, karena ia satu-satunya tombol yang sudah terbaca
                # "buka yang tersembunyi" tanpa perlu satu kata pun di layar,
                # dan selama giliran berjalan ia memang tak dipakai apa-apa:
                # tak ada pelengkapan di sini, dan '\t' ada di bawah ' '
                # sehingga sampai sekarang ia jatuh ke cabang terakhir lalu
                # diabaikan begitu saja.
                view.toggle_pikir()
                return True
            if ch == "\x01":                        # Ctrl+A -> awal baris
                typing_state["pos"] = 0
                _sinkron()
                return True
            if ch == "\x05":                        # Ctrl+E -> akhir baris
                typing_state["pos"] = len(typing_state["buf"])
                _sinkron()
                return True
            if ch == "\x17":                        # Ctrl+W -> hapus satu kata
                _hapus_kata()
                return True
            if ch == "\x15":                        # Ctrl+U -> hapus ke awal baris
                p = typing_state["pos"]
                typing_state["buf"] = typing_state["buf"][p:]
                typing_state["pos"] = 0
                _sinkron()
                return True
            if ch == "\x0b":                        # Ctrl+K -> hapus ke akhir baris
                p = typing_state["pos"]
                typing_state["buf"] = typing_state["buf"][:p]
                _sinkron()
                return True
            if ch >= " ":                          # karakter tercetak
                p = typing_state["pos"]
                buf = typing_state["buf"]
                typing_state["buf"] = buf[:p] + ch + buf[p:]
                typing_state["pos"] = p + 1
                _sinkron()
                return True
            return False                            # kontrol lain (^R/^C dsb.)

        try:
            _ke_dasar_layar()
            with Live(view, console=console, refresh_per_second=6,
                      transient=False, vertical_overflow="visible") as live:
                _LIVE["live"] = live
                worker_thread.start()
                _last_size = console.size
                while worker_thread.is_alive():
                    try:
                        # Cetak konten tertampung (thread utama, di titik aman
                        # antar-frame) — worker TIDAK pernah mencetak sendiri.
                        _flush_konten()
                        if console.size != _last_size:
                            _last_size = console.size
                            # JANGAN stop/clear/start: clear() memadamkan
                            # SELURUH layar tiap kali ukuran terdeteksi berubah
                            # — itulah kilat "kedip-kedip" yang paling kentara.
                            # _retempel_live menempelkan region ke dasar lagi
                            # (terminal yang membesar meninggalkannya menggantung)
                            # lalu memicu refresh sekali; tanpa itu pun tick
                            # berikutnya (≤167ms) merapikan lebarnya.
                            # Live yang sedang di-STOP (menu ask_user tampil)
                            # TIDAK boleh di-refresh: refresh() mencetak
                            # Control() ke console dan akan merusak menu
                            # inquirer — cukup catat ukuran barunya saja.
                            if not _LIVE["paused"]:
                                _retempel_live(live)
                        if input_paused["on"]:
                            # ask_user sedang tampil -> JANGAN baca console;
                            # biarkan inquirer yang menerima seluruh ketikan.
                            worker_thread.join(timeout=0.1)
                        elif _msvcrt is not None:
                            if _msvcrt.kbhit():
                                # Kumpulkan seluruh karakter yang menunggu di buffer
                                # console. Tempelan menghasilkan rentetan panjang;
                                # jika dibaca satu per satu, baris-baru di dalamnya
                                # memecahnya jadi banyak pesan terpisah dan membengkakkan
                                # layar karena tak sempat diringkas.
                                rentetan = []
                                while _msvcrt.kbhit():
                                    rentetan.append(_msvcrt.getwch())
                                teks_mentah = "".join(rentetan)

                                # Karakter extended (panah, dll) datang berpasangan
                                # dengan \x00/\xe0 -> proses per karakter seperti biasa.
                                if len(teks_mentah) == 1 or "\x00" in teks_mentah or "\xe0" in teks_mentah:
                                    for ch in teks_mentah:
                                        if ch == "\x03":
                                            raise KeyboardInterrupt
                                        _ketik(ch)
                                else:
                                    # Tempelan: ganti baris-baru jadi spasi karena kotak
                                    # ini satu baris. Jika panjang, ringkas jadi penanda.
                                    simpanan = _tempelan.simpanan()
                                    kirim = teks_mentah.endswith(("\r", "\n"))
                                    # Tab ikut dijadikan spasi: kotak ini SATU baris,
                                    # dan tempelan kode berindentasi tab kalau tidak
                                    # akan menyisipkan tab mentah ke ketikan.
                                    bersih = (teks_mentah.replace("\r", " ")
                                              .replace("\n", " ")
                                              .replace("\t", " ").rstrip())
                                    if simpanan.perlu_diringkas(bersih):
                                        bersih = simpanan.simpan(bersih)
                                    if bersih:
                                        p = typing_state["pos"]
                                        buf = typing_state["buf"]
                                        typing_state["buf"] = buf[:p] + bersih + buf[p:]
                                        typing_state["pos"] = p + len(bersih)
                                        _sinkron()
                                    if kirim:
                                        _ketik("\r")
                            else:
                                time.sleep(0.03)
                        else:
                            worker_thread.join(timeout=0.1)
                    except KeyboardInterrupt:
                        if not interrupted:
                            interrupted = True
                            cancel_event.set()
                            view.cancelling = True
                        else:
                            break
                # Selesai: tandai & render sekali lagi supaya footer final tampil.
                # Jawaban TIDAK ditaruh di region live (bisa sangat panjang ->
                # bikin kedip & scroll rusak); dicetak ke riwayat setelah Live tutup.
                view.done = True
                live.refresh()
        except KeyboardInterrupt:
            interrupted = True
            cancel_event.set()
        finally:
            cbs_alive["on"] = False   # worker yatim tak boleh mencetak lagi
            _LIVE["live"] = None
            _LIVE["paused"] = False
        # Sisa konten yang tertampung sebelum Live tutup (baris terakhir dari
        # worker) dicetak di sini, saat Live sudah benar-benar berhenti.
        _flush_konten()
        # Suara ikut berhenti begitu gilirannya berhenti — apa pun sebabnya.
        # Kabar yang masih mengantre saat itu sudah BASI: mendengar rencana
        # langkah yang tak pernah jadi dikerjakan lebih membingungkan daripada
        # diam, dan pada pembatalan ia terdengar seperti Ctrl+C yang diabaikan.
        _suara.diam()
        # Giliran web yang dibatalkan: pastikan sesi browser tak tertinggal macet.
        if interrupted and agent.model_spec.is_web:
            _reset_web_hub_if_stuck(worker_thread)

        err = result["error"]
        ans = (result["answer"] or "").strip()
        if isinstance(err, (KeyboardInterrupt, llm.Cancelled)) or (
                interrupted and not ans and err is None):
            # Benar-benar terputus (tak ada jawaban yang sempat jadi).
            _tambah_konten([_TM("\n  [yellow]◼ dibatalkan[/yellow]\n")])
        # Cabang khusus rate-limit API DIHAPUS: batas pemakaian kini datang dari
        # SITUS AI web, dan itu sudah ditangani lebih baik di core sebagai
        # WebLimitError/WebBusyError — lengkap dengan kapan bisa dipakai lagi
        # dan ulang-otomatis. Sisanya jatuh ke cabang error umum di bawah.
        elif err is not None:
            _tambah_konten([_TM(
                f"\n  [red]✖ error:[/red] {_esc(str(err))}\n")])
        else:
            # Jawaban ditambahkan ke TUMPUKAN BAWAH (di luar region live, yang
            # menutup sesudah giliran) supaya ia ikut menempel ke dasar layar dan
            # ikut berpindah saat terminal diubah ukurannya — bukan ke scrollback
            # yang "menjauh" dari kotak. Yang SELESAI tepat saat Ctrl+C ditekan
            # tetap tampil: sudah tersimpan di memory, jangan dibuang.
            if ans:
                blok: list = []
                # Header bot cukup SEKALI per giliran — kalau narasi sudah
                # menampilkannya, jawaban akhir tak perlu header kedua.
                ada_header = not view._said
                blok.append(_KOSONG)
                if ada_header:
                    blok.append(_TM("  [bold #fc9018]🤖 bagas-ai[/]"))
                    blok.append(Padding(_md(ans), (0, 3, 0, 3)))
                else:
                    blok.append(Padding(_md(ans), (1, 3, 0, 3)))
                _tambah_konten(blok)
                # JAWABAN AKHIR ikut dibacakan. Dulu cuma narasi antar-langkah
                # yang bersuara, dan itu membuat fiturnya tampak rusak pada
                # giliran yang paling lazim: pertanyaan yang dijawab LANGSUNG
                # tanpa satu langkah tool pun tak pernah melewati jalur narasi,
                # jadi layar penuh jawaban tapi laptop diam sama sekali.
                # Tak dibacakan bila gilirannya DIBATALKAN: Ctrl+C berarti
                # "berhenti", dan laptop yang tetap membacakan jawabannya
                # sesudah itu terdengar seperti pembatalan yang diabaikan.
                # penuh=True: jawaban akhir dibacakan UTUH. Batas pendek yang
                # dipakai narasi ada supaya suara tak tertinggal saat langkah
                # datang beruntun — di sini tak ada langkah berikutnya yang
                # perlu dikejar, jadi memotongnya cuma membuat pesannya
                # terdengar separuh.
                bersuara = not interrupted and prefs.load().get("suara", True)
                if bersuara:
                    # GETARAN dulu, baru dibacakan. Kabar antar-langkah dan
                    # jawaban akhir sama-sama keluar sebagai kalimat, jadi dari
                    # jendela lain keduanya terdengar serupa dan pengguna tak
                    # tahu gilirannya sudah selesai tanpa menengok layar. Dua
                    # dengung pendek ini yang membedakannya — satu ketukan yang
                    # artinya cuma satu hal: sudah sampai kesimpulan.
                    _suara.getar()
                    _suara.ucap(ans, penuh=True)
                    _kabar_suara()
                # PENANDA TUGAS SELESAI — dering + jendela berkedip di taskbar.
                # Diletakkan di sini, bukan di tiap langkah: yang ditandai
                # adalah "tak ada lagi serah-terima perintah", yaitu saat
                # jawaban akhir sudah tampil.
                #
                # Bila jawabannya sedang DIBACAKAN, penandanya MENUNGGU sampai
                # bacaannya habis — deringan yang menimpa kalimat terakhir
                # justru menelan kabar yang sedang disampaikan.
                #
                # Tidak dibunyikan saat giliran dibatalkan: Ctrl+C berarti
                # "berhenti", dan merayakannya sebagai penyelesaian itu keliru.
                if not interrupted:
                    _tanda.selesai(
                        tunggu=(lambda: _suara.tunggu_diam())
                        if bersuara else None)
            # Ringkasan giliran SETELAH jawaban (urutan yang benar).
            stps = view.all_steps
            if stps:
                n_file = sum(1 for s in stps if s["name"] in _TOOL_UBAH_FILE)
                n_fail = sum(1 for s in stps if s["failed"])
                seg = [f"{len(stps)} langkah"]
                if n_file:
                    seg.append(f"{n_file} file")
                if n_fail:
                    seg.append(f"[#f0603c]{n_fail} gagal[/]")
                seg += [_fmt_elapsed(time.time() - view.start),
                        f"⚡ {_fmt(agent.tokens_last.total)} token"]
                _tambah_konten([Padding(_TM(
                    "[dim]" + " · ".join(seg) + "[/]"), (1, 3, 0, 3))])
        _reindex_if_edited()

    def _reindex_if_edited() -> None:
        """Bila giliran barusan menulis/menghapus file, segarkan PETA PROYEK &
        system prompt supaya pemahaman bagas-ai selalu sesuai kode terbaru."""
        if any(s.get("name") in _TOOL_UBAH_FILE for s in steps.values()):
            try:
                projectindex.invalidate()   # jangan pakai memo basi pasca-edit
                agent.refresh_system_prompt()
            except Exception:  # noqa: BLE001
                pass

    def process_classic(text: str) -> None:
        nonlocal status_obj
        status_obj = Status(agent, total=_total_global)
        header = {"shown": False}
        # Nomor langkah & hasil di-reset tiap giliran -> nomor tetap kecil (1..k)
        # dan berkas hasil-penuhnya pun ikut ditimpa per giliran.
        steps.clear()
        step_ctr["n"] = 0
        cur_step.clear()
        turn_start = time.time()

        def say(content: str, akhir: bool = False) -> None:
            """Tampilkan ucapan/narasi bagas-ai: 1 header per giliran, indentasi rapi.

            `akhir=True` menandai JAWABAN AKHIR, yang dibacakan utuh — bukan
            dipotong sependek narasi antar-langkah."""
            if not content or not content.strip():
                return
            baru_bicara = not header["shown"]
            if baru_bicara:
                console.print()
                console.print("  [bold #fc9018]🤖 bagas-ai[/]")
                header["shown"] = True
            console.print(Padding(_md(content.strip()),
                                  (0 if baru_bicara else 1, 3, 0, 3)))
            # Mode klasik punya jalur cetaknya sendiri, jadi ia harus
            # menyuarakan sendiri juga — kalau tidak, suara cuma bekerja di
            # satu mode tampilan dan pengguna mode lain mengira fiturnya rusak.
            if prefs.load().get("suara", True):
                _suara.ucap(content, penuh=akhir)
                _kabar_suara()

        def _on_pikir_klasik(piece: str) -> None:
            """Aliran pikiran -> penghitung di bawah baris status. Mode klasik
            tak punya gelung pembaca tombol, jadi bloknya tak bisa dibuka di
            sini (lihat Status.note_pikir & _blok_pikir)."""
            status_obj.note_pikir(piece)

        def on_retry(attempt: int, wait: float, exc: Exception) -> None:
            """Dipertahankan demi kecocokan; jalur web tak memakai on_retry —
            penantian saat server penuh ditangani di dalam core (WebBusyError
            -> tunggu lalu ulangi) sehingga tak pernah sampai ke sini."""
            status_obj.note_retry(wait, f"percobaan ke-{attempt}")

        # Jalankan jawaban AI di THREAD LATAR BELAKANG supaya thread utama bebas
        # menangkap Ctrl+C secara responsif. Ctrl+C pertama -> minta batal secara
        # halus (cancel_event); Ctrl+C kedua -> tinggalkan worker (daemon) & kembali
        # ke prompt tanpa menunggu.
        cancel_event = threading.Event()
        result: dict = {"answer": None, "error": None}

        def worker() -> None:
            try:
                result["answer"] = agent.run(
                    text, on_tool=on_tool, on_message=say,
                    on_retry=on_retry, cancel_event=cancel_event,
                    on_tool_result=finish_step,
                    on_status=lambda m: status_obj.note_phase(_fase_status(m)),
                    on_padat=_jeda_padat(status_obj),
                    on_reasoning=_on_pikir_klasik,
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        worker_thread = threading.Thread(target=worker, daemon=True)
        interrupted = False
        forced = False
        try:
            _ke_dasar_layar()
            with Live(status_obj, console=console, refresh_per_second=6,
                      transient=True) as live:
                _LIVE["live"] = live
                worker_thread.start()
                _last_size = console.size
                while worker_thread.is_alive():
                    try:
                        # Konten tertampung (mis. gema suara) dicetak dari thread
                        # utama, bukan dari worker yang bisa menabrak refresh Live.
                        _flush_konten()
                        if console.size != _last_size:
                            _last_size = console.size
                            # Sama seperti mode mengalir: tanpa stop/clear/start
                            # — _retempel_live cukup (menempel ke dasar lagi
                            # lalu rich merapikan region sendiri).
                            # Tapi jangan saat Live di-stop (menu ask_user):
                            # refresh() akan mencetak Control() ke console.
                            if not _LIVE["paused"]:
                                _retempel_live(live)
                        worker_thread.join(timeout=0.1)
                    except KeyboardInterrupt:
                        if not interrupted:
                            interrupted = True
                            cancel_event.set()
                            status_obj.note_cancelling()
                        else:
                            # Ctrl+C kedua: jangan tunggu lagi, tinggalkan worker.
                            forced = True
                            break
        except KeyboardInterrupt:
            # Ctrl+C di jendela sempit di luar join() (mis. saat Live start /
            # thread mulai): perlakukan sebagai pembatalan, jangan sampai lolos
            # & menjatuhkan REPL.
            interrupted = True
            cancel_event.set()
        finally:
            _LIVE["live"] = None
            _LIVE["paused"] = False
        # Sisa konten tertampung (mis. gema suara) dicetak saat Live sudah tutup.
        _flush_konten()
        # Giliran web yang dibatalkan: pastikan sesi browser tak tertinggal macet.
        if (interrupted or forced) and agent.model_spec.is_web:
            _reset_web_hub_if_stuck(worker_thread)

        err = result["error"]
        if forced or interrupted or isinstance(err, (KeyboardInterrupt, llm.Cancelled)):
            # Memory sudah dirapikan & disimpan di dalam agent.run().
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        # Cabang khusus rate-limit API DIHAPUS: batas pemakaian kini datang dari
        # SITUS AI web, dan itu sudah ditangani lebih baik di core sebagai
        # WebLimitError/WebBusyError — lengkap dengan kapan bisa dipakai lagi
        # dan ulang-otomatis. Sisanya jatuh ke cabang error umum di bawah.
        elif err is not None:
            console.print(f"\n  [red]✖ error:[/red] {err}\n")
        else:
            # Mode klasik punya jalur penutupnya sendiri; getarannya dipasang di
            # sini juga supaya penanda "sudah sampai kesimpulan" tak cuma ada di
            # satu mode tampilan.
            if (result["answer"] or "").strip() \
                    and prefs.load().get("suara", True):
                _suara.getar()
            # penuh=True: jawaban AKHIR dibacakan utuh. Tanpa ini mode klasik
            # memakai batas narasi (160 huruf) sementara mode mengalir memakai
            # 900 — TERUKUR pada jawaban 1260 huruf: 125 vs 881. Itulah "kok
            # suaranya baca setengah-setengah" yang dulu cuma diperbaiki di
            # satu mode tampilan.
            say(result["answer"], akhir=True)
            # PENANDA TUGAS SELESAI — dipasang di sini JUGA. Dilaporkan
            # pengguna: "selesai tapi sound-nya nggak bunyi". Sebabnya
            # penandanya cuma ada di jalur mode mengalir, sementara mode klasik
            # (juga jalur cadangan saat mode mengalir gagal) punya penutupnya
            # sendiri — persis pola yang sudah pernah terjadi pada getaran &
            # panjang bacaan, dan terulang lagi di sini.
            if (result["answer"] or "").strip():
                _tanda.selesai(
                    tunggu=(lambda: _suara.tunggu_diam())
                    if prefs.load().get("suara", True) else None)
            _turn_footer(turn_start)
        _reindex_if_edited()

    def _turn_footer(turn_start: float) -> None:
        """Ringkasan giliran: langkah, file disentuh, waktu, token — hanya bila
        ada kerja tool (chat biasa tetap bersih tanpa footer)."""
        if not steps:
            return
        n_step = len(steps)
        n_file = sum(1 for s in steps.values()
                     if s.get("name") in _TOOL_UBAH_FILE)
        n_fail = sum(1 for s in steps.values() if s.get("failed"))
        el = _fmt_elapsed(time.time() - turn_start)
        tok = _fmt(agent.tokens_last.total)
        parts = [f"{n_step} langkah"]
        if n_file:
            parts.append(f"{n_file} file")
        if n_fail:
            parts.append(f"[#f0603c]{n_fail} gagal[/]")
        parts.append(el)
        parts.append(f"⚡ {tok} token")
        body = " [dim]·[/] ".join(parts)
        console.print(Padding(_TM(f"[dim]{body}[/dim]"),
                              (1, 3, 0, 3)))

    # --- aksi menu (inquirer) ---
    def _warn_glm_vpn() -> None:
        """Peringatan VPN bila model yang dipilih adalah GLM (chat.z.ai)."""
        if agent.model_spec.connector != "glm":
            return
        console.print(
            "  [bold {tema.p('aksen2')}]⚠ GLM (chat.z.ai) memerlukan VPN aktif[/] "
            "[dim]— disarankan [bold]Cloudflare WARP[/].[/]\n"
            "  [dim]Model ini sering bermasalah; jika error berulang "
            "di terminal, coba ganti server VPN.[/]\n")

    def pick_model() -> str | None:
        """Menu pilih model. Return ID model SEBELUMNYA bila yang dipilih adalah
        connector web (pemanggil lalu menjalankan _connect_web), selain itu None."""
        def _describe(spec) -> str:
            # Satu baris: nama (rata) + badge JALUR + SARAN "cocok untuk apa".
            # Lencananya menandai JALUR, bukan kemampuan: 🌐 = lewat browser
            # (butuh login sekali, jendela browser hidup), 🤖 = lewat API
            # (butuh API key penyedia, tanpa browser). Itulah beda yang paling
            # terasa saat memilih — bukan reasoning/multimodal.
            badge = " 🌐" if spec.is_web else " 🤖"
            if spec.ditunda:
                return f"{spec.label:<28}{badge}  —  ⏸ ditunda sementara"
            note = f"  —  {spec.note}" if spec.note else ""
            # Dikatakan DI DAFTAR, bukan hanya saat dipilih lalu ditolak: entri
            # yang terlihat sama seperti yang lain padahal pasti gagal membuat
            # pengguna menabraknya dulu untuk tahu.
            if spec.is_api and not config.has_api_key(spec.provider):
                note += f"  (butuh {config.api_key_env(spec.provider)})"
            return f"{spec.label:<28}{badge}{note}"

        # Model yang ditunda TETAP TAMPIL, tapi redup & dilewati kursor. Kalau
        # dibuang dari daftar, pengguna mengira connector-nya sudah dihapus —
        # padahal utuh dan tinggal dinyalakan lagi.
        choices = [
            Choice(key, _describe(spec), nonaktif=spec.ditunda)
            for _, key, spec in models.catalog()
        ]
        try:
            sel = inquirer.select(
                message="Pilih model (tiap model ada sarannya)",
                choices=choices, pointer="❯",
                default=next((k for _, k, s in models.catalog()
                              if s.id == agent.model and s.aktif), None),
            ).execute()
            prev = agent.model
            try:
                label = agent.set_model(sel)
            except ValueError as exc:
                # models._pastikan_aktif menolak model yang ditunda & model (API)
                # tanpa key. Alasannya sudah lengkap di pesan galatnya, jadi
                # cukup ditampilkan — model yang aktif tak berubah.
                console.print(f"  [yellow]⚠ {_esc(str(exc))}[/yellow]\n")
                return None
            console.print(f"[green]✓ Model: {label}[/green] "
                          f"[dim]({agent.model})[/dim]")
            _warn_glm_vpn()
            if agent.model_spec.is_web:
                return prev
        except (KeyboardInterrupt, EOFError):
            pass
        return None

    def pick_effort() -> None:
        """/effort — satu perintah, DUA mekanisme yang sama sekali berbeda.

        Model (web): mode berpikir adalah TOMBOL di halaman situsnya, jadi yang
        dilakukan program adalah MENGKLIKNYA di browser (pick_web_option).
        Model (API): mode berpikir adalah PARAMETER yang ikut tiap permintaan
        (`extra_body.chat_template_kwargs`), jadi cukup disimpan di sini
        (pick_api_effort) tanpa menyentuh jaringan sedikit pun.

        Karena itu keduanya tak bisa dilebur: yang satu butuh browser hidup &
        bisa gagal, yang satu tak pernah gagal."""
        if agent.model_spec.is_web:
            pick_web_option()
        else:
            pick_api_effort()

    def pick_api_effort() -> None:
        """/effort untuk model API: pilih tingkat berpikir yang dikirim sebagai
        parameter permintaan.

        Model tanpa tingkat berpikir DIKATAKAN TERUS TERANG, tidak diberi menu
        palsu berisi pilihan yang tak berpengaruh — endpoint NVIDIA
        MENERIMA parameter yang tak didukung tanpa keluhan (TERUKUR), jadi menu
        yang terlihat bekerja padahal tidak justru menyesatkan."""
        spec = agent.model_spec
        if not spec.effort_levels:
            catatan = spec.effort_catatan or (
                "model ini tak punya saklar mode berpikir yang bisa diatur.")
            console.print(f"  [dim]{_esc(spec.label)}: {_esc(catatan)}[/dim]\n")
            return

        aktif = agent.effort or spec.effort_default
        width = max(
            (len(models.EFFORT_INFO.get(lv, (lv,))[0]) for lv in spec.effort_levels),
            default=0)
        choices = []
        for lv in spec.effort_levels:
            label, desc, ikon = models.EFFORT_INFO.get(lv, (lv, "", "◇"))
            tanda = "  (aktif)" if lv == aktif else ""
            # inquirer TIDAK memproses markup rich — tulis polos.
            choices.append(Choice(
                lv, f"{ikon} {label:<{width}}  —  {desc}{tanda}"))
        try:
            sel = inquirer.select(
                message=f"Mode berpikir {spec.label}",
                choices=choices, pointer="❯",
                long_instruction=(
                    "Dikirim sebagai parameter tiap permintaan — berlaku "
                    "mulai pesan berikutnya."),
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return
        try:
            hasil = agent.set_effort(sel)
        except ValueError as exc:
            console.print(f"  [yellow]⚠ {_esc(str(exc))}[/yellow]\n")
            return
        if not hasil:
            return
        label = models.EFFORT_INFO.get(hasil, (hasil,))[0]
        console.print(f"  [#9fc93c]✓ Mode berpikir: {_esc(label)}[/]")
        if spec.effort_catatan:
            console.print(f"  [dim]{_esc(spec.effort_catatan)}[/dim]")
        console.print()

    def pick_browser() -> None:
        """Ganti browser yang dipakai connector (CONNECTOR_BROWSER_CHANNEL)."""
        from ..setup_wizard import _read_env, _write_env
        pilihan = [
            ("brave", "Brave"),
            ("chrome", "Chrome"),
            ("msedge", "Microsoft Edge"),
            ("chrome-beta", "Chrome Beta"),
        ]
        aktif = (config.CONNECTOR_BROWSER_CHANNEL or "").strip().lower()
        items = [f"{nama}{' (aktif)' if key == aktif else ''}"
                 for key, nama in pilihan]
        try:
            sel = inquirer.select(message="Pilih browser:", choices=items).execute()
        except (KeyboardInterrupt, EOFError):
            return
        # Pengguna memilih nama, cari key-nya. startswith tak cukup: "Chrome"
        # akan cocok lebih dulu dengan "Chrome Beta". Cocokkan persis atau
        # ikuti suffix " (aktif)".
        for key, nama in pilihan:
            if sel == nama or sel.startswith(nama + " ("):
                if key == aktif:
                    return
                env_path = config.ENV_FILE
                data = _read_env(env_path)
                data["CONNECTOR_BROWSER_CHANNEL"] = key
                _write_env(env_path, data)
                config.CONNECTOR_BROWSER_CHANNEL = key
                console.print(f"[green]✓ Browser: {nama}[/green]\n"
                              "[dim]Mulai ulang bagas-ai untuk menerapkan.[/]")
                return

    def pick_web_option() -> None:
        """/effort untuk model web: pilih tombol di UI situs (varian model /
        mode berpikir) lalu program yang mengekliknya di browser."""
        spec = agent.model_spec
        try:
            from .. import connectors
            conn = connectors.get_connector(spec.connector)
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]connector tak siap: {_esc(str(exc))}[/red]")
            return
        opts = conn.web_options()
        if not opts:
            console.print(
                f"  [dim]{_esc(spec.label)} tak punya tombol model/berpikir yang "
                "bisa diatur dari sini.[/dim]")
            return
        # inquirer TIDAK memproses markup rich — tulis polos, kalau tidak tag
        # seperti [dim] ikut tampil mentah di layar.
        width = max((len(t) for t, _ in opts), default=0)
        choices = [Choice(text, f"{text:<{width}}  —  {desc}")
                   for text, desc in opts]
        try:
            sel = inquirer.select(
                message=f"Tombol {spec.label} (diklik di UI web)",
                choices=choices, pointer="❯",
                long_instruction="Program akan mengklik tombol ini langsung di situsnya.",
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return

        state = {"msg": f"mengklik '{sel}' di {spec.label}…"}
        result: dict = {"ok": None, "error": None}

        def worker() -> None:
            try:
                result["ok"] = conn.set_web_option(sel)
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        wt = threading.Thread(target=worker, daemon=True)
        FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        try:
            _ke_dasar_layar()
            with Live(_region_stack(_oneline(Text())), console=console,
                      refresh_per_second=10, transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    live.update(_region_stack(_oneline(_TM(
                        f"  [{tema.p('aksen')}]{frame}[/] [dim]{_esc(state['msg'])}[/]"))))
                    wt.join(timeout=0.1)
        except KeyboardInterrupt:
            # Sama seperti pembatalan giliran: bereskan di latar, dan HANYA
            # untuk profil layanan ini (lihat _reset_web_hub_if_stuck).
            _reset_web_hub_if_stuck(wt)
            console.print("  [yellow]◼ dibatalkan[/yellow]\n")
            return

        if result["error"] is not None:
            console.print(f"  [yellow]⚠ {_esc(str(result['error']))}[/yellow]\n")
        else:
            console.print(f"  [#9fc93c]✓ {_esc(str(result['ok']))}[/]\n")

    _MESIN_JELAS = {
        "edge": ("suara Indonesia natural (daring)",
                 "Ardi/Gadis — suara neural, pelafalan Indonesia"),
        "sapi": ("suara bawaan Windows (luring)",
                 "System.Speech — selalu ada, tak butuh internet"),
        "say": ("suara bawaan macOS (luring)", "perintah `say`"),
        "espeak": ("espeak (luring)", "spd-say / espeak-ng"),
    }

    def do_compact() -> None:
        """/compact — simpan riwayat percakapan ke berkas memory JSON.

        Seluruhnya dikerjakan di laptop: tak ada pesan yang dikirim ke situs,
        model tak dimintai ringkasan, dan chat yang sedang berjalan TIDAK
        diapa-apakan. Berkasnya bekal untuk percakapan berikutnya — dipasang
        dengan /send-compact sesudah /new.

        Bar kemajuannya SAMA PERSIS dengan yang muncul saat pemadatan otomatis
        — dua jalan yang mengerjakan hal yang sama tak boleh terlihat berbeda."""
        class _Tampil:
            """Penampung sekadarnya: _jeda_padat cuma butuh `.note_padat` &
            `.padat`, jadi tak perlu menyeret seluruh mesin TurnView ke sini."""
            padat = None

            def note_padat(self, frac: float, ket: str) -> None:
                mulai = self.padat[2] if self.padat else time.time()
                self.padat = (frac, ket, mulai)

        tampil = _Tampil()
        lapor = _jeda_padat(tampil)
        mulai = time.time()
        hasil: dict = {"teks": ""}

        def kerja() -> None:
            hasil["teks"] = agent.padatkan_sekarang(on_padat=lapor)

        wt = threading.Thread(target=kerja, daemon=True)
        _ke_dasar_layar()
        with Live(_region_stack(_KOSONG), console=console, refresh_per_second=12,
                  transient=True) as live:
            wt.start()
            while wt.is_alive():
                p = tampil.padat
                live.update(_region_stack(
                    _baris_padat(*p, time.time() - mulai) if p
                    else Text("  ⏸ menyiapkan…", style="dim")))
                wt.join(timeout=0.05)
        # Kalimat penutup menyesuaikan JALUR. "chat di situs tak disentuh"
        # cuma bermakna di jalur (web), tempat percakapan hidup di server
        # orang lain. Di jalur (API) riwayatnya kita sendiri yang pegang,
        # jadi yang perlu ditegaskan justru bahwa menyimpan tak memotong apa pun.
        sisa = ("chat di situs tak disentuh." if agent.model_spec.is_web
                else "percakapan ini jalan terus.")
        console.print(f"  [#9fc93c]✓ riwayat tersimpan[/] [dim]— {sisa}[/]\n")
        console.print(Padding(_md(hasil["teks"]), (0, 3, 1, 3)))

    def do_send_compact(arg: str = "") -> None:
        """/send-compact — unggah berkas memory ke percakapan web sekarang.

        Pasangan /compact: yang itu menyimpan, yang ini memasang. Dipakai
        sesudah /new, sehingga chat yang bersih langsung tahu sudah sampai mana
        pekerjaannya — tanpa mengetik ulang puluhan ribu karakter."""
        if not agent.model_spec.is_web:
            console.print("  [yellow]/send-compact hanya untuk model web."
                          "[/yellow]\n")
            return
        state = {"pesan": "menyiapkan berkas ingatan…"}

        def render():
            return _TM(
                f"  [{tema.p('aksen_terang')}]◐[/] [dim]{_esc(state['pesan'])}[/]")

        hasil = {"teks": "", "galat": None}

        def kerja() -> None:
            try:
                hasil["teks"] = agent.kirim_memory(
                    arg.strip().strip('"').strip("'") or None,
                    on_status=lambda m: state.__setitem__("pesan", m),
                    on_notice=lambda m: state.__setitem__("pesan", m))
            except BaseException as exc:  # noqa: BLE001
                hasil["galat"] = exc

        wt = threading.Thread(target=kerja, daemon=True)
        try:
            _ke_dasar_layar()
            with Live(_region_stack(render()), console=console, refresh_per_second=8,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    live.update(_region_stack(render()))
                    wt.join(timeout=0.1)
        except KeyboardInterrupt:
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
            return
        if hasil["galat"] is not None:
            console.print(f"\n  [red]✖ gagal mengirim ingatan:[/red] "
                          f"{hasil['galat']}\n")
            return
        console.print("  [#9fc93c]✓ ingatan terpasang[/] [dim]— lanjutkan "
                      "seperti biasa.[/]\n")
        teks = (hasil["teks"] or "").strip()
        if teks:
            console.print(Padding(_md(teks), (0, 3, 1, 3)))

    def _kabar_suara() -> None:
        """Umumkan SEKALI kalau suaranya bermasalah / berpindah mesin.

        Tanpa ini alasannya cuma terlihat kalau pengguna kebetulan mengetik
        /mic — padahal yang ia rasakan justru saat itu juga: suaranya berubah,
        atau tiba-tiba tak ada bunyi sama sekali. Kesunyian yang tak dijelaskan
        mustahil dibedakan dari fitur yang rusak."""
        try:
            pesan = _suara.catatan_baru()
        except Exception:  # noqa: BLE001 - notifikasi tak boleh menjatuhkan giliran
            return
        if pesan:
            console.print(f"  [dim]♪ {_esc(pesan)}[/dim]")

    # --- /voice: mikrofon jadi cara memberi perintah ------------------------
    def _voice_masuk(teks: str) -> None:
        """Satu perintah utuh dari mikrofon. Dipanggil dari THREAD PENDENGAR.

        Diperlakukan persis seperti kalimat yang diketik — termasuk gemanya —
        supaya tak ada jalur kedua yang perlu dipelihara. Bedanya cuma satu
        penanda 🎙 supaya pengguna bisa membedakan mana yang ia ucapkan."""
        teks = (teks or "").strip()
        if not teks:
            return
        # Kotak idle sedang menunggu -> teksnya dimasukkan ke sana (kotak itu
        # memblokir thread utama; antrean saja takkan pernah terbaca). Kalau
        # giliran sedang berjalan, ia masuk antrean seperti ketikan biasa dan
        # disisipkan di batas langkah berikutnya.
        kotak_chat.galat_kirim = ""
        # GEMANYA TIDAK DICETAK DI SINI untuk jalur kotak idle. Kotak itu
        # mengembalikan teksnya ke gelung utama, yang memang sudah menggemakan
        # setiap pesan — jadi mencetaknya lebih dulu berarti kalimat yang sama
        # muncul DUA KALI berturut-turut, sekali ber-🎙 dan sekali polos.
        # Dilaporkan pengguna lewat tangkapan layar.
        #
        # Yang dikirim ke sana cuma penandanya: gelung utama memakainya untuk
        # memberi 🎙 pada gemanya sendiri. Dipasang SEBELUM kirim, sebab
        # kotaknya bisa menjawab lebih dulu daripada baris berikutnya di sini.
        voice_state["terucap"] = teks
        if kotak_chat.kirim_dari_luar(teks):
            return
        voice_state.pop("terucap", None)
        # Jalur antrean tak punya gema lain — di sini ia satu-satunya.
        _tambah_konten([Text("\n"), _gema_prompt(teks, prefix="🎙 ")])
        with antre_lock:
            prompt_queue.append(teks)
        # DUA keadaan yang sangat berbeda, dan dulu keduanya diberi kalimat yang
        # sama — padahal yang kedua berarti perintahnya MENGENDAP sampai
        # pengguna menekan Enter, hal yang justru tak ia lakukan saat bicara.
        if kotak_chat.galat_kirim:
            _tambah_konten([_TM(
                f"  [yellow]⚠ perintah suara tak bisa masuk ke kotak "
                f"({_esc(kotak_chat.galat_kirim)}).[/yellow]\n"
                "  [dim]Ia menunggu di antrean — tekan Enter untuk "
                "menjalankannya.[/dim]")])
        else:
            _tambah_konten([_TM(
                "  [dim]— disisipkan ke giliran yang sedang "
                "berjalan[/dim]")])

    def _voice_kabar(pesan: str, batal: bool = False) -> None:
        """Kabar dari pendengar. HANYA pembatalan yang sampai ke layar.

        Sisanya (pengenalan gagal, mikrofon berhenti) dulu ikut dicetak, dan
        di pemakaian sehari-hari barisan itu cuma menumpuk di atas kotak chat
        tanpa mengubah apa pun yang bisa dikerjakan pengguna — mubazir, kata
        yang punya layarnya. Pembatalan lain ceritanya: ia berarti kalimat yang
        baru saja diucapkan TIDAK jadi dikirim, dan itu wajib diketahui.

        Mikrofon yang mati tetap terbaca tanpa baris ini — penanda 🎙 di bar
        status ikut hilang begitu pendengarnya berhenti, dan /voice tetap
        menyebutkan alasannya."""
        if not batal:
            return
        console.print(f"  [dim]🎙 {_esc(pesan)}[/dim]")

    # TRANSKRIP TIAP UCAPAN TIDAK DITAMPILKAN. Ia sempat ada dan berguna saat
    # menelusuri kenapa perintah tak tertangkap, tapi dalam pemakaian
    # sehari-hari ia membanjiri layar di atas kotak chat dengan potongan
    # obrolan ruangan — itu keluaran debug, bukan bagian dari alat kerja.
    #
    # Yang tetap tampil dua hal saja: perintah yang JADI (gemanya sama persis
    # dengan ketikan) dan kabar yang bisa ditindaklanjuti (mis. kata penutup
    # terucap padahal namaku belum disebut). Jalurnya sendiri tetap ada di
    # dengar.Pendengar.on_dengar untuk penelusuran.

    def show_voice(arg: str = "") -> None:
        """/voice — mikrofon: sebut "bagas ai …" lalu diam sejenak.

        Sengaja MATI secara bawaan, dan tak disimpan ke preferensi: mikrofon
        yang menyala sendiri di sesi berikutnya adalah kejutan yang tak seorang
        pun minta."""
        pilihan = arg.strip().lower()
        pendengar = voice_state.get("pendengar")

        if pilihan in ("off", "mati"):
            if pendengar is not None:
                pendengar.berhenti()
                voice_state["pendengar"] = None
            console.print("  [#f7d488]○ mikrofon MATI[/]\n")
            return

        if pilihan in ("on", "hidup"):
            if pendengar is not None and pendengar.aktif:
                console.print("  [dim]mikrofon sudah menyala.[/dim]\n")
                return
            p = _dengar.Pendengar(_voice_masuk, _voice_kabar,
                                  jangkauan=voice_state.get("jangkauan"))
            alasan = p.mulai()
            if alasan:
                console.print(f"  [yellow]⚠ mikrofon tak bisa dinyalakan:[/]\n"
                              f"  [dim]{_esc(alasan)}[/dim]\n")
                return
            voice_state["pendengar"] = p
            # Nada NAIK. Di thread sendiri: bunyinya ±0,25 detik dan tak boleh
            # menahan keterangan di bawah ini.
            threading.Thread(target=_dengar.bunyi, args=(True,),
                             daemon=True).start()
            nama = _dengar.nama_mikrofon() or "mikrofon bawaan"
            # Kalibrasi derau berjalan di thread perekam & butuh ±0,7 detik;
            # angkanya ditampilkan begitu siap supaya "kenapa tak dengar" bisa
            # dijawab tanpa menebak.
            def _lapor_ambang() -> None:
                for _ in range(30):
                    if p.ambang:
                        console.print(
                            f"  [dim]🎙 jangkauan {p.jangkauan} · derau "
                            f"{p.derau:.0f} → ambang {p.ambang:.0f}"
                            f" (lanjut {p.ambang_lanjut:.0f})[/dim]")
                        return
                    time.sleep(0.1)

            threading.Thread(target=_lapor_ambang, daemon=True).start()
            console.print(
                f"  [#9fc93c]● mikrofon AKTIF[/] [dim]— {_esc(nama)}[/]\n"
                "  [dim]sebut[/] [{tema.p('aksen')}]\"bagas ai\"[/] [dim]lalu ucapkan "
                "perintahmu; berhenti bicara[/] "
                f"[{tema.p('aksen')}]{_dengar.JEDA_SELESAI:.0f} detik[/] [dim]sudah "
                "menutupnya — tak ada kata penutup. Contoh:[/]\n"
                "  [dim]  \"bagas ai tolong buka main.py\"  → (diam) → "
                "terkirim[/]\n"
                "  [dim]begitu namaku terdengar aku MENJAWAB "
                f"(\"{_dengar.sapaan()}\"), dan bar "
                "status di bawah berubah jadi[/] [{tema.p('aksen')}]● merekam[/][dim]. "
                "Meneruskan kalimat sebelum jeda itu habis mengulang "
                "hitungannya. Ucapkan[/] [{tema.p('aksen')}]\"batalkan\"[/] [dim]untuk "
                "membuang rekaman yang sedang berjalan. Satu perintah maksimal "
                f"{_dengar.MAKS_REKAM:.0f} detik.[/dim]\n")
            return

        if pilihan in _dengar.JANGKAUAN:
            # Jaraknya beda tiap ruangan & tiap posisi duduk, jadi sakelarnya
            # harus bisa diputar SEKARANG — bukan lewat menyunting .env lalu
            # menjalankan ulang seluruh sesi. Yang di .env tetap jadi bawaan
            # sesi berikutnya (VOICE_JANGKAUAN).
            voice_state["jangkauan"] = pilihan
            j = _dengar.profil(pilihan)
            console.print(f"  [#9fc93c]● jangkauan mikrofon: "
                          f"{pilihan.upper()}[/] [dim]— ambang "
                          f"{j.kali_derau:g}× derau ruangan[/]")
            if pendengar is not None and pendengar.aktif:
                # Ambangnya dihitung sekali saat kalibrasi, jadi perubahan
                # baru berlaku sesudah mikrofonnya dinyalakan ulang. Dikerjakan
                # di sini, bukan disuruh ke pengguna: setelan yang tampak
                # berubah tapi belum bekerja adalah kebohongan kecil yang
                # mahal untuk ditelusuri.
                pendengar.berhenti()
                voice_state["pendengar"] = None
                console.print("  [dim]mikrofon dinyalakan ulang agar ambangnya "
                              "dihitung ulang…[/dim]")
                show_voice("on")
            else:
                console.print("  [dim]berlaku saat[/] [#fcc048]/voice on[/]"
                              "[dim]. Permanen: tulis[/] "
                              f"[{tema.p('aksen')}]VOICE_JANGKAUAN={pilihan}[/] "
                              "[dim]di ~/.bagasai/.env[/]\n")
            return

        if pilihan in ("jangkau", "jarak", "jangkauan"):
            ok, alasan = _dengar.siap()
            if not ok:
                console.print(f"  [yellow]⚠ {_esc(alasan)}[/yellow]\n")
                return
            # SATU-SATUNYA jawaban jujur untuk "kejauhan nggak dari sini?".
            # Jarak, bentuk ruangan, dan mikrofonnya cuma ada di rumah
            # pengguna; ambang mana pun yang dipilih dari sini tetap tebakan
            # sampai diukur dari titik yang benar-benar dipakai.
            console.print(
                "\n  [bold {tema.p('aksen')}]🎙 Ukur jangkauan[/]\n"
                "  [dim]1. pergilah ke tempat kamu biasa memberi perintah "
                "(kasur / ruang tengah)[/]\n"
                "  [dim]2. DIAM dulu 1 detik — derau ruangan diukur[/]\n"
                "  [dim]3. lalu bicara 6 detik dengan suara sewajarnya, mis.[/]"
                " [{tema.p('aksen')}]\"bagas ai tolong buka main titik py\"[/]\n")
            console.print("  [dim]mulai… diam sebentar[/]")
            try:
                h = _dengar.ukur(6.0)
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]✖ gagal merekam:[/red] {exc}\n")
                return
            sampai = h["suara_p90"] > h["ambang"]
            console.print(
                f"\n  [dim]derau ruangan   :[/] {h['derau']:.0f}"
                f"  [dim](saat ramai {h['derau_ramai']:.0f})[/]\n"
                f"  [dim]suaramu dari sana:[/] {h['suara_p90']:.0f}"
                f"  [dim](puncak {h['puncak']:.0f})[/]\n"
                f"  [dim]ambang '{h['jangkauan']}' :[/] {h['ambang']:.0f}"
                f"  [dim](lanjut {h['ambang_lanjut']:.0f})[/]")
            if sampai:
                console.print(f"  [#9fc93c]✓ sampai[/] [dim]— suaramu "
                              f"{h['suara_p90'] / max(h['ambang'], 1):.1f}× di "
                              "atas ambang dari titik itu.[/]")
            elif h["saran"]:
                console.print(
                    f"  [yellow]✖ belum sampai[/] [dim]di jangkauan "
                    f"'{h['jangkauan']}'.[/] [#9fc93c]Pakai[/] "
                    f"[{tema.p('aksen')}]/voice {h['saran']}[/][dim] — di situ suaramu "
                    "lewat.[/]")
            else:
                # Bahkan profil paling longgar pun tak menangkapnya. Yang
                # tersisa di luar kendali bagas-ai: penguat mikrofon Windows,
                # dan "peredam bising" bawaan Realtek yang memang dirancang
                # MEMBUANG suara jauh.
                console.print(
                    "  [yellow]✖ tak terjangkau bahkan di setelan paling "
                    "longgar.[/]\n"
                    "  [dim]Sisanya di luar bagas-ai — dua yang paling sering "
                    "jadi sebabnya:[/]\n"
                    "  [dim]  · Pengaturan Suara → mikrofon → naikkan volume "
                    "& 'Microphone Boost'[/]\n"
                    "  [dim]  · matikan 'Audio enhancements' / peredam bising "
                    "Realtek — peredam itu memang dibuat MEMBUANG suara "
                    "jauh[/]\n"
                    "  [dim]  · atau pakai HP-mu sebagai mikrofon (AudioRelay "
                    "sudah terpasang di laptop ini) lalu taruh di ruangan "
                    "itu[/]")
            if h["teks"]:
                console.print(f"  [#9fc93c]✓ terdengar:[/] "
                              f"[{tema.p('teks')}]{_esc(h['teks'])}[/]\n")
            else:
                console.print("  [dim]tak ada kata yang dikenali dari rekaman "
                              "itu.[/dim]\n")
            return

        if pilihan in ("tes", "test", "coba"):
            ok, alasan = _dengar.siap()
            if not ok:
                console.print(f"  [yellow]⚠ {_esc(alasan)}[/yellow]\n")
                return
            # Sesudah merekam masih ada perjalanan ke pengenal suara (daring,
            # TERUKUR ±3 detik). Tanpa disebut, jeda itu tampak seperti macet.
            console.print("  [dim]🎙 merekam 5 detik — bicaralah sekarang, "
                          "lalu tunggu sebentar untuk pengenalannya…[/dim]")
            try:
                teks, puncak = _dengar.dengar_sekali(5.0)
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]✖ gagal merekam:[/red] {exc}\n")
                return
            # Dua kegagalan yang dari luar tampak sama (tak ada teks) dipisah
            # oleh angka ini: mikrofon bisu vs pengenalan yang tak paham.
            # Angkanya DIBANDINGKAN dengan ambangnya, bukan berdiri sendiri.
            # "Mikrofonnya tuli" kemarin lahir persis dari sini: ambang dipatok
            # 180 sementara mikrofon pengguna ber-gain rendah (derau 0,5), jadi
            # suaranya tak pernah lewat — dan tak ada satu angka pun di layar
            # yang bisa menunjukkannya.
            lantai = _dengar.profil(voice_state.get("jangkauan")).lantai
            console.print(
                f"  [dim]tingkat suara tertinggi: {puncak:.0f}   "
                f"ambang bicara: ≥{lantai:.0f} (mengikuti derau ruangan — "
                f"ukur dari tempat dudukmu dengan[/dim] [{tema.p('aksen')}]/voice "
                f"jangkau[/][dim])[/dim]"
                + ("\n  [yellow]mikrofonnya nyaris tak menangkap apa-apa[/]"
                   "[dim] — periksa mikrofon yang dipilih Windows, atau "
                   "naikkan volumenya di Pengaturan Suara.[/dim]"
                   if puncak < lantai else ""))
            if teks:
                console.print(f"  [#9fc93c]✓ terdengar:[/] "
                              f"[{tema.p('teks')}]{_esc(teks)}[/]\n")
            else:
                console.print("  [yellow]tak ada ucapan yang dikenali[/yellow]"
                              "[dim] — coba lebih dekat ke mikrofon, atau "
                              "periksa koneksi (pengenalannya daring).[/dim]\n")
            return

        aktif = pendengar is not None and pendengar.aktif
        ok, alasan = _dengar.siap()
        console.print()
        console.print("  [bold #fcc048]🎙 Perintah suara[/] "
                      f"[dim]— {'AKTIF' if aktif else 'MATI'}[/]")
        console.print(f"  [dim]mikrofon :[/] {_esc(_dengar.nama_mikrofon() or '-')}")
        jnama = voice_state.get("jangkauan") or _dengar._nama_jangkauan()
        console.print(
            f"  [dim]jangkauan:[/] [{tema.p('aksen')}]{jnama}[/]"
            + (f"  [dim](derau {pendengar.derau:.0f} → ambang "
               f"{pendengar.ambang:.0f})[/]" if aktif and pendengar.ambang
               else "  [dim](dekat / normal / jauh)[/]"))
        if not ok:
            console.print(f"  [yellow]⚠ {_esc(alasan)}[/yellow]")
        console.print("  [dim]cara pakai:[/] sebut [#fcc048]\"bagas ai\"[/] "
                      "lalu perintahnya, lalu [{tema.p('aksen')}]diam 2 detik[/] "
                      "[dim]· buang dengan[/] [{tema.p('aksen')}]\"batalkan\"[/]")
        console.print("  [dim]yang dikirim hanya yang terucap SESUDAH "
                      "namaku.[/dim]")
        console.print("  [dim]/voice on · off · tes ·[/] [#fcc048]jangkau[/]"
                      "[dim] (ukur dari tempat dudukmu) ·[/] "
                      "[{tema.p('aksen')}]dekat|normal|jauh[/]\n")

    def show_mic(arg: str = "") -> None:
        """/mic — kabar AI dibacakan pengeras suara; on/off/tes.

        Suara bisa mengganggu, jadi sakelarnya wajib gampang ditemukan. Dan
        kalau ia tak berbunyi, alasannya harus DIKATAKAN — kesunyian yang tak
        dijelaskan mustahil dibedakan dari fitur yang rusak."""
        pilihan = arg.strip().lower()
        if pilihan in ("off", "mati", "on", "hidup"):
            nyala = pilihan in ("on", "hidup")
            prefs.save(suara=nyala)
            if not nyala:
                _suara.diam()
            warna = "#9fc93c" if nyala else tema.p("aksen_terang")
            console.print(f"  [{warna}]✓ suara kabar {'AKTIF' if nyala else 'MATI'}"
                          f"[/]\n")
            return

        aktif = bool(prefs.load().get("suara", True))
        if pilihan in ("tes", "test", "coba"):
            if not aktif:
                console.print("  [#f7d488]suara sedang MATI[/] "
                              "[dim]— nyalakan dulu: /mic on[/dim]\n")
                return
            # DIPERIKSA dulu, bukan langsung mengaku berhasil. Dulu baris
            # "mengucapkan contoh…" tercetak apa pun keadaannya — termasuk saat
            # tak ada satu pun mesin yang memenuhi syarat bahasa, sehingga
            # sebuah tes yang PASTI gagal tetap dilaporkan seolah berjalan.
            if not _suara.mesin_tersedia():
                console.print(f"  [yellow]⚠ {_esc(_suara.alasan_diam())}"
                              "[/yellow]\n")
                return
            _suara.ucap("Halo, ini suara bagas a i. Kabar dari model akan "
                        "dibacakan seperti ini.")
            console.print("  [dim]♪ mengucapkan contoh…[/dim]")
            _kabar_suara()
            console.print()
            return

        mesin = _suara.mesin_tersedia()
        console.print()
        console.print("  [bold #fcc048]♪ Suara kabar[/] "
                      "[dim]— tiap kabar dari model dibacakan pengeras suara, "
                      "terdengar di jendela mana pun[/]")
        console.print(
            f"  [dim]status: [/]"
            f"[{'#9fc93c' if aktif else tema.p('aksen_terang')}]{'aktif' if aktif else 'mati'}[/]"
            f"  [dim]· /mic off · /mic tes[/dim]")
        console.print("  [dim]giliran yang sampai kesimpulan ditandai dua "
                      "dengung pendek — 'getaran' penanda selesai[/dim]")
        console.print("  [dim]hanya suara berbahasa Indonesia yang dipakai; "
                      "mesin tanpa suara Indonesia dilewati[/dim]\n")
        if not mesin:
            console.print(f"  [yellow]⚠ {_esc(_suara.alasan_diam())}[/yellow]\n")
            return
        # Urutannya PENTING ditampilkan: yang teratas dipakai, sisanya cadangan
        # kalau yang di atas gagal (mis. laptop sedang luring).
        for i, m in enumerate(mesin):
            judul, ket = _MESIN_JELAS.get(m, (m, ""))
            tanda = "[#9fc93c]▸[/]" if i == 0 else "[dim]·[/]"
            label = "dipakai" if i == 0 else "cadangan"
            console.print(f"    {tanda} [#fc9018]{judul}[/] "
                          f"[dim]({label}) — {_esc(ket)}[/dim]")
        # Kalau mesinnya sempat berganti (mis. laptop luring, atau menyiapkan
        # suara natural terlalu lambat di mesin pelan), SEBUTKAN sebabnya di
        # sini. Tanpa ini pengguna cuma mendengar suaranya berubah sendiri dan
        # tak punya satu pun cara untuk tahu kenapa.
        catatan = _suara.catatan()
        if catatan:
            console.print(f"\n  [dim]catatan terakhir: {_esc(catatan)}[/dim]")
        # KENAPA suara Windows tak ikut jadi cadangan. Ini pertanyaan yang pasti
        # muncul begitu daftarnya cuma berisi satu baris, dan jawabannya bukan
        # "rusak" melainkan pilihan yang disengaja.
        if sys.platform == "win32" and "sapi" not in mesin:
            terpasang = _suara.suara_tersedia()
            daftar = ", ".join(b.split("|")[0].strip() for b in terpasang)
            console.print(
                "\n  [dim]Suara bawaan Windows[/] [{tema.p('aksen_terang')}]tidak dipakai[/] "
                "[dim]— tak ada suara Indonesia terpasang"
                + (f" (yang ada: {_esc(daftar)})" if daftar else "")
                + ". Suara Inggris sengaja dilewati: melafalkan kalimat "
                "Indonesia dengannya sulit dimengerti, dan terdengar seperti "
                "suara asing yang muncul sendiri.[/dim]")
            console.print("  [dim]Mau cadangan luring? tambahkan suara "
                          "Indonesia lewat Settings › Time & language › "
                          "Speech.[/dim]")
        if "edge" not in mesin:
            console.print("\n  [dim]Untuk suara Indonesia yang natural: "
                          "[/][bold]pip install edge-tts[/]"
                          "[dim] (butuh internet saat dipakai).[/dim]")
        console.print()

    def show_tim(arg: str = "") -> None:
        """/tim — lihat 24 spesialis yang bekerja pasif; /tim off|on mematikan.

        Fitur pasif WAJIB punya sakelar yang terlihat: kalau ia mengubah hasil
        tanpa bisa dilihat maupun dimatikan, setiap keanehan jadi mustahil
        dilacak — pengguna tak punya cara memisahkan 'model yang begitu' dari
        'tim yang menyarankan begitu'."""
        from .. import prefs as _prefs
        from .. import tim as _tim

        pilihan = arg.strip().lower()
        if pilihan in ("off", "mati", "on", "hidup"):
            nyala = pilihan in ("on", "hidup")
            _prefs.save(tim=nyala)
            kata = "AKTIF" if nyala else "MATI"
            warna = "#9fc93c" if nyala else tema.p("aksen_terang")
            console.print(f"  [{warna}]✓ tim spesialis {kata}[/]\n")
            return

        aktif = bool(_prefs.load().get("tim", True))
        console.print()
        console.print(
            f"  [bold {tema.p('aksen')}]Tim {len(_tim.ANGGOTA)} spesialis[/] "
            f"[dim]— bekerja pasif, dibangunkan oleh isi pekerjaan[/]")
        console.print(
            f"  [dim]status: [/]"
            f"[{'#9fc93c' if aktif else tema.p('aksen_terang')}]{'aktif' if aktif else 'mati'}[/]"
            f"  [dim]· /tim off untuk mematikan · maksimal "
            f"{_tim._MAKS_PER_LANGKAH} orang per langkah, tiap orang bicara "
            f"sekali per giliran[/]\n")
        lebar = max(len(a.nama) for a in _tim.ANGGOTA)
        for a in sorted(_tim.ANGGOTA, key=lambda x: x.prioritas):
            console.print(f"    [#fc9018]{a.nama:<{lebar}}[/]  "
                          f"[dim]{_esc(a.bidang)}[/]")
        console.print()

    def pick_web_mode() -> None:
        """/mode — pilih MODE KERJA situs (buat gambar, buat video, dsb) lalu
        program yang menekan tombolnya di browser.

        Beda dari /effort yang memilih varian MODEL & usaha berpikir: yang ini
        mengubah APA yang dihasilkan situs. Di Dola, membuat video memang tak
        bisa diminta lewat kalimat — tombolnya harus ditekan lebih dulu, dan
        sesudah itu komposernya berganti jadi "Describe the actions in the
        video". Tanpa tombol itu, permintaan sebagus apa pun dijawab teks."""
        spec = agent.model_spec
        try:
            from .. import connectors
            conn = connectors.get_connector(spec.connector)
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]connector tak siap: {_esc(str(exc))}[/red]")
            return
        opts = conn.web_mode_options()
        if not opts:
            # Dibedakan dengan tegas dari "situsnya tak punya": yang belum
            # dipetakan bisa dipetakan nanti, dan pengguna berhak tahu bedanya.
            console.print(
                f"  [dim]Tombol mode {_esc(spec.label)} belum dipetakan di "
                "bagas-ai. Situsnya mungkin punya, tapi selektornya belum "
                "diukur — jadi belum bisa ditekan dari sini. Yang sudah siap: "
                "Dola & Qwen.[/dim]\n")
            return
        # Mode itu MENEMPEL: sekali "Create Video" menyala, semua pesan
        # berikutnya dianggap permintaan video. Jalan pulangnya ditawarkan di
        # menu yang sama — kalau tidak, satu klik keliru mengunci sesi.
        MATIKAN = "← chat biasa"
        width = max([len(t) for t, _ in opts] + [len(MATIKAN)])
        choices = [Choice(text, f"{text:<{width}}  —  {desc}")
                   for text, desc in opts]
        if conn.web_mode_off_selector:
            choices.append(Choice(
                MATIKAN, f"{MATIKAN:<{width}}  —  matikan mode, kembali "
                         "mengobrol seperti biasa"))
        # Situs tanpa tombol "matikan" (mis. Dola) tetap harus punya jalan
        # pulang yang JELAS — kalau tidak, mode yang menempel terasa seperti
        # kerusakan. Untuk situs itu jalannya memulai chat baru.
        petunjuk = ("Tombolnya ditekan langsung di situsnya; berlaku untuk "
                    "pesan berikutnya.")
        if not conn.web_mode_off_selector:
            petunjuk += " Untuk kembali ke chat biasa, mulai chat baru (/new)."
        try:
            sel = inquirer.select(
                message=f"Mode kerja {spec.label} (ditekan di UI web)",
                choices=choices, pointer="❯",
                long_instruction=petunjuk,
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return

        result: dict = {"ok": None, "error": None}

        def worker() -> None:
            try:
                result["ok"] = (conn.clear_web_mode() if sel == MATIKAN
                                else conn.set_web_option(sel))
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        wt = threading.Thread(target=worker, daemon=True)
        FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        try:
            _ke_dasar_layar()
            with Live(_region_stack(_oneline(Text())), console=console,
                      refresh_per_second=10, transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    kerja = ("mematikan mode" if sel == MATIKAN
                             else f"menekan '{_esc(sel)}'")
                    live.update(_region_stack(_oneline(_TM(
                        f"  [{tema.p('aksen')}]{frame}[/] [dim]{kerja} di "
                        f"{_esc(spec.label)}…[/dim]"))))
                    wt.join(timeout=0.1)
        except KeyboardInterrupt:
            _reset_web_hub_if_stuck(wt)
            console.print("  [yellow]◼ dibatalkan[/yellow]\n")
            return

        if result["error"] is not None:
            console.print(f"  [yellow]⚠ {_esc(str(result['error']))}[/yellow]\n")
        elif sel == MATIKAN:
            console.print(f"  [#9fc93c]✓ {_esc(str(result['ok']))}[/]\n")
        else:
            console.print(
                f"  [#9fc93c]✓ {_esc(str(result['ok']))}[/]  "
                f"[dim]— kirim pesanmu sekarang; mode ini berlaku untuk "
                f"permintaan berikutnya.[/dim]\n")

    def _web_service_pick() -> str:
        """Pilih service web mana yang dikelola (kalau lebih dari satu punya
        profil login tersimpan). Return "" bila batal / tak ada."""
        from ..connectors import browser as _br
        svcs = []
        for _, _key, spec in models.catalog():
            if spec.connector and spec.connector not in svcs:
                if _br.profile_dir(spec.connector).exists():
                    svcs.append(spec.connector)
        if not svcs:
            console.print("  [dim]belum ada sesi AI web (belum pernah login).[/dim]\n")
            return ""
        if len(svcs) == 1:
            return svcs[0]
        try:
            return inquirer.select(
                message="Kelola sesi web milik layanan mana?",
                choices=[Choice(s, s) for s in svcs], pointer="❯").execute()
        except (KeyboardInterrupt, EOFError):
            return ""

    def _web_busy(msg: str, fn):
        """Jalankan aksi browser dengan baris status hidup (bisa Ctrl+C)."""
        result: dict = {"val": None, "err": None}

        def worker() -> None:
            try:
                result["val"] = fn()
            except BaseException as exc:  # noqa: BLE001
                result["err"] = exc

        wt = threading.Thread(target=worker, daemon=True)
        FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        try:
            _ke_dasar_layar()
            with Live(_region_stack(_oneline(Text())), console=console,
                      refresh_per_second=10, transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    live.update(_region_stack(_oneline(_TM(
                        f"  [{tema.p('aksen')}]{frame}[/] [dim]{_esc(msg)}[/]"))))
                    wt.join(timeout=0.1)
        except KeyboardInterrupt:
            _reset_web_hub_if_stuck(wt)
            console.print("  [yellow]◼ dibatalkan[/yellow]\n")
            return None, KeyboardInterrupt()
        return result["val"], result["err"]

    def manage_web_sessions() -> None:
        """/web — kelola sesi AI web: hapus percakapan yang menumpuk di akun,
        atau logout (hapus profil login browser)."""
        from .. import connectors
        from ..connectors import browser as _br

        if not connectors.playwright_available():
            console.print("  [yellow]⚠ Connector butuh Playwright.[/]\n")
            return
        svc = _web_service_pick()
        if not svc:
            return
        conn = connectors.get_connector(svc)
        own = conn.own_chats()
        console.print(
            f"  [dim]Layanan:[/] [bold]{_esc(conn.label)}[/]   "
            f"[dim]chat tercatat dibuat bagas-ai:[/] [{tema.p('aksen_terang')}]{len(own)}[/]\n")

        choices = [
            Choice("prune", "🧹 Hapus chat lama buatan bagas-ai (sisakan N terbaru)"),
            Choice("pick", "🗂  Pilih chat untuk dihapus (daftar dari akun)"),
            Choice("allown", "🧨 Hapus SEMUA chat buatan bagas-ai"),
            Choice("logout", "🔌 Logout & hapus profil login browser"),
            Choice("cancel", "↩ Batal"),
        ]
        try:
            act = inquirer.select(message=f"Kelola sesi {conn.label}",
                                  choices=choices, pointer="❯").execute()
        except (KeyboardInterrupt, EOFError):
            return
        if act == "cancel":
            return

        if act == "logout":
            try:
                if not inquirer.confirm(
                        message=f"Hapus profil login {conn.label}? "
                                "(harus login ulang nanti)", default=False).execute():
                    return
            except (KeyboardInterrupt, EOFError):
                return
            ok = _br.forget_profile(svc)
            console.print(
                f"  [#9fc93c]✓ profil {_esc(conn.label)} dihapus — login ulang "
                f"saat dipakai lagi.[/]\n" if ok else
                f"  [yellow]⚠ sebagian file profil masih terkunci; tutup Chrome "
                f"lalu coba lagi.[/]\n")
            return

        if not conn.supports_chat_admin():
            console.print(f"  [yellow]⚠ {_esc(conn.label)} belum mendukung "
                          "pengelolaan chat dari bagas-ai.[/]\n")
            return

        if act == "prune":
            if not own:
                console.print("  [dim]belum ada chat buatan bagas-ai.[/dim]\n")
                return
            try:
                keep_s = inquirer.text(
                    message="Sisakan berapa chat terbaru?", default="10").execute()
                keep = max(0, int(str(keep_s).strip() or "10"))
            except (KeyboardInterrupt, EOFError, ValueError):
                return
            if len(own) <= keep:
                console.print(f"  [dim]tak ada yang perlu dihapus "
                              f"({len(own)} <= {keep}).[/dim]\n")
                return
            n, err = _web_busy(f"menghapus {len(own) - keep} chat lama…",
                               lambda: conn.prune_own_chats(keep))
            if err is None:
                console.print(f"  [#9fc93c]✓ {n} chat lama dihapus, "
                              f"{keep} terbaru disimpan.[/]\n")
            elif not isinstance(err, KeyboardInterrupt):
                console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")
            return

        if act == "allown":
            if not own:
                console.print("  [dim]belum ada chat buatan bagas-ai.[/dim]\n")
                return
            try:
                if not inquirer.confirm(
                        message=f"Hapus SEMUA {len(own)} chat buatan bagas-ai "
                                f"di {conn.label}?", default=False).execute():
                    return
            except (KeyboardInterrupt, EOFError):
                return
            n, err = _web_busy(f"menghapus {len(own)} chat…",
                               lambda: conn.prune_own_chats(0))
            if err is None:
                console.print(f"  [#9fc93c]✓ {n} chat dihapus.[/]\n")
            elif not isinstance(err, KeyboardInterrupt):
                console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")
            return

        # act == "pick": ambil daftar chat dari akun lalu pilih yang mau dihapus.
        chats, err = _web_busy("mengambil daftar chat dari akun…", conn.list_chats)
        if err is not None:
            if not isinstance(err, KeyboardInterrupt):
                console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")
            return
        if not chats:
            console.print("  [dim]tak ada chat di akun ini.[/dim]\n")
            return
        own_ids = {r.get("id") for r in own}
        opts = []
        for c in chats[:80]:
            mark = " [dibuat bagas-ai]" if c.get("id") in own_ids else ""
            when = str(c.get("updated") or c.get("created") or "")[:10]
            opts.append(Choice(c["id"],
                               f"{(c.get('title') or '')[:56]:<58}{when}{mark}"))
        try:
            picked = inquirer.checkbox(
                message=f"Pilih chat untuk DIHAPUS ({len(chats)} total)",
                choices=opts, pointer="❯",
                instruction="(spasi pilih, enter konfirmasi)").execute()
        except (KeyboardInterrupt, EOFError):
            return
        if not picked:
            console.print("  [dim](tidak ada yang dihapus)[/dim]\n")
            return
        n, err = _web_busy(f"menghapus {len(picked)} chat…",
                           lambda: conn.delete_chats(list(picked)))
        if err is None:
            conn.forget_chats(set(picked))
            console.print(f"  [#9fc93c]✓ {n} chat dihapus.[/]\n")
        elif not isinstance(err, KeyboardInterrupt):
            console.print(f"  [yellow]⚠ {_esc(str(err))}[/]\n")

    def delete_sessions() -> None:
        sessions = session_mod.list_sessions()
        if not sessions:
            console.print("[dim](tidak ada sesi di folder ini)[/dim]")
            return
        try:
            if len(sessions) == 1:
                s = sessions[0]
                if inquirer.confirm(
                        message=f"Hapus sesi {s.id} ({session_mod.user_msg_count(s)} pesan)?",
                        default=False).execute():
                    if session_mod.delete(s):
                        console.print("[green]✓ 1 sesi dihapus.[/green]")
                        _delete_web_chats_of([s])
                return
            choices = [Choice(s.path.name,
                              f"{s.id}  ({session_mod.user_msg_count(s)} pesan)"
                              + ("  (aktif)" if s.id == agent.session.id else ""))
                       for s in sessions]
            picked = inquirer.checkbox(message="Pilih sesi untuk DIHAPUS",
                                       choices=choices, pointer="❯",
                                       instruction="(spasi pilih, enter konfirmasi)").execute()
        except (KeyboardInterrupt, EOFError):
            return
        removed = [s for s in sessions
                   if s.path.name in picked and session_mod.delete(s)]
        console.print(f"[green]✓ {len(removed)} sesi dihapus.[/green]" if removed
                      else "[dim](tidak ada yang dihapus)[/dim]")
        # Satu sesi terminal = satu percakapan browser -> ikut dihapus.
        _delete_web_chats_of(removed)

    def show_help() -> None:
        c = tema.p("aksen_terang")
        pout(Panel(
            "[dim]ketik pesan biasa untuk mengobrol dengan bagas-ai[/dim]\n\n"
            f"[{c}]/menu[/]     menu interaktif        [{c}]/model[/]    pilih model + saran\n"
            f"[{c}]/effort[/]   mode berpikir          [{c}]/new[/]      sesi baru\n"
            f"[{c}]/add-dir[/]  tambah folder konteks  [{c}]/dirs[/]     folder konteks aktif\n"
            f"[{c}]/rm-dir[/]   lepas folder konteks   [{c}]/delete[/]   hapus sesi\n"
            f"[{c}]/memory[/]   memori jangka panjang  [{c}]/scripts[/]  skrip tersimpan\n"
            f"[{c}]/reset[/]    kosongkan riwayat      [{c}]/clear[/]    bersihkan layar\n"
            f"[{c}]/review[/]   cari bug seluruh proyek [{c}]/scan[/]     segarkan peta proyek\n"
            f"[{c}]/bot[/]      bot Telegram on/off    [{c}]/permissions-bot[/] izin bot\n"
            f"[{c}]/mode[/]     mode kerja situs       [{c}]/scripts[/]  script memory\n"
            f"[{c}]/update[/]   cek pembaruan          [#f0603c]/exit[/]     keluar",
            title=tema.terjemah("[bold #fcc048]❔ Bantuan[/]"), title_align="left",
            border_style=tema.p("aksen"), box=box.ROUNDED, padding=(1, 2)))

    def _baris_versi() -> None:
        """Versi dari SEMUA sumber, bukan cuma GitHub — yang menentukan apa yang
        benar-benar jalan adalah salinan terpasang, bukan isi repo."""
        try:
            v = updater.versions()
        except Exception:  # noqa: BLE001
            return
        t = Text("  ")
        t.append("terpasang ", style="dim")
        t.append(v.get("terpasang") or "?", style=tema.p("aksen2"))
        if v.get("repo"):
            t.append("   repo ", style="dim")
            t.append(v["repo"], style="#9fc93c")
        if v.get("remote") and v["remote"] != v.get("repo"):
            t.append("   remote ", style="dim")
            t.append(v["remote"], style=tema.p("aksen_terang"))
        if v.get("commit_lokal"):
            t.append(f"   commit {v['commit_lokal']}", style="dim")
        console.print(_oneline(t))

    def do_update() -> None:
        console.print("\n  [dim]🔄 memeriksa pembaruan (GitHub + paket yang "
                      "benar-benar terpasang)…[/dim]")
        try:
            res = updater.check()
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memeriksa:[/red] {e}\n")
            return
        st = res.get("status")

        if st == "up_to_date":
            console.print(
                f"  [bold #9fc93c]✓ bagas-ai sudah versi terbaru.[/]  "
                f"[dim]({res.get('local','')})[/dim]"
            )
            _baris_versi()
            console.print()
            return

        if st == "stale_install":
            # Repo mutakhir TAPI yang terpasang tidak. Tanpa pemeriksaan ini
            # keadaannya tak terlihat sama sekali: git bilang aman, versi sama,
            # padahal kode yang jalan masih yang lama.
            body = Text()
            body.append("Repo sudah mutakhir, tapi paket yang TERPASANG "
                        "tertinggal.\n\n", style=f"bold {tema.p('aksen_terang')}")
            body.append("Yang benar-benar dijalankan adalah salinan di "
                        "site-packages, dan isinya berbeda dari repo:\n\n",
                        style="dim")
            for b in (res.get("beda") or [])[:10]:
                body.append("  • ", style=tema.p("aksen2"))
                body.append(b + "\n")
            pout(Panel(body, title=tema.terjemah("[bold #fcc048]🔄 Pemasangan tertinggal[/]"),
                       title_align="left", border_style=tema.p("aksen_terang"),
                       box=box.ROUNDED, padding=(1, 2)))
            try:
                go = inquirer.confirm(message="Pasang ulang sekarang?",
                                      default=True).execute()
            except (KeyboardInterrupt, EOFError):
                go = False
            if not go:
                console.print("  [dim](dilewati)[/dim]\n")
                return
            console.print("  [dim]⏳ memasang ulang…[/dim]")
            _terapkan_update()
            return
        if st == "no_git":
            console.print("  [red]✖ git tidak ditemukan[/red] — pasang git dulu agar bisa memperbarui.\n")
            return
        if st == "no_repo":
            console.print("  [yellow]ℹ Tak bisa menentukan sumber pembaruan (REPO_URL kosong).[/yellow]\n")
            return
        if st == "no_upstream":
            console.print("  [yellow]ℹ Tidak ada remote/upstream yang dilacak.[/yellow]\n")
            return
        if st == "fetch_error":
            console.print(f"  [red]✖ gagal fetch:[/red] {res.get('detail','')}\n")
            return

        if st == "setup_needed":
            # Instalasi tanpa repo git penopang (salinan pip / installer dari
            # folder). Bisa disiapkan otomatis: clone lalu reinstall.
            body = Text()
            body.append("Auto-update belum disiapkan untuk instalasi ini.\n\n",
                        style=f"bold {tema.p('aksen_terang')}")
            body.append(f"Sumber : {res.get('repo_url','')}\n", style="dim")
            body.append(f"Branch : {res.get('branch','')}", style="dim")
            pout(Panel(body, title=tema.terjemah("[bold #fcc048]🔄 Siapkan pembaruan[/]"),
                       title_align="left", border_style=tema.p("aksen"),
                       box=box.ROUNDED, padding=(1, 2)))
            try:
                go = inquirer.confirm(message="Siapkan & perbarui sekarang?",
                                      default=True).execute()
            except (KeyboardInterrupt, EOFError):
                go = False
            if not go:
                console.print("  [dim](dilewati)[/dim]\n")
                return
            console.print("  [dim]⏳ menyiapkan repo & memasang pembaruan…[/dim]")
        elif st == "update_available":
            n = res.get("behind", "?")
            log = res.get("log", "")
            body = Text()
            body.append(f"{n} pembaruan tersedia  ", style=f"bold {tema.p('aksen_terang')}")
            body.append(f"({res.get('local','')} → {res.get('remote','')})\n\n",
                        style="dim")
            if log:
                for line in log.splitlines():
                    body.append("  • ", style=tema.p("aksen2"))
                    body.append(line + "\n")
            pout(Panel(body, title=tema.terjemah("[bold #fcc048]🔄 Pembaruan bagas-ai[/]"),
                       title_align="left", border_style=tema.p("aksen"),
                       box=box.ROUNDED, padding=(1, 2)))
            try:
                go = inquirer.confirm(message="Terapkan pembaruan sekarang?",
                                      default=True).execute()
            except (KeyboardInterrupt, EOFError):
                go = False
            if not go:
                console.print("  [dim](dilewati)[/dim]\n")
                return
            console.print("  [dim]⏳ menarik & memasang pembaruan…[/dim]")
        else:
            console.print(f"  [red]✖ status tak terduga:[/red] {st}\n")
            return

        _terapkan_update()

    def _terapkan_update() -> None:
        try:
            out = updater.apply()
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memperbarui:[/red] {e}\n")
            return
        ost = out.get("status")
        if ost == "pull_error":
            console.print(f"  [red]✖ git pull gagal:[/red] {out.get('detail','')}\n")
            return
        if ost == "clone_error":
            console.print(f"  [red]✖ clone gagal:[/red] {out.get('detail','')}\n")
            return
        if ost != "updated":
            console.print(f"  [red]✖ gagal ({ost}):[/red] {out.get('detail','')}\n")
            return

        note = f"\n  [{tema.p('aksen_terang')}]ℹ {_esc(out['note'])}[/]" if out.get("note") else ""
        if out.get("verified"):
            # TERVERIFIKASI = isi paket terpasang benar-benar sama dengan repo,
            # bukan sekadar "pip keluar dengan kode 0". Bedanya penting: yang
            # kedua pernah berbohong.
            langsung = out.get("how") == "langsung"
            console.print(
                "  [bold #9fc93c]✓ bagas-ai diperbarui & diverifikasi[/]  "
                f"[dim]v{_esc(out.get('version', ''))}[/dim]"
                + ("\n  [dim]kode terbaru sudah aktif — cukup jalankan ulang "
                   "perintahnya, tak ada yang perlu ditunggu.[/dim]"
                   if langsung else
                   "\n  [dim]jalankan ulang[/dim] [{tema.p('aksen_terang')}]bagas-ai[/] "
                   "[dim]agar perubahan aktif.[/dim]")
                + note)
            _baris_versi()
            console.print()
            return

        console.print("  [bold #f7d488]⚠ pembaruan belum tuntas.[/]" + note)
        for b in (out.get("diff") or [])[:6]:
            console.print(f"    [dim]• {_esc(b)}[/dim]")
        if out.get("pip_detail"):
            console.print(f"    [dim]pip: {_esc(out['pip_detail'])}[/dim]")
        console.print()

    def _dir_tree_panel(p, title: str) -> None:
        body = Text()
        body.append(f"{p}\n\n", style="bold #9fc93c")
        body.append(workspace.tree(p), style="dim")
        pout(Panel(body, title=title, title_align="left",
                   border_style="#9fc93c", box=box.ROUNDED, padding=(1, 2)))

    def do_add_dir(path: str) -> None:
        try:
            p = workspace.add(path)
        except ValueError as e:
            console.print(f"  [red]✖[/red] {e}\n")
            return
        agent.refresh_system_prompt()  # bagas-ai langsung "paham" folder ini
        _dir_tree_panel(p, "[bold #9fc93c]📂 Folder konteks ditambahkan[/]")
        console.print(
            "  [dim]bagas-ai kini memahami & bisa baca/tulis file di folder ini "
            "(pakai path absolut).[/dim]\n"
        )

    def do_rm_dir(path: str) -> None:
        if workspace.remove(path):
            agent.refresh_system_prompt()
            console.print(f"  [#9fc93c]✓ Folder konteks dilepas:[/] [dim]{path}[/dim]\n")
        else:
            console.print(f"  [yellow]ℹ Folder itu tidak ada di daftar konteks.[/yellow]\n")

    def ask_add_dir() -> None:
        """Box input path untuk /add-dir TANPA argumen — dengan autolengkap
        path (Tab) dan khusus folder, jadi tak perlu hafal/ketik path lengkap
        di belakang perintah."""
        try:
            path = inquirer.filepath(
                message="Path folder yang mau ditambahkan:",
                only_directories=True,
                long_instruction=("Tab = autolengkap path · Enter = tambah · "
                                  "Ctrl+C = batal"),
            ).execute()
        except (KeyboardInterrupt, EOFError):
            console.print("  [dim]dibatalkan.[/dim]\n")
            return
        path = (path or "").strip().strip('"').strip("'")
        if not path:
            console.print("  [dim]dibatalkan (kosong).[/dim]\n")
            return
        do_add_dir(path)

    def show_dirs() -> None:
        dirs = workspace.list_dirs()
        if not dirs:
            console.print(
                "  [dim]Belum ada folder konteks tambahan.[/dim]  "
                "Ketik [{tema.p('aksen_terang')}]/add-dir[/] untuk menambah (muncul box input "
                "path), atau langsung [{tema.p('aksen_terang')}]/add-dir <path>[/].\n"
            )
            return
        body = Text()
        body.append("Folder yang bagas-ai pahami (selain root project):\n\n",
                    style="dim")
        for d in dirs:
            body.append("  📂 ", style="#9fc93c")
            body.append(f"{d}\n")
        body.append("\nLepas dengan /rm-dir <path>.", style="dim")
        pout(Panel(body, title="[bold #9fc93c]📂 Folder konteks[/]",
                   title_align="left", border_style="#9fc93c",
                   box=box.ROUNDED, padding=(1, 2)))

    def do_action(action: str) -> bool:
        nonlocal agent, session
        if action in ("exit", "quit"):
            return True
        if action == "model":
            prev = _with_console(pick_model)
            if prev is not None:
                _with_console(_connect_web, prev)
        elif action == "effort":
            _with_console(pick_effort)
        elif action == "web":
            _with_console(manage_web_sessions)
        elif action == "browser":
            _with_console(pick_browser)
        elif action == "dirs":
            show_dirs()
        elif action == "delete":
            _with_console(delete_sessions)
        elif action == "new":
            _save_total()  # persist kontribusi sesi lama ke total global
            session = Session.create()
            agent = Agent(session=session)
            agent.start_new_web_chat(immediate=True)
            grand["base"] = prefs.get_total_tokens()  # sesi baru mulai dari total
            console.clear()
            _dorong_ke_bawah([Padding(_banner(agent, False), (0, _LPAD, 0, _LPAD))])
            first_idle[0] = True
        elif action == "reset":
            agent.reset()
            console.print("[dim](riwayat dikosongkan)[/dim]")
        elif action == "clear":
            console.clear()
            _dorong_ke_bawah([Padding(_banner(agent, False), (0, _LPAD, 0, _LPAD))])
            first_idle[0] = True
        elif action == "memory":
            facts = longmem.all_facts()
            pout(Panel("\n".join(f"• {f}" for f in facts) or "[dim]kosong[/dim]",
                       title="[bold #9fc93c]🧠 Memory jangka panjang[/]",
                       title_align="left", border_style="#9fc93c",
                       box=box.ROUNDED, padding=(1, 2)))
        elif action == "scripts":
            items = scripts.index_list()
            txt = "\n".join(f"• [{tema.p('aksen2')}]{it['name']}[/]: {it.get('description') or '-'}"
                            for it in items) or "[dim]belum ada[/dim]"
            pout(Panel(txt, title=tema.terjemah("[bold #fc9018]📜 Script memory[/]"),
                       title_align="left", border_style=tema.p("aksen2"),
                       box=box.ROUNDED, padding=(1, 2)))
        elif action == "help":
            show_help()
        elif action == "update":
            _with_console(do_update)
        elif action == "scan":
            do_scan()
        elif action == "bot":
            do_bot()
        elif action in ("permissions-bot", "perms-bot", "permissions"):
            _with_console(do_permissions_bot)
        return False

    def do_scan() -> None:
        console.print("  [dim]🔍 memindai proyek & menyusun peta…[/dim]")
        try:
            txt = projectindex.ensure(force=True)
            agent.refresh_system_prompt()
            nfiles = txt.count("\n- ")
            console.print(
                f"  [#9fc93c]✓ peta proyek diperbarui[/] [dim]· ~{nfiles} file · "
                f"{len(txt):,} karakter — bagas-ai kini paham struktur terbaru tanpa "
                f"baca ulang.[/]\n".replace(",", "."))
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal memindai:[/red] {e}\n")

    # --- Bot Telegram DI DALAM sesi CLI -------------------------------------
    def _tg_event(kind: str, text: str) -> None:
        """Tampilkan aktivitas bot Telegram di terminal (dipanggil dari thread bot).

        Saat dropdown/menu (inquirer) sedang menggambar, mencetak dari thread
        lain MERUSAK tampilannya — event DITAHAN dulu lalu dicetak setelah
        menu selesai (lihat _with_console / choice_handler)."""
        if input_paused["on"]:
            _tg_pending.append((kind, text))
            if len(_tg_pending) > 50:
                _tg_pending.pop(0)
            return
        _tg_emit(kind, text)

    def _tg_flush() -> None:
        while _tg_pending:
            k, t = _tg_pending.pop(0)
            _tg_emit(k, t)

    def _tg_emit(kind: str, text: str) -> None:
        # Lewat _tambah_konten: saat giliran berjalan (Live aktif) pesan ini
        # DITAMPUNG lalu dicetak oleh thread utama — mencetak langsung dari
        # thread bot di tengah refresh Live meninggalkan jejak yang sama
        # dengan print dari worker.
        t = _esc(text or "")
        if kind == "in":
            _tambah_konten([_TM(
                f"\n  [{tema.p('aksen2')}]📲 Telegram ▸[/] [{tema.p('teks')}]{t}[/]")])
        elif kind == "out":
            snip = t if len(t) <= 600 else t[:600] + "…"
            _tambah_konten([_TM(
                f"  [#9fc93c]  ↳ balasan:[/] [dim]{snip}[/]")])
        elif kind == "perm":
            _tambah_konten([_TM(f"\n  [#f7d488]🔔 {t}[/]")])
        elif kind == "error":
            _tambah_konten([_TM(f"  [red]📲 error:[/] {t}")])
        else:
            _tambah_konten([_TM(f"  [dim]📲 {t}[/]")])

    def do_bot() -> None:
        svc = tg_service.get("svc")
        # Toggle-off juga untuk svc yang MASIH proses menyala (alive tapi belum
        # running) — kalau tidak, /bot berikutnya membuat service KEDUA dan dua
        # polling bentrok ("Conflict: terminated by other getUpdates").
        if svc is not None and (svc.running or svc.alive()):
            console.print("  [dim]📲 mematikan bot Telegram…[/dim]")
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                pass
            tg_service["svc"] = None
            console.print("  [#f7d488]○ bot Telegram MATI.[/]\n")
            return
        if not config.TELEGRAM_BOT_TOKEN:
            console.print("  [red]✖ TELEGRAM_BOT_TOKEN belum diisi di .env[/] "
                          "[dim]— dapatkan dari @BotFather, lalu isi di "
                          f"{config.ENV_FILE}.[/]\n")
            return
        console.print("  [dim]📲 menyalakan bot Telegram…[/dim]")
        try:
            from .telegram_bot import TelegramService  # lazy: hindari impor berat
            svc = TelegramService()
            ok = svc.start(on_event=_tg_event, agent=agent)
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✖ gagal:[/] {e}\n")
            return
        if ok:
            tg_service["svc"] = svc
            ids = sorted(telegram_perms.allowed_ids())
            idtxt = (str(ids) if ids
                     else "belum ada — kirim pesan pertama dari HP, kamu otomatis jadi pemilik")
            console.print(
                f"  [#9fc93c]✓ bot Telegram AKTIF[/] [dim]— kontrol bagas-ai dari HP-mu "
                f"selama sesi ini hidup. Folder: {config.PROJECT_ROOT}\n"
                f"     ID diizinkan: {idtxt}. Aktivitas tampil di sini. "
                f"Atur izin: [/][{tema.p('aksen_terang')}]/permissions-bot[/][dim].[/]\n")
        elif svc.error is not None:
            console.print(f"  [red]✖ gagal menyalakan bot:[/] {svc.error}\n")
        elif svc.alive():
            # Belum 'running' tapi thread masih menyala (jaringan lambat) -> SIMPAN
            # supaya tak jadi bot yatim & tetap bisa dimatikan via /bot.
            tg_service["svc"] = svc
            console.print("  [#f7d488]… bot lambat menyala[/] [dim]— tunggu sebentar "
                          "lalu coba kirim pesan; /bot lagi untuk mematikan.[/]\n")
        else:
            console.print("  [red]✖ bot berhenti tak terduga saat menyala.[/]\n")

    def do_permissions_bot() -> None:
        env_ids = set(config.TELEGRAM_ALLOWED_IDS)
        while True:
            pend = telegram_perms.pending()
            allowed = sorted(telegram_perms.allowed_ids())
            head = _TM(
                f"[bold {tema.p('aksen2')}]🔐 Izin bot Telegram[/]\n"
                f"[dim]Diizinkan:[/] {allowed or '(belum ada)'}\n"
                f"[dim]Menunggu izin:[/] {len(pend)}")
            pout(Panel(head, border_style=tema.p("aksen2"), box=box.ROUNDED, padding=(1, 2)))
            choices = []
            for cid, info in pend.items():
                choices.append(Choice(("approve", int(cid)),
                                      f"✅ Izinkan {info.get('name', '?')} (id {cid})"))
                choices.append(Choice(("deny", int(cid)), f"🗑 Tolak id {cid}"))
            for cid in allowed:
                if cid in env_ids:
                    continue  # dari .env -> ubah di .env, bukan di sini
                choices.append(Choice(("revoke", cid), f"🚫 Cabut izin id {cid}"))
            choices.append(Choice(("add", None), "➕ Tambah ID manual"))
            choices.append(Choice(("done", None), "↩ Selesai"))
            try:
                act = inquirer.select(message="Pilih aksi izin", choices=choices,
                                      pointer="❯").execute()
            except (KeyboardInterrupt, EOFError):
                return
            kind, cid = act
            if kind == "done":
                return
            if kind == "approve":
                telegram_perms.add_allowed(cid)
                console.print(f"  [green]✓ id {cid} kini diizinkan[/]")
            elif kind == "deny":
                telegram_perms.deny(cid)
                console.print(f"  [yellow]🗑 id {cid} ditolak[/]")
            elif kind == "revoke":
                telegram_perms.remove_allowed(cid)
                console.print(f"  [yellow]🚫 izin id {cid} dicabut[/]")
            elif kind == "add":
                try:
                    val = inquirer.text(message="ID Telegram (angka):").execute()
                except (KeyboardInterrupt, EOFError):
                    continue
                if val and val.strip().lstrip("-").isdigit():
                    telegram_perms.add_allowed(int(val.strip()))
                    console.print(f"  [green]✓ id {val.strip()} ditambahkan[/]")
                else:
                    console.print("  [yellow]ID harus angka.[/]")

    def _pilih_tema() -> None:
        """Menu /theme: daftar tema di KIRI, PRATINJAU HIDUP di KANAN dalam
        bingkai — pratinjau berganti mengikuti pilihan yang sedang disorot,
        jadi pengguna melihat tepat satu tema dalam konteks utuhnya (gradasi
        logo, footer, gema prompt, kotak chat) sebelum menekan Enter. Pilihan
        tersimpan di prefs, jadi bertahan antar-jalanan."""
        nonlocal kotak_chat
        from prompt_toolkit.application import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        daftar = tema.daftar()
        idx = {"i": next((i for i, (tid, *_r) in enumerate(daftar)
                          if tid == tema.nama_aktif()), 0)}

        def teks_daftar() -> FormattedText:
            """Kolom kiri: satu entri per tema, sorotan mengikuti kursor."""
            frag: list[tuple[str, str]] = [("", "\n")]
            for i, (tid, label, desc) in enumerate(daftar):
                t = tema.TEMA[tid]
                aktif_skrg = i == idx["i"]
                dipakai = tid == tema.nama_aktif()
                frag.append((f"fg:{t['menu_aktif_bg']} bold",
                             " ❭ " if aktif_skrg else "   "))
                if aktif_skrg:
                    frag.append((f"bg:{t['menu_aktif_bg']} "
                                 f"fg:{t['menu_aktif_teks']} bold", label))
                else:
                    frag.append((f"fg:{t['aksen']}", label))
                frag.append(("class:muted", " ✓" if dipakai else ""))
                frag.append(("", "\n"))
            return FormattedText(frag)

        def teks_pratinjau() -> FormattedText:
            """Kolom kanan: pratinjau tema yang SEDANG disorot."""
            tid, label, desc = daftar[idx["i"]]
            t = tema.TEMA[tid]
            frag: list[tuple[str, str]] = [
                (f"fg:{t['aksen']} bold", f" {label}"),
                ("class:muted", f" — {desc}\n\n"),
                ("", " "),
            ]
            # Gradasi logo (7 titik).
            for hexa in t["grad"]:
                frag.append((f"fg:{hexa}", "██"))
            frag.append(("", "\n\n"))
            # Contoh baris footer.
            bgf = f"bg:{t['bg_footer']}"
            frag.append((f"{bgf} fg:{t['merek_footer']} bold",
                         " ⬢ bagas-ai "))
            frag.append((f"{bgf} fg:{t['sep_footer']}", "│ "))
            frag.append((f"{bgf} fg:{t['model_footer']}", "🌐 Gemini "))
            frag.append((f"{bgf} fg:{t['cmd_footer']}", "/menu "))
            frag.append((f"{bgf} fg:{t['exit_footer']}", "/exit \n\n"))
            # Contoh gema prompt.
            bgg = f"bg:{t['gema_bg']}"
            frag.append((f"{bgg} fg:{t['gema_garis']} bold", " ▌ "))
            frag.append((f"{bgg} fg:{t['gema_teks']} bold",
                         "pesanmu tampil di sini\n\n"))
            # Contoh kotak chat (tepi + ❯).
            tepi = f"fg:{t['tepi']}"
            frag.append((tepi, " ╭───────╮\n"))
            frag.append((tepi, " │ "))
            frag.append((f"fg:{t['aksen']} bold", "❯ "))
            frag.append(("", "ketik…"))
            frag.append((tepi, " │\n"))
            frag.append((tepi, " ╰───────╯\n"))
            return FormattedText(frag)

        def teks_tepi_pratinjau(bawah: bool) -> FormattedText:
            """Tepi bingkai pratinjau (atas berjudul, bawah polos) — warnanya
            mengikuti tema yang sedang disorot. Bersisi tiga, tanpa tepi
            kanan: lebar emoji tak seragam antar-terminal dan tepi kanan
            yang menuntut lebar persis justru bergerigi (keputusan yang sama
            dengan bingkai menu, lihat ui/menu.py)."""
            t = tema.TEMA[daftar[idx["i"]][0]]
            tepi = f"fg:{t['tepi']}"
            if bawah:
                return FormattedText([(tepi, " ╰" + "─" * 34)])
            return FormattedText([
                (tepi, " ╭─ "),
                (f"fg:{t['aksen']} bold", "Pratinjau"),
                (tepi, " " + "─" * 24),
            ])

        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _naik(_e):
            idx["i"] = (idx["i"] - 1) % len(daftar)

        @kb.add("down")
        @kb.add("c-n")
        @kb.add("tab")
        def _turun(_e):
            idx["i"] = (idx["i"] + 1) % len(daftar)

        @kb.add("enter")
        def _pilih(e):
            e.app.exit(result=daftar[idx["i"]][0])

        @kb.add("escape")
        @kb.add("c-c")
        def _batal(e):
            e.app.exit(exception=KeyboardInterrupt, style="class:aborting")

        aplikasi = Application(
            layout=Layout(HSplit([
                VSplit([
                    Window(FormattedTextControl(teks_daftar),
                           width=26, wrap_lines=False),
                    Window(width=1),
                    HSplit([
                        Window(FormattedTextControl(
                            lambda: teks_tepi_pratinjau(False)), height=1),
                        Window(FormattedTextControl(teks_pratinjau),
                               wrap_lines=True),
                        Window(FormattedTextControl(
                            lambda: teks_tepi_pratinjau(True)), height=1),
                    ]),
                ]),
                Window(height=1, char=" "),
                Window(FormattedTextControl(
                    lambda: FormattedText(
                        [("class:muted",
                          " ↑↓ pilih   ·   enter pakai   ·   esc batal ")])),
                       height=1),
            ])),
            full_screen=False, key_bindings=kb, style=_buat_pt_style())
        try:
            pilihan = aplikasi.run()
        except KeyboardInterrupt:
            return
        if tema.set_tema(str(pilihan)):
            # Kotak chat dibangun ulang: tepinya, "❯", footer idle, dan menu
            # autocomplete prompt_toolkit menangkap gaya saat KONSTRUKSI —
            # tanpa pembangunan ulang semuanya tetap memakai tema lama.
            kotak_chat = _buat_kotak()
            console.print(_TM(
                f"  [#9fc93c]✓ tema aktif:[/] [bold]{tema.label_aktif()}[/] — "
                "kotak chat & footer langsung berganti. (Warna markdown "
                "jawaban menyusul saat bagasai dijalankan ulang.)\n"))

    def open_menu() -> bool:
        try:
            action = inquirer.select(
                message="Menu bagas-ai", pointer="❯",
                choices=[Choice("model", "🔀 Ganti model"),
                         Choice("effort", "🎚 Mode / effort"),
                         Choice("new", "✨ Sesi baru"),
                         Choice("delete", "🗑 Hapus sesi"),
                         Choice("web", "🌐 Sesi AI web (hapus chat / logout)"),
                         Choice("memory", "🧠 Memory"),
                         Choice("scripts", "📜 Scripts"),
                         Choice("reset", "🧹 Reset riwayat"),
                         Choice("clear", "🖥 Bersihkan layar"),
                         Choice("update", "🔄 Cek pembaruan"),
                         Choice("help", "❔ Bantuan"),
                         Choice("exit", "🚪 Keluar"),
                         Choice("cancel", "↩ Batal")]).execute()
        except (KeyboardInterrupt, EOFError):
            return False
        if action == "cancel":
            return False
        return do_action(action)

    # --- input (prompt_toolkit hanya saat idle) ---
    # ATURAN MUTLAK: Backspace polos = hapus 1 HURUF, apa pun terminalnya.
    # Ctrl+Backspace = hapus 1 KATA bila bisa dibedakan; bila tidak, ia ikut
    # hapus 1 huruf (arah gagal yang AMAN — kebalikannya merusak ketikan).
    #
    # Di prompt_toolkit, `backspace` dan `c-h` adalah KEY YANG SAMA (alias:
    # Keys.Backspace = Keys.ControlH) — pembedanya hanya DATA byte mentah
    # event. TERBUKTI EMPIRIS di terminal pengguna (Windows Terminal/ConPTY,
    # regresi v1.0.42): Backspace polos tiba dengan data '\x7f' — BUKAN
    # '\x08' seperti klaim tabel keycodes Win32 legacy. Ctrl+Backspace tiba
    # sebagai '\x08' (sama seperti terminal VT Linux/mac).
    #
    # Maka penanda hapus-kata = '\x08' di SEMUA platform: '\x7f' (Backspace
    # polos WT/VT) selalu 1 huruf. Konsekuensi di console legacy murni yang
    # mengirim '\x08' untuk Backspace polos: di sana Backspace polos akan
    # menghapus kata — bila itu terjadi, JANGAN memutar arah penanda lagi
    # (itu merusak terminal utama pengguna); pakai deteksi jenis input.
    # Hapus kata juga selalu tersedia lewat Ctrl+W & Alt+Backspace.
    kb = KeyBindings()

    def _del_word(event):
        pos = event.current_buffer.document.find_start_of_previous_word()
        if pos is not None:
            event.current_buffer.delete_before_cursor(count=-pos)

    kb.add("c-w")(_del_word)                   # Ctrl+W
    kb.add("escape", "backspace")(_del_word)   # Alt+Backspace

    _DATA_KATA = "\x08"   # Ctrl+Backspace (WT/ConPTY & VT); '\x7f' = polos

    @kb.add("c-h")
    def _backspace_pintar(event):
        """Backspace polos = 1 huruf; Ctrl+Backspace = 1 kata (lihat blok
        komentar di atas soal alias key & data byte)."""
        data = event.key_sequence[0].data if event.key_sequence else ""
        if data == _DATA_KATA:
            _del_word(event)
        else:
            event.current_buffer.delete_before_cursor(1)

    # Enter saat menu sugesti "/..." terbuka: terapkan opsi yang tersorot (atau
    # opsi PERTAMA bila belum ada yang disorot) LALU LANGSUNG JALANKAN — satu
    # Enter saja, tanpa Enter kedua. "/mod" + Enter = perintah /model berjalan.
    # Aman dari salah-jalan: menu ini hanya muncul untuk token perintah slash di
    # awal baris (SlashCompleter), jadi tak pernah menyela pengetikan pesan biasa.
    @kb.add("enter", filter=has_completions)
    def _pilih_dan_jalankan_sugesti(event):
        buf = event.current_buffer
        state = buf.complete_state
        if state and state.completions:
            pilih = state.current_completion or state.completions[0]
            buf.apply_completion(pilih)
        buf.validate_and_handle()

    # Enter biasa (menu sugesti sedang tertutup) = kirim. Filternya ditulis
    # eksplisit, bukan diandalkan pada urutan pendaftaran: prompt_toolkit
    # memilih binding yang TERAKHIR cocok, jadi tanpa `~has_completions` binding
    # ini akan menelan Enter milik menu sugesti di atas.
    @kb.add("enter", filter=~has_completions)
    def _kirim(event):
        # Abaikan Enter yang datang tepat setelah tempelan. Beberapa terminal
        # (mis. Windows Terminal) memecah tempelan di baris kosong dan
        # menyisipkan Enter sebagai event terpisah, yang tanpa ini langsung
        # mengirim pesan setengah jadi. 0.1 detik cukup untuk menelan seluruh
        # deretan Enter dari tempelan, tapi tak sampai menelan Enter yang
        # sengaja ditekan pengguna sesudah membaca hasil tempelannya.
        import time as _time
        terakhir = getattr(event.app, "_tempel_terakhir", 0.0)
        if terakhir and (_time.monotonic() - terakhir) < 0.1:
            return
        event.current_buffer.validate_and_handle()

    # Ctrl+C / Ctrl+D: disediakan sendiri karena Application biasa (tak seperti
    # PromptSession) tak membawa keduanya. Keys.SIGINT ikut dipasang untuk
    # terminal yang mengirim sinyal alih-alih byte \x03.
    @kb.add("c-c")
    @kb.add(Keys.SIGINT)
    def _batal_ketik(event):
        event.app.exit(exception=KeyboardInterrupt, style="class:exiting")

    # TEMPELAN PANJANG diringkas jadi satu baris penanda (lihat tempelan.py).
    # Tanpa ini, menempelkan log 300 baris membuat kotak ketik membengkak
    # menutupi layar dan mendorong hilang riwayat percakapan yang justru sedang
    # dibaca. Isinya tetap utuh: penanda ditukar kembali dengan teks aslinya
    # tepat sebelum pesannya dikirim.
    #
    # prompt_toolkit mengenali tempelan di kedua jenis terminal: lewat urutan
    # bracketed-paste di terminal VT, dan lewat pengenal-tempelan bawaan
    # ConsoleInputReader di Windows. Binding ini didaftarkan SESUDAH binding
    # bawaannya, jadi ia yang menang (prompt_toolkit memilih yang terakhir
    # cocok) — yang bawaan cuma menyisipkan teksnya apa adanya.
    @kb.add(Keys.BracketedPaste)
    def _tempel(event):
        data = (event.data or "").replace("\r\n", "\n").replace("\r", "\n")
        if not data:
            return
        simpanan = _tempelan.simpanan()
        import time as _time
        # --- deteksi file media yang di-drop → pratinjau + penanda [foto] ----
        # Gambar: blok warna Minecraft; VIDEO: ikut diterima (analisis via
        # model API vision) dengan panel judul saja — mekanisme pending &
        # pelampirannya sama persis.
        try:
            _cek = data.strip().strip('"').strip("'")
            if _cek and (is_image_path(_cek) or is_video_path(_cek)):
                resolved = str(Path(_cek).resolve())
                if not _isi_state_media(resolved):
                    raise ValueError("media tak bisa dipratinjau")
                _pending_gambar["path"] = resolved
                event.current_buffer.text = "[foto]"
                event.current_buffer.cursor_position = len("[foto]")
                event.app.invalidate()
                event.app._tempel_terakhir = _time.monotonic()
                return
        except Exception:  # noqa: BLE001
            pass
        if simpanan.perlu_diringkas(data):
            event.current_buffer.insert_text(simpanan.simpan(data))
        else:
            # Tempelan pendek disisipkan apa adanya. Baris-barunya diganti spasi:
            # kotak ini satu baris (multiline=False), jadi baris-baru di dalamnya
            # tak pernah terlihat sebagai baris baru — ia cuma jadi celah aneh di
            # tengah kalimat.
            event.current_buffer.insert_text(" ".join(data.split("\n")))
        event.app._tempel_terakhir = _time.monotonic()

    @kb.add("c-d")
    def _eof_ketik(event):
        buf = event.current_buffer
        if buf.text:
            buf.delete()          # ada teks -> Ctrl+D = hapus 1 huruf di depan
        else:
            event.app.exit(exception=EOFError, style="class:exiting")

    # Gaya status bar: latar gelap hangat + aksen kuning-oranye per segmen.
    def _buat_pt_style() -> PTStyle:
        """Gaya prompt_toolkit dari TEMA AKTIF — dibangun ulang tiap kotak
        chat dibuat, supaya /theme mengganti kotak & footernya hidup-hidup."""
        return PTStyle.from_dict({
            "bottom-toolbar": f"bg:{tema.p('bg_footer')} "
                              f"{tema.p('model_footer')} noreverse",
            "garis": tema.p("tepi"),            # tepi kotak chat
            "tanda": f"bold {tema.p('aksen')}",  # "❯" di dalam kotak
            "sep": tema.p("sep_footer"),
            "brand": f"{tema.p('merek_footer')} bold",
            "model": f"{tema.p('model_footer')} bold",
            "eff": tema.p("ubah_footer"),
            "sesi": tema.p("ubah_footer"),
            "git": tema.p("git_footer"),
            "ubah": tema.p("ubah_footer"),
            "total": tema.p("git_footer"),
            "cmd": tema.p("cmd_footer"),
            "exit": tema.p("exit_footer"),
            "muted": tema.p("muted_footer"),
            # Menu autocomplete "/..." — gelap bertema.
            "completion-menu": f"bg:{tema.p('menu_bg')} {tema.p('menu_teks')}",
            "completion-menu.completion":
                f"bg:{tema.p('menu_bg')} {tema.p('menu_teks')}",
            "completion-menu.completion.current":
                f"bg:{tema.p('menu_aktif_bg')} {tema.p('menu_aktif_teks')} bold",
            "completion-menu.meta.completion":
                f"bg:{tema.p('menu_meta_bg')} {tema.p('menu_meta_teks')}",
            "completion-menu.meta.completion.current":
                f"bg:{tema.p('tepi_redup')} {tema.p('menu_teks')}",
        })
    # Status bar PERMANEN di paling bawah (selalu terlihat & rapi).
    def status_bar():
        s = agent.tokens_session
        spec = agent.model_spec
        # Seluruh model berbasis browser -> penanda selalu sama.
        kind = "🌐" if spec.is_web else "🤖"
        eff = ""
        sep = " <sep>│</sep> "
        # PEMAKAIAN TOKEN TOTAL sengaja tak ditampilkan lagi. Penghitungan &
        # penyimpanannya TIDAK dihentikan (lihat grand/_save_total) — yang
        # dibuang hanya tampilannya, karena angka seumur-hidup itu tak pernah
        # menjadi dasar keputusan apa pun saat mengetik. Pemakaian sesi tetap
        # ada: itu yang berubah selama bekerja.
        # Mikrofon yang hidup TIDAK boleh cuma diketahui dari ingatan pengguna:
        # ia merekam ruangan, jadi keadaannya harus terbaca sekali lihat di
        # tempat yang selalu tampak.
        _p = voice_state.get("pendengar")
        mik = ""
        if _p is not None and _p.aktif:
            # DUA keadaan yang sangat berbeda: "mendengarkan" (menunggu namaku)
            # dan "merekam" (apa pun yang terucap sekarang jadi perintah).
            mik = (f"{sep}<sesi>● merekam</sesi>" if _p.merekam
                   else f"{sep}<sesi>🎙 dengar</sesi>")
        bagian = {
            "merek": " <brand>⬢ bagas-ai</brand>",
            "model": f"{sep}{kind} <model>{spec.label}</model>{eff}{mik}",
            "perintah": "<cmd>/menu</cmd> <muted>·</muted> ",
            "ctrlc": " <muted>atau ctrl+c</muted>",
            "exit": "<exit>/exit</exit>",
        }
        git_branch, git_changed = _git_info()
        bagian["git"] = f"{sep}<git>🌿 {git_branch}</git>" if git_branch else ""
        bagian["ubah"] = f"{sep}<ubah>📝 {git_changed}</ubah>" if git_changed else ""
        lebar = _lebar_kotak()
        # Aturan penyusutan dipakai BERSAMA dengan bar versi rich (_bar_status),
        # supaya bentuknya tak berubah saat giliran mulai/selesai.
        ada = _bagian_bar(lebar, lambda b: _panjang_tampak(bagian[b]) + 1)

        kiri = "".join(bagian[b] for b in ("merek", "model", "git", "ubah") if b in ada)
        if not kiri.startswith(" "):     # tanpa merek, pemisah di depan dibuang
            kiri = " " + kiri.lstrip().removeprefix("<sep>│</sep>").lstrip()
        kanan = (bagian["perintah"] if "perintah" in ada else "") \
            + bagian["exit"] \
            + (bagian["ctrlc"] if "ctrlc" in ada else "") + " "

        # Didorong ke tepi KANAN, bukan disambung dengan pemisah: perintah
        # bukan bagian dari deretan keterangan di kiri — ia hal lain, dan
        # memisahkannya secara ruang membuat keduanya terbaca sekali lihat.
        # Lebar dihitung dari teks TAMPAK (tanpa tag), kalau tidak paddingnya
        # jauh terlalu besar dan barisnya justru terlipat.
        antara = lebar - _panjang_tampak(kiri) - _panjang_tampak(kanan)
        spasi = " " * max(0, antara)
        return HTML(kiri + spasi + kanan)

    def _buat_kotak() -> KotakChat:
        """Pabrik kotak chat — /theme membangun ulang lewat sini supaya gaya
        (tepi, ❯, footer, menu autocomplete) mengikuti tema yang baru."""
        return KotakChat(status=status_bar, key_bindings=kb,
                         style=_buat_pt_style(), completer=SlashCompleter())

    kotak_chat = _buat_kotak()

    # Pada idle pertama, konten startup sudah dorong ke dasar layar secara manual
    # (lihat main()), jadi _ke_dasar_layar() di tanya() TIDAK dipanggil — push
    # itu hanya menciptakan celah kosong yang memotong tampilan logo. Setelah
    # giliran pertama selesai, semua idle berikutnya pakai push=True seperti biasa.
    first_idle = [True]

    while True:
        # ANTREAN DULU: prompt yang diketik+Enter selama giliran sebelumnya
        # dikerjakan berurutan. TANPA gema lagi di sini — pesannya sudah
        # tergema saat diketik (lihat _ketik), jadi menggemakannya sekali lagi
        # membuat satu pesan tampak dikirim dua kali.
        #
        # Di sinilah perintah yang diketik selagi AI menjawab akhirnya
        # dijalankan: ia sengaja dilewati penyisipan (lihat _ambil_sisipan) dan
        # menunggu di antrean sampai giliran benar-benar berhenti.
        with antre_lock:
            raw = prompt_queue.pop(0) if prompt_queue else None
        if raw is None:
            try:
                # patch_stdout: aktivitas bot Telegram (dari thread latar)
                # tercetak RAPI di atas kotak, tak merusak baris ketikan.
                # `default` = sisa ketikan yang belum di-Enter saat giliran
                # tadi selesai — tak ada ketikan yang hilang, tinggal lanjut.
                prefill = typing_state["buf"]
                prefill_pos = typing_state["pos"]
                typing_state["buf"] = ""
                typing_state["pos"] = 0
                with patch_stdout(raw=True):
                    # Pada idle pertama jangan push: konten sudah dorong ke dasar
                    # di main(). Idle berikutnya tetap push seperti biasa.
                    raw = kotak_chat.tanya(
                        prefill, prefill_pos, push=not first_idle[0])
                    first_idle[0] = False
            except KeyboardInterrupt:
                # Ctrl+C di kotak yang sedang MENUNGGU = keluar, sepadan dengan
                # /exit (lewat break yang sama, jadi sesi tersimpan & Chrome
                # ditutup rapi). Dulu ketukan ini cuma diabaikan, dan itu bikin
                # bagas-ai terasa tak mau ditutup dengan cara yang dipakai
                # hampir semua program terminal.
                #
                # KECUALI kotaknya masih berisi ketikan: di situ Ctrl+C hampir
                # pasti dimaksudkan membuang kalimat, bukan menutup program.
                # Ketukan pertama membuangnya, ketukan kedua (kotak sudah
                # kosong) baru keluar — supaya satu ketukan salah tak
                # menghapus kalimat panjang DAN menutup program sekaligus.
                if kotak_chat.sisa().strip():
                    console.print("  [dim]ketikan dibuang — Ctrl+C sekali "
                                  "lagi untuk keluar[/dim]")
                    continue
                break
            except EOFError:
                break
            # Bersihkan panel gambar setelah kotak selesai.
            _gambar_state.clear()
            _pending_gambar.clear()
            # Kotaknya menghilang begitu Enter ditekan (erase_when_done); yang
            # mengabadikan pesan adalah gema ini — bentuknya sama persis dengan
            # gema pesan yang diantrekan. Gema masuk ke TUMPUKAN BAWAH (bukan
            # scrollback) supaya ia ikut menempel ke dasar layar; region
            # giliran yang segera menyusul sudah merendernya.
            if raw.strip():
                # PUTIH MURNI (#ffffff) & TEBAL, bukan `white` milik rich —
                # `white` dipetakan ke warna 7 palet terminal, yang di banyak
                # tema justru abu-abu. Pesan pengguna adalah penanda batas
                # antar-giliran: satu-satunya baris yang harus bisa ditemukan
                # seketika saat menggulung riwayat panjang, jadi ia diberi
                # kontras tertinggi di layar.
                # Perintah yang DIUCAPKAN ditandai di gema ini juga, bukan
                # dicetak sendiri sebelum masuk kotak (lihat _voice_masuk).
                # pop() dipakai, bukan get(): penandanya berlaku sekali pakai,
                # supaya ketikan berikutnya tak ikut dikira ucapan.
                # Baris kosong DI ATAS gema: jarak dari output giliran
                # sebelumnya (jawaban/baris langkah terakhir).
                lisan = voice_state.pop("terucap", None) == raw.strip()
                _tambah_konten([_KOSONG, _gema_prompt(
                    raw.strip(), prefix="🎙 " if lisan else "")])
        text = raw.strip()
        if not text:
            continue
        if text.startswith("/"):
            cmd = text[1:].strip().lower()
            if cmd == "menu":
                if _with_console(open_menu):
                    break
            elif cmd == "review":
                # Audit bug/kesalahan sistem menyeluruh — dijalankan sbg giliran.
                console.print("  [dim]🔎 mereview proyek untuk bug & kesalahan "
                              "sistem…[/dim]")
                try:
                    process(_REVIEW_PROMPT)
                except KeyboardInterrupt:
                    console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
                _save_total()
            elif cmd.startswith("model ") or cmd == "model":
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    prev_model = agent.model
                    try:
                        console.print(f"[green]✓ Model: {agent.set_model(parts[1])}[/green]")
                    except ValueError as e:
                        console.print(f"[red]{e}[/red]")
                    else:
                        _warn_glm_vpn()
                        # Model connector web: CONNECT sekarang juga (login sekali
                        # bila belum pernah; sudah pernah -> langsung ke sesi chat).
                        # Dibungkus _with_console: sesi loginnya menyodorkan menu
                        # & input, dan kotak chat tak boleh lenyap selama itu.
                        if agent.model_spec.is_web:
                            _with_console(_connect_web, prev_model)
                else:
                    prev_model = _with_console(pick_model)
                    if prev_model is not None:
                        _with_console(_connect_web, prev_model)
            elif cmd == "add-dir" or cmd.startswith("add-dir "):
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    do_add_dir(parts[1].strip().strip('"').strip("'"))
                else:
                    # Tanpa argumen -> box input path (autolengkap folder).
                    _with_console(ask_add_dir)
            elif cmd == "rm-dir" or cmd.startswith("rm-dir "):
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    do_rm_dir(parts[1].strip().strip('"').strip("'"))
                else:
                    console.print("  [yellow]Pakai: /rm-dir <path folder>[/yellow]\n")
            elif cmd == "mode":
                _with_console(pick_web_mode)
            elif cmd == "theme":
                _with_console(_pilih_tema)
            elif cmd == "tim" or cmd.startswith("tim "):
                show_tim(text[4:].strip())
            elif cmd == "mic" or cmd.startswith("mic "):
                show_mic(text[4:].strip())
            elif cmd == "voice" or cmd.startswith("voice "):
                show_voice(text[6:].strip())
            elif cmd == "compact":
                do_compact()
            elif cmd == "send-compact" or cmd.startswith("send-compact "):
                do_send_compact(text[13:].strip())
            elif cmd == "live":
                tui_mode["on"] = not tui_mode["on"]
                if tui_mode["on"]:
                    console.print("  [#9fc93c]✓ tampilan mengalir AKTIF[/] "
                                  "[dim]— langkah/diff/jawaban mengalir ke "
                                  "scrollback + footer status hidup di bawah.[/]\n")
                else:
                    console.print("  [#f7d488]○ tampilan interaktif MATI[/] "
                                  "[dim]— pakai tampilan mengalir biasa.[/]\n")
            else:
                if do_action(cmd):
                    break
            continue
        try:
            # Penanda tempelan ditukar kembali dengan isi aslinya DI SINI —
            # satu-satunya gerbang yang dilewati semua pesan, baik yang diketik
            # di kotak idle maupun yang mengantre dari ketikan saat giliran
            # sebelumnya berjalan. Yang tergema ke riwayat tetap bentuk
            # ringkasnya (lihat gema di atas): itu memang yang membuat
            # riwayatnya tetap bisa dibaca.
            # [foto] + gambar pending -> penanda lampiran [GAMBAR] (lihat
            # _kembangkan_foto); core yang memisahkannya jadi lampiran.
            _msg = _kembangkan_foto(_tempelan.simpanan().kembangkan(text))
            process(_msg)
        except KeyboardInterrupt:
            # Jaring pengaman terakhir: Ctrl+C tak boleh menjatuhkan REPL.
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        _save_total()

    _save_total()
    if tg_service.get("svc") is not None:
        # stop() aman dipanggil apa pun keadaannya (running / masih menyala).
        try:
            tg_service["svc"].stop()
        except Exception:  # noqa: BLE001
            pass
    # Hentikan pengucap: proses PowerShell-nya harus ikut mati, kalau tidak ia
    # menyelesaikan kalimat terakhir setelah terminalnya sudah ditutup.
    try:
        _suara.tutup()
    except Exception:  # noqa: BLE001
        pass
    # Mikrofon DILEPAS. Thread-nya daemon (takkan menahan proses), tapi aliran
    # audionya memegang perangkat: tanpa ini indikator "sedang merekam" di
    # Windows bisa menyala beberapa saat sesudah bagas-ai ditutup.
    if voice_state.get("pendengar") is not None:
        try:
            voice_state["pendengar"].berhenti()
        except Exception:  # noqa: BLE001
            pass
    # Tutup browser connector dengan RAPI supaya Chrome tak mengira dirinya
    # crash & menawarkan "Restore pages?" saat dipakai lagi.
    try:
        from ..connectors import browser as _br
        _br.shutdown()
    except Exception:  # noqa: BLE001
        pass
    console.clear()
    console.print("\n  [#fcc048]⬢ bagas-ai[/]  [dim]— sampai jumpa! 👋[/dim]\n")


if __name__ == "__main__":
    main()

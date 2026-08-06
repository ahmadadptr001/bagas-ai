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

import difflib
import re
import sys
import threading
import time
import unicodedata

try:  # keyboard non-blocking (Windows): ketikan-selama-giliran & Ctrl+C
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows
    _msvcrt = None

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
from ..core import Agent  # noqa: E402
from ..session import Session  # noqa: E402
# Prompt interaktif MILIK SENDIRI (dulu InquirerPy) — lihat ui/menu.py.
from ..ui.menu import Choice, inquirer  # noqa: E402

# Tema Markdown selaras palet "zenitsu" (kuning-oranye) agar jawaban AI
# (heading, list, kutipan,
# kode, tautan) serasi dengan seluruh UI — bukan warna default rich yang kontras.
_MD_THEME = Theme({
    "markdown.h1": "bold #fcc048",
    "markdown.h1.border": "#fcc048",
    "markdown.h2": "bold #fc9018",
    "markdown.h3": "bold #ffb861",
    "markdown.h4": "bold #9fc93c",
    "markdown.h5": "bold #f7d488",
    "markdown.h6": "bold #fca830",
    "markdown.item.bullet": "bold #fcc048",
    "markdown.item.number": "bold #fc9018",
    "markdown.code": "#ffd9a0 on #3a2a1a",       # `inline code`
    "markdown.link": "#fc9018 underline",
    "markdown.link_url": "dim #ffcf8a",
    "markdown.block_quote": "italic #f7d488",
    "markdown.block_quote_border": "#7a5c3a",
    "markdown.hr": "#4a3826",
    "markdown.strong": "bold #f7e6d0",
    "markdown.emph": "italic #f2e3cc",
    "markdown.text": "#f2e3cc",
})
console = Console(theme=_MD_THEME)  # auto-detect VT -> warna/emoji mulus

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
    ("tim", "24 spesialis yang meninjau pekerjaan secara pasif"),
    ("mic", "suara: kabar AI dibacakan pengeras suara (on/off/tes)"),
    ("voice", "mikrofon: sebut \"bagas ai …\" lalu diam sejenak (on/off/tes)"),
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


def _update_notice() -> None:
    """Notifikasi ringkas bila versi usang (dari cache), lalu cek ulang di latar.

    Non-blocking: notifikasi diambil dari hasil cek TERAKHIR yang tersimpan,
    sedangkan pengecekan baru ke GitHub berjalan di latar untuk startup berikut.
    """
    try:
        cache = updater.read_cache()
        if cache.get("status") == "update_available":
            # Sampai sini artinya pemasangan paksa saat startup GAGAL
            # (jaringan/git) — beri jalan manual.
            n = cache.get("behind", "?")
            local, remote = cache.get("local", ""), cache.get("remote", "")
            ver = f" ({local} → {remote})" if local and remote else ""
            pout(
                f"[#f7d488]⬆ Pembaruan bagas-ai tersedia[/] "
                f"[dim]— {n} commit lebih baru{ver} (pemasangan otomatis "
                f"gagal).[/dim]  Ketik [#ffb861]/update[/] untuk mencoba lagi.",
                bottom=0,
            )
        # Segarkan cache di thread latar; startup berikutnya otomatis
        # MEMASANG pembaruan yang ditemukan (paksa, tanpa tanya).
        updater.background_refresh(min_interval=1800)
    except Exception:
        pass

# Gradasi ungu -> biru (magenta neon) untuk teks shadow.
# Gradasi wordmark: emas terang -> oranye -> cokelat bara, arah
# yang sama dengan cahaya di latar tema ini.
_GRAD = ["#fde68a", "#fcc048", "#fca830", "#fc9018", "#e8760c", "#c25a08", "#9c4800"]


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
_FASE_PIKIR = "berpikir"


def _web_phase(msg: str) -> str:
    """Ringkas status connector web jadi SATU kata fase untuk baris status."""
    m = (msg or "").lower()
    # "menjawab" ikut jadi "berpikir": bagi yang menunggu, keduanya sama saja —
    # model belum selesai. Membedakannya cuma menambah kata yang berkedip.
    if "menjawab" in m or "berpikir" in m:
        return _FASE_PIKIR
    if "login" in m or "sign-in" in m or "sign in" in m:
        return "menunggu login"
    if "mengetik" in m or "mengirim" in m or "mengunggah" in m or "lampiran" in m:
        return "analisis pesan"
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
_GARIS_KOTAK = "#7a5c3a"


_TAG_HTML_RE = re.compile(r"<[^>]+>")


def _panjang_tampak(html: str) -> int:
    """Berapa KOLOM yang benar-benar dipakai sepotong markup HTML prompt_toolkit.

    Tag gaya (`<brand>…</brand>`) tak memakan ruang di layar, jadi len() atas
    teks mentahnya jauh melebihi lebar sesungguhnya — dan perataan yang
    dihitung darinya meleset puluhan kolom. Emoji dihitung dua kolom karena
    memang selebar itu di terminal."""
    tampak = _TAG_HTML_RE.sub("", html)
    tampak = tampak.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    lebar = 0
    for ch in tampak:
        lebar += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return lebar


def _lebar_kotak() -> int:
    """Lebar kotak chat = LEBAR PENUH terminal.

    Bukan dibatasi _KOTAK_MAKS lagi: kotak chat dan bar status adalah satu
    kesatuan yang menempel di dasar layar, dan bar status memang selebar
    terminal. Kotak yang lebih sempit membuat keduanya tampak tak sejajar."""
    return max(20, console.width)


# Baris kosong pemberi napas DI ATAS kotak chat saja. Di BAWAHNYA sengaja tak
# ada: kotak dan bar status satu kesatuan yang menempel di dasar layar, jadi
# celah di antara keduanya justru memutus kesatuan itu.
_KOSONG = Text("")


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
    atas.append("╭" + "─" * (lebar - 2) + "╮", style=_GARIS_KOTAK)
    baris = Text()
    baris.append("│ ", style=_GARIS_KOTAK)
    baris.append("❯ ", style="bold #fcc048")
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
        baris.append(tampak[:rel], style="#f2e3cc")
        baris.append("▌", style="#fcc048")
        baris.append(tampak[rel:], style="#f2e3cc")
    # Potong dulu, baru ratakan sampai tepi kanan — kalau tidak, isi yang
    # kepanjangan mendorong tepi kanannya keluar layar dan kotaknya patah.
    baris.truncate(lebar - 1, overflow="ellipsis")
    pad = (lebar - 1) - baris.cell_len
    if pad > 0:
        baris.append(" " * pad)
    baris.append("│", style=_GARIS_KOTAK)
    bawah = Text()
    bawah.append("╰" + "─" * (lebar - 2) + "╯", style=_GARIS_KOTAK)
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
_BG_HASIL = "#241a10"


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
    gaya = f"on {_BG_HASIL}"
    tepi = "#f0603c" if gagal else "#7a5c3a"
    out = []
    for i, ln in enumerate(tampil):
        baris = Text("     ")
        # Garis tepi kiri: penanda "ini satu blok", jauh lebih murah daripada
        # bingkai penuh yang bakal beradu dengan kotak chat satu-satunya.
        baris.append("▏", style=f"{tepi} {gaya}")
        redup = sisa > 0 and i == len(tampil) - 1
        baris.append(" " + ln, style=f"{'#a89078 italic' if redup else '#d9c4a6'} {gaya}")
        baris.truncate(lebar, overflow="ellipsis")
        pad = lebar - baris.cell_len
        if pad > 0:
            baris.append(" " * pad, style=gaya)
        out.append(_oneline(baris))
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
    """Dorong kursor ke baris TERAKHIR layar memakai baris kosong.

    Baris kosongnya jatuh di petak yang barusan ditinggalkan kotak idle (sudah
    dihapus, jadi memang kosong), sehingga tak ada celah baru yang terlihat."""
    sisa = _sisa_baris_bawah()
    if sisa and sisa > 1:
        console.file.write("\n" * (sisa - 1))
        console.file.flush()


# --- bar status permanen ---------------------------------------------------
#
# Bar ini PASANGAN TETAP kotak chat: kotak selalu menempel persis di atasnya,
# dan keduanya tak pernah menghilang — baik saat kamu mengetik maupun saat AI
# masih menyusun jawabannya. Saat idle ia digambar prompt_toolkit (lihat
# KotakChat), saat giliran berjalan ia digambar rich dari sini; isinya dijaga
# sama persis supaya perpindahan antar keduanya tak terlihat.
_BG_BAR = "#1a120b"

# Urutan PENGORBANAN saat terminal menyempit, dari isi yang paling bisa
# dilepas. Bar status dipatok di dasar layar, jadi ia tak boleh terlipat: satu
# baris yang meluber merusak seluruh susunan kotak chat di atasnya.
#
# Daftar ini dipakai BERSAMA oleh kedua bar — versi prompt_toolkit (saat idle)
# dan versi rich (saat giliran berjalan). Keduanya tampil bergantian di tempat
# yang sama persis, jadi aturan yang berbeda akan terlihat sebagai layar yang
# melompat tiap kali giliran mulai atau selesai.
_TINGKAT_BAR: tuple[tuple[str, ...], ...] = (
    ("merek", "model", "sesi", "perintah", "ctrlc"),
    ("merek", "model", "sesi", "perintah"),
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
        "sesi": f"{SEP}⚡ {_fmt(s.total)} sesi",
        "perintah": "/menu · /exit",
        "ctrlc": " atau ctrl+c",
        "exit": "/exit",
    }
    ada = _bagian_bar(console.width, lambda b: Text(teks[b]).cell_len + 1)

    bar = Text(style=f"on {_BG_BAR}")
    if "merek" in ada:
        bar.append(" ⬢ bagas-ai", style="bold #fcc048")
    if "model" in ada:
        if "merek" in ada:
            bar.append(SEP, style="#4a3826")
        else:
            bar.append(" ")
        bar.append(f"{'🌐' if spec.is_web else '🤖'} ")
        bar.append(spec.label, style="bold #fc9018")
    if "sesi" in ada:
        bar.append(SEP, style="#4a3826")
        bar.append(f"⚡ {_fmt(s.total)}", style="#f7d488")
        bar.append(" sesi", style="#a89078")

    # Perintah didorong ke tepi KANAN, bukan disambung dengan pemisah:
    # perintah bukan bagian dari deretan keterangan di kiri — ia hal lain, dan
    # memisahkannya secara ruang membuat keduanya terbaca sekali lihat.
    kanan = Text(style=f"on {_BG_BAR}")
    if "perintah" in ada:
        kanan.append("/menu", style="#ffb861")
        kanan.append(" · ", style="#a89078")
    kanan.append("/exit", style="#f0603c")
    if "ctrlc" in ada:
        kanan.append(" atau ctrl+c", style="#a89078")
    kanan.append(" ")

    # Sisanya diisi spasi supaya latarnya jadi PITA penuh, bukan potongan
    # pendek yang menggantung di tengah baris.
    antara = console.width - bar.cell_len - kanan.cell_len
    bar.append(" " * max(1, antara))
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
        self.buffer = Buffer(
            multiline=False,
            completer=completer,
            complete_while_typing=True,
            history=InMemoryHistory(),
            accept_handler=self._terima,
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
        kotak = HSplit(
            [
                self._tepi("╭", "╮"),
                VSplit([self._sisi(), Window(width=1), self._isi,
                        Window(width=1), self._sisi()]),
                ConditionalContainer(
                    VSplit([self._sisi(), Window(width=1),
                            CompletionsMenu(max_height=_MENU_MAKS),
                            Window(), self._sisi()],
                           height=self._tinggi_menu),
                    filter=has_completions),
                self._tepi("╰", "╯"),
            ],
        )   # tanpa `width`: kotak mengisi LEBAR PENUH terminal (lihat
            # _lebar_kotak), sejajar dengan bar status di bawahnya.
        return HSplit([
            # PENDORONG. Inilah yang memakukan kotak + bar ke DASAR LAYAR:
            # prompt_toolkit selalu memberi aplikasi non-fullscreen setinggi
            # sisa baris di bawah kursor (renderer: height = max(
            # _min_available_height, …)), jadi ruang itu memang sudah ada —
            # dulu saja seluruhnya menumpuk di atas sehingga kotaknya
            # menggantung di tengah layar. Window lentur ini menelan sisa
            # ruangnya, dan yang di bawahnya terdorong mentok ke bawah.
            Window(),
            # Satu baris napas di atas kotak — kembaran _KOSONG di sisi rich,
            # supaya jaraknya tak berubah sedikit pun saat giliran selesai dan
            # tampilannya berpindah tangan dari rich ke sini.
            Window(height=1),
            kotak,
            # TANPA baris kosong di sini: bar status menempel langsung.
            Window(FormattedTextControl(self._status), height=1,
                   dont_extend_height=True, style="class:bottom-toolbar"),
        ])

    # --- pemakaian -----------------------------------------------------------
    def tanya(self, default: str = "", posisi: int | None = None) -> str:
        """Tampilkan kotak sampai Enter, kembalikan teksnya.

        `default` = sisa ketikan dari giliran sebelumnya, `posisi` = di mana
        kursornya tadi berada. Keduanya dibawa utuh supaya penyerahan dari
        kotak-saat-sibuk ke kotak idle tak terasa: kursor tak melompat ke ujung
        di tengah kalimat yang sedang dibetulkan. KeyboardInterrupt/EOFError
        diteruskan ke pemanggil."""
        n = len(default)
        kursor = n if posisi is None else max(0, min(int(posisi), n))
        self.buffer.reset(Document(default, kursor))
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
_TOOL_DIFF = ("write_file", "edit_file", "append_file")

# SEMUA tool yang mengubah isi disk. Dipakai ringkasan giliran ("N file") dan
# pemicu penyegaran peta proyek. Dulu hanya write_file/delete_file — giliran
# penuh edit_file diringkas "0 file", dan peta proyek DIAM-DIAM basi sesudah
# edit sehingga giliran berikutnya bekerja dari peta lama.
_TOOL_UBAH_FILE = {
    "write_file", "edit_file", "append_file", "delete_file", "move_file",
    "copy_file", "replace_in_files", "download_file", "zip_extract",
    "undo_changes",
}


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


def _print_diff(path: str, old: str, new: str, is_new: bool, limit: int = 200) -> None:
    """Tampilan editor: header status + line-numbered diff (bg hijau/merah).

    Seluruh diff dicetak SEKALI sebagai satu Group (statis, atomik) — mengalir
    ke scrollback seperti output biasa, tak pernah disela/ditimpa footer live."""
    icon, label = ("✨", "dibuat") if is_new else ("📝", "diubah")
    rows: list = [Text.from_markup(
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
    console.print(Group(*rows))


def _print_delete(path: str, content: str, limit: int = 80) -> None:
    rows: list = [Text.from_markup(
        f"\n  [bold]🗑 [cyan]{_esc(path)}[/cyan][/bold] [dim](dihapus)[/dim]")]
    warna = _pewarna(path, content)
    for i, line in enumerate(content.splitlines(), start=1):
        if i > limit:
            rows.append(Text("  ... (dipotong)", style="dim"))
            break
        rows.append(_row(str(i), "-", _ambil(warna, i) or line, _DEL))
    console.print(Group(*rows))


def show_logo() -> None:
    """Wordmark modern: figlet bergradasi + garis aksen gradasi + tagline bersih
    (tanpa doodle ASCII)."""
    m = " " * _LPAD  # indent kiri agar tidak mepet
    console.print()
    if Figlet is not None:
        try:
            art = Figlet(font="ansi_shadow").renderText("bagas-ai")
            lines = [ln for ln in art.split("\n") if ln.strip()]
        except Exception:
            lines = ["b a g a s - a i"]
    else:
        lines = ["b a g a s - a i"]
    width = max((len(ln) for ln in lines), default=24)
    # Terminal lebih sempit dari seni figlet? wrap bikin logo jadi sampah —
    # jatuh ke wordmark teks biasa yang selalu muat.
    if width + _LPAD > console.width:
        lines = ["b a g a s - a i"]
        width = len(lines[0])
    for i, ln in enumerate(lines):
        t = Text(m + ln, style=f"bold {_GRAD[min(i, len(_GRAD) - 1)]}")
        t.no_wrap = True
        console.print(t)
    # Garis aksen gradasi di bawah wordmark (aksen modern pengganti doodle).
    seg = max(12, min(width, 56))
    per = max(1, seg // len(_GRAD))
    bar = Text(m)
    for col in _GRAD:
        bar.append("━" * per, style=col)
    console.print(bar)
    sub = Text(m)
    sub.append("AI agent serbaguna", style="bold #f2e3cc")
    sub.append("  ·  terminal · telegram · multitasking", style="dim")
    console.print(_oneline(sub))


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
_PIL_TERANG, _PIL_REDUP, _PIL_SISA = "bold #fcc048", "#a86a18", "#4a3826"


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
    t.append("  ⏸ ", style="bold #fcc048")
    t.append("memadatkan ingatan", style="#fcc048")
    t.append("  ")
    t.append_text(_bar_pil(frac, fase=int(el * 12)))
    t.append(f" {int(round(frac * 100)):>3}%", style="bold #fc9018")
    # ETA dari kemajuan NYATA, bukan tebakan: waktu yang sudah lewat dibagi
    # bagian yang sudah selesai. Di bawah 8% angkanya masih liar (satu tahap
    # cepat bisa menghasilkan perkiraan sepersepuluh detik), jadi ditahan dulu.
    # Ini juga sebabnya bar ini boleh ada sementara bar ETA untuk jawaban AI
    # dulu dibuang: yang di sini mengukur pekerjaan LOKAL yang tahapannya
    # diketahui, bukan menebak kapan situs selesai menjawab.
    if 0.08 <= frac < 1.0:
        t.append(f"  ~{jalan / frac - jalan:.1f}s", style="#f7d488")
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
        self.disp = 0.0
        self.retry_until = 0.0
        self.retry_msg = ""
        self.cancelling = False
        # Pemadatan ingatan yang sedang berjalan: (pecahan, keterangan, mulai).
        # None = tak sedang memadatkan.
        self.padat: tuple[float, str, float] | None = None

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
        return Group(
            self._baris_status(),
            _KOSONG,
            *_kotak_chat(),
            _bar_status(self.agent, self.total()),
        )

    def _baris_status(self) -> Text:
        el = time.time() - self.start
        now = time.time()
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]

        dot = "[#4a3826]•[/]"

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
            t.append(f"  {frame} ", style="bold #f7d488")
            t.append("layanan sibuk — menunggu lalu melanjutkan", style="#f7d488")
            t.append(f"  {left:.0f}s", style="bold #fca830")
            if self.retry_msg:
                t.append(f"  ·  {self.retry_msg}", style="dim #f7d488")
            t.append("     Ctrl+C batal", style="dim italic")
            return _oneline(t)

        target = float(self.agent.tokens_live)
        self.disp += (target - self.disp) * 0.30  # easing -> angka mengalir
        if abs(target - self.disp) < 1:
            self.disp = target
        t = Text()
        t.append(f"  {frame} ", style="bold #fcc048")
        t.append(self.phase, style="#fcc048")
        t.append(f"  {_fmt_elapsed(el)}", style="bold #fc9018")
        t.append("   ")
        t.append_text(Text.from_markup(dot))
        t.append(f"  ⚡ {_fmt(int(self.disp))}", style="#f7d488")
        t.append(" token", style="dim")
        if self.tool:
            t.append("   ")
            t.append_text(Text.from_markup(dot))
            t.append(f"  🔧 {self.tool}", style="#ffd9a0")
        if self.step:
            t.append("   ")
            t.append_text(Text.from_markup(dot))
            t.append(f"  langkah {self.step}", style="dim #ffb861")
        t.append("     Ctrl+C batal", style="dim italic")
        return _oneline(t)


# Tips singkat yang BERGANTIAN muncul di bawah status selama AI bekerja
# (seperti Claude CLI) — biar waktu menunggu tetap informatif.
#
# ATURAN ISI: tiap tips WAJIB menggambarkan bagas-ai yang SEKARANG. Tips yang
# menjanjikan hal usang lebih merugikan daripada tak ada tips sama sekali —
# pengguna mencoba, gagal, lalu berhenti percaya pada seluruh barisan ini. Dua
# yang sudah pernah basi dan kini diperbaiki: "/effort mengatur kedalaman
# berpikir" (sejak semua model lewat browser, /effort MENGKLIK pemilih mode di
# situsnya, bukan mengirim parameter API) dan "naik kelas otomatis" (mekanisme
# itu ikut terhapus bersama model ber-API-key; yang tersisa penjaga anti-macet).
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
    "/effort memilih varian model & mode berpikir langsung di situs modelnya",
    "kalau model mengulang langkah yang sama, bagas-ai menyetopnya lalu cari jalan lain",
    "/web merapikan chat yang menumpuk di situs model · sekalian logout kalau perlu",
    "/bot menyalakan kontrol lewat Telegram — perintahkan bagas-ai dari HP",
    "/scripts menyimpan perintah panjang jadi satu nama pendek",
    "/live mengalihkan tampilan mengalir ↔ klasik bila terminalmu bermasalah",
)


class TurnView:
    """Tampilan SATU GILIRAN yang MENGALIR seperti terminal biasa.

    SEMUA konten (narasi, langkah, diff, jawaban) dicetak STATIS ke scrollback
    begitu tersedia — tidak ada yang dirender di region tetap yang
    menimpa/menutupi apa pun. Yang hidup (rich.Live) cuma empat baris di paling
    bawah: spinner status, tips, KOTAK CHAT, lalu BAR STATUS. Tingginya kecil &
    tetap (tiap baris _oneline anti-wrap), jadi ia tak pernah lebih tinggi dari
    layar dan tak pernah menutupi ketikan pengguna maupun diff — akar bug
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
        self.disp = 0.0
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
                out.append(Padding(Text("🤖 bagas-ai", style="bold #fc9018"),
                                   (1, 0, 0, 2)))
                self._said = True
            out.append(Padding(_md(text.strip()),
                               (0 if baru_bicara else 1, 3, 0, 3)))
            self.commit(out)

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
        out = [_KOSONG, _oneline(Text.from_markup(
            f"  {icon} [#f2e3cc]{phase}[/]  [white]{_esc(label)}[/]"))]
        lines = rec.get("_lines") or []
        if lines:
            out.extend(_pratinjau_hasil(lines, gagal=failed))
        return out

    def __rich__(self):
        """Region live, dari atas ke bawah: kalimat yang sedang ditulis AI ·
        spinner + tips · KOTAK CHAT · BAR STATUS.

        Dua yang terakhir urutannya HARAM ditukar atau dilewati: kotak chat
        menempel persis di atas bar status, sama seperti saat idle. Itulah yang
        membuat tempat mengetik terasa satu benda yang tak pernah pindah —
        segala yang hidup (spinner, tips, kalimat AI) tumbuh ke ATAS, bukan
        menyelip di antara keduanya.

        Semua baris _oneline anti-wrap -> tinggi kecil & stabil, selalu muat di
        layar, tak pernah menimpa konten yang sudah tercetak. Saat done, region
        dikosongkan: semuanya sudah berada di scrollback."""
        if self.done:
            return Text("")
        rows = []
        footer = self._footer()
        if isinstance(footer, Group):
            rows.extend(footer.renderables)
        else:
            rows.append(footer)
        # Satu baris kosong di atas & di bawah kotak chat — lihat _KOSONG.
        rows.append(_KOSONG)
        rows.extend(self._kotak_ketikan())
        rows.append(_bar_status(self.agent, self.total()))
        return Group(*rows)

    def _footer(self):
        el = time.time() - self.start
        frame = self.FRAMES[int(el * 10) % len(self.FRAMES)]
        now = time.time()
        if self.padat is not None:
            return _baris_padat(*self.padat, el)
        if self.cancelling:
            return _oneline(Text.from_markup(
                f"  [bold #f0603c]{frame}[/] [#f0603c]membatalkan — "
                f"menunggu langkah aman berhenti[/]   [dim italic]Ctrl+C lagi = paksa[/]"))
        if now < self.retry_until:
            left = self.retry_until - now
            return _oneline(Text.from_markup(
                f"  [bold #f7d488]{frame}[/] [#f7d488]layanan sibuk — menunggu lalu "
                f"melanjutkan[/] [bold #fca830]{left:.0f}s[/]   [dim italic]Ctrl+C batal[/]"))
        target = float(self.agent.tokens_live)
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
            extra = f"   [dim]·[/]   [#ffd9a0]🔧 {self.tool}[/]{lbl}"
        # Segmen "◇ effort" DIHAPUS: sejak semua model lewat browser,
        # agent.effort selalu None (mesin effort ala API ikut terhapus bersama
        # model ber-API-key), jadi ia tak pernah tampil — cuma menyisakan cabang
        # mati yang menyesatkan pembaca kode.
        status = _oneline(Text.from_markup(
            f"  [bold #fcc048]{frame}[/] [#fcc048]{self.phase}[/]   [dim]·[/]   "
            f"[#fc9018]{_fmt_elapsed(el)}[/]   [dim]·[/]   [#f7d488]⚡ {tok}[/] "
            f"[dim]token[/]{extra}"
            f"   [dim italic]Ctrl+C batal[/]"))
        rows = [status]
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
def _banner(agent: Agent, resumed: bool) -> Panel:
    spec = agent.model_spec
    # Seluruh model berbasis browser, jadi penanda jenis lama (reasoning /
    # multimodal / chat) tak lagi membedakan apa pun. Yang berguna sekarang:
    # menegaskan bahwa model ini berjalan lewat browser.
    kind = "🌐 via browser"
    # Kolom label rata kanan (abu) + nilai berwarna -> sejajar & profesional.
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="#a89078", min_width=7)
    grid.add_column(overflow="fold")
    eff = (f"   [#a89078]·[/]   [#ffd9a0]◇ {agent.effort}[/]"
           if agent.effort else "")
    tag = "dilanjutkan" if resumed else "sesi baru"
    grid.add_row("Model", f"[bold #fc9018]{spec.label}[/]   [dim]{kind}[/]{eff}")
    grid.add_row("Folder", f"[#9fc93c]{config.PROJECT_ROOT}[/]")
    grid.add_row("Sesi", f"[#f7d488]{agent.session.id}[/]   [dim]· {tag}[/]")
    # Mode lewati-izin TIDAK boleh senyap: selama ini aktif, bagas-ai boleh
    # menulis & menghapus di folder mana pun tanpa satu pun konfirmasi, jadi
    # keadaannya harus terbaca sekali lihat.
    if permissions.skip_aktif():
        grid.add_row("Izin", "[bold #f0603c]⚠ --skip-permissions[/]   "
                             "[dim]akses ke SEMUA folder tanpa konfirmasi[/]")

    head = Text.assemble(
        ("● ", "bold #9fc93c"), ("siap", "bold #9fc93c"),
        ("   dimana pun, dari terminal ini", "dim italic"),
    )
    hint = Text.from_markup(
        "[dim]ketik pesan untuk mengobrol[/dim]   "
        "[#ffb861]/menu[/] [dim]menu[/dim]   "
        "[#ffb861]/model[/] [dim]ganti model[/dim]   "
        "[#f0603c]/exit[/] [dim]keluar[/dim]"
    )
    body = Group(head, Text(), grid, Rule(style="#3a2a1a"), hint)
    return Panel(body, border_style="#fcc048", box=box.ROUNDED, padding=(1, 2),
                 title="[bold #fcc048]⬢ bagas-ai[/]", title_align="left")


# ---------------------------------------------------------------------------
# Loop utama
# ---------------------------------------------------------------------------
def main(resume: bool = False) -> None:
    console.clear()
    show_logo()          # tampil segera setelah preload -> pengguna tahu app hidup
    console.print()

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
        console.print(
            "  [yellow]⚠ peta proyek dilewati saat start[/] "
            f"[dim](instalasi tampaknya belum tuntas: {type(_prime_exc).__name__}). "
            "Tutup bagas-ai lalu reinstall/`bagasai update` bila ini berulang.)[/]")

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
    # Pemanasan impor `openai` DIHAPUS bersama klien API — pustaka itu tak lagi
    # dipakai sama sekali. (Padanannya untuk jalur browser sudah ada di tempat
    # lain: sesi Playwright dihidupkan sekali lalu dipertahankan antar giliran.)
    pout(_banner(agent, resumed), bottom=0)
    if resumed:
        console.print(Padding(Rule("[dim]percakapan sebelumnya[/dim]",
                                    style="#3a2a1a"), (1, 0, 0, 0)))
        for m in agent.memory.messages:
            role, content = m.get("role"), (m.get("content") or "")
            if role == "user":
                # WAJIB di-escape: teks pengguna yang memuat '[i]' / '[red]'
                # (lazim di kode, mis. arr[i]) akan ditafsirkan rich sebagai
                # markup — teks berubah gaya & kurungnya hilang saat replay.
                console.print(f"\n  [bold #fcc048]❯[/] [bold #ffffff]{_esc(content)}[/]")
            elif role == "assistant" and content:
                console.print("\n  [bold #fc9018]🤖 bagas-ai[/]")
                console.print(Padding(_md(content), (1, 3, 0, 3)))
        console.print(Rule("[dim]lanjut di bawah[/dim]", style="#3a2a1a"))
    if os_status in ("added", "updated"):
        verb = "terdeteksi & disimpan" if os_status == "added" else "diperbarui"
        pout(f"[dim]🖥  OS {verb}: {osinfo.summary()} — perintah terminal akan "
             f"disesuaikan.[/dim]", bottom=0)
    # Peta proyek: dari cache instan (disegarkan di latar), atau sedang dibangun
    # pertama kali di latar — dua-duanya TANPA menunda prompt.
    _pn = _primed_map.count("\n- ")
    if _pn:
        pout(f"[dim]🗺  peta proyek siap (~{_pn} file) — disegarkan di latar; "
             f"ketik [/][#ffb861]/scan[/][dim] untuk paksa pindai ulang.[/]",
             bottom=0)
    else:
        pout("[dim]🗺  peta proyek dibangun di latar — langsung ngetik aja, "
             "tak perlu menunggu.[/dim]", bottom=0)
    _update_notice()  # info bila versi usang (dari cache) + cek ulang di latar
    console.print()

    live_holder: dict = {"live": None}
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
        """Angka "🔋 total" di bar status — sama persis saat idle & saat sibuk."""
        return grand["base"] + agent.tokens_session.total

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
    # (langkah/diff/jawaban statis di scrollback); False = tampilan klasik.
    tui_mode = {"on": True}

    def _step_label(name: str, args: dict) -> str:
        a = args if isinstance(args, dict) else {}
        if name == "run_command":
            return a.get("command", "") or "perintah"
        if name == "run_python":
            return "kode Python"
        if name == "run_script":
            return f"skrip {a.get('name', '')}"
        if name == "read_file":
            return a.get("path", "")
        if name == "list_dir":
            return a.get("path", ".") or "."
        if name == "web_search":
            return a.get("query", "")
        if name == "write_file":
            return a.get("path", "")
        if name == "delete_file":
            return a.get("path", "")
        if name == "save_script":
            return a.get("name", "")
        if name == "remember":
            return a.get("fact", "") or "fakta"
        # Tool lain: tunjukkan TARGETNYA (path/url/kueri) alih-alih nama tool
        # mentah — "mengedit src/app.py" jauh lebih informatif daripada
        # "bekerja edit_file". Urutan kunci dipilih dari argumen paling khas.
        for k in ("path", "source", "dest_path", "url", "query", "pattern",
                  "bg_id"):
            v = a.get(k)
            if isinstance(v, str) and v:
                return v
        return name

    # Saat prompt pilihan (ask_user) aktif, POLLER keyboard di loop giliran
    # (msvcrt) HARUS berhenti membaca — kalau tidak, ketikan user DICURI poller
    # dan dropdown inquirer rusak (keduanya membaca console yang sama).
    input_paused = {"on": False}
    # Event Telegram yang datang saat console "dipinjam" menu — dicetak nanti.
    _tg_pending: list[tuple[str, str]] = []

    def _with_console(fn, *a, **k):
        """Jalankan aksi ber-dropdown (inquirer) dengan console dipinjam penuh:
        poller input berhenti & pesan thread lain (Telegram) ditahan dulu —
        mencegah menu rusak oleh cetakan yang menyela."""
        input_paused["on"] = True
        try:
            return fn(*a, **k)
        finally:
            input_paused["on"] = False
            _tg_flush()

    def choice_handler(question: str, options: list[str], multiple: bool) -> str:
        """Pertanyaan dari dalam giliran: klarifikasi ask_user, dan permintaan
        IZIN akses folder luar (permissions.py).

        Pertanyaan & jawabannya tak dicetak terpisah lagi: menu bagas-ai sudah
        menampilkan pertanyaannya di dalam kotak lalu meninggalkan satu baris
        ringkasan "✓ pertanyaan · jawaban" sesudah kotaknya hilang."""
        input_paused["on"] = True
        live = live_holder.get("live")
        if live:
            live.stop()
        try:
            answer = _tanya_pilihan(question, list(options), bool(multiple))
        except (KeyboardInterrupt, EOFError):
            answer = "(dibatalkan)"
        finally:
            input_paused["on"] = False
            _tg_flush()
        if live:
            live.start()
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
        if name in _TOOL_DIFF and p:
            old, new, exists = _isi_sebelum_sesudah(name, p, args)
            # old == new pada file yang sudah ada = tulisan itu akan DITOLAK
            # tool-nya (potongan/penyusutan drastis) atau memang tanpa efek —
            # jangan cetak header diff yang menyesatkan.
            if not (exists and old == new):
                _print_diff(p, old, new, is_new=not exists)
        elif name == "delete_file" and p:
            full = config.PROJECT_ROOT / p
            content = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
            _print_delete(p, content)

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
                    cancel_event=cancel_event,
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        def render():
            return Text.from_markup(
                f"  [#ffb861]◐[/] [dim]{_esc(state['status'])}[/]")

        wt = threading.Thread(target=worker, daemon=True)
        interrupted = False
        try:
            with Live(render(), console=console, refresh_per_second=8,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    try:
                        live.update(render())
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
            _pick_web_chat(connectors.get_connector(spec.connector))
            if result["login"]:
                console.print(
                    f"  [#9fc93c]✓ login berhasil — terhubung ke "
                    f"[bold]{_esc(spec.label)}[/bold]. Jendela diminimalkan; "
                    f"chat & jawaban di terminal ini.[/]\n")
            else:
                console.print(
                    f"  [#9fc93c]✓ terhubung — sesi login [bold]"
                    f"{_esc(spec.label)}[/bold] masih aktif, langsung ke chat.[/]\n")
            return
        err = result["error"]
        why = ("dibatalkan" if interrupted or isinstance(err, llm.Cancelled)
               else f"gagal: {err}")
        console.print(f"  [yellow]⚠ koneksi {_esc(spec.label)} {_esc(str(why))}[/]")
        _revert_model(prev_model_id)

    def _pick_web_chat(conn) -> None:
        """Menu PILIH SESI di AI web setelah model web dipilih.

        Melanjutkan percakapan lama berarti konteks proyek yang sudah dikirim di
        sana tetap dipakai — AI web tak perlu 'membaca ulang' proyek dari nol
        (berguna untuk --resume). Satu sesi terminal terikat ke satu chat."""
        if not conn.supports_resume():
            return
        rows = conn.own_chats()
        linked = getattr(agent, "_web_chat_id", "")
        if not rows and not linked:
            return  # belum ada chat lama -> langsung chat baru saja

        def _when(ts) -> str:
            try:
                return time.strftime("%d/%m %H:%M", time.localtime(float(ts)))
            except (TypeError, ValueError):
                return ""

        choices = [Choice("__new__", "✨ Mulai percakapan BARU di web")]
        for r in rows[:15]:
            mark = "  ← terpakai sesi ini" if r.get("id") == linked else ""
            title = (r.get("title") or "(tanpa judul)")[:52]
            choices.append(Choice(r["id"], f"{title:<54}{_when(r.get('ts'))}{mark}"))
        try:
            sel = inquirer.select(
                message="Lanjutkan percakapan web yang mana?",
                choices=choices, pointer="❯", default=linked or "__new__",
                long_instruction="Melanjutkan chat lama = konteks proyek tak perlu "
                                 "dikirim ulang.",
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return
        if sel == "__new__":
            agent.start_new_web_chat()
            console.print("  [dim]→ percakapan web BARU akan dibuat saat kamu "
                          "mengirim pesan pertama.[/dim]")
            return
        agent.use_web_chat(sel)
        title = next((r.get("title") for r in rows if r.get("id") == sel), sel)
        console.print(f"  [#9fc93c]✓ melanjutkan:[/] [bold]{_esc(str(title))}[/] "
                      f"[dim]— konteks proyek sudah ada di percakapan itu.[/]")

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
            """Bekukan konten ke riwayat terminal (tercetak DI ATAS region live).

            SEKALI print untuk seluruh batch (atomik): dicetak satu-satu memberi
            celah bagi refresh live menyela di antara dua baris."""
            if not cbs_alive["on"]:
                return
            if renderables:
                console.print(Group(*renderables))

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
            if name in _TOOL_DIFF and p:
                # WAJIB lewat _isi_sebelum_sesudah: untuk edit_file/append_file
                # isi barunya BUKAN args["content"] (edit_file pakai old_text/
                # new_text). Menyalin content mentah membuat `new` kosong pada
                # edit_file, sehingga diff menampilkan SELURUH file sebagai
                # terhapus — seakan kode dihapus lalu ditulis ulang.
                old, new, exists = _isi_sebelum_sesudah(name, p, args)
                # old == new pada file yang sudah ada = akan DITOLAK / tanpa
                # efek — jangan cetak header diff yang menyesatkan.
                if not (exists and old == new):
                    _print_diff(p, old, new, is_new=not exists)
            elif name == "delete_file" and p:
                full = config.PROJECT_ROOT / p
                content = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
                _print_delete(p, content)

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
            """Status connector web (menyiapkan sesi / berpikir / menjawab)."""
            if cbs_alive["on"]:
                view.note_phase(_web_phase(msg))

        def _on_tim(nama: list[str]) -> None:
            """Rekan satu tim yang ikut meninjau langkah barusan (lihat tim.py).

            Ditampilkan sebagai satu baris redup, bukan panel: ini peristiwa
            latar yang menemani langkah — bukan hasil yang perlu direnungkan.
            Tanpa baris ini, sudut pandang tambahan itu bekerja tanpa jejak dan
            pengguna tak punya cara tahu siapa yang sedang menemani."""
            if not cbs_alive["on"] or not nama:
                return
            _commit([_oneline(Text.from_markup(
                f"  [dim]‧ ikut meninjau: {_esc(', '.join(nama))}[/dim]"))])

        def _on_notice(msg: str) -> None:
            """Kabar dari mesin giliran: pesan susulan disisipkan, naik kelas,
            atau tindakan anti-macet.

            Labelnya dipilih dari ISI pesannya. Penyisipan diperiksa LEBIH DULU
            dan tanpa embel-embel "konteks dipertahankan": kalimat itu benar
            untuk pemulihan anti-macet, tapi jadi membingungkan pada peristiwa
            yang sama sekali bukan pemulihan."""
            if "disisipkan" in msg:
                _commit([Text.from_markup(
                    f"  [#fc9018]✉ {_esc(msg)}[/] "
                    f"[dim]— bagas-ai yang menentukan urutannya[/]")])
                return
            if "langkah" in msg and "batas" in msg:
                # Batas langkah bukan kegagalan maupun pemulihan — ia keputusan
                # yang bisa ditindaklanjuti pengguna, jadi ditandai tersendiri
                # supaya tak terbaca sebagai error.
                _commit([Text.from_markup(f"  [#f7d488]⏱ {_esc(msg)}[/]")])
                return
            label = ("⚡ naik kelas otomatis:" if "→" in msg
                     else "🛟 anti-macet:")
            _commit([Text.from_markup(
                f"  [#f7d488]{label}[/] [dim]{_esc(msg)} "
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
                # yang sebenarnya ingin ditunjukkan pengguna.
                return [_tempelan.simpanan().kembangkan(t) for t in diambil]

        _padat = _jeda_padat(view)

        def worker() -> None:
            try:
                result["answer"] = agent.run(
                    text, on_tool=_on_tool, on_message=_on_msg,
                    on_retry=_on_retry, cancel_event=cancel_event,
                    on_tool_result=_on_result, on_notice=_on_notice,
                    on_status=_on_status, ambil_sisipan=_ambil_sisipan,
                    on_tim=_on_tim, on_padat=_padat,
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
            """Kirim ketikan + posisi kursor ke kotak chat di footer."""
            view.typing = typing_state["buf"]
            view.typing_pos = typing_state["pos"]

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
                    _commit([_oneline(Text.from_markup(
                        f"  [bold #fcc048]❯[/] [bold #ffffff]{_esc(teks)}[/]"))])
                    # Perintah menunggu; tanpa keterangan ini ia tampak
                    # "terkirim tapi tak terjadi apa-apa" sampai giliran usai.
                    if _perintah(teks):
                        _commit([_oneline(Text.from_markup(
                            f"  [dim]dijalankan setelah {_esc(agent.model_spec.label)} "
                            f"selesai menjawab[/dim]"))])
                return True
            if ch in ("\x08", "\x7f"):             # Backspace / Ctrl+Backspace
                # KEDUANYA hapus 1 HURUF. Arah byte-nya tak bisa dipercaya:
                # msvcrt klasik bilang polos='\x08' & Ctrl='\x7f', tapi ConPTY
                # (Windows Terminal) TERBUKTI mengirim '\x7f' untuk Backspace
                # POLOS (regresi v1.0.42 di jalur prompt_toolkit). Salah tebak
                # di sini berarti Backspace polos menghapus sekata — merusak.
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
            if ch >= " ":                          # karakter tercetak
                p = typing_state["pos"]
                buf = typing_state["buf"]
                typing_state["buf"] = buf[:p] + ch + buf[p:]
                typing_state["pos"] = p + 1
                _sinkron()
                return True
            return False                            # kontrol lain (^R/^C dsb.)

        try:
            # Jaring pengaman: kalau ada yang menggeser kursor sesudah gema
            # (menu, notifikasi bot, layar dibersihkan), region hidup tetap
            # mulai dari dasar layar. No-op bila kursornya memang sudah di sana.
            _ke_dasar_layar()
            with Live(view, console=console, refresh_per_second=12,
                      transient=False, vertical_overflow="visible") as live:
                live_holder["live"] = live
                worker_thread.start()
                while worker_thread.is_alive():
                    try:
                        if input_paused["on"]:
                            # ask_user sedang tampil -> JANGAN baca console;
                            # biarkan inquirer yang menerima seluruh ketikan.
                            worker_thread.join(timeout=0.1)
                        elif _msvcrt is not None:
                            if _msvcrt.kbhit():
                                ch = _msvcrt.getwch()
                                if ch == "\x03":              # Ctrl+C
                                    raise KeyboardInterrupt
                                else:
                                    _ketik(ch)
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
            live_holder["live"] = None
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
            console.print("\n  [yellow]◼ dibatalkan[/yellow]\n")
        # Cabang khusus rate-limit API DIHAPUS: batas pemakaian kini datang dari
        # SITUS AI web, dan itu sudah ditangani lebih baik di core sebagai
        # WebLimitError/WebBusyError — lengkap dengan kapan bisa dipakai lagi
        # dan ulang-otomatis. Sisanya jatuh ke cabang error umum di bawah.
        elif err is not None:
            console.print(f"\n  [red]✖ error:[/red] {err}\n")
        else:
            # Jawaban dicetak SEBAGAI RIWAYAT biasa (di luar region live) supaya
            # sepanjang apa pun tak bikin kedip dan terminal tetap bisa di-scroll.
            # Ini juga menyelamatkan jawaban yang SELESAI tepat saat Ctrl+C ditekan
            # (sudah tersimpan di memory — tampilkan, jangan dibuang).
            if ans:
                console.print()
                # Header bot cukup SEKALI per giliran — kalau narasi sudah
                # menampilkannya, jawaban akhir tak perlu header kedua.
                ada_header = not view._said
                if ada_header:
                    console.print("  [bold #fc9018]🤖 bagas-ai[/]")
                console.print(Padding(_md(ans),
                                      (0 if ada_header else 1, 3, 0, 3)))
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
                console.print(Padding(Text.from_markup(
                    "[dim]" + " · ".join(seg) + "[/]"), (1, 3, 0, 3)))
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
                    on_status=lambda m: status_obj.note_phase(_web_phase(m)),
                    on_padat=_jeda_padat(status_obj),
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        worker_thread = threading.Thread(target=worker, daemon=True)
        interrupted = False
        forced = False
        try:
            _ke_dasar_layar()          # sama seperti mode mengalir — lihat di sana
            with Live(status_obj, console=console, refresh_per_second=12,
                      transient=True) as live:
                live_holder["live"] = live
                worker_thread.start()
                while worker_thread.is_alive():
                    try:
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
            live_holder["live"] = None
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
                     if s["name"] in ("write_file", "delete_file"))
        n_fail = sum(1 for s in steps.values() if s["failed"])
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
        console.print(Padding(Text.from_markup(f"[dim]{body}[/dim]"),
                              (1, 3, 0, 3)))

    # --- aksi menu (inquirer) ---
    def pick_model() -> str | None:
        """Menu pilih model. Return ID model SEBELUMNYA bila yang dipilih adalah
        connector web (pemanggil lalu menjalankan _connect_web), selain itu None."""
        def _describe(spec) -> str:
            # Satu baris: nama (rata) + badge kemampuan + SARAN "cocok untuk apa".
            # Semua model kini web, jadi lencana reasoning/multimodal tak lagi
            # membedakan apa pun — cukup satu penanda bahwa ini lewat browser.
            badge = " 🌐" if spec.is_web else "  "
            if spec.ditunda:
                return f"{spec.label:<28}{badge}  —  ⏸ ditunda sementara"
            note = f"  —  {spec.note}" if spec.note else ""
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
            console.print(f"[green]✓ Model: {agent.set_model(sel)}[/green] "
                          f"[dim]({agent.model})[/dim]")
            if agent.model_spec.is_web:
                return prev
        except (KeyboardInterrupt, EOFError):
            pass
        return None

    def pick_effort() -> None:
        """/effort — untuk model web berarti MENGKLIK tombol mode berpikir di UI
        situsnya, bukan mengirim parameter API.

        Cabang model ber-API-key (menu effort dari reasoning_style, set_effort)
        DIHAPUS bersama katalog NVIDIA: seluruh model kini web, jadi satu-satunya
        jalur yang tersisa adalah pick_web_option."""
        pick_web_option()

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
            with Live(_oneline(Text()), console=console, refresh_per_second=10,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    live.update(_oneline(Text.from_markup(
                        f"  [#fcc048]{frame}[/] [dim]{_esc(state['msg'])}[/]")))
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
        with Live(_KOSONG, console=console, refresh_per_second=12,
                  transient=True) as live:
            wt.start()
            while wt.is_alive():
                p = tampil.padat
                live.update(_baris_padat(*p, time.time() - mulai) if p
                            else Text("  ⏸ menyiapkan…", style="dim"))
                wt.join(timeout=0.05)
        console.print("  [#9fc93c]✓ riwayat tersimpan[/] [dim]— chat di situs "
                      "tak disentuh.[/]\n")
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
            return Text.from_markup(
                f"  [#ffb861]◐[/] [dim]{_esc(state['pesan'])}[/]")

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
            with Live(render(), console=console, refresh_per_second=8,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    live.update(render())
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
        console.print(f"\n  [#fcc048]🎙 ❯[/] [bold #ffffff]{_esc(teks)}[/]")
        # Kotak idle sedang menunggu -> teksnya dimasukkan ke sana (kotak itu
        # memblokir thread utama; antrean saja takkan pernah terbaca). Kalau
        # giliran sedang berjalan, ia masuk antrean seperti ketikan biasa dan
        # disisipkan di batas langkah berikutnya.
        kotak_chat.galat_kirim = ""
        if kotak_chat.kirim_dari_luar(teks):
            return
        with antre_lock:
            prompt_queue.append(teks)
        # DUA keadaan yang sangat berbeda, dan dulu keduanya diberi kalimat yang
        # sama — padahal yang kedua berarti perintahnya MENGENDAP sampai
        # pengguna menekan Enter, hal yang justru tak ia lakukan saat bicara.
        if kotak_chat.galat_kirim:
            console.print(
                f"  [yellow]⚠ perintah suara tak bisa masuk ke kotak "
                f"({_esc(kotak_chat.galat_kirim)}).[/yellow]\n"
                "  [dim]Ia menunggu di antrean — tekan Enter untuk "
                "menjalankannya.[/dim]")
        else:
            console.print("  [dim]— disisipkan ke giliran yang sedang "
                          "berjalan[/dim]")

    def _voice_kabar(pesan: str) -> None:
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
            p = _dengar.Pendengar(_voice_masuk, _voice_kabar)
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
                            f"  [dim]🎙 derau ruangan {p.derau:.1f} → ambang "
                            f"bicara {p.ambang:.0f}[/dim]")
                        return
                    time.sleep(0.1)

            threading.Thread(target=_lapor_ambang, daemon=True).start()
            console.print(
                f"  [#9fc93c]● mikrofon AKTIF[/] [dim]— {_esc(nama)}[/]\n"
                "  [dim]sebut[/] [#fcc048]\"bagas ai\"[/] [dim]lalu ucapkan "
                "perintahmu; berhenti bicara[/] "
                f"[#fcc048]{_dengar.JEDA_SELESAI:.0f} detik[/] [dim]sudah "
                "menutupnya — tak ada kata penutup. Contoh:[/]\n"
                "  [dim]  \"bagas ai tolong buka main.py\"  → (diam) → "
                "terkirim[/]\n"
                "  [dim]begitu namaku terdengar ada ketukan pendek, dan bar "
                "status di bawah berubah jadi[/] [#fcc048]● merekam[/][dim]. "
                "Meneruskan kalimat sebelum jeda itu habis mengulang "
                "hitungannya. Ucapkan[/] [#fcc048]\"batalkan\"[/] [dim]untuk "
                "membuang rekaman yang sedang berjalan. Satu perintah maksimal "
                f"{_dengar.MAKS_REKAM:.0f} detik.[/dim]\n")
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
            console.print(
                f"  [dim]tingkat suara tertinggi: {puncak:.0f}   "
                f"ambang bicara: ±{_dengar._LANTAI:.0f} (mengikuti derau "
                f"ruangan)[/dim]"
                + ("\n  [yellow]mikrofonnya nyaris tak menangkap apa-apa[/]"
                   "[dim] — periksa mikrofon yang dipilih Windows, atau "
                   "naikkan volumenya di Pengaturan Suara.[/dim]"
                   if puncak < _dengar._LANTAI else ""))
            if teks:
                console.print(f"  [#9fc93c]✓ terdengar:[/] "
                              f"[#f2e3cc]{_esc(teks)}[/]\n")
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
        if not ok:
            console.print(f"  [yellow]⚠ {_esc(alasan)}[/yellow]")
        console.print("  [dim]cara pakai:[/] sebut [#fcc048]\"bagas ai\"[/] "
                      "lalu perintahnya, lalu [#fcc048]diam 2 detik[/] "
                      "[dim]· buang dengan[/] [#fcc048]\"batalkan\"[/]")
        console.print("  [dim]yang dikirim hanya yang terucap SESUDAH "
                      "namaku.[/dim]")
        console.print("  [dim]/voice on · /voice off · /voice tes[/dim]\n")

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
            warna = "#9fc93c" if nyala else "#f7d488"
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
            f"[{'#9fc93c' if aktif else '#f7d488'}]{'aktif' if aktif else 'mati'}[/]"
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
                "\n  [dim]Suara bawaan Windows[/] [#f7d488]tidak dipakai[/] "
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
            warna = "#9fc93c" if nyala else "#f7d488"
            console.print(f"  [{warna}]✓ tim spesialis {kata}[/]\n")
            return

        aktif = bool(_prefs.load().get("tim", True))
        console.print()
        console.print(
            f"  [bold #fcc048]Tim {len(_tim.ANGGOTA)} spesialis[/] "
            f"[dim]— bekerja pasif, dibangunkan oleh isi pekerjaan[/]")
        console.print(
            f"  [dim]status: [/]"
            f"[{'#9fc93c' if aktif else '#f7d488'}]{'aktif' if aktif else 'mati'}[/]"
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
            with Live(_oneline(Text()), console=console, refresh_per_second=10,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    kerja = ("mematikan mode" if sel == MATIKAN
                             else f"menekan '{_esc(sel)}'")
                    live.update(_oneline(Text.from_markup(
                        f"  [#fcc048]{frame}[/] [dim]{kerja} di "
                        f"{_esc(spec.label)}…[/dim]")))
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
            with Live(_oneline(Text()), console=console, refresh_per_second=10,
                      transient=True) as live:
                wt.start()
                while wt.is_alive():
                    frame = FRAMES[int(time.time() * 10) % len(FRAMES)]
                    live.update(_oneline(Text.from_markup(
                        f"  [#fcc048]{frame}[/] [dim]{_esc(msg)}[/]")))
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
            f"[dim]chat tercatat dibuat bagas-ai:[/] [#ffb861]{len(own)}[/]\n")

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
        c = "#ffb861"
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
            title="[bold #fcc048]❔ Bantuan[/]", title_align="left",
            border_style="#fcc048", box=box.ROUNDED, padding=(1, 2)))

    def _baris_versi() -> None:
        """Versi dari SEMUA sumber, bukan cuma GitHub — yang menentukan apa yang
        benar-benar jalan adalah salinan terpasang, bukan isi repo."""
        try:
            v = updater.versions()
        except Exception:  # noqa: BLE001
            return
        t = Text("  ")
        t.append("terpasang ", style="dim")
        t.append(v.get("terpasang") or "?", style="#fc9018")
        if v.get("repo"):
            t.append("   repo ", style="dim")
            t.append(v["repo"], style="#9fc93c")
        if v.get("remote") and v["remote"] != v.get("repo"):
            t.append("   remote ", style="dim")
            t.append(v["remote"], style="#f7d488")
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
                        "tertinggal.\n\n", style="bold #f7d488")
            body.append("Yang benar-benar dijalankan adalah salinan di "
                        "site-packages, dan isinya berbeda dari repo:\n\n",
                        style="dim")
            for b in (res.get("beda") or [])[:10]:
                body.append("  • ", style="#fc9018")
                body.append(b + "\n")
            pout(Panel(body, title="[bold #fcc048]🔄 Pemasangan tertinggal[/]",
                       title_align="left", border_style="#f7d488",
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
                        style="bold #f7d488")
            body.append(f"Sumber : {res.get('repo_url','')}\n", style="dim")
            body.append(f"Branch : {res.get('branch','')}", style="dim")
            pout(Panel(body, title="[bold #fcc048]🔄 Siapkan pembaruan[/]",
                       title_align="left", border_style="#fcc048",
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
            body.append(f"{n} pembaruan tersedia  ", style="bold #f7d488")
            body.append(f"({res.get('local','')} → {res.get('remote','')})\n\n",
                        style="dim")
            if log:
                for line in log.splitlines():
                    body.append("  • ", style="#fc9018")
                    body.append(line + "\n")
            pout(Panel(body, title="[bold #fcc048]🔄 Pembaruan bagas-ai[/]",
                       title_align="left", border_style="#fcc048",
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

        note = f"\n  [#f7d488]ℹ {_esc(out['note'])}[/]" if out.get("note") else ""
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
                   "\n  [dim]jalankan ulang[/dim] [#ffb861]bagas-ai[/] "
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
                "Ketik [#ffb861]/add-dir[/] untuk menambah (muncul box input "
                "path), atau langsung [#ffb861]/add-dir <path>[/].\n"
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
                _connect_web(prev)
        elif action == "effort":
            _with_console(pick_effort)
        elif action == "web":
            _with_console(manage_web_sessions)
        elif action == "dirs":
            show_dirs()
        elif action == "delete":
            _with_console(delete_sessions)
        elif action == "new":
            _save_total()  # persist kontribusi sesi lama ke total global
            session = Session.create()
            agent = Agent(session=session)
            grand["base"] = prefs.get_total_tokens()  # sesi baru mulai dari total
            console.clear()
            show_logo()
            console.print()
            pout(_banner(agent, False), bottom=0)
            console.print()
        elif action == "reset":
            agent.reset()
            console.print("[dim](riwayat dikosongkan)[/dim]")
        elif action == "clear":
            console.clear()
            show_logo()
            console.print()
            pout(_banner(agent, False), bottom=0)
            console.print()
        elif action == "memory":
            facts = longmem.all_facts()
            pout(Panel("\n".join(f"• {f}" for f in facts) or "[dim]kosong[/dim]",
                       title="[bold #9fc93c]🧠 Memory jangka panjang[/]",
                       title_align="left", border_style="#9fc93c",
                       box=box.ROUNDED, padding=(1, 2)))
        elif action == "scripts":
            items = scripts.index_list()
            txt = "\n".join(f"• [#fc9018]{it['name']}[/]: {it.get('description') or '-'}"
                            for it in items) or "[dim]belum ada[/dim]"
            pout(Panel(txt, title="[bold #fc9018]📜 Script memory[/]",
                       title_align="left", border_style="#fc9018",
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
        t = _esc(text or "")
        if kind == "in":
            console.print(f"\n  [#fc9018]📲 Telegram ▸[/] [#f2e3cc]{t}[/]")
        elif kind == "out":
            snip = t if len(t) <= 600 else t[:600] + "…"
            console.print(f"  [#9fc93c]  ↳ balasan:[/] [dim]{snip}[/]")
        elif kind == "perm":
            console.print(f"\n  [#f7d488]🔔 {t}[/]")
        elif kind == "error":
            console.print(f"  [red]📲 error:[/] {t}")
        else:
            console.print(f"  [dim]📲 {t}[/]")

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
            ok = svc.start(on_event=_tg_event)
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
                f"Atur izin: [/][#ffb861]/permissions-bot[/][dim].[/]\n")
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
            head = Text.from_markup(
                f"[bold #fc9018]🔐 Izin bot Telegram[/]\n"
                f"[dim]Diizinkan:[/] {allowed or '(belum ada)'}\n"
                f"[dim]Menunggu izin:[/] {len(pend)}")
            pout(Panel(head, border_style="#fc9018", box=box.ROUNDED, padding=(1, 2)))
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
        if simpanan.perlu_diringkas(data):
            event.current_buffer.insert_text(simpanan.simpan(data))
            return
        # Tempelan pendek disisipkan apa adanya. Baris-barunya diganti spasi:
        # kotak ini satu baris (multiline=False), jadi baris-baru di dalamnya
        # tak pernah terlihat sebagai baris baru — ia cuma jadi celah aneh di
        # tengah kalimat.
        event.current_buffer.insert_text(" ".join(data.split("\n")))

    @kb.add("c-d")
    def _eof_ketik(event):
        buf = event.current_buffer
        if buf.text:
            buf.delete()          # ada teks -> Ctrl+D = hapus 1 huruf di depan
        else:
            event.app.exit(exception=EOFError, style="class:exiting")

    # Gaya status bar: latar gelap hangat + aksen kuning-oranye per segmen.
    _pt_style = PTStyle.from_dict({
        "bottom-toolbar": "bg:#1a120b #f2e3cc noreverse",
        "garis": "#7a5c3a",      # tepi kotak chat
        "tanda": "bold #fcc048",  # "❯" di dalam kotak
        "sep": "#4a3826",
        "brand": "#fcc048 bold",
        "model": "#fc9018 bold",
        "eff": "#ffd9a0",
        "sesi": "#f7d488",
        "total": "#9fc93c",
        "cmd": "#ffb861",
        "exit": "#f0603c",
        "muted": "#a89078",
        # Menu autocomplete "/..." — selaras tema kuning-oranye.
        "completion-menu": "bg:#241a10 #f2e3cc",
        "completion-menu.completion": "bg:#241a10 #f2e3cc",
        "completion-menu.completion.current": "bg:#fcc048 #241a10 bold",
        "completion-menu.meta.completion": "bg:#1a120b #a89078",
        "completion-menu.meta.completion.current": "bg:#4a3826 #f2e3cc",
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
            "model": f"{sep}{kind} <model>{spec.label}</model>{eff}",
            "sesi": f"{sep}<sesi>⚡ {_fmt(s.total)}</sesi> <muted>sesi</muted>{mik}",
            "perintah": "<cmd>/menu</cmd> <muted>·</muted> ",
            "ctrlc": " <muted>atau ctrl+c</muted>",
            "exit": "<exit>/exit</exit>",
        }
        lebar = _lebar_kotak()
        # Aturan penyusutan dipakai BERSAMA dengan bar versi rich (_bar_status),
        # supaya bentuknya tak berubah saat giliran mulai/selesai.
        ada = _bagian_bar(lebar, lambda b: _panjang_tampak(bagian[b]) + 1)

        kiri = "".join(bagian[b] for b in ("merek", "model", "sesi") if b in ada)
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
        return HTML(kiri + " " * max(1, antara) + kanan)

    kotak_chat = KotakChat(status=status_bar, key_bindings=kb, style=_pt_style,
                           completer=SlashCompleter())

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
                    raw = kotak_chat.tanya(prefill, prefill_pos)
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
            # Kotaknya menghilang begitu Enter ditekan (erase_when_done), jadi
            # yang mengabadikan pesan ke riwayat adalah gema ini — bentuknya
            # sama persis dengan gema pesan yang diantrekan.
            #
            # Didorong ke dasar layar SEBELUM gemanya, bukan sesudah: baris
            # kosong pendorongnya lalu jatuh di petak bekas kotak yang barusan
            # dihapus, jadi tak ada celah yang terlihat di bawah gema.
            if raw.strip():
                _ke_dasar_layar()
                # PUTIH MURNI (#ffffff) & TEBAL, bukan `white` milik rich —
                # `white` dipetakan ke warna 7 palet terminal, yang di banyak
                # tema justru abu-abu. Pesan pengguna adalah penanda batas
                # antar-giliran: satu-satunya baris yang harus bisa ditemukan
                # seketika saat menggulung riwayat panjang, jadi ia diberi
                # kontras tertinggi di layar.
                console.print(f"  [bold #fcc048]❯[/] "
                              f"[bold #ffffff]{_esc(raw.strip())}[/]")
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
                        # Model connector web: CONNECT sekarang juga (login sekali
                        # bila belum pernah; sudah pernah -> langsung ke sesi chat).
                        if agent.model_spec.is_web:
                            _connect_web(prev_model)
                else:
                    prev_model = _with_console(pick_model)
                    if prev_model is not None:
                        _connect_web(prev_model)
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
                pick_web_mode()
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
            process(_tempelan.simpanan().kembangkan(text))
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

"""Konverter tema bagas-ai -> Textual CSS berbasis variabel.

CARA KERJA (penting, jangan diubah tanpa membaca ini):

Warna TIDAK ditulis langsung di CSS. CSS di sini memakai variabel
(``$t-aksen`` dst.) yang nilainya diinjeksi lewat
``BagasAIApp.get_css_variables()`` dari tema aktif. Untuk ganti tema kita
cukup memanggil ``app.refresh_css()``: Textual menyuntik ulang variabel lalu
me-parse ulang SELURUH stylesheet — semua aturan langsung bernilai warna
baru.

Versi lama menulis warna mentah ke ``App.CSS`` lalu menambahkannya sebagai
sumber stylesheet kedua saat ``/theme``. Aturan tema LAMA tetap ada di
stylesheet dan bertarung dengan yang baru — hasilnya ganti tema tampak TIDAK
berubah sama sekali.

CATATAN LAYOUT:

Header dan status bar di-dock pada Screen. Workspace di antaranya berisi
kolom percakapan dan sidebar. Panel footer mengalir di kolom percakapan,
sehingga input tidak dapat menimpa sidebar saat panel atau lebar berubah.
"""
from __future__ import annotations

from . import tema

# Kunci tema yang menjadi variabel CSS ($t-<kunci>).
KUNCI_VARIABEL = (
    "aksen", "aksen2", "aksen_terang", "tepi", "tepi_redup",
    "redup", "teks", "gema_bg", "gema_garis", "gema_teks",
    "bg_footer", "merek_footer", "model_footer", "sep_footer",
    "muted_footer", "cmd_footer", "exit_footer", "git_footer",
    "ubah_footer", "menu_bg", "menu_teks", "menu_aktif_bg",
    "menu_aktif_teks", "menu_meta_bg", "menu_meta_teks",
)

# Variabel tambahan (perhitungan) — nama -> (dasar, campuran, takaran)
_CAMPURAN = {
    # Sorotan dropdown: menu_bg ditarik 22% ke arah aksen. Baris sorotan
    # penuh aksen terlalu menyala ("gong"); ini jauh lebih tenang.
    "menu_sorot": ("menu_bg", "aksen", 0.22),
    # Hover terpisah dari tepi_redup: dulu hover meminjam warna BORDER,
    # jadi di tema terang teks gelap di atas abu #909090 cuma 3.5:1.
    # Campuran tipis ke aksen menjaga teks tetap >= 4.5 di semua tema.
    "menu_hover": ("menu_bg", "aksen", 0.10),
}


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def campur(c1: str, c2: str, t: float) -> str:
    """Campur dua warna hex (t=0 -> c1, t=1 -> c2)."""
    r1, g1, b1 = _rgb(c1)
    r2, g2, b2 = _rgb(c2)
    return "#%02x%02x%02x" % (
        round(r1 + (r2 - r1) * t),
        round(g1 + (g2 - g1) * t),
        round(b1 + (b2 - b1) * t),
    )


def variabel(theme_id: str | None = None) -> dict[str, str]:
    """Peta variabel CSS ``t-*`` dari tema aktif (atau tema tertentu)."""
    if theme_id is not None:
        src = tema.TEMA.get(theme_id, tema.TEMA.get("biru", {}))
        ambil = lambda k: src.get(k, "")  # noqa: E731
    else:
        ambil = tema.p

    hasil = {f"t-{k}": ambil(k) for k in KUNCI_VARIABEL}
    for nama, (dasar, camp, takaran) in _CAMPURAN.items():
        try:
            hasil[f"t-{nama}"] = campur(ambil(dasar), ambil(camp), takaran)
        except Exception:  # noqa: BLE001 — warna tema cacat
            hasil[f"t-{nama}"] = ambil(dasar)
    return hasil


def _terang(hex_warna: str) -> bool:
    """True bila hex = latar terang (luminance WCAG sederhana)."""
    try:
        h = str(hex_warna).lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) > 140
    except Exception:  # noqa: BLE001 — hex rusak: anggap gelap (aman)
        return False


def latar_terang(theme_id: str | None = None) -> bool:
    """True bila latar utama tema (gema_bg) terang — vslight dsb."""
    if theme_id is not None:
        src = tema.TEMA.get(theme_id, tema.TEMA.get("biru", {}))
        bg = src.get("gema_bg", "#121212")
    else:
        bg = tema.p("gema_bg")
    return _terang(bg)


def markdown_styles(theme_id: str | None = None) -> dict[str, str]:
    """Gaya Rich ``markdown.*`` untuk jawaban AI — paritas dengan CLI.

    CLI memakai Theme yang sama (lihat ``cli._MD_THEME``): heading/list/
    inline-code mengikuti palet zenitsu aktif, bukan warna default rich.
    Textual me-render Markdown lewat ``app.console``; cukup suntik dict ini
    ke console saat mount / ganti tema.

    Latar inline-code mengikuti permukaan tema: tema gelap memakai
    ``menu_meta_bg`` (senada panel menu), tema terang memakai slate netral.
    Dulu latarnya cokelat tetap ``#3a2a1a`` — di tema biru/dingin warnanya
    terlihat kotor dan tidak ikut /theme.
    """
    if theme_id is not None:
        src = tema.TEMA.get(theme_id, tema.TEMA.get("biru", {}))
        ambil = lambda k: src.get(k, "")  # noqa: E731
        terang = latar_terang(theme_id)
    else:
        ambil = tema.p
        terang = latar_terang()
    # Latar inline-code mengikuti permukaan tema, bukan cokelat warisan
    # Ember: di tema dingin (Lautan/Cupertino) cokelat terlihat kotor.
    bg_kode = "#eef1f5" if terang else ambil("menu_meta_bg")
    return {
        "markdown.h1": f"bold {ambil('aksen')}",
        "markdown.h1.border": ambil("aksen"),
        "markdown.h2": f"bold {ambil('aksen2')}",
        "markdown.h3": f"bold {ambil('aksen_terang')}",
        "markdown.h4": f"bold {ambil('aksen2')}",
        "markdown.h5": f"bold {ambil('aksen_terang')}",
        "markdown.h6": f"bold {ambil('aksen2')}",
        "markdown.item.bullet": f"bold {ambil('aksen')}",
        "markdown.item.number": f"bold {ambil('aksen2')}",
        "markdown.code": f"{ambil('aksen_terang')} on {bg_kode}",
        "markdown.link": f"{ambil('aksen2')} underline",
        "markdown.link_url": f"dim {ambil('aksen_terang')}",
        "markdown.block_quote": f"italic {ambil('aksen_terang')}",
        "markdown.block_quote_border": ambil("tepi"),
        "markdown.hr": ambil("tepi_redup"),
        "markdown.strong": f"bold {ambil('teks')}",
        "markdown.emph": f"italic {ambil('teks')}",
        "markdown.text": ambil("teks"),
    }


def generate_css(theme_id: str | None = None) -> str:
    """Hasilkan CSS Textual yang memakai variabel ``$t-*``.

    ``theme_id`` diabaikan (dipertahankan demi kompatibilitas tanda tangan);
    warna diambil saat runtime dari ``variabel()`` sehingga ganti tema cukup
    memanggil ``refresh_css()``.
    """
    return """\
Screen {
    background: $t-gema_bg;
    color: $t-teks;
    scrollbar-background: $t-gema_bg;
    scrollbar-background-hover: $t-gema_bg;
    scrollbar-background-active: $t-gema_bg;
    scrollbar-color: $t-tepi_redup;
    scrollbar-color-hover: $t-tepi;
    scrollbar-color-active: $t-aksen;
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 1;
}

/* ── Isi utama: logo (auto) + daftar pesan (sisa ruang) ─────────────── */

#logo {
    dock: top;
    layout: horizontal;
    width: 100%;
    height: 3;
    padding: 1 2 0 2;
    align-vertical: middle;
    background: $t-bg_footer;
    color: $t-aksen;
    border-bottom: solid $t-tepi_redup;
}

#titlebar-brand, #titlebar-actions {
    width: 1fr;
    height: 1;
}
#titlebar-lights { width: 8; height: 1; }
#logo-content {
    width: 1fr;
    height: 1;
    text-align: left;
}
#titlebar-chat {
    width: 12;
    height: 1;
    text-align: center;
    background: $t-menu_aktif_bg;
    color: $t-menu_aktif_teks;
}
#titlebar-actions { align-horizontal: right; }
#titlebar-actions Button {
    width: 3;
    min-width: 3;
    height: 1;
    min-height: 1;
    margin-left: 1;
    padding: 0;
    border: none;
    background: $t-menu_bg;
    color: $t-redup;
}
#titlebar-actions Button:hover { color: $t-teks; background: $t-menu_hover; }

#workspace { width: 100%; height: 1fr; }
#conversation { width: 1fr; height: 100%; }
#history { width: 100%; height: 1fr; }
#empty-state {
    position: absolute;
    width: 100%;
    height: 8;
    max-height: 100%;
    text-align: center;
    background: $t-gema_bg;
}

/* Kartu inspector mengikuti tema, termasuk tema terang. */
#sidebar {
    dock: none;
    background: $t-menu_meta_bg;
    overflow-y: auto;
    scrollbar-size-vertical: 1;
}

#plan-side, #system-panel, #sidebar #path-footer {
    background: $t-menu_bg;
    border: round $t-tepi_redup;
    padding: 0 1;
    margin-bottom: 1;
    height: auto;
}

#sidebar #path-footer:hover {
    border: round $t-tepi;
    background: $t-menu_hover;
}

#input-send {
    width: 3;
    min-width: 3;
    height: 1;
    min-height: 1;
    padding: 0;
    margin-left: 1;
    border: none;
    background: $t-menu_aktif_bg;
    color: $t-menu_aktif_teks;
    text-style: bold;
}

#input-send:hover, #input-send:focus {
    background: $t-aksen;
    color: $t-gema_bg;
    text-style: bold;
}

#input-more, #input-effort {
    width: 3;
    min-width: 3;
    height: 1;
    min-height: 1;
    border: none;
    padding: 0;
    background: $t-menu_bg;
    color: $t-redup;
}
#input-more { margin-right: 1; }
#input-effort { margin-left: 1; }
#input-more:hover, #input-effort:hover {
    color: $t-teks;
    background: $t-menu_hover;
}

#messages {
    width: 100%;
    height: 1fr;
    background: $t-gema_bg;
    color: $t-teks;
    /* Longgar: napas atas/bawah antar blok pesan + sisi kiri/kanan. */
    padding: 1 2;
    overflow-x: hidden;
    overflow-y: auto;
    /* ScrollBar membaca warna dari PARENT-nya, bukan dari Screen. Tanpa
       ini scrollbar memakai token bawaan Textual (track hitam, tebal 2
       sel). Dibuat 1 sel dan menyatu dengan kanvas. */
    scrollbar-size-vertical: 1;
    scrollbar-background: $t-gema_bg;
    scrollbar-background-hover: $t-gema_bg;
    scrollbar-background-active: $t-gema_bg;
    scrollbar-color: $t-tepi_redup;
    scrollbar-color-hover: $t-tepi;
    scrollbar-color-active: $t-tepi;
}

/* ── Footer di dalam kolom percakapan ────────────────────────────── */

#footer {
    width: 100%;
    height: auto;
    max-height: 75%;
    background: $t-gema_bg;
    /* Tanpa padding samping: footer harus berhenti TEPAT di divider
       sidebar. Indentasi teks diatur oleh padding dalam #input-row. */
    padding: 0;
}

#statusbar-sidebar-toggle {
    display: none;
    dock: left;
    width: 3;
    min-width: 3;
    margin: 0;
    height: 1;
    border: none;
    background: transparent;
    background-tint: transparent;
    border: none;
    padding: 0;
    color: $t-aksen;
}
#statusbar-sidebar-toggle { display: none; }
#sidebar-close { display: none; width: 100%; height: 1; border: none; background: transparent; background-tint: transparent; color: $t-aksen; }

/* Panel-panel bawah diberi margin-top: teks jawaban/prompt di atasnya
   tidak menempel rapat ke box thinking / loading / antrean. */
#plan, #image-preview, #thinking-block {
    width: 100%;
    height: auto;
    /* Kartu bantuan di atas composer: permukaan naik satu tingkat dari
       kanvas, sama seperti panel picker dan kotak input. */
    background: $t-menu_bg;
    padding: 1 1;
    margin-top: 1;
}

#plan {
    max-height: 12;
    /* Hairline, bukan garis tebal: pemisah ala macOS. */
    border-top: solid $t-tepi_redup;
}

#image-preview {
    max-height: 6;
    padding: 0 2;
    margin-top: 0;
    background: $t-gema_bg;
}

/* Header satu baris; isi memakai viewport kecil yang bisa digulir.
   Padding vertikal/margin membuat header tertutup boros beberapa baris. */
#thinking-block {
    padding: 0 1;
    margin-top: 0;
    border: none;
}

#progress {
    width: 100%;
    height: 1;
    max-height: 1;
    background: $t-gema_bg;
    color: $t-aksen;
    padding: 0 2;
    margin-top: 0;
}

/* ── Strip antrean: DI AREA TERMINAL (bukan footer) ──────────────────── */
/* Nempel di bawah jawaban terakhir; diredupkan sampai benar-benar      */
/* dijalankan, saat itu prompt pindah ke riwayat sebagai pesan normal.  */

#queue-strip {
    width: 100%;
    height: auto;
    max-height: 4;
    background: $t-gema_bg;
    /* Sejajarkan gema antrean dengan teks di #messages. */
    padding: 1 2;
}

/* ── Kotak input ────────────────────────────────────────────────────── */

#chatbox {
    width: 100%;
    height: auto;
    background: $t-gema_bg;
    /* Jarak antara riwayat/panel di atas dan kotak input. */
    margin-top: 1;
    /* Napas kiri/kanan: composer tidak menempel ke tepi terminal. */
    padding: 0 2 1 2;
}

#input-row {
    width: 100%;
    /* auto: tinggi mengikuti #chat-input yang tumbuh saat teks membungkus
       (maks 5 baris, diatur _sesuaikan_tinggi di chat_box.py). Border
       round menambah 2 baris sendiri. */
    height: auto;
    /* Composer adalah kartu terang di atas kanvas gelap — bukan area yang
       menyatu dengan riwayat. Ini yang membuatnya terasa seperti kotak
       pesan aplikasi desktop. */
    background: $t-menu_bg;
    /* Garis istirahat dibuat selembut mungkin; aksen hanya muncul saat
       fokus supaya kotak input terasa "hidup" tanpa berteriak. */
    border: round $t-tepi_redup;
    /* Napas dalam kotak: spasi vertikal + horizontal yang longgar. */
    padding: 1 1;
}

#input-row:focus-within {
    border: round $t-tepi;
}

#input-row.-sibuk {
    border: round $t-tepi_redup;
}

#input-prompt {
    display: none;
    width: 2;
    height: 1;
    padding: 0;
    color: $t-aksen;
    text-style: bold;
}

#chat-input {
    width: 1fr;
    /* Tinggi diubah dinamis oleh ChatBox._sesuaikan_tinggi: 1 saat kosong,
       tumbuh mengikuti baris terbungkus, berhenti di 5 lalu menggulir.
       height: auto TIDAK bisa dipakai — TextArea tidak menyediakan
       tinggi-otomatis-mengikuti-isi. */
    height: 1;
    padding: 0;
    border: none;
    background: $t-menu_bg;
    color: $t-teks;
}

#chat-input:focus {
    border: none;
    background: $t-menu_bg;
    background-tint: $t-menu_bg;
}

#input-hint {
    display: none;
    width: 100%;
    height: 1;
    padding: 0 2;
    color: $t-redup;
    background: transparent;
    text-align: right;
}

/* Placeholder & seleksi di dalam TextArea memakai palet tema, bukan
   bawaan Textual (biru generik yang tak ikut /theme). */
#chat-input .text-area--placeholder {
    color: $t-redup;
    text-style: italic;
}

#chat-input .text-area--cursor {
    background: $t-aksen;
    color: $t-gema_bg;
}

#chat-input .text-area--selection {
    background: $t-menu_aktif_bg;
    color: $t-menu_aktif_teks;
}

/* ── Dropdown autocomplete: DI ATAS kotak input ─────────────────────── */
/* Teks opsi TIDAK diberi warna di Python (cukup bold/dim) supaya warna  */
/* diatur sepenuhnya dari sini — termasuk saat baris tersorot, yang      */
/* dulu membuat teks menyatu dengan background sorotan.                  */

#autocomplete-title {
    display: none;
    width: 100%;
    height: 1;
    /* left 3 = border(1)+padding-list(1)+padding-option(1): judul sejajar
       tepi kiri TEKS opsi di bawahnya (dulu left 2, satu kolom nyasar). */
    padding: 1 3 0 3;
    color: $t-aksen;
    text-style: bold;
    background: $t-gema_bg;
    overflow-x: hidden;
}

#autocomplete-list {
    display: none;
    width: 100%;
    height: auto;
    max-height: 9;
    background: $t-menu_bg;
    color: $t-menu_teks;
    border: round $t-tepi_redup;
    padding: 0 1;
    margin-top: 1;
    overflow-x: hidden;
    scrollbar-size-vertical: 1;
    scrollbar-background: $t-menu_bg;
    scrollbar-background-hover: $t-menu_bg;
    scrollbar-background-active: $t-menu_bg;
    scrollbar-color: $t-tepi_redup;
    scrollbar-color-hover: $t-tepi;
    scrollbar-color-active: $t-tepi;
}

#autocomplete-list > .option-list--option {
    /* vertical 0: tinggi baris = 1 baris teks (dulu 1+teks+1 = 3 baris
       per opsi — picker jadi renggang dan max-height 12 cuma muat ~4). */
    padding: 0 1;
    background: transparent;
    color: $t-menu_teks;
}

#autocomplete-list > .option-list--option-highlighted {
    background: $t-menu_sorot;
    color: $t-menu_teks;
    text-style: bold;
}

#autocomplete-list > .option-list--option-hover {
    background: $t-menu_hover;
    color: $t-menu_teks;
}

#autocomplete-hint {
    width: 100%;
    height: 1;
    /* Searah dengan title (left 3) supaya judul, daftar, hint satu garis. */
    padding: 1 3;
    color: $t-menu_meta_teks;
    background: $t-gema_bg;
}

/* ── Status bar ─────────────────────────────────────────────────────── */

#statusbar {
    dock: bottom;
    width: 100%;
    height: 2;
    border-top: solid $t-tepi_redup;
    background: $t-bg_footer;
    color: $t-model_footer;
    /* Napas kiri/kanan: pill merek tidak menempel di tepi terminal. */
    padding: 0 1;
}

/* ── Modal (menu pilih / konfirmasi / input) ────────────────────────── */
/* Gaya modal DIBAKUKAN di luar tema: backdrop PEKAT (85% hitam) supaya */
/* dialog menonjol dari isi apa pun di belakangnya, panel DARK GRAY,    */
/* border PUTIH. Di tema terang sekalipun modal tetap gelap — dialog    */
/* adalah "lapisan di atas aplikasi", bukan bagian dari palet tema.     */

/* MultiSelectScreen tak mewarisi SelectScreen (beda tipe hasil), tapi
   pakai struktur DOM yang sama — selektornya diikutkan di sini. */
SelectScreen, MultiSelectScreen, ConfirmScreen, TextPromptScreen {
    align: center middle;
    background: #000000 85%;
}

#select-container, #confirm-container, #text-container {
    background: #2b2b2b;
    color: #f0f0f0;
    border: round #ffffff;
    padding: 1 2;
}

#select-title, #confirm-title, #text-title {
    color: #ffffff;
    text-style: bold;
}

#select-hint, #text-hint, #select-empty {
    color: #b8b8b8;
}

#select-options {
    background: #2b2b2b;
    color: #f0f0f0;
    border: none;
    scrollbar-size-vertical: 1;
}

#select-options > .option-list--option {
    background: transparent;
    color: #f0f0f0;
}

#select-options > .option-list--option-highlighted {
    background: #4a4a4a;
    color: #ffffff;
    text-style: bold;
}

#select-options > .option-list--option-hover {
    background: #3d3d3d;
    color: #ffffff;
}

#text-input {
    background: #1e1e1e;
    color: #ffffff;
    border: round #ffffff;
}

#confirm-btn-yes, #confirm-btn-no {
    color: #b8b8b8;
    background: #2b2b2b;
}

ConfirmScreen Static.-active {
    background: #4a4a4a;
    color: #ffffff;
    text-style: bold;
}

/* ── Responsif (kelas dipasang otomatis oleh breakpoints) ───────────── */

Screen.-sempit #titlebar-lights, Screen.-sempit #titlebar-chat { display: none; }
Screen.-sempit #titlebar-brand { width: 1fr; }
Screen.-sempit #titlebar-actions { width: 7; }
Screen.-sempit #logo { padding: 0 1; height: 2; }
Screen.-sempit #chatbox { padding: 0 1; margin-top: 0; }
Screen.-sempit #input-row { padding: 0 1; }
Screen.-sempit #statusbar-sidebar-toggle, Screen.-normal #statusbar-sidebar-toggle {
    display: block;
}
Screen.-sempit #statusbar { padding-left: 1; }
Screen.-normal #statusbar { padding-left: 1; }
Screen.-sempit #sidebar-close, Screen.-normal #sidebar-close { display: block; }
Screen.-sempit #plan { max-height: 5; }
Screen.-sempit #autocomplete-list { max-height: 6; }
Screen.-sempit #autocomplete-hint { display: none; }

Screen.-pendek #logo { height: 2; }
Screen.-pendek #input-row { padding: 0 1; }
Screen.-pendek #chatbox { padding-bottom: 0; margin-top: 0; }
Screen.-pendek #footer { max-height: 60%; }
Screen.-pendek #plan { max-height: 4; }
Screen.-pendek #autocomplete-list { max-height: 5; }
Screen.-pendek #autocomplete-hint { display: none; }

/* Kompatibilitas: kelas lama .compact masih dihormati. */
.compact #logo { height: 2; }
.compact #plan { max-height: 4; }
"""

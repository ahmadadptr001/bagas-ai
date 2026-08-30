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

CATATAN LAYOUT (tetap berlaku):

Di Textual, SEMUA widget ber-``dock: bottom`` diletakkan pada baris paling
bawah region induknya — mereka SALING MENIMPA, bukan menumpuk rapi. Hanya
``#footer`` yang di-dock; panel-panel di dalamnya mengalir vertikal normal
sehingga ``#messages`` menyusut tepat sebanyak tinggi footer.
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
    width: 100%;
    height: auto;
    max-height: 12;
    padding: 0 1;
    color: $t-aksen;
}

#messages {
    width: 100%;
    height: 1fr;
    background: $t-gema_bg;
    color: $t-teks;
    padding: 0 1;
    overflow-x: hidden;
    overflow-y: auto;
}

/* ── Footer: SATU-SATUNYA widget yang di-dock ───────────────────────── */

#footer {
    dock: bottom;
    width: 100%;
    height: auto;
    max-height: 75%;
    background: $t-gema_bg;
}

#sidebar-toggle {
    display: none;
    width: auto;
    min-width: 3;
    margin: 0;
    height: 1;
    border: none;
    background: $t-gema_bg;
    color: $t-aksen;
}
#chat-row { width: 100%; height: auto; }
#chat-row #chatbox { width: 1fr; }
#sidebar-close { display: none; width: 100%; height: 1; border: none; background: $t-gema_bg; color: $t-aksen; }

/* Panel-panel bawah diberi margin-top: teks jawaban/prompt di atasnya
   tidak menempel rapat ke box thinking / loading / antrean. */
#plan, #image-preview, #thinking-block, #streaming-preview {
    width: 100%;
    height: auto;
    background: $t-gema_bg;
    padding: 0 1;
    margin-top: 1;
}

#plan {
    max-height: 12;
    border-top: tall $t-tepi_redup;
}

#image-preview { max-height: 12; }

#thinking-block {
    max-height: 8;
    border-top: tall $t-tepi_redup;
}

#streaming-preview {
    max-height: 8;
    border-top: tall $t-tepi_redup;
}

#progress {
    width: 100%;
    height: auto;
    max-height: 3;
    background: $t-gema_bg;
    color: $t-aksen;
    padding: 0 1;
    margin-top: 1;
}

/* ── Strip antrean: DI AREA TERMINAL (bukan footer) ──────────────────── */
/* Nempel di bawah jawaban terakhir; diredupkan sampai benar-benar      */
/* dijalankan, saat itu prompt pindah ke riwayat sebagai pesan normal.  */

#queue-strip {
    width: 100%;
    height: auto;
    max-height: 4;
    background: $t-gema_bg;
    padding: 0 1;
}

/* ── Kotak input ────────────────────────────────────────────────────── */

#chatbox {
    width: 100%;
    height: auto;
    background: $t-gema_bg;
}

#input-row {
    width: 100%;
    /* auto: tinggi mengikuti #chat-input yang tumbuh saat teks membungkus
       (maks 5 baris, diatur _sesuaikan_tinggi di chat_box.py). Border
       round menambah 2 baris sendiri. */
    height: auto;
    background: $t-gema_bg;
    border: round $t-tepi;
    padding: 0 1;
}

#input-row.-sibuk {
    border: round $t-tepi_redup;
}

#input-prompt {
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
    background: $t-gema_bg;
    color: $t-teks;
}

#chat-input:focus {
    border: none;
    background: $t-gema_bg;
    background-tint: $t-gema_bg;
}

/* ── Dropdown autocomplete: DI ATAS kotak input ─────────────────────── */
/* Teks opsi TIDAK diberi warna di Python (cukup bold/dim) supaya warna  */
/* diatur sepenuhnya dari sini — termasuk saat baris tersorot, yang      */
/* dulu membuat teks menyatu dengan background sorotan.                  */

#autocomplete-list {
    display: none;
    width: 100%;
    height: auto;
    max-height: 10;
    background: $t-menu_bg;
    color: $t-menu_teks;
    border: round $t-tepi_redup;
    padding: 0 1;
    overflow-x: hidden;
    scrollbar-size-vertical: 1;
}

#autocomplete-list > .option-list--option {
    padding: 0 1;
    background: transparent;
    color: $t-menu_teks;
}

#autocomplete-list > .option-list--option-highlighted {
    background: $t-menu_sorot;
    color: $t-menu_teks;
}

#autocomplete-list > .option-list--option-hover {
    background: $t-tepi_redup;
    color: $t-menu_teks;
}

#autocomplete-hint {
    width: 100%;
    height: 1;
    padding: 0 2;
    color: $t-menu_meta_teks;
    background: $t-gema_bg;
}

/* ── Status bar ─────────────────────────────────────────────────────── */

#statusbar {
    width: 100%;
    height: 1;
    background: $t-bg_footer;
    color: $t-model_footer;
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

Screen.-sempit #logo { display: none; }
Screen.-sempit #sidebar-toggle { display: block; }
Screen.-normal #sidebar-toggle { display: block; }
Screen.-sempit #sidebar-close, Screen.-normal #sidebar-close { display: block; }
Screen.-sempit #plan { max-height: 5; }
Screen.-sempit #thinking-block { max-height: 4; }
Screen.-sempit #streaming-preview { max-height: 4; }
Screen.-sempit #autocomplete-list { max-height: 5; }
Screen.-sempit #autocomplete-hint { display: none; }

Screen.-pendek #logo { display: none; }
Screen.-pendek #footer { max-height: 60%; }
Screen.-pendek #plan { max-height: 4; }
Screen.-pendek #thinking-block { max-height: 3; }
Screen.-pendek #streaming-preview { max-height: 3; }
Screen.-pendek #autocomplete-list { max-height: 4; }
Screen.-pendek #autocomplete-hint { display: none; }

/* Kompatibilitas: kelas lama .compact masih dihormati. */
.compact #logo { display: none; }
.compact #plan { max-height: 4; }
"""

"""Sistem tema warna bagas-ai — satu sumber kebenaran untuk seluruh UI.

Dipilih lewat /theme dan tersimpan di prefs.json. Semua permukaan utama —
footer, kotak chat, gema prompt, banner/logo, panel bantuan, menu pilihan —
membaca warnanya dari sini, jadi berganti tema benar-benar mengganti wajah
aplikasi, bukan sekadar satu-dua garis.

Dua pintu disediakan:
  p(kunci)        -> nilai warna tema AKTIF (hex atau list untuk gradien).
  terjemah(teks)  -> menukar HEX WARISAN (emas/oranye era pra-tema) yang masih
                     tertanam di markup lama menjadi warna tema aktif. Ini
                     yang membuat teks-teks status lama ikut berganti tanpa
                     perlu menulis ulang ratusan baris.

Tiap tema menyimpan KELUARGA warna, bukan satu aksen: latar footer & warna
teksnya (tema terang), strip gema prompt (tema gelap netral), gradien logo
(7 titik), dan pasangan menu terang/gelap. Kontras tiap pasangan dijaga
terbaca — aksen di atas latar gelap, gelap di atas footer terang.
"""
from __future__ import annotations

from .. import prefs

# --- tema yang tersedia --------------------------------------------------------
# "default" (Ember) WAJIB memuat nilai persis skema lama (emas + footer putih
# yang sudah disetujui pengguna) supaya prefs lama yang menunjuknya tidak
# berubah sepeser pun. TAPI ia bukan lagi titik awal aplikasi: bawaan pabrik
# kini "biru" — berlabel Lautan di menu /theme. Id dan labelnya memang tak
# seragam; id dipertahankan "biru" karena tersimpan begitu di prefs.json
# pengguna — menggantinya berarti tema semua orang lepas ke default.
TEMA: dict[str, dict] = {
    "default": {
        "label": "Ember",
        "desc": "emas hangat — ciri khas bagas-ai",
        "aksen": "#fcc048", "aksen2": "#fc9018",
        "aksen_terang": "#f7d488", "tepi": "#7a5c3a",
        "tepi_redup": "#4a3826", "redup": "#a89078", "teks": "#f2e3cc",
        "grad": ["#fde68a", "#fcc048", "#fca830", "#fc9018",
                 "#e8760c", "#c25a08", "#9c4800"],
        "gema_bg": "#121212", "gema_garis": "#e8e8e8", "gema_teks": "#ffffff",
        "bg_footer": "#f7f3ec", "merek_footer": "#7a4f00",
        "model_footer": "#3d3229", "sep_footer": "#cfc2ab",
        "muted_footer": "#8a8072", "cmd_footer": "#7a4f00",
        "exit_footer": "#b3402a", "git_footer": "#4e7a22",
        "ubah_footer": "#8a6a3a",
        "menu_bg": "#241a10", "menu_teks": "#f2e3cc",
        "menu_aktif_bg": "#fcc048", "menu_aktif_teks": "#241a10",
        "menu_meta_bg": "#1a120b", "menu_meta_teks": "#a89078",
    },
    "biru": {
        "label": "Lautan",
        "desc": "biru tenang — fokus & sejuk sepanjang sesi",
        "aksen": "#58a6ff", "aksen2": "#7aa2f7",
        "aksen_terang": "#a5c8ff", "tepi": "#3b5b8c",
        "tepi_redup": "#2a3f5f", "redup": "#7d8ba1", "teks": "#e3ecf7",
        "grad": ["#dbeafe", "#93c5fd", "#58a6ff", "#3b82f6",
                 "#2563eb", "#1d4ed8", "#1e3a8a"],
        "gema_bg": "#121212", "gema_garis": "#c9dcf2", "gema_teks": "#ffffff",
        "bg_footer": "#edf3fb", "merek_footer": "#1d4ed8",
        "model_footer": "#2b3440", "sep_footer": "#c3cfe0",
        "muted_footer": "#7e899b", "cmd_footer": "#1d4ed8",
        "exit_footer": "#b3402a", "git_footer": "#4e7a22",
        "ubah_footer": "#5a6a80",
        "menu_bg": "#14202e", "menu_teks": "#e3ecf7",
        "menu_aktif_bg": "#58a6ff", "menu_aktif_teks": "#0d1b2a",
        "menu_meta_bg": "#0d1b2a", "menu_meta_teks": "#7d8ba1",
    },
    "mono": {
        "label": "Hitam-Putih",
        "desc": "monokrom — kontras murni tanpa warna",
        "aksen": "#e8e8e8", "aksen2": "#cfcfcf",
        "aksen_terang": "#d8d8d8", "tepi": "#707070",
        "tepi_redup": "#4a4a4a", "redup": "#8f8f8f", "teks": "#efefef",
        "grad": ["#ffffff", "#f0f0f0", "#d8d8d8", "#b8b8b8",
                 "#989898", "#787878", "#585858"],
        "gema_bg": "#121212", "gema_garis": "#e8e8e8", "gema_teks": "#ffffff",
        "bg_footer": "#f2f2f2", "merek_footer": "#3a3a3a",
        "model_footer": "#262626", "sep_footer": "#c6c6c6",
        "muted_footer": "#808080", "cmd_footer": "#3a3a3a",
        "exit_footer": "#5a5a5a", "git_footer": "#5a5a5a",
        "ubah_footer": "#6a6a6a",
        "menu_bg": "#222222", "menu_teks": "#efefef",
        "menu_aktif_bg": "#e8e8e8", "menu_aktif_teks": "#1a1a1a",
        "menu_meta_bg": "#1a1a1a", "menu_meta_teks": "#8f8f8f",
    },
    "hijau": {
        "label": "Hutan",
        "desc": "hijau lumut — tenang di mata untuk sesi panjang",
        "aksen": "#9fc93c", "aksen2": "#7ee787",
        "aksen_terang": "#cdeba0", "tepi": "#4e7a22",
        "tepi_redup": "#33491f", "redup": "#8fa07a", "teks": "#eaf2df",
        "grad": ["#eafcc4", "#cdeba0", "#9fc93c", "#7ab52e",
                 "#5c9422", "#3f701c", "#2a4d14"],
        "gema_bg": "#121212", "gema_garis": "#d8ecc0", "gema_teks": "#ffffff",
        "bg_footer": "#f0f5ea", "merek_footer": "#3f701c",
        "model_footer": "#2d3329", "sep_footer": "#c8d5bd",
        "muted_footer": "#7f8a77", "cmd_footer": "#3f701c",
        "exit_footer": "#b3402a", "git_footer": "#4e7a22",
        "ubah_footer": "#6d7a5e",
        "menu_bg": "#18220f", "menu_teks": "#eaf2df",
        "menu_aktif_bg": "#9fc93c", "menu_aktif_teks": "#15200e",
        "menu_meta_bg": "#131b0b", "menu_meta_teks": "#8fa07a",
    },
    "ungu": {
        "label": "Galaksi",
        "desc": "ungu neon — malam baru, kode baru",
        "aksen": "#c792ea", "aksen2": "#b48ef0",
        "aksen_terang": "#dfc2f5", "tepi": "#6b4a8f",
        "tepi_redup": "#432e5c", "redup": "#9a8aad", "teks": "#f0e8f7",
        "grad": ["#f3e8fb", "#dfc2f5", "#c792ea", "#a06ee0",
                 "#7e4fc4", "#5d37a0", "#3d2470"],
        "gema_bg": "#121212", "gema_garis": "#e2cff2", "gema_teks": "#ffffff",
        "bg_footer": "#f3eefb", "merek_footer": "#5d37a0",
        "model_footer": "#2f2a38", "sep_footer": "#d0c6e0",
        "muted_footer": "#837a91", "cmd_footer": "#5d37a0",
        "exit_footer": "#b3402a", "git_footer": "#4e7a22",
        "ubah_footer": "#6f6480",
        "menu_bg": "#1c1426", "menu_teks": "#f0e8f7",
        "menu_aktif_bg": "#c792ea", "menu_aktif_teks": "#170f22",
        "menu_meta_bg": "#150e1e", "menu_meta_teks": "#9a8aad",
    },
    # Dua tema "modern" ala Visual Studio Code (Dark/Light Modern) —
    # paletnya diambil dari nilai resminya: editor #1e1e1e / #ffffff,
    # aksen biru #3794ff / #005fb8, seleksi biru #04395e / #cfe4fb.
    # Keduanya SATU KELUARGA: sama-sama biru VS Code, hanya beda sisi
    # terangnya — jadi pengguna tak perlu belajar dua bahasa warna.
    "vsdark": {
        "label": "Kode Gelap",
        "desc": "ala VS Code Dark Modern — biru profesional di abu pekat",
        "aksen": "#3794ff", "aksen2": "#4ec9b0",
        "aksen_terang": "#9cdcfe", "tepi": "#3c3c3c",
        "tepi_redup": "#2b2b2b", "redup": "#9d9d9d", "teks": "#cccccc",
        "grad": ["#cfe6ff", "#9cdcfe", "#569cd6", "#3794ff",
                 "#1f6fd0", "#0f4c8c", "#082d54"],
        "gema_bg": "#1e1e1e", "gema_garis": "#cccccc", "gema_teks": "#ffffff",
        "bg_footer": "#181818", "merek_footer": "#3794ff",
        "model_footer": "#cccccc", "sep_footer": "#2b2b2b",
        "muted_footer": "#9d9d9d", "cmd_footer": "#3794ff",
        "exit_footer": "#f14c4c", "git_footer": "#89d185",
        "ubah_footer": "#d7ba7d",
        "menu_bg": "#1f1f1f", "menu_teks": "#cccccc",
        "menu_aktif_bg": "#04395e", "menu_aktif_teks": "#ffffff",
        "menu_meta_bg": "#181818", "menu_meta_teks": "#9d9d9d",
    },
    "vslight": {
        "label": "Kode Terang",
        "desc": "ala VS Code Light Modern — bersih & kontras untuk siang hari",
        "aksen": "#005fb8", "aksen2": "#0a7d6f",
        "aksen_terang": "#0a5ca8", "tepi": "#c8c8c8",
        "tepi_redup": "#e0e0e0", "redup": "#6f6f6f", "teks": "#3b3b3b",
        "grad": ["#d6ecff", "#b3d8f8", "#7ab8ef", "#4a9be0",
                 "#2a80cc", "#005fb8", "#00457f"],
        "gema_bg": "#ffffff", "gema_garis": "#3b3b3b", "gema_teks": "#1f1f1f",
        "bg_footer": "#f8f8f8", "merek_footer": "#005fb8",
        "model_footer": "#3b3b3b", "sep_footer": "#e5e5e5",
        "muted_footer": "#6f6f6f", "cmd_footer": "#005fb8",
        "exit_footer": "#cd3131", "git_footer": "#107c10",
        "ubah_footer": "#8a5a00",
        "menu_bg": "#ffffff", "menu_teks": "#3b3b3b",
        "menu_aktif_bg": "#cfe4fb", "menu_aktif_teks": "#1f1f1f",
        "menu_meta_bg": "#f8f8f8", "menu_meta_teks": "#6f6f6f",
    },
}

# Hex WARISAN (skema emas lama) -> kunci palet. Dipakai terjemah() supaya
# markup lama di seluruh cli ikut tema tanpa ditulis ulang satu per satu.
_PETA_WARISAN: dict[str, str] = {
    "#fcc048": "aksen", "#fca830": "aksen2", "#fc9018": "aksen2",
    "#ffb861": "aksen_terang", "#f7d488": "aksen_terang",
    "#ffd9a0": "aksen_terang", "#fde68a": "aksen_terang",
    "#e8760c": "aksen2", "#c25a08": "aksen2", "#9c4800": "tepi",
    "#7a5c3a": "tepi", "#4a3826": "tepi_redup",
    "#a89078": "redup", "#8f7a62": "redup",
    "#f2e3cc": "teks", "#241a10": "menu_bg", "#1a120b": "menu_meta_bg",
}

# Bawaan pabrik: Lautan (id "biru") — bukan "default"/Ember. Kunci "default"
# tetap ada di TEMA semata demi prefs lama yang menunjuknya secara eksplisit.
_aktif: dict = TEMA["biru"]
_nama: str = "biru"


def _muat() -> None:
    """Pasang tema tersimpan di prefs (dipanggil sekali saat impor)."""
    global _aktif, _nama
    nama = (prefs.load().get("tema") or "biru")
    if nama in TEMA:
        _nama, _aktif = nama, TEMA[nama]


_muat()


def p(kunci: str):
    """Nilai warna tema aktif; kunci tak dikenal -> warna aksen (aman dipakai)."""
    return _aktif.get(kunci, _aktif["aksen"])


def nama_aktif() -> str:
    return _nama


def label_aktif() -> str:
    return _aktif.get("label", _nama)


def set_tema(nama: str) -> bool:
    """Ganti tema aktif + simpan ke prefs. False bila nama tak dikenal."""
    global _aktif, _nama
    if nama not in TEMA:
        return False
    _nama, _aktif = nama, TEMA[nama]
    try:
        prefs.save(tema=nama)
    except Exception:  # noqa: BLE001 - prefs gagal tak boleh membatalkan tema
        pass
    return True


def daftar() -> list[tuple[str, str, str]]:
    """[(id, label, deskripsi)] urut tampil di menu /theme."""
    return [(n, t["label"], t["desc"]) for n, t in TEMA.items()]


def terjemah(markup: str) -> str:
    """Tukar hex warisan di markup rich menjadi warna tema aktif.

    Cuma string yang berubah — markup tanpa hex warisan dikembalikan apa
    adanya, jadi aman dipasang di seluruh jalur cetak."""
    if not any(h in markup for h in _PETA_WARISAN):
        return markup
    for hexa, kunci in _PETA_WARISAN.items():
        if hexa in markup:
            markup = markup.replace(hexa, str(p(kunci)))
    return markup

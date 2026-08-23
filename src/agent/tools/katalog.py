"""Katalog tool berjenjang: yang INTI selalu dikirim, sisanya diminta saat perlu.

Masalah yang dipecahkan: seluruh 60 tool dulu dicantumkan di pesan pembuka tiap
sesi web — ~10 rb karakter — padahal satu giliran biasa memakai 3–5 tool. Sisanya
bukan cuma memboroskan pesan, tapi mengencerkan perhatian: makin banyak pilihan,
makin sering salah pilih, dan salah pilih tool adalah salah satu sumber blunder
yang paling sering terlihat pengguna.

Yang TIDAK dilakukan di sini: menghapus tool. Kemampuannya utuh — tool di luar
inti tinggal diminta lewat `list_tools(kategori)`, satu bolak-balik ekstra yang
hanya terjadi pada giliran yang memang membutuhkannya. Menyembunyikan tanpa
menyediakan jalan mengambilnya sama saja memangkas kemampuan diam-diam, dan itu
jauh lebih buruk daripada katalog yang kepanjangan.

Aturan penempatan (dipakai saat menimbang tool baru):
  - INTI  = dipakai di sebagian besar giliran ngoding, ATAU dibutuhkan untuk
            menegakkan aturan protokol (mis. validate_project, web_preview).
  - LAIN  = berguna tapi situasional; giliran yang membutuhkannya sudah pasti
            tahu ia sedang membutuhkannya (mis. memotong video, membuat zip).
"""
from __future__ import annotations

from .base import REGISTRY, tool

# Tool yang IKUT di pesan pembuka. Dijaga tetap pendek — tiap tambahan di sini
# harganya dibayar di SETIAP sesi, bukan cuma sesi yang memakainya.
INTI: tuple[str, ...] = (
    # berkas
    "read_file", "read_files", "write_file", "edit_file", "edit_files",
    "append_file", "delete_file", "list_dir", "make_dir",
    # mencari di proyek
    "search_text", "search_multi_text", "glob_files",
    # menjalankan
    "run_command", "run_python", "run_command_bg", "bg_output", "bg_send",
    # memastikan hasil
    "validate_project", "run_tests", "project_info", "web_preview",
    "take_screenshot",
    # riwayat kode
    "git_status", "git_diff", "git_log", "git_commit",
    "git_stash", "git_blame",
    # internet
    "web_search", "fetch_url",
    # konteks kerja & pengujian
    "kerja_terakhir", "catat_kerja", "sasaran", "test_function",
    # ke pengguna & alur kerja
    "ask_user", "ask_user_telegram", "plan", "plan_step", "remember", "undo_changes",
    # pintu ke sisanya
    "cari_tool", "list_tools",
)

# Sisanya, dikelompokkan supaya bisa diminta per kebutuhan. Nama kategorinya
# sengaja kata sehari-hari, bukan istilah teknis: yang mencarinya adalah model
# yang sedang berpikir "aku butuh mengunduh gambar", bukan "aku butuh modul
# extras".
LAIN: dict[str, tuple[str, ...]] = {
    "aset": ("web_search_image", "web_extractor", "download_file",
             "http_request", "attach_file", "analyze_image"),
    "media": ("media_info", "media_convert", "media_compress", "media_trim",
              "media_merge", "media_extract_audio", "media_thumbnail"),
    "arsip": ("zip_create", "zip_extract"),
    "berkas": ("move_file", "copy_file", "diff_files", "replace_in_files"),
    "sistem": ("clipboard_read", "clipboard_write", "notify", "open_path"),
    "skrip": ("save_script", "run_script", "list_scripts"),
    "memori": ("list_memory", "forget"),
    "proses": ("bg_stop", "bg_list"),
    "git": ("git_show",),
    "pro": ("find_todos", "code_metrics", "diagnose", "bookmark", "changelog"),
}

# Petunjuk khusus per kategori — dulu tinggal di pesan pembuka dan dibaca setiap
# sesi walau jarang terpakai. Di sini ia sampai TEPAT saat dibutuhkan, yaitu
# ketika modelnya sendiri yang meminta kategori itu.
_PANDUAN: dict[str, str] = {
    "aset": (
        "Strategi mengambil aset (gambar, sprite, suara, musik, font, dataset) "
        "— di sinilah paling sering terbuang banyak giliran karena kata kunci "
        "acak & jalan memutar:\n"
        "1. TETAPKAN SPESIFIKASI dulu: isi apa, format apa (mp3? png "
        "transparan? ttf?), harus gratis/bebas lisensi.\n"
        "2. LANGSUNG KE SUMBERNYA, jangan pencarian umum — musik & sfx: "
        "pixabay.com / freesound.org; sprite & aset game: kenney.nl / "
        "opengameart.org; foto: pixabay.com / unsplash.com; font: Google Fonts "
        "(berkas .ttf-nya di github.com/google/fonts). Kunci dengan operator "
        "`site:` di web_search.\n"
        "3. KATA KUNCI bahasa Inggris, 2-4 kata benda konkret = isi + jenis + "
        "format. Benar: 'site:pixabay.com galaxy ambient music'. Salah: "
        "kalimat panjang atau menyalin deskripsi tugas mentah-mentah.\n"
        "4. MAKSIMAL 2x web_search per berkas. Gagal? ubah SATU kata paling "
        "menentukan, jangan menyusun kombinasi acak baru.\n"
        "5. fetch_url hanya untuk MENEMUKAN URL berkas aslinya (berujung "
        ".mp3/.png/.zip/.ttf). Minta login/berbayar? tinggalkan, pindah sumber. "
        "Untuk membongkar satu halaman jadi daftar gambar/tautan/heading yang "
        "siap pakai, web_extractor lebih tepat daripada fetch_url.\n"
        "5b. GAMBAR/FOTO: pakai web_search_image — URL-nya sudah diperiksa "
        "hidup lebih dulu. JANGAN menyusun alamat unsplash/pexels dari "
        "ingatan: alamat tebakan hampir selalu 404, dan memperbaikinya satu "
        "per satu menghabiskan giliran tanpa hasil.\n"
        "6. download_file ke folder proyek, pastikan ukurannya > 0, lalu "
        "BERHENTI mencari. Berkas pertama yang layak sudah cukup.\n"
        "JANGAN memberi daftar tautan agar pengguna mengunduh sendiri: aset "
        "yang tak diunduh lewat download_file sama saja tak ada, dan kodemu "
        "akan menunjuk berkas yang tak pernah wujud."
    ),
    "proses": (
        "Prompt interaktif dari proses latar ('Ok to proceed?', pertanyaan "
        "CLI) dijawab lewat bg_send — jangan biarkan prosesnya menggantung "
        "menunggu ketikan yang tak pernah datang."
    ),
    "skrip": (
        "Pakai save_script untuk pekerjaan yang akan BERULANG di proyek ini, "
        "lalu run_script memanggilnya. Untuk sekali pakai, run_command saja."
    ),
}


def baris_tool(nama: str) -> str:
    """Satu baris katalog untuk `nama` (kosong bila tool-nya tak ada)."""
    t = REGISTRY.get(nama)
    if t is None:
        return ""
    fn = t.schema.get("function", t.schema)
    desc = (fn.get("description", "") or "").strip().split("\n")[0]
    params = fn.get("parameters", {}).get("properties", {}) or {}
    wajib = set(fn.get("parameters", {}).get("required", []) or [])
    bagian = [f"{p}{'*' if p in wajib else ''}:{i.get('type', 'any')}"
              for p, i in params.items()]
    return f"- {nama}({', '.join(bagian)}) — {desc}"


def katalog_inti() -> str:
    """Baris katalog untuk tool INTI saja (yang ikut di pesan pembuka).

    web_preview dibuang dari daftar saat dijeda (config.WEB_PREVIEW): biar
    hemat sungguhan, bukan cuma kosmetik — deskripsi tool ikut dibayar di
    pesan pembuka SETIAP sesi, dan namanya yang masih tercantum hanya
    menggoda model mencobanya lalu gagal."""
    from .. import config
    nama_inti = (n for n in INTI
                 if config.WEB_PREVIEW or n != "web_preview")
    return "\n".join(b for b in (baris_tool(n) for n in nama_inti) if b)


def ringkasan_kategori() -> str:
    """Satu baris per kategori di luar inti, untuk disebut di pesan pembuka."""
    return "\n".join(
        f"- {nama}: {', '.join(isi)}"
        for nama, isi in LAIN.items()
        if any(n in REGISTRY for n in isi)
    )


# Kata tugas/isi umum yang BUKAN kata kunci pencarian tool. Dihilangkan supaya
# "cara mengunduh gambar" tidak mencari kata 'cara'/'mengunduh' yang ada di
# mana-mana, melainkan fokus ke 'unduh' & 'gambar'.
_STOP_CARI = frozenset("""
yang dan di ke dari untuk pada dengan agar supaya bisa dapat mau ingin harus
sudah belum tidak ini itu ada adalah akan juga lalu maka atau jika kalau karena
tapi namun saya aku kamu kita anda tolong buat bikin caranya bagaimana apa mana
saja semua tool tools cara mau ingin pengen perlu butuh saya aku bisa tolong
bantu bantuan the a an and of to for with in on at by is are was were can could
would should have has had do does did need needs want wants please make create
""".split())


@tool
def cari_tool(kebutuhan: str, jumlah: int = 5) -> str:
    """Cari TOOL yang paling cocok untuk kebutuhanmu, dari SEMUA tool yang ada (inti & tambahan). Deskripsikan apa yang ingin kamu lakukan dengan kata-katamu sendiri — tool ini mencocokkannya dengan nama & deskripsi tiap tool, lalu mengembalikan yang paling relevan beserta NAMA PERSISnya. Pakai saat kamu ragu tool apa yang tersedia untuk suatu pekerjaan — jangan menebak nama tool dan jangan menyerah.

    kebutuhan: apa yang ingin kamu lakukan, bebas kata-kata, mis. 'unduh gambar
        dari internet', 'ubah video jadi gif', 'jalankan server di latar',
        'baca beberapa file sekaligus', 'kirim notifikasi'.
    jumlah: berapa hasil yang ditampilkan (default 5, maksimal 10).
    """
    import re as _re

    toks = _re.findall(r"[a-z0-9_]+\b", (kebutuhan or "").lower())
    kata = [t for t in toks if len(t) >= 3 and t not in _STOP_CARI]
    if not kata:
        return ("[error] kebutuhan terlalu umum. Sebut apa yang ingin kamu "
                "lakukan dengan kata kunci, mis. 'unduh gambar', 'ubah video "
                "jadi gif', 'jalankan server di latar'.")

    # Bobot IDF: kata yang muncul di BANYAK tool (mis. 'file', 'buat') kurang
    # informatif daripada kata langka (mis. 'zip', 'gif'). Tanpa ini, kebutuhan
    # 'buat file zip' disamakan oleh 'file' yang ada di mana-mana dan tool yang
    # tepat (zip_create) kalah peringkat oleh tool nama-abjad yang kebetulan
    # memuat 'file'.
    import math as _math

    n_tool = max(1, len(REGISTRY))
    df: dict[str, int] = {}
    for nama, t in REGISTRY.items():
        fn = t.schema.get("function", t.schema)
        desc = (fn.get("description", "") or "").lower()
        gabung = nama.lower() + " " + desc
        for k in kata:
            if k in gabung:
                df[k] = df.get(k, 0) + 1
    idf = {k: 1.0 + _math.log(n_tool / (1 + df.get(k, 0)))
           for k in kata}

    skor: list[tuple[float, str]] = []
    for nama, t in REGISTRY.items():
        fn = t.schema.get("function", t.schema)
        deskripsi = (fn.get("description", "") or "").lower()
        s = 0.0
        for k in kata:
            if k in nama.lower():
                s += 4.0 * idf[k]      # nama tool memuat kata = hampir pasti cocok
            elif k in deskripsi:
                s += 2.0 * idf[k]      # hanya di deskripsi = relevan tapi tak langsung
        if s:
            skor.append((s, nama))
    skor.sort(key=lambda x: (-x[0], x[1]))

    n = max(1, min(int(jumlah), 10))
    pilihan = skor[:n]
    if not pilihan:
        return (f"Tidak ada tool yang cocok dengan '{kebutuhan}'. Coba kata "
                "kunci yang lebih spesifik, atau list_tools() untuk melihat "
                "kategori yang tersedia.")
    baris = [f"Tool paling cocok untuk '{kebutuhan}':"]
    for _, nama in pilihan:
        baris.append(baris_tool(nama))
    baris.append("")
    # Kalimat ini sampai ke DUA jalur model, jadi ia tak boleh menyebut
    # satu mekanisme pun: model (web) memanggil tool lewat blok teks
    # [[TOOL]], model (API) lewat function-calling asli. Menyebut salah
    # satunya berarti menyuruh separuh model memakai protokol yang bukan
    # miliknya — dan model (API) yang menuliskan [[TOOL]] sebagai teks
    # menghasilkan giliran yang tampak menjawab tapi tak menjalankan apa pun.
    baris.append("Pakai NAMA PERSIS di atas saat memanggilnya.")
    return "\n".join(baris)


@tool
def list_tools(kategori: str = "") -> str:
    """Ambil daftar tool DI LUAR yang sudah kamu punya, per kategori: aset (mengunduh gambar/font/audio/dataset), media (video & audio), arsip (zip), berkas (pindah/salin/banding/ganti massal), sistem (clipboard, notifikasi, buka berkas), skrip, memori, proses (kelola proses latar), git. Panggil ini saat butuh sesuatu yang tak ada di daftar langkahmu — jangan menyerah atau menyuruh pengguna melakukannya sendiri.

    kategori: nama kategori di atas. Kosongkan untuk melihat daftar kategori
        beserta isinya secara ringkas.
    """
    k = (kategori or "").strip().lower()
    if not k:
        return ("Kategori tool tambahan (panggil list_tools('<nama>') untuk "
                "rinciannya):\n" + ringkasan_kategori())
    if k not in LAIN:
        return (f"[error] kategori '{k}' tak ada. Yang tersedia: "
                + ", ".join(LAIN) + ".")
    baris = [b for b in (baris_tool(n) for n in LAIN[k]) if b]
    if not baris:
        return f"[error] kategori '{k}' kosong di pemasangan ini."
    keluar = f"Tool kategori '{k}':\n" + "\n".join(baris)
    if k in _PANDUAN:
        keluar += "\n\n" + _PANDUAN[k]
    return keluar

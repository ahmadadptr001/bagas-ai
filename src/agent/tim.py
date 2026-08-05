"""Tim 23 spesialis yang bekerja PASIF di belakang satu model.

Sebelum ini bagas-ai bekerja seperti satu orang serba bisa: ia menulis kode,
lalu menilai kodenya sendiri dengan sudut pandang yang sama persis. Yang luput
di tahap menulis juga luput di tahap menilai — dan yang paling sering luput
justru hal yang butuh sudut pandang LAIN: kredensial yang terbawa ke repo,
kontras tombol yang tak terbaca, kueri yang jadi lambat begitu datanya banyak.

Tim ini memasok sudut pandang itu. Tiap anggota punya bidang, dan yang
membangunkannya adalah ISI PEKERJAAN — bukan perintah pengguna. Menyentuh
berkas login membangunkan Keamanan; menyentuh .css membangunkan UI/UX dan
Aksesibilitas. Itulah arti "pasif": tak ada yang perlu diminta.

Tiga hal yang SENGAJA dibatasi, karena tanpanya fitur ini justru merusak:

  1. Satu anggota bicara SEKALI saja per giliran. Nasihat yang sama diulang
     lima langkah berturut-turut berubah jadi latar belakang yang diabaikan
     model — dan mendesak keluar konteks yang benar-benar penting.
  2. Paling banyak dua anggota bangun per langkah. Duapuluh tiga daftar periksa
     sekaligus bukan bantuan, melainkan kebisingan.
  3. Hanya tool yang MENGUBAH sesuatu yang membangunkan tim. Membaca berkas tak
     menghasilkan apa pun yang perlu ditinjau.

Semua ini berjalan di SATU sesi browser yang sama: tak ada tab tambahan, tak
ada kuota tambahan. Yang berubah cuma sudut pandang yang dibawa ke tiap langkah.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Jumlah anggota yang boleh bangun dalam SATU langkah.
_MAKS_PER_LANGKAH = 2
# Panjang maksimal satu catatan (huruf) — penjaga terakhir agar blok yang
# ditempel ke pesan tak pernah membengkak.
_MAKS_CATATAN = 700


@dataclass(frozen=True)
class Anggota:
    """Satu spesialis: siapa dia, kapan bangun, dan apa yang ia periksa."""

    nama: str
    bidang: str
    # --- pemicu (apa pun yang cocok akan membangunkannya) ---
    ekstensi: tuple[str, ...] = ()      # ".css", ".tsx", …
    jalur: tuple[str, ...] = ()         # potongan nama/berkas: "auth", "login"
    isi: tuple[str, ...] = ()           # regex pada isi tulisan/perintah
    tool: tuple[str, ...] = ()          # nama tool yang membangunkannya
    # --- yang ia bawa ke meja ---
    periksa: tuple[str, ...] = ()
    # Makin kecil makin didahulukan saat lebih dari _MAKS_PER_LANGKAH bangun.
    prioritas: int = 50

    def catatan(self) -> str:
        """Blok pendek untuk ditempel ke pesan berikutnya."""
        poin = "\n".join(f"- {p}" for p in self.periksa[:3])
        teks = f"[{self.nama} — {self.bidang}]\n{poin}"
        return teks[:_MAKS_CATATAN]


# --- 23 anggota -------------------------------------------------------------
# Prioritas mengikuti seberapa mahal kesalahannya kalau lolos: kebocoran
# kredensial jauh lebih mahal daripada judul halaman yang kurang enak dibaca.
ANGGOTA: tuple[Anggota, ...] = (
    Anggota(
        # Sengaja TANPA daftar ekstensi: "berkas .py" bukan alasan memanggil
        # spesialis keamanan. Yang memanggilnya adalah urusannya — berkas yang
        # menangani identitas/uang/unggahan, atau kode yang memakai pola
        # berbahaya. Dengan daftar ekstensi luas, ia bangun di hampir tiap
        # langkah dan nasihatnya berubah jadi latar yang diabaikan.
        "Keamanan", "celah & penyalahgunaan",
        jalur=("auth", "login", "session", "password", "token", "admin",
               "upload", "payment", "bayar", "crypt", "hash", "jwt", "oauth"),
        isi=(r"\b(eval|exec|pickle\.loads|os\.system|subprocess)\b",
             r"\b(SELECT|INSERT|UPDATE|DELETE)\b.*\+.*\b(input|param|arg)",
             r"\binnerHTML\b", r"\bverify\s*=\s*False\b"),
        periksa=(
            "masukan dari luar divalidasi sebelum dipakai (query, path, perintah)",
            "tak ada kredensial/kunci yang tertulis langsung di kode",
            "kegagalan otentikasi menolak akses, bukan meneruskannya diam-diam",
        ), prioritas=1),
    Anggota(
        "Rahasia & Kredensial", "kunci, token, berkas .env",
        ekstensi=(".env", ".pem", ".key", ".yml", ".yaml", ".json", ".ini"),
        jalur=(".env", "secret", "credential", "config"),
        isi=(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}",
             r"(?i)\bAKIA[0-9A-Z]{16}\b", r"(?i)\bBEGIN [A-Z ]*PRIVATE KEY\b"),
        periksa=(
            "nilai rahasia dibaca dari environment, bukan ditulis di berkas",
            "berkas rahasia masuk .gitignore sebelum sempat ter-commit",
            "contoh konfigurasi memakai nilai palsu yang jelas-jelas contoh",
        ), prioritas=1),
    Anggota(
        "Basis Data", "skema, kueri, migrasi",
        ekstensi=(".sql", ".prisma"),
        jalur=("model", "schema", "migration", "migrasi", "repository", "dao"),
        isi=(r"(?i)\b(CREATE TABLE|ALTER TABLE|DROP TABLE)\b",
             r"(?i)\bSELECT\b.*\bFROM\b", r"\.(find|aggregate|query)\("),
        periksa=(
            "kolom yang dipakai menyaring/mengurutkan punya indeks",
            "perubahan skema bisa dijalankan pada data yang sudah ada",
            "tak ada kueri di dalam perulangan (N+1)",
        ), prioritas=10),
    Anggota(
        "Backend", "logika server & alur data",
        ekstensi=(".py", ".go", ".rb", ".java", ".rs", ".php"),
        jalur=("server", "service", "handler", "controller", "usecase", "core"),
        periksa=(
            "jalur gagal ditangani sejelas jalur berhasil",
            "operasi yang bisa diulang tak menghasilkan data ganda",
            "batas waktu & percobaan ulang jelas untuk panggilan keluar",
        ), prioritas=20),
    Anggota(
        "API", "kontrak antar-bagian",
        jalur=("api", "route", "endpoint", "rest", "graphql", "rpc"),
        isi=(r"(?i)\b(app|router)\.(get|post|put|patch|delete)\s*\(",
             r"(?i)\bfetch\s*\(", r"(?i)\baxios\."),
        periksa=(
            "kode status & bentuk galat konsisten dengan endpoint lain",
            "perubahan bentuk data tak mematahkan pemakai yang sudah ada",
            "masukan wajib divalidasi di pintu masuk, bukan di dalam",
        ), prioritas=20),
    Anggota(
        "Frontend", "struktur & keadaan antarmuka",
        ekstensi=(".jsx", ".tsx", ".vue", ".svelte", ".js", ".ts"),
        jalur=("component", "komponen", "page", "halaman", "view"),
        periksa=(
            "keadaan memuat, kosong, dan gagal punya tampilannya sendiri",
            "tak ada pekerjaan berat di jalur render",
            "data dari server dianggap bisa terlambat atau tak datang",
        ), prioritas=25),
    Anggota(
        "UI/UX", "kejelasan & alur pemakaian",
        ekstensi=(".css", ".scss", ".html", ".jsx", ".tsx", ".vue", ".svelte"),
        jalur=("style", "theme", "ui", "design"),
        isi=(r"(?i)\b(color|background|font-size|padding|margin)\s*:",),
        periksa=(
            "tiap aksi memberi umpan balik yang terlihat",
            "yang paling sering dipakai paling mudah dijangkau",
            "pesan galat memberi tahu apa yang harus dilakukan berikutnya",
        ), prioritas=30),
    Anggota(
        "Aksesibilitas", "bisa dipakai semua orang",
        ekstensi=(".html", ".jsx", ".tsx", ".vue", ".svelte", ".css"),
        isi=(r"<(button|a|img|input|label)\b", r"(?i)\baria-", r"(?i)\bonClick\b"),
        periksa=(
            "kontras teks terhadap latar minimal 4.5:1",
            "semua yang bisa diklik juga bisa dicapai lewat keyboard",
            "gambar bermakna punya teks alternatif; yang hiasan dikosongkan",
        ), prioritas=30),
    Anggota(
        "Responsif & Mobile", "layar sempit dan sentuhan",
        ekstensi=(".css", ".scss", ".html", ".jsx", ".tsx", ".vue"),
        isi=(r"(?i)@media\b", r"(?i)\b(width|min-width|max-width)\s*:",
             r"(?i)\b(flex|grid)\b"),
        periksa=(
            "tak ada yang terpotong pada lebar 360 px",
            "sasaran sentuh minimal 44x44 px",
            "tata letak tak bergantung pada hover semata",
        ), prioritas=35),
    Anggota(
        "Kinerja", "kecepatan yang terasa",
        ekstensi=(".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs"),
        isi=(r"\bfor\b.*\bfor\b", r"(?i)\bsleep\s*\(", r"(?i)\bwhile\s+True\b"),
        periksa=(
            "biaya tumbuh wajar saat data bertambah sepuluh kali",
            "hasil yang mahal & jarang berubah disimpan sementara",
            "yang tak dibutuhkan seketika dimuat belakangan",
        ), prioritas=40),
    Anggota(
        "QA & Pengujian", "bukti bahwa ia benar",
        ekstensi=(".py", ".js", ".ts", ".go", ".rs", ".java"),
        jalur=("test", "spec", "__tests__", "uji"),
        periksa=(
            "kasus batas diuji, bukan cuma jalur yang mulus",
            "uji menyatakan hasil yang benar, bukan sekadar 'tak melempar'",
            "gagalnya uji menunjukkan apa yang rusak",
        ), prioritas=15),
    Anggota(
        # Digabung dengan Pemantauan & Log: keduanya menjawab pertanyaan yang
        # sama — "apa yang bisa dilihat saat ada yang salah" — dan sebagai dua
        # anggota terpisah keduanya selalu bangun berbarengan lalu menghabiskan
        # jatah satu langkah berdua.
        "Galat & Pemantauan", "apa yang terlihat saat gagal",
        ekstensi=(".py", ".js", ".ts", ".go", ".rb", ".java"),
        jalur=("log", "monitor", "metric", "telemetry"),
        isi=(r"(?i)\b(try|except|catch|rescue)\b", r"(?i)\braise\b",
             r"(?i)\bthrow\b", r"(?i)\b(print|console\.log|logger|logging)\b"),
        periksa=(
            "galat yang ditelan diam-diam selalu jadi bug yang sulit dilacak",
            "pesan galat menyebut apa yang gagal dan nilai apa yang terlibat",
            "log memberi konteks untuk melacak satu kejadian, tanpa data rahasia",
        ), prioritas=15),
    Anggota(
        "Dependensi", "pustaka pihak ketiga",
        jalur=("requirements", "package.json", "pyproject", "go.mod",
               "cargo.toml", "gemfile", "pom.xml"),
        tool=("run_command", "run_script"),
        isi=(r"(?i)\b(pip|npm|yarn|pnpm)\s+(install|add)\b",),
        periksa=(
            "pustaka baru memang perlu — bukan menggantikan sepuluh baris kode",
            "versinya dipatok supaya hasilnya bisa diulang",
            "lisensi & kesehatan proyeknya layak dipakai",
        ), prioritas=25),
    Anggota(
        "DevOps & Rilis", "cara ia dijalankan",
        ekstensi=(".dockerfile", ".tf"),
        jalur=("dockerfile", "docker-compose", ".github", "ci", "deploy",
               "makefile", "workflow"),
        periksa=(
            "langkah yang sama menghasilkan hasil yang sama di mesin lain",
            "rahasia disuntikkan saat jalan, tak ikut ter-build",
            "ada jalan kembali bila rilisnya bermasalah",
        ), prioritas=35),
    Anggota(
        "Konfigurasi", "nilai yang berbeda antar-lingkungan",
        ekstensi=(".ini", ".cfg", ".toml", ".yaml", ".yml", ".env"),
        jalur=("config", "setting", "konfigurasi"),
        periksa=(
            "nilai bawaan aman untuk dipakai apa adanya",
            "konfigurasi yang salah ketahuan saat start, bukan saat dipakai",
            "beda antar-lingkungan hanya soal nilai, bukan soal kode",
        ), prioritas=40),
    Anggota(
        "Arsitektur", "bentuk keseluruhan",
        tool=("write_file", "move_file", "make_dir"),
        periksa=(
            "berkas baru diletakkan sesuai pola yang sudah ada di proyek",
            "ketergantungan mengalir satu arah, tak berputar",
            "satu bagian punya satu alasan untuk berubah",
        ), prioritas=45),
    Anggota(
        "Data & Analitik", "isi data dan maknanya",
        ekstensi=(".csv", ".parquet", ".ipynb"),
        jalur=("data", "etl", "pipeline", "analytic", "report", "laporan"),
        isi=(r"(?i)\b(pandas|numpy|dataframe|groupby)\b",),
        periksa=(
            "nilai kosong & pencilan ditangani sadar, bukan kebetulan",
            "satuan dan zona waktu dinyatakan tegas",
            "hasil hitungannya bisa ditelusuri balik ke sumbernya",
        ), prioritas=40),
    Anggota(
        "Dokumentasi", "supaya bisa dilanjutkan orang lain",
        ekstensi=(".md", ".rst", ".txt"),
        jalur=("readme", "docs", "changelog", "dokumentasi"),
        periksa=(
            "langkah menjalankan dari nol benar-benar bisa diikuti",
            "yang dijelaskan alasannya, bukan mengulang isi kode",
            "contohnya bisa disalin dan langsung jalan",
        ), prioritas=55),
    Anggota(
        # Digabung dengan Internasionalisasi: keduanya mengurus KATA yang sampai
        # ke pengguna, dan pemicunya nyaris selalu berbarengan.
        "Bahasa & Lokalisasi", "kata yang dibaca pengguna",
        ekstensi=(".html", ".jsx", ".tsx", ".vue", ".md", ".json"),
        jalur=("i18n", "locale", "lang", "translation", "terjemahan"),
        isi=(r"(?i)\b(placeholder|label|title|alert|toast|error)\b",
             r"(?i)\b(strftime|toLocaleString|Intl\.)\b"),
        periksa=(
            "istilahnya konsisten di seluruh antarmuka",
            "kalimatnya memakai bahasa pengguna, bukan bahasa mesin",
            "tanggal & angka diformat menurut lokal, bukan dipatok",
        ), prioritas=60),
    Anggota(
        "SEO & Metadata", "cara ia ditemukan",
        ekstensi=(".html",),
        jalur=("head", "meta", "sitemap", "robots"),
        isi=(r"(?i)<(title|meta|h1)\b",),
        periksa=(
            "tiap halaman punya judul & deskripsi sendiri",
            "hierarki heading urut, satu h1 per halaman",
            "tautan penting bisa dicapai tanpa menjalankan skrip",
        ), prioritas=65),
    Anggota(
        # Bekerja di AWAL giliran, sebelum satu berkas pun disentuh — lihat
        # Tim.awal(). Karena itu pemicunya tak perlu cocok dengan berkas apa pun.
        "Perencana", "urutan kerja & batas selesai",
        tool=("plan", "plan_step"),
        periksa=(
            "pecah jadi langkah yang tiap-tiapnya bisa dinyatakan selesai",
            "kerjakan yang menghalangi langkah lain lebih dulu",
            "sebutkan apa yang TIDAK dikerjakan, supaya batasnya jelas",
        ), prioritas=5),
    Anggota(
        # Penjaga konteks. Ini satu-satunya anggota yang tugasnya MENULIS,
        # bukan meninjau: tanpa catatan yang hidup, tiap sesi baru mulai dari
        # nol dan menebak ulang bentuk proyeknya — persis "konteksnya lari ke
        # mana-mana". Lihat juga Tim.perlu_agents_md().
        "Juru Konteks", "menjaga AGENTS.md tetap benar",
        jalur=("agents.md", "claude.md", "package.json", "requirements",
               "pyproject", "go.mod", "cargo.toml", "makefile"),
        tool=("make_dir", "move_file"),
        periksa=(
            "AGENTS.md WAJIB diperbarui begitu bentuk proyek berubah "
            "(folder/modul baru, perintah jalan/uji berubah, dependensi baru)",
            "isinya: cara menjalankan, cara menguji, peta folder, keputusan "
            "yang tak boleh diubah sembarangan — bukan salinan kode",
            "tulis dengan [[TOOL]] write_file/edit_file sekarang juga, jangan "
            "ditunda ke akhir giliran",
        ), prioritas=8),
    Anggota(
        "Produk & Aturan Bisnis", "apakah ini yang diminta",
        tool=("write_file", "edit_file", "edit_files", "replace_in_files"),
        periksa=(
            "yang dikerjakan menjawab permintaan aslinya, bukan tafsirnya",
            "kasus yang tak disebut pengguna dipilih dengan alasan, lalu dikatakan",
            "yang di luar permintaan tak ikut diubah diam-diam",
        ), prioritas=45),
)

assert len(ANGGOTA) == 23, f"tim harus 23 orang, sekarang {len(ANGGOTA)}"

# Regex dikompilasi SEKALI di impor. Pemicu diperiksa tiap langkah tool, dan
# mengompilasi ulang ~60 pola tiap kali adalah biaya yang tak perlu dibayar.
_ISI_RE: dict[str, tuple[re.Pattern, ...]] = {
    a.nama: tuple(re.compile(p) for p in a.isi) for a in ANGGOTA
}

# Kunci argumen tool yang berisi TULISAN (bukan path). Isinya yang diperiksa
# terhadap pola `isi`.
_KUNCI_ISI = ("content", "text", "isi", "new", "replacement", "command",
              "code", "script", "pattern")
# Kunci argumen tool yang berisi PATH.
_KUNCI_PATH = ("path", "dest", "src", "file", "dir")


def _kumpulkan(args: dict) -> tuple[list[str], str]:
    """Pisahkan argumen tool jadi (daftar path, gabungan tulisan).

    edit_files & replace_in_files membawa daftar suntingan alih-alih satu path,
    jadi keduanya ikut ditelusuri — kalau tidak, justru perubahan paling besar
    yang tak pernah membangunkan siapa pun."""
    paths: list[str] = []
    tulisan: list[str] = []

    def satu(d: dict) -> None:
        for k, v in d.items():
            if not isinstance(v, str) or not v:
                continue
            if k in _KUNCI_PATH:
                paths.append(v)
            elif k in _KUNCI_ISI:
                tulisan.append(v)

    satu(args)
    for e in (args.get("edits") or []):
        if isinstance(e, dict):
            satu(e)
    for p in (args.get("paths") or []):
        if isinstance(p, str):
            paths.append(p)
    return paths, "\n".join(tulisan)


# Tingkat bukti. Angka kecil = buktinya lebih menunjuk langsung ke anggota ini.
#   _LANGSUNG : nama jalur atau isi tulisan menyebut bidangnya (mis. berkas
#               "auth/login.py", atau kode yang memanggil eval)
#   _JENIS    : cuma jenis berkasnya yang cocok (mis. semua .py)
#   _UMUM     : cuma nama toolnya yang cocok (mis. semua write_file)
_LANGSUNG, _JENIS, _UMUM, _TAK_COCOK = 0, 1, 2, 9


def _cocok(a: Anggota, name: str, paths: list[str], tulisan: str) -> int:
    """Sekuat apa bukti bahwa anggota ini punya urusan dengan langkah tadi.

    Tingkat bukti dibedakan dari prioritas, dan itu bukan kerapian belaka —
    keduanya TERBUKTI saling menutupi saat dicoba:

      - pemicu NAMA TOOL cocok dengan hampir tiap penulisan berkas, jadi
        Arsitektur & Produk memborong jatah; menulis README membangunkan
        mereka, sementara Dokumentasi tak pernah kebagian;
      - pemicu EKSTENSI yang luas (.py/.js/.ts/…) cocok dengan hampir tiap
        berkas kode, jadi Keamanan & QA memborong; menulis util.go biasa
        membangunkan keduanya, sementara Backend tak pernah kebagian.

    Karena itu bukti yang MENUNJUK LANGSUNG (nama jalur / isi tulisan) selalu
    didahulukan di atas bukti jenis berkas, dan itu di atas bukti nama tool.
    Prioritas hanya memilah di dalam tingkat yang sama."""
    rendah = [p.lower() for p in paths]
    if a.jalur and any(j in p for p in rendah for j in a.jalur):
        return _LANGSUNG
    if tulisan:
        for pola in _ISI_RE[a.nama]:
            if pola.search(tulisan):
                return _LANGSUNG
    if a.ekstensi and any(
            os.path.splitext(p)[1] in a.ekstensi for p in rendah):
        return _JENIS
    if name in a.tool:
        return _UMUM
    return _TAK_COCOK


@dataclass
class Tim:
    """Keadaan tim SEPANJANG SATU GILIRAN.

    Menyimpan siapa yang sudah bicara, supaya tiap anggota bicara sekali saja —
    aturan yang menjaga fitur ini tetap berguna alih-alih jadi kebisingan yang
    diabaikan model."""

    aktif: bool = True
    sudah: set[str] = field(default_factory=set)
    riwayat: list[str] = field(default_factory=list)
    # Bentuk proyek berubah di giliran ini (folder/modul/dependensi/perintah)?
    _struktur_berubah: bool = False
    # AGENTS.md sudah disentuh di giliran ini?
    _agents_ditulis: bool = False
    # Tuntutan menulis AGENTS.md sudah disampaikan? (sekali saja per giliran)
    _agents_diminta: bool = False

    def awal(self) -> str:
        """Blok yang ditempel ke pesan TUGAS pertama — jatah si Perencana.

        Perencana bekerja sebelum satu berkas pun disentuh, jadi ia tak bisa
        menunggu dibangunkan oleh langkah tool seperti yang lain: saat langkah
        pertama terjadi, keputusan yang seharusnya ia bantu sudah diambil."""
        if not self.aktif:
            return ""
        p = next((a for a in ANGGOTA if a.nama == "Perencana"), None)
        if p is None or p.nama in self.sudah:
            return ""
        self.sudah.add(p.nama)
        self.riwayat.append(p.nama)
        return ("\n\n[TIM] Sebelum mulai, sudut pandang perencana:\n"
                + p.catatan())

    def perlu_agents_md(self) -> str:
        """Tuntutan memperbarui AGENTS.md — bila memang perlu & belum diminta.

        Inilah bagian yang membuat konteks tak lari ke mana-mana: begitu bentuk
        proyek berubah, catatannya harus ikut berubah DI GILIRAN YANG SAMA.
        Ditunda ke lain waktu berarti tak pernah — dan sesi berikutnya menebak
        ulang bentuk proyek dari nol."""
        if not self.aktif or self._agents_diminta:
            return ""
        if not self._struktur_berubah or self._agents_ditulis:
            return ""
        self._agents_diminta = True
        return (
            "\n\n[TIM — Juru Konteks] Bentuk proyek berubah di giliran ini, "
            "sementara AGENTS.md belum ikut diperbarui. Perbarui SEKARANG lewat "
            "[[TOOL]] (baca dulu bila sudah ada, lalu tulis versi barunya): "
            "cara menjalankan, cara menguji, peta folder, dan keputusan yang "
            "tak boleh diubah sembarangan. Ini bukan pekerjaan tambahan — "
            "tanpanya sesi berikutnya menebak ulang proyek ini dari nol."
        )

    def _catat_struktur(self, name: str, paths: list[str]) -> None:
        """Catat apakah langkah ini mengubah BENTUK proyek (bukan isi berkas)."""
        rendah = [p.lower().replace("\\", "/") for p in paths]
        if any(p.endswith(("agents.md", "claude.md")) for p in rendah):
            self._agents_ditulis = True
            return
        if name in ("make_dir", "move_file"):
            self._struktur_berubah = True
            return
        # Berkas yang MENDEFINISIKAN cara proyek dijalankan/dibangun.
        penanda = ("package.json", "requirements.txt", "pyproject.toml",
                   "go.mod", "cargo.toml", "makefile", "dockerfile",
                   "docker-compose.yml")
        if any(p.rsplit("/", 1)[-1] in penanda for p in rendah):
            self._struktur_berubah = True

    def tinjau(self, name: str, args: dict, sukses: bool) -> list[Anggota]:
        """Anggota mana yang bangun untuk langkah ini (dan dicatat sudah bicara).

        `sukses=False` -> tak ada yang bangun: meninjau perubahan yang gagal
        diterapkan hanya membuang perhatian model ke sesuatu yang tak ada."""
        if not self.aktif or not sukses:
            return []
        paths, tulisan = _kumpulkan(args)
        self._catat_struktur(name, paths)
        calon = []
        for a in ANGGOTA:
            if a.nama in self.sudah:
                continue
            kuat = _cocok(a, name, paths, tulisan)
            if kuat != _TAK_COCOK:
                calon.append((kuat, a.prioritas, a))
        # Bukti spesifik dulu, baru prioritas di dalam tingkat yang sama.
        calon.sort(key=lambda x: (x[0], x[1]))
        terpilih = [a for _, _, a in calon[:_MAKS_PER_LANGKAH]]
        for a in terpilih:
            self.sudah.add(a.nama)
            self.riwayat.append(a.nama)
        return terpilih

    def blok(self, anggota: list[Anggota]) -> str:
        """Blok yang ditempel ke pesan berikutnya untuk model."""
        if not anggota:
            return ""
        isi = "\n\n".join(a.catatan() for a in anggota)
        return (
            "\n\n[TIM] Rekan satu tim ikut melihat langkah barusan. Terapkan "
            "yang relevan sekarang juga — kalau memang sudah beres, lanjut "
            "tanpa berkomentar.\n" + isi
        )


def cari(nama: str) -> Anggota | None:
    """Anggota dengan nama ini (tak peka huruf besar/kecil, boleh sebagian)."""
    n = nama.strip().lower()
    if not n:
        return None
    for a in ANGGOTA:
        if a.nama.lower() == n:
            return a
    for a in ANGGOTA:
        if n in a.nama.lower():
            return a
    return None

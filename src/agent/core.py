"""Agent inti: satu giliran percakapan + eksekusi tool. Dipakai semua antarmuka.

SELURUH model bagas-ai berbasis browser (lihat models.py), jadi tiap giliran
diteruskan ke situs AI web lewat Playwright dan tool "dipanggil" memakai
protokol penanda [[TOOL]] di bawah. Jalur API NVIDIA — streaming delta,
tool-calling gaya OpenAI, retry rate-limit, watchdog macet, naik-kelas otomatis
— sudah dihapus seluruhnya.

Menangani: system prompt dinamis, protokol tool web, kaitan sesi terminal <->
percakapan browser, dan penyimpanan sesi.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import json
import logging
import mimetypes
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import config, interaction, konteks, llm, models, prefs, prompts
from . import tim as _tim
from .memory import Memory
from .session import Session
from .tools import base as tools
from .tools import katalog as _katalog

# Modul ini SUDAH memanggil log.debug (di _padatkan_web) sejak lama tanpa pernah
# punya logger — NameError-nya menunggu di jalur yang paling tak boleh gagal:
# tepat saat chat lama menolak meringkas, yaitu satu-satunya alasan blok
# `except` itu ada.
log = logging.getLogger(__name__)

# --- Protokol tool untuk CONNECTOR web (Kimi/Qwen web) ---
# AI web tak punya function-calling API, jadi kita ajari ia "memanggil" tool
# dengan menuliskan blok teks bertanda kurung siku ganda yang mudah di-parse,
# lalu bagas-ai mengeksekusi tool itu SUNGGUHAN di laptop (mesin tool yang sama
# di laptop) dan mengirim balik hasilnya — berulang sampai selesai.
# Penanda ditulis ulang oleh AI web dengan variasi kecil (spasi di dalam kurung,
# huruf kecil), jadi polanya dibuat longgar — kalau tidak, penanda lolos ke layar.
_OPEN_MARK = r"\[\[\s*TOOL\s*\]\]"
_CLOSE_MARK = r"\[\[\s*/\s*TOOL\s*\]\]"
# Sebagian model punya format pemanggilan tool BAWAAN dan memakainya walau
# diminta memakai penanda kita — Qwen, misalnya, mengeluarkan
# <tool_call>{...}</tool_call>. Menerima kedua bentuk jauh lebih murah daripada
# memaksa model mengubah kebiasaannya, dan isinya sama-sama JSON.
_ALT_OPEN = r"<\s*tool_call\s*>"
_ALT_CLOSE = r"<\s*/\s*tool_call\s*>"
_WEB_TOOL_RE = re.compile(
    _OPEN_MARK + r"(.*?)" + _CLOSE_MARK
    + r"|" + _ALT_OPEN + r"(.*?)" + _ALT_CLOSE,
    re.DOTALL | re.IGNORECASE)
# Pagar blok kode markdown (```json / ```): AI web sering merender usulan tool
# sebagai blok kode, jadi pagarnya harus dibuang sebelum JSON di-parse.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*")
# Penanda dari tool yang MENGHASILKAN GAMBAR (lihat tools/screen.py). File-nya
# dilampirkan ke pesan berikutnya supaya AI web benar-benar bisa MELIHATnya.
_IMAGE_MARK_RE = re.compile(r"^\[GAMBAR\][ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Pengingat singkat yang ditempel di TIAP giliran (selain yang pertama, yang
# sudah memuat protokol penuh). Tanpa ini, percakapan panjang membuat AI web
# lupa dan kembali menampilkan kode untuk disalin manual.
_WEB_REMINDER = (
    "[Pengingat: kalau permintaan ini perlu MENGUBAH file atau menjalankan "
    "sesuatu, keluarkan blok [[TOOL]] — jangan menampilkan kode untuk kusalin "
    "sendiri. Aku yang mengeksekusi dan mengirim balik hasilnya. SATU blok "
    "[[TOOL]] per pesan, jangan menumpuk dua atau lebih: tunggu [[HASIL]] dulu "
    "baru tentukan langkah berikutnya. Buka tiap pesan bertool dengan SATU "
    "kalimat pendek yang memberi tahu apa yang SEDANG kamu lakukan (mis. "
    "'Aku baca dulu main.py-nya.') — kalimat itu yang dilihat pengguna. "
    "Bentuk 'sekarang saya akan …' dan rangkaian rencana beberapa langkah ke "
    "depan JANGAN dipakai: bloknya sudah ada di pesan yang sama, jadi kamu "
    "bukan akan mengerjakan — kamu sedang mengerjakan. "
    "JANGAN memakai tool bawaanmu sendiri (Execute Python code, Run code, "
    "Code Interpreter, sandbox Python, pencarian web bawaan, artifact, "
    "canvas): semuanya berjalan di server layananmu dan TIDAK menyentuh "
    "berkas di laptopku, jadi hasilnya fiksi dan tak ada yang berubah. "
    "Lupa tool yang tersedia? panggil cari_tool('kebutuhanmu') — ia mencari "
    "dari SEMUA tool dan memberi nama persisnya; jangan menebak nama tool. "
    "Untuk membaca berkas pakai read_file, untuk menjalankan Python di "
    "laptopku pakai run_python — keduanya lewat [[TOOL]]. "
    "Untuk MENGUBAH file yang sudah ada: baca dulu (read_file) lalu keluarkan "
    "edit_file berisi HANYA potongan yang berubah (old_text/new_text) — jangan "
    "write_file seluruh file, dan jangan menulis file lewat run_python/"
    "run_command. write_file cuma untuk file BARU atau tulis-ulang-total dengan "
    "isi lengkap. File BARU yang PANJANG (> ±100 baris) WAJIB ditulis BERTAHAP "
    "tanpa diminta: write_file bagian awal, lalu append_file lanjutannya pesan "
    "demi pesan — pesan yang kepanjangan DIPOTONG situs dan blok terpotong tak "
    "bisa kueksekusi. Jangan pernah menyuruh pengguna melakukan langkah manual "
    "apa pun. LANGSUNG KE INTI: tanpa pembuka basa-basi, tanpa mengulang "
    "permintaan, langkah seminimal mungkin — begitu cukup info, langsung "
    "kerjakan/jawab. Hanya tool file yang menampilkan diff berwarna untuk "
    "kutinjau sebelum file disentuh.]"
)
# Batas langkah tool per giliran web (jaring anti-loop-liar).
#
# Dinaikkan dari 24 ke 60 karena batasnya TERLALU SERING tercapai pada pekerjaan
# yang sah, bukan pada loop liar: membangun satu halaman baru saja gampang
# menghabiskan 20+ langkah (peta proyek, baca beberapa berkas, tulis komponen,
# perbaiki impor, jalankan dev server, preview, perbaiki tampilan, preview
# lagi). Giliran yang terpotong di tengah lebih merugikan daripada giliran yang
# kepanjangan — pekerjaan setengah jadi harus diulang dari awal oleh pengguna.
#
# Menaikkannya tetap aman karena loop liar sudah dijaga oleh penjaga LAIN yang
# jauh lebih cepat bereaksi dan tak bergantung pada angka ini: dup_hits
# (langkah persis sama berulang) dan fail_streak (gagal/timeout beruntun),
# keduanya menyerah setelah 3 kali. Batas ini cuma jaring terakhir.
_WEB_MAX_STEPS = 60
# Sisa langkah saat pengguna & model mulai diberi tahu bahwa batasnya dekat.
# Diberi tahu SEBELUM mentok, bukan sesudah: kalau sudah mentok, satu-satunya
# yang bisa dilakukan tinggal menyesal.
_SISA_LANGKAH_PERINGATAN = 8

# Label UI yang DISISIPKAN situs saat memotong jawaban yang terlalu panjang
# (terlihat di kimi.com sebagai "Output stopped" di bawah kode). Dijangkar ke
# EKOR teks supaya kalimat biasa yang kebetulan memuat frasa ini tak tertangkap.
_OUTPUT_STOP_RE = re.compile(
    r"\n?[ \t]*(?:Output stopped|Generation stopped|输出已停止)\.?[ \t]*$",
    re.IGNORECASE)
# Berapa kali maksimal meminta lanjutan output yang terpotong dalam SATU kirim.
_MAX_LANJUT = 3
# Berapa kali AI web boleh didesak MENUTUP dengan teks biasa sesudah tool-nya
# dimatikan (dup_hits/fail_streak). Model yang sedang macet cenderung membalas
# desakan itu dengan blok langkah LAGI; tanpa batas ini giliran bisa berputar
# di antara "simpulkan" dan blok tool sampai kuota habis.
_MAKS_TUTUP = 2
# Berapa kali konteks boleh dipadatkan otomatis dalam SATU giliran. Lebih dari
# ini berarti meringkas pun tak menolong, dan memutar terus cuma menghabiskan
# kuota situs tanpa memajukan pekerjaan.
_MAKS_PADAT = 2

# Tool yang benar-benar MENGUBAH berkas kode. Dipakai untuk memutuskan apakah
# perlu validasi otomatis sebelum jawaban akhir (kalau tak ada kode yang berubah,
# tak ada yang perlu divalidasi). Ekstensi berkas kode difilter terpisah agar
# perubahan pada aset/teks (mis. gambar hasil download) tak memicu validasi.
_TOOL_MUTASI = {
    "write_file", "edit_file", "append_file", "replace_in_files", "move_file",
}
# Tool yang MENGUBAH KEADAAN (file/sistem/proses). Sesudah salah satunya SUKSES,
# cache anti-ulang dibersihkan: hasil read_file/list_dir/run_command yang
# tersimpan sudah BASI — mengembalikannya lagi (plus teguran "kamu sudah
# menjalankan langkah ini") membuat AI web bekerja dari isi file lama dan
# menuduhnya mengulang padahal ia memverifikasi perubahan yang sah.
_TOOL_STATE = _TOOL_MUTASI | {
    "delete_file", "copy_file", "make_dir", "download_file", "zip_extract",
    "zip_create", "run_command", "run_python", "run_script", "run_command_bg",
    "bg_stop", "bg_send", "undo_changes",
}
_EKS_KODE = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
    ".svelte", ".go", ".rs", ".php", ".rb", ".java", ".kt", ".c", ".h",
    ".cpp", ".cc", ".cs", ".swift", ".dart", ".json", ".css", ".scss",
}
# Batas panjang hasil tool yang dikirim balik ke AI web (hemat & fokus).
_WEB_RESULT_CAP = 6000


def _menyentuh_kode(name: str, args: dict) -> bool:
    """True bila argumen tool menyasar berkas ber-EKSTENSI KODE.

    Perubahan pada aset/teks biasa (gambar, .md, .txt) tak perlu memicu validasi
    kompilasi/lint. replace_in_files tak menyebut satu path pasti, jadi selalu
    dianggap menyentuh kode (aman: paling banter validasi berjalan sia-sia)."""
    if name == "replace_in_files":
        return True
    for kunci in ("path", "dest"):
        nilai = args.get(kunci)
        if isinstance(nilai, str) and nilai:
            import os as _os
            if _os.path.splitext(nilai)[1].lower() in _EKS_KODE:
                return True
    return False


# Berkas yang hasilnya KELIHATAN — mengubahnya menuntut pembuktian visual,
# bukan sekadar "lint lulus". Sengaja tak memasukkan .js/.py: keduanya bisa saja
# menggambar UI, tapi jauh lebih sering bukan, dan memaksa screenshot pada tiap
# perubahan logika akan membakar giliran untuk hal yang tak ada gunanya dilihat.
_EKS_TAMPILAN = {".tsx", ".jsx", ".vue", ".svelte", ".astro", ".html", ".htm",
                 ".css", ".scss", ".sass", ".less", ".styl"}


def _menyentuh_tampilan(args: dict) -> bool:
    """True bila argumen tool menyasar berkas yang hasilnya kelihatan di layar."""
    import os as _os
    for kunci in ("path", "dest"):
        nilai = args.get(kunci)
        if isinstance(nilai, str) and nilai:
            if _os.path.splitext(nilai)[1].lower() in _EKS_TAMPILAN:
                return True
    # edit_files membawa daftar suntingan, bukan satu path.
    for e in (args.get("edits") or []):
        if isinstance(e, dict):
            nilai = e.get("path")
            if isinstance(nilai, str) and _os.path.splitext(
                    nilai)[1].lower() in _EKS_TAMPILAN:
                return True
    return False


# Pertanyaan pilihan yang ditulis sebagai TEKS BIASA. Pesan tanpa blok
# diperlakukan sebagai jawaban akhir, jadi pertanyaan begini tak pernah sampai
# ke pengguna sebagai pertanyaan — gilirannya habis dan ia cuma melihat AI
# balik bertanya. Yang benar: ask_user, yang sungguh-sungguh menunggu jawaban.
_TANYA_PILIHAN_RE = re.compile(
    r"\b(?:atau|mana|sebaiknya|pilih|preferensi|lebih suka|mau yang)\b", re.I)


def _tanya_tanpa_ask_user(text: str) -> bool:
    """True bila balasan berupa pertanyaan pilihan, bukan jawaban."""
    t = (text or "").strip()
    if not t or len(t) > 900 or "?" not in t:
        return False
    # Harus BERAKHIR sebagai pertanyaan: laporan hasil yang kebetulan memuat
    # tanda tanya di tengah bukan sasaran teguran ini.
    if not t.rstrip().endswith("?"):
        return False
    return bool(_TANYA_PILIHAN_RE.search(t))


# --- pesan susulan: pengguna mengetik SELAGI giliran berjalan ---------------
#
# Sebelumnya pesan begitu diparkir sampai giliran sekarang benar-benar tuntas,
# lalu dijalankan sebagai giliran terpisah. Itu benar secara mesin tapi salah
# secara percakapan: pengguna sering mengirimnya justru KARENA melihat arah
# kerjanya meleset ("bukan itu", "sekalian rapikan header-nya"), dan menahannya
# berarti AI meneruskan pekerjaan yang sudah tak diinginkan sampai selesai.
#
# Sekarang ia DISISIPKAN ke giliran yang sedang berjalan, di batas langkah
# berikutnya — yaitu bersama [[HASIL]]. Batas itu dipilih dengan sengaja: di
# situ AI baru saja menutup satu pesan, jadi tak ada balasan yang terpotong di
# tengah, dan ia memang sedang memutuskan langkah berikutnya.
#
# Urutan kerjanya diserahkan ke AI, bukan dipaksa dari sini: hanya ia yang tahu
# seberapa jauh pekerjaan sekarang sudah berjalan dan seberapa berat permintaan
# barunya. Yang dipaksa cuma satu — keputusannya harus DIUCAPKAN, supaya
# pengguna tak menebak-nebak mana yang sedang dikerjakan.
def _blok_sisipan(pesan: list[str]) -> str:
    """Rakit blok "pesan baru dari pengguna" untuk ditempel ke [[HASIL]]."""
    bersih = []
    for t in pesan:
        t = (t or "").strip()
        if not t:
            continue
        # Penanda protokol DINETRALKAN. Pengguna bisa saja mengetik "[[HASIL]]"
        # atau "[[/TOOL]]" — entah tak sengaja saat menempel log, entah iseng —
        # dan penanda asli di tengah teks pengguna akan mengacaukan pembacaan
        # blok di giliran ini.
        bersih.append(t.replace("[[", "⟦").replace("]]", "⟧"))
    if not bersih:
        return ""
    isi = "\n\n".join(bersih)
    ganda = len(bersih) > 1
    return (
        "\n\n[[PESAN BARU DARI PENGGUNA]]\n" + isi + "\n[[/PESAN BARU]]\n"
        + (f"({len(bersih)} pesan, urut dari yang paling awal.)\n" if ganda else "")
        + "Pesan itu dikirim SELAGI kamu bekerja, jadi pengguna belum melihat "
        "hasil langkah di atas.\n"
        "Putuskan sendiri urutannya lalu KERJAKAN — jangan balik bertanya mana "
        "dulu. Pegangannya:\n"
        "- Mengubah/membatalkan tugas yang sedang berjalan (mis. 'bukan itu', "
        "'ganti jadi…') -> hentikan yang lama, kerjakan yang baru.\n"
        "- Cepat & tak bergantung pekerjaan sekarang -> selesaikan yang cepat "
        "dulu, lalu lanjutkan yang tadi.\n"
        "- Berat & berdiri sendiri -> tuntaskan dulu yang sedang berjalan, baru "
        "kerjakan ini. Jangan tinggalkan pekerjaan setengah jadi.\n"
        "Sebutkan pilihanmu dalam satu kalimat pembuka (mis. 'Aku selesaikan "
        "dulu X yang tinggal sedikit, baru kukerjakan Y.') supaya pengguna tahu "
        "mana yang sedang berjalan. Tetap satu blok [[TOOL]] per pesan."
    )


def _potong_tengah(teks: str, batas: int) -> str:
    """Pangkas teks panjang dari TENGAH, sisakan kepala & ekornya.

    Dipakai untuk riwayat percakapan yang disimpan ke berkas memory. Memotong
    dari ekor (cara yang biasa) justru membuang bagian paling berguna: hasil
    akhir sebuah langkah, galat yang muncul di baris terakhir, atau penutup
    berkas yang baru ditulis. Dua ujungnya dipertahankan; yang hilang bagian
    tengah, dan besarnya disebutkan apa adanya supaya tak ada yang mengira
    berkasnya memang sependek itu."""
    if batas <= 0 or len(teks) <= batas:
        return teks
    sisi = max(batas // 2 - 40, 80)
    hilang = len(teks) - sisi * 2
    if hilang <= 0:
        return teks
    return (f"{teks[:sisi]}\n"
            f"… [{hilang} karakter di tengah dipotong bagas-ai] …\n"
            f"{teks[-sisi:]}")


_BULAN_ID = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
             "Agustus", "September", "Oktober", "November", "Desember")


def _tahun_ini() -> str:
    return str(_dt.date.today().year)


def _hari_ini() -> str:
    """Tanggal hari ini dalam bahasa Indonesia.

    Dikirim ke model karena ia TIDAK PUNYA cara lain mengetahuinya: tanggal tak
    pernah ada di prompt sebelumnya, sehingga instruksi "cari dokumentasi
    terbaru" mustahil dijalankan — model tak tahu "terbaru" itu tahun berapa,
    dan biasanya menebak dari batas pengetahuannya sendiri yang sudah lewat."""
    t = _dt.date.today()
    return f"{t.day} {_BULAN_ID[t.month - 1]} {t.year}"


def _web_tool_protocol() -> str:
    """KONSTITUSI protokol tool untuk AI web: aturan yang berlaku di SETIAP
    giliran, dan hanya itu.

    Dirampingkan dari ~27,8 rb karakter jadi sekitar sepertiganya. Yang dibuang
    BUKAN aturannya — tiap aturan di sini lahir dari bug yang pernah terlihat,
    jadi menghapusnya berarti mengundang bug itu kembali. Yang dipindahkan
    adalah TEMPATNYA:

      - aturan situasional -> jadi teguran yang dikirim tepat saat pelanggaran
        terjadi (lihat gelung di _run_connector: janji tanpa blok, kode tak
        ditulis ke berkas, tampilan diubah tapi tak pernah dilihat, bertanya
        tanpa ask_user);
      - panduan per-tool -> jadi deskripsi tool itu sendiri, yang memang sudah
        ikut di katalog;
      - panduan per-kategori (mis. strategi mengunduh aset) -> ke
        tools/katalog.py, sampai hanya kepada giliran yang memintanya.

    Alasannya terukur di proyek ini sendiri: aturan prosa di pesan pembuka
    DILANGGAR (Kimi tetap memakai "Execute Python code" walau dilarang aturan
    7), sedangkan teguran yang tiba di detik kesalahannya BEKERJA. Yang datang
    tepat waktu mengalahkan yang datang lebih dulu — bahkan bila kalimatnya
    sama persis.

    Penegasan huruf besar juga ditekan (dari 36 jadi segelintir): kalau
    semuanya WAJIB, model memilih sendiri mana yang benar-benar wajib.
    """
    return (
        "Konteks jujur soal percakapan ini, supaya tidak salah paham:\n\n"
        "Pesan-pesan ini dikirim & dibaca oleh **bagas-ai**, program agent "
        "open-source yang berjalan di laptop saya (saya pemiliknya, dan saya "
        "yang meminta ini). Kamu tidak mengeksekusi apa pun sendiri — kamu "
        "tetap model bahasa yang menghasilkan teks. Perannya begini:\n"
        "  1. Kamu mengusulkan SATU langkah dalam format di bawah.\n"
        "  2. bagas-ai di laptop saya menjalankannya sungguhan, lalu\n"
        "  3. mengirim hasil nyatanya balik ke percakapan ini.\n\n"
        "Kamu otaknya, program lokal itu tangannya. Kalau sebuah usulan gagal "
        "dijalankan, kamu akan menerima pesan errornya.\n\n"
        f"Hari ini {_hari_ini()}.\n\n"
        "== FORMAT USULAN ==\n"
        "JSON di dalam blok kode ```json (teks biasa dirusak perenderan situs — "
        "__nama__ berubah jadi tebal):\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "<nama_tool>", "args": {"<param>": "<nilai>"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "Contoh:\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "write_file", "args": {"path": "contoh.py", '
        '"content": "def halo():\\n    print(\'hai\')\\n"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "== TUJUH ATURAN YANG TAK BISA DITAWAR ==\n"
        "1. JSON valid (newline jadi \\n, kutip jadi \\\"), selalu di dalam "
        "```json di antara penanda [[TOOL]].\n"
        "2. SATU blok per pesan. Jangan pernah menumpuk dua atau lebih, walau "
        "langkahnya terasa independen — langkah kedua pasti kamu susun sebelum "
        "melihat hasil langkah pertama, jadi ia cuma tebakan, dan tetap "
        "kujalankan walau yang pertama ternyata gagal.\n"
        "3. Kalimat niat dan bloknya SEPAKET dalam satu pesan. Kalau kamu "
        "menulis 'aku baca dulu main.py', blok read_file-nya ada di pesan yang "
        "sama, tepat di bawahnya. Pesan tanpa blok kuanggap jawaban akhir — "
        "jadi 'rencana dulu, blok menyusul' membuat gilirannya habis percuma.\n"
        "4. Perubahan berkas lewat write_file/edit_file/edit_files saja — bukan "
        "lewat run_python/run_command (open().write, sed -i, echo >, heredoc, "
        "skrip generator). Bukan soal gaya: untuk write_file dan edit_file, "
        "bagas-ai menampilkan diff berwarna ke pengguna SEBELUM berkasnya "
        "disentuh. Perubahan lewat skrip tak terlihat sama sekali, dan pengguna "
        "kehilangan satu-satunya kesempatan meninjau.\n"
        "5. Jangan memakai tool bawaanmu sendiri: 'Execute Python code', 'Run "
        "code', 'Code Interpreter', sandbox Python, pencarian web bawaan, "
        "artifact, canvas. Sandbox itu berjalan di server layananmu, bukan di "
        "laptop saya — ia tak punya satu pun berkas proyek ini, jadi apa pun "
        "yang kamu 'baca' di sana adalah fiksi dan tak ada berkas saya yang "
        "berubah. Untuk membaca berkas: read_file. Untuk menjalankan Python di "
        "laptop saya: run_python. Keduanya lewat [[TOOL]].\n"
        "6. Selesai = balas tanpa blok. Jawaban akhir tak boleh menyuruh saya "
        "mengerjakan hal yang bisa kamu lakukan sendiri lewat tool (menyalin "
        "kode, menulis sisa berkas, menjalankan perintah). Selesaikan dulu, "
        "baru menutup.\n"
        "7. Bingung, atau ada beberapa pendekatan yang sama-sama masuk akal? "
        "panggil ask_user(question, options) dengan 2-6 opsi konkret yang "
        "bisa dibandingkan — jangan menebak, dan jangan bertanya lewat teks "
        "biasa (pesan tanpa blok itu jawaban akhir; pertanyaanmu takkan pernah "
        "terjawab).\n"
        "    Pilih bentuk menunya dengan sadar lewat `multiple`: false untuk "
        "pilihan yang saling MENIADAKAN (modal ATAU halaman sendiri), true "
        "untuk yang bisa berdampingan (fitur mana SAJA yang dipasang). Uji "
        "cepatnya — kalau memilih dua sekaligus masuk akal, pakai true.\n"
        "    Pengguna selalu bisa mengetik jawabannya sendiri di menu itu, "
        "jadi opsimu tak perlu mencakup segala kemungkinan; cukup yang paling "
        "mungkin. Kalau jawabannya berupa beberapa hal, ia kembali bernomor "
        "'(1) … ; (2) …' — baca semuanya, jangan cuma yang pertama.\n\n"
        "== CARA BICARA ==\n"
        "Setiap pesan berisi blok dibuka dengan SATU kalimat pendek orang "
        "pertama tentang apa yang sedang kamu lakukan, menyebut berkas/perintah "
        "yang kamu sentuh. Kalimat itu yang tampil di layar pengguna — ia tak "
        "melihat blok tool-nya, jadi tanpa kalimat itu layarnya sunyi.\n"
        "Bentuknya SEDANG mengerjakan, bukan AKAN mengerjakan. Kamu memang "
        "sedang mengerjakannya — bloknya ada di pesan yang sama, di bawah "
        "kalimat itu — jadi menuliskannya sebagai rencana justru keliru, dan "
        "bagi pengguna terbaca seperti kamu menunda.\n"
        "  BURUK: 'Oke, sekarang saya akan membaca file WorksSection.tsx untuk "
        "memahami strukturnya, lalu saya akan membuat komponen detailnya, "
        "setelah itu saya akan memvalidasinya.'\n"
        "  BAIK:  'Aku baca WorksSection.tsx dulu.'\n"
        "  BAIK:  'Ketemu — error-nya di baris 42, aku perbaiki sekarang.'\n"
        "Jangan mengumumkan rencana beberapa langkah ke depan (untuk itu ada "
        "tool plan). Satu kalimat untuk satu langkah yang SEDANG kamu kerjakan; "
        "langkah berikutnya diumumkan nanti saat gilirannya tiba. Pakai bahasa "
        "pengguna. Tanpa sapaan kosong, tanpa mengulang permintaan dengan "
        "kata-katamu sendiri, tanpa minta izin untuk langkah yang jelas perlu.\n"
        "Jawaban akhir: hasilnya dulu di kalimat pertama, detail seperlunya.\n\n"
        "== KEBIASAAN YANG DIHARAPKAN ==\n"
        "- Kerjakan yang diminta saja; jangan memperluas cakupan sendiri.\n"
        "- Jangan membaca ulang berkas yang isinya sudah ada di percakapan ini, "
        "dan jangan memverifikasi ulang langkah yang jelas berhasil. Read_file "
        "sendiri MENOLAK baca ulang berkas yang tak berubah ('[SUDAH DIBACA]') — "
        "hormati itu, jangan panggil dengan force=True kecuali isinya benar-benar "
        "hilang dari konteks.\n"
        "- Kerja yang barusan dilakukan TERCATAT otomatis di worklog. Untuk "
        "mengingat apa yang sudah kamu lakukan sesi ini (file yang disentuh, "
        "hasil tool), panggil kerja_terakhir() — BUKAN membaca ulang berkas. "
        "Setelah langkah penting, kunci konteksnya dengan catat_kerja(...) "
        "(mis. penyebab error, keputusan desain) supaya tak perlu ditebak ulang.\n"
        "- Sebelum menjelajah untuk tugas baru, panggil sasaran(tugas) untuk tahu "
        "berkas mana yang paling relevan — lalu baca hanya yang di daftar itu.\n"
        "- Hal kompleks yang bisa diuji: UJI dengan kode Python, jangan menebak. "
        "test_function(path, symbol, args) menjalankan satu fungsi/kelas dari "
        "berkasnya dengan argumen contoh; run_python untuk eksperimen bebas. "
        "Logika yang rumit (penguraian, perhitungan, transformasi data, "
        "perilaku tepi) hampir selalu layak diuji dulu — uji yang gagal adalah "
        "bukti, bukan perasaan.\n"
        "- validate_project MENYUSUN SENDIRI rencana ujinya (skrip package.json, "
        "runner, README) — tapi lint saja sering tidak membuktikan apa-apa. "
        "CIPTAKAN uji spesifik untuk perubahanmu lewat extra_checks (satu per "
        "baris): 'pytest tests/test_util.py -k hitung', 'python -c \"from "
        "src.a import b; b()\"', 'node -e ...' — validasi yang tahu persis apa "
        "yang barusan diubah jauh lebih berharga daripada validasi umum.\n"
        "- Mulai dari peta proyek di bawah. search_text/search_multi_text/glob_files "
        "jauh lebih cepat daripada list_dir berulang, dan project_info memberi ekosistem, "
        "skrip, serta entry point proyek dalam satu panggilan.\n"
        "- Sesudah mengubah kode, WAJIB periksa manual tiap perubahan secara "
        "teliti: baca ulang berkas yang diubah dan berkas lain yang berhubungan "
        "dengannya untuk memastikan tak ada error logika, variabel tak terdefinisi, "
        "atau efek samping. JANGAN HANYA andalkan validate_project — tool itu cuma "
        "cek sintaksis dasar (py_compile), bukan logika atau kelengkapan kode. "
        "Setelah yakin manual, baru jalankan validate_project (isi paths dengan "
        "berkas yang kamu ubah)."
        + (" Sesudah mengubah tampilan, lihat sendiri "
           "hasilnya: web_preview untuk halaman web, take_screenshot untuk "
           "aplikasi desktop — lalu sebutkan apa yang kamu lihat.\n"
           if config.WEB_PREVIEW else
           " Sesudah mengubah tampilan, pastikan strukturnya benar dari "
           "berkasnya (class/id/kondisi tampil tiap keadaan).\n")
        + "- Sebelum memakai pustaka/framework yang cepat berubah, pastikan "
        "dengan web_search memakai tahun berjalan di kuerinya (mis. "
        f"'next.js app router {_tahun_ini()} docs') — ingatanmu punya batas "
        "waktu. Tak perlu untuk kode proyek ini sendiri atau sintaks dasar.\n"
        "- Tugas 3 langkah atau lebih: buka dengan plan(steps=[...]), lalu "
        "plan_step(n) tiap berpindah langkah. Rencananya tampil ke pengguna.\n"
        "- Berkas panjang ditulis bertahap: write_file bagian awal (±100 "
        "baris), lalu append_file lanjutannya di pesan berikutnya. Pesan yang "
        "kepanjangan dipotong situs ini, dan blok yang terpotong tak bisa "
        "kujalankan sama sekali.\n"
        "- read_files membaca sampai 5 berkas sekali jalan, edit_files "
        "menerapkan sampai 8 suntingan — pakai begitu kamu sudah tahu pasti "
        "daftarnya, karena satu panggilan = satu kali menunggu.\n"
        "- Path relatif ke folder proyek, pakai garis miring biasa "
        "(src/app/main.py). Berkas di luar folder proyek boleh diminta, tapi "
        "saya menanyakan izin pengguna dulu; kalau hasilnya '[DITOLAK]', itu "
        "keputusannya — jangan mencoba path itu lagi dengan bentuk lain.\n\n"
        "== PILIH TOOL YANG TEPAT ==\n"
        "Tiap panggilan tool = satu kali menunggu (belasan detik), jadi SATU "
        "tool yang menjawab banyak sekaligus jauh lebih berharga daripada "
        "beberapa tool kecil beruntun. Cocokkan kasusmu dengan tabel ini dan "
        "pilih yang paling langsung menjawab:\n"
        "- Cari teks/definisi/isi di kode -> search_text, atau search_multi_text "
        "untuk beberapa pola sekaligus. JANGAN list_dir lalu read_file berulang.\n"
        "- Cari file dari namanya -> glob_files. Struktur satu folder -> "
        "list_dir (sekali saja, bukan berulang). Gambaran proyek (ekosistem, "
        "entry point, skrip) -> project_info.\n"
        "- Sudah tahu 2-5 file yang dibutuhkan -> read_files SEKALIGUS, bukan "
        "read_file satu per satu. File besar: read_file(outline=true) dulu, "
        "lalu baca bagian yang perlu dengan start_line/end_line.\n"
        "- Mau tahu berkas mana yang relevan untuk satu tugas -> sasaran(tugas). "
        "Ingin mengingat kerja yang barusan -> kerja_terakhir().\n"
        "- Membuktikan satu fungsi/kelas -> test_function(path, symbol, args).\n"
        "- Ubah SEBAGIAN file yang sudah ada -> edit_file/edit_files "
        "(menampilkan diff ke pengguna sebelum disentuh). write_file HANYA "
        "untuk file BARU atau tulis-ulang-total.\n"
        "- Perintah cepat -> run_command. Proses yang terus jalan (server, "
        "watch, build besar) -> run_command_bg lalu bg_output — run_command "
        "biasa akan menggantung. Jalankan kode Python -> run_python.\n"
        + ("- Lihat hasil tampilan web -> web_preview; aplikasi desktop -> "
           "take_screenshot. Sebutkan apa yang kamu lihat.\n"
           if config.WEB_PREVIEW else
           "- Lihat hasil tampilan aplikasi desktop -> take_screenshot. "
           "Sebutkan apa yang kamu lihat.\n")
        + "- Cari info terkini di internet -> web_search. Baca satu URL yang "
        "sudah kamu tahu -> fetch_url.\n"
        "- Butuh kemampuan yang tak ada di daftar bawah (unduh aset, olah "
        "media, zip, clipboard, dll) -> cari_tool('kebutuhanmu') atau "
        "list_tools('<kategori>') dulu; jangan menyerah atau menyuruh pengguna "
        "melakukannya sendiri. Lupa tool apa yang ada? cari_tool selalu "
        "menjawabnya dalam satu panggilan.\n\n"
        "== LANGKAH YANG BISA DIUSULKAN ==\n"
        "(tanda * = argumen wajib)\n"
        f"{_katalog.katalog_inti()}\n\n"
        "Butuh yang lain — mengunduh aset dari internet, mengolah video/audio, "
        "zip, clipboard, notifikasi, kelola proses latar? panggil "
        "cari_tool('kebutuhanmu') (mencari dari SEMUA tool, tanpa perlu tahu "
        "kategorinya) atau list_tools('<kategori>') dulu; jangan menyerah dan "
        "jangan menyuruh pengguna melakukannya sendiri. Kategorinya:\n"
        f"{_katalog.ringkasan_kategori()}"
    )

# Backslash yang BUKAN escape JSON sah (mis. path Windows "src\entities" yang
# kehilangan gandanya saat dirender web) -> digandakan agar JSON bisa dibaca.
# KEPALA PESAN KONTEKS — ditaruh PALING DEPAN, bukan cuma di ekor.
# TERUKUR: dengan instruksinya hanya di bagian bawah pesan ~41 rb karakter,
# Gemini mengabaikannya dan langsung mengusulkan langkah ("Sekarang aku baca
# pyproject.toml…") alih-alih membalas SIAP. Yang dibaca paling awal punya
# bobot jauh lebih besar, jadi tiga hal yang paling sering menggagalkan giliran
# dinyatakan di sini sebelum protokol panjangnya dimulai.
#
# Dipakai DUA jalur pengiriman konteks — lampiran JSON dan teks penuh — supaya
# keduanya membuka dengan peringatan yang sama persis.
_KEPALA_KONTEKS = (
    "PESAN 1 DARI 2 — INI KONTEKS, BUKAN TUGAS.\n"
    "Jangan mengerjakan apa pun sekarang. Baca dulu seluruh aturan "
    "main di bawah, lalu balas SATU BARIS saja: `SIAP`. Tugas yang "
    "sebenarnya kukirim di pesan BERIKUTNYA.\n\n"
    "TIGA hal yang paling sering membuat kerjamu hangus — camkan "
    "sejak sekarang:\n"
    "1. SATU blok [[TOOL]] per pesan. Jangan pernah menumpuk dua "
    "atau lebih; tunggu hasilnya dulu.\n"
    "2. JANGAN memakai tool bawaan situsmu sendiri — jangan "
    "menjalankan Python/analysis/code-interpreter, jangan mencari "
    "web sendiri, jangan membuat artifact. Sandbox-mu ADALAH "
    "komputer lain: berkas proyekku tidak ada di sana, jadi apa "
    "pun yang kamu jalankan sendiri TIDAK menyentuh laptopku dan "
    "hasilnya nol — kamu cuma mengarang keadaan berkas yang tak "
    "pernah kamu lihat. SATU-SATUNYA cara menyentuh berkasku "
    "adalah blok [[TOOL]] yang kueksekusi di sini lalu kukirimkan "
    "hasilnya kembali padamu.\n"
    "3. JANGAN menulis kode panjang sekaligus. Situs ini MEMOTONG "
    "pesan yang kepanjangan, dan blok yang terpotong TAK BISA "
    "kueksekusi sama sekali — seluruh isinya hangus, bukan cuma "
    "ekornya. Tulis SEPARUH-SEPARUH: kirim bagian AWAL dulu "
    "(maksimal ±100 baris / ±4.000 karakter), tunggu [[HASIL]], "
    "baru lanjutkan bagian berikutnya lewat append_file (atau "
    "edit_file untuk berkas yang sudah ada) sampai lengkap. Yang "
    "penting berkasnya ADA lalu diteruskan bertahap — bukan utuh "
    "dalam satu tembakan lalu terpotong dan hilang semua.\n\n"
    "==========\n\n"
)

_BAD_ESCAPE_RE = re.compile(r'(?<!\\)\\(?![\\/"bfnrtu]|u[0-9a-fA-F]{4})')


# Artefak PERENDERAN yang membuat JSON tak sah. Situs menata blok kode dengan
# spasi non-breaking & tanda kutip tipografis; JSON standar menolak keduanya,
# sehingga usulan tool yang sebenarnya benar gagal dibaca.
_JSON_ARTIFACTS = {
    "\xa0": " ", " ": " ", " ": " ", " ": " ", "​": "",
    "﻿": "", "“": '"', "”": '"', "‘": "'", "’": "'",
}


def _clean_json_text(raw: str) -> str:
    """Ganti artefak perenderan agar JSON-nya bisa dibaca apa adanya."""
    for buruk, baik in _JSON_ARTIFACTS.items():
        if buruk in raw:
            raw = raw.replace(buruk, baik)
    return raw


def _escape_control_in_strings(raw: str) -> str:
    """Escape baris-baru/tab MENTAH yang berada DI DALAM string JSON.

    Ini penting: isi file selalu multi-baris, dan model biasanya menuliskan
    baris-baru sungguhan di dalam "content" alih-alih \\n. JSON standar
    melarangnya, sehingga usulan write_file DIAM-DIAM gagal dibaca — akibatnya
    AI web seolah hanya bisa membaca & menjalankan perintah, tak pernah benar
    benar mengubah file."""
    out: list[str] = []
    dalam_string = False
    escape = False
    for ch in raw:
        if dalam_string:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == '"':
                dalam_string = False
                out.append(ch)
                continue
            if ch in "\n\r\t":
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
                continue
            out.append(ch)
            continue
        if ch == '"':
            dalam_string = True
        out.append(ch)
    return "".join(out)


def _buang_koma_buntut(raw: str) -> str:
    """Buang koma buntut (sebelum } atau ]) yang berada DI LUAR string.

    Koma semacam itu membuat JSON tak sah padahal isinya sehat — model kerap
    menulisnya. Pembersihan dilakukan sadar-string dengan pola mesin keadaan
    yang sama seperti _escape_control_in_strings: koma di DALAM nilai (mis.
    potongan kode `f(a[1,])` pada write_file) tak boleh tersentuh, sebab
    mengubahnya diam-diam sama dengan merusak isi berkas yang diminta model."""
    out: list[str] = []
    dalam_string = False
    escape = False
    i, n = 0, len(raw or "")
    while i < n:
        ch = raw[i]
        if dalam_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                dalam_string = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            dalam_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "}]":
                i += 1          # buang komanya; penutupnya diproses normal
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _json_tool_obj(raw: str) -> dict | None:
    """Baca satu objek tool JSON dari teks.

    Beberapa perbaikan dicoba berurutan karena JSON yang ditulis model kerap
    rusak oleh hal-hal yang di luar kendalinya: perenderan situs, baris-baru
    mentah di dalam string, dan backslash yang kehilangan gandanya."""
    start = (raw or "").find("{")
    if start < 0:
        return None
    body = raw[start:]
    # Urutan perbaikan sengaja dari yang PALING TIDAK mengubah isi: teks apa
    # adanya dulu, baru normalisasi artefak render. Kalau dinormalkan lebih
    # dulu, spasi non-breaking di DALAM isi file ikut jadi spasi biasa dan
    # kode yang ditulis jadi berbeda dari yang dimaksud model.
    bersih = _clean_json_text(body)
    # Koma buntut sebelum }/] — JSON tak sah, tapi model kerap menulisnya dan
    # ia membuat usulan tool yang lainnya SEHAT gagal dibaca. Tanpa toleransi
    # ini gilirannya jatuh ke jalur "kirim ulang blokmu": satu putaran penuh
    # browser (belasan detik) terbuang untuk hal yang bisa dibenarkan di tempat.
    # Pembersihnya SADAR-STRING (lihat _buang_koma_buntut) sehingga koma di
    # dalam nilai — mis. kode pada write_file — tak ikut tersentuh.
    for candidate in (body,
                      _escape_control_in_strings(body),
                      bersih,
                      _escape_control_in_strings(bersih),
                      _BAD_ESCAPE_RE.sub(r"\\\\", bersih),
                      _BAD_ESCAPE_RE.sub(
                          r"\\\\", _escape_control_in_strings(bersih)),
                      _buang_koma_buntut(body),
                      _buang_koma_buntut(_escape_control_in_strings(bersih))):
        try:
            # raw_decode: berhenti di akhir objek JSON pertama, sisanya diabaikan.
            obj, _ = json.JSONDecoder().raw_decode(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and (obj.get("tool") or obj.get("name")):
            return _bersihkan_nilai(obj)
    return None


def _bersihkan_nilai(obj: Any) -> Any:
    """Bersihkan artefak render dari NILAI string hasil parsing.

    Spasi non-breaking & kutip tipografis di dalam isi file selalu berasal dari
    cara situs menata blok kode, bukan dari maksud model — kalau dibiarkan, ia
    tertulis ke file sebagai karakter tak terlihat yang bikin kode rusak dan
    sulit ditelusuri."""
    if isinstance(obj, str):
        return _clean_json_text(obj)
    if isinstance(obj, dict):
        return {k: _bersihkan_nilai(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bersihkan_nilai(v) for v in obj]
    return obj


def _tool_args(obj: dict) -> dict:
    """Argumen panggilan tool dari objek JSON, berdasarkan kunci args/arguments/
    parameters.

    Tiga nama karena tiga kebiasaan yang semuanya nyata di lapangan: protokol
    bagas-ai memakai "args", format bawaan Qwen "arguments", dan format legacy
    OpenAI "parameters". Dulu yang terakhir TIDAK dikenali — panggilannya tetap
    dieksekusi tapi dengan args KOSONG, sehingga tool yang jelas-jelas diminta
    model (mis. read_file) berjalan tanpa path dan gagal: persis keluhan 'tool
    sudah dikasih instruksi tapi tidak dieksekusi'."""
    args = obj.get("args") or obj.get("arguments") or obj.get("parameters")
    return args if isinstance(args, dict) else {}


def _parse_web_tool_calls(text: str, code_blocks: Any = ()) -> list[dict]:
    """Ambil daftar {'name','arguments'} dari blok [[TOOL]] pada balasan AI web.

    Toleran terhadap cara AI web merender blok: pagar markdown (```json), label
    bahasa yang bocor, spasi, teks tambahan, dan escape yang rusak.

    `code_blocks` = isi MENTAH blok kode dari DOM. Bila teks yang dirender gagal
    dibaca (paling sering: backslash pada path Windows hilang), usulan diambil
    dari sini karena isinya persis seperti yang ditulis AI web.
    """
    calls: list[dict] = []
    for m in _WEB_TOOL_RE.finditer(text or ""):
        # group(1) = bentuk [[TOOL]]…[[/TOOL]], group(2) = <tool_call>…</tool_call>
        body = _FENCE_RE.sub("", m.group(1) or m.group(2) or "").strip()
        obj = _json_tool_obj(body)
        if obj is not None:
            calls.append({"name": str(obj.get("tool") or obj.get("name")),
                          "arguments": _tool_args(obj)})

    # Cadangan: sebagian/seluruh usulan gagal dibaca dari teks -> pakai isi
    # mentah blok kode (byte apa adanya, tak tersentuh perenderan markdown).
    n_markers = len(re.findall(_OPEN_MARK, text or "", re.IGNORECASE)) + \
        len(re.findall(_ALT_OPEN, text or "", re.IGNORECASE))
    if code_blocks and len(calls) < n_markers:
        from_code: list[dict] = []
        for raw in code_blocks:
            obj = _json_tool_obj(str(raw))
            if obj is not None:
                from_code.append(
                    {"name": str(obj.get("tool") or obj.get("name")),
                     "arguments": _tool_args(obj)})
        if len(from_code) > len(calls):
            calls = from_code

    # Cadangan 2: sebagian model menulis usulan sebagai JSON BIASA tanpa penanda
    # apa pun (Qwen kerap begitu meski protokolnya sudah dijelaskan). Diterima
    # HANYA bila objeknya benar-benar berbentuk panggilan tool (punya nama tool
    # DAN args) dan balasannya nyaris tak berisi teks lain — supaya penjelasan
    # yang KEBETULAN memuat contoh JSON tidak ikut dieksekusi.
    if not calls:
        for raw in list(code_blocks or ()) + [text or ""]:
            obj = _json_tool_obj(str(raw))
            if obj is None:
                continue
            args = _tool_args(obj)
            if not isinstance(args, dict) or not args:
                continue
            sisa = _FENCE_RE.sub("", str(raw))
            sisa = re.sub(r"\{.*\}", "", sisa, flags=re.DOTALL).strip()
            if len(sisa) > 80:      # ada prosa panjang -> kemungkinan penjelasan
                continue
            calls.append({"name": str(obj.get("tool") or obj.get("name")),
                          "arguments": args})
            break

    return [c for c in calls if c["name"] and isinstance(c["arguments"], dict)]


# Penanda protokol yang boleh SAJA tersisa di teks (mis. blok rusak / tak
# berpasangan). Semuanya dibuang sebelum jawaban ditampilkan ke pengguna.
_WEB_MARKER_RE = re.compile(
    _OPEN_MARK + r"|" + _CLOSE_MARK + r"|" + _ALT_OPEN + r"|" + _ALT_CLOSE
    + r"|\[\[\s*/?\s*HASIL[^\]]*\]\]",
    re.IGNORECASE)


def _sisa_prosa(text: str) -> bool:
    """Masih ada KALIMAT tersisa sesudah blok tool & penandanya dibuang?

    Dipakai untuk membedakan dua keadaan yang di layar kelihatan sama —
    balasan yang benar-benar tak terbaca, dan balasan yang terbaca sempurna
    tapi seluruhnya berupa usulan langkah. Yang kedua bukan kerusakan format,
    melainkan model yang menolak berhenti; keduanya butuh penanganan berbeda."""
    return bool(_strip_tool_json(_strip_web_markers(text)))


def _strip_web_markers(text: str) -> str:
    """Buang blok usulan tool + SISA penanda protokol dari teks jawaban.

    Tanpa ini, penanda seperti `[[/TOOL]]` bisa bocor ke layar saat blok tool
    rusak/tak berpasangan — pengguna melihat penanda alih-alih jawaban."""
    out = _WEB_TOOL_RE.sub("", text or "")
    # Blok yang TAK PERNAH DITUTUP: situs memotong pesan yang kepanjangan
    # ("Output stopped"), dan potongannya sering jatuh TEPAT di tengah usulan
    # tool. Yang tersisa adalah pembuka + JSON separuh jadi; membuang
    # penandanya saja justru menyisakan JSON telanjang lalu ia tercetak ke
    # layar sebagai "jawaban". Semua yang berada di belakang pembuka yatim itu
    # adalah muatan mesin yang belum selesai, jadi dipotong sekalian.
    sisa = [m for m in (re.search(_OPEN_MARK, out, re.IGNORECASE),
                        re.search(_ALT_OPEN, out, re.IGNORECASE)) if m]
    if sisa:
        out = out[:min(m.start() for m in sisa)]
    out = _WEB_MARKER_RE.sub("", out)
    # CATATAN: dulu di sini ada penyapuan "semua baris yang isinya cuma pagar
    # kode". Maksudnya membersihkan pagar yatim sisa blok tool yang dibuang,
    # tapi polanya tak bisa membedakan pagar yatim dari pagar SUNGGUHAN milik
    # jawaban — jadi setiap ```bash / ```json di jawaban AI ikut lenyap, dan
    # blok kodenya jatuh jadi paragraf biasa. Persis keluhan "```-nya jadi
    # baris kosong". Pembersihan pagar yatim kini dilakukan di _rapikan_pagar,
    # yang hanya menyentuh pagar yang benar-benar TAK BERPASANGAN.
    #
    # Bungkus pengalih dibuka DI SINI karena inilah satu-satunya gerbang yang
    # dilewati setiap teks jawaban sebelum sampai ke layar — jadi tak ada jalur
    # yang tertinggal, dari situs mana pun.
    out = _buka_bungkus_tautan(out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# URL apa pun di dalam teks jawaban (dipakai untuk membuka bungkus pengalih).
_URL_RE = re.compile(r"https?://[^\s\)\]\}<>\"'`]+")
# Nama parameter yang MEMUAT alamat aslinya pada laman pengalih. Diurut dari
# yang paling khas supaya `url=` tak keburu menang di alamat yang punya dua-duanya.
_PARAM_TUJUAN = ("target", "redirect_url", "redirect", "url", "u", "q")


def _buka_bungkus_tautan(text: str) -> str:
    """Kembalikan URL yang DIBUNGKUS laman pengalih ke alamat aslinya.

    Situs chat menulis ulang setiap tautan di jawabannya supaya kliknya lewat
    laman pencatat mereka dulu. Dola paling kentara: `http://localhost:3000`
    berubah jadi
    `https://sg-link.byteoversea.com/?target=http%3A%2F%2Flocalhost%3A3000&…`.
    Di terminal itu bukan cuma jelek — Ctrl+klik jadi membuka laman pengalihnya,
    dan untuk alamat lokal seperti localhost hasilnya mustahil dibuka sama
    sekali, karena servernya ada di mesin ini, bukan di internet.

    Syaratnya sengaja ketat supaya tautan sah tak ikut dirombak:
      - nilai parameternya harus URL absolut http/https yang ter-encode, dan
      - inangnya harus BEDA dengan inang tujuan (kalau sama, itu tautan biasa
        yang kebetulan punya parameter bernama `url`, mis. hasil pencarian).
    """
    def ganti(m: re.Match) -> str:
        asal = m.group(0)
        # Tanda baca penutup kalimat bukan bagian alamat; dilepas dulu lalu
        # ditempel lagi supaya titik di akhir kalimat tak ikut terbawa.
        ekor = ""
        while asal and asal[-1] in ".,;:!?":
            ekor = asal[-1] + ekor
            asal = asal[:-1]
        try:
            pecah = urlsplit(asal)
            kueri = parse_qs(pecah.query)
        except ValueError:                     # URL cacat -> biarkan apa adanya
            return m.group(0)
        for nama in _PARAM_TUJUAN:
            for nilai in kueri.get(nama, []):
                if not nilai.lower().startswith(("http://", "https://")):
                    continue
                try:
                    tujuan = urlsplit(nilai)
                except ValueError:
                    continue
                if not tujuan.netloc or tujuan.netloc == pecah.netloc:
                    continue
                return nilai + ekor
        return m.group(0)

    return _URL_RE.sub(ganti, text or "")


_PAGAR_BARIS_RE = re.compile(r"^[ \t]*```[a-zA-Z0-9_+-]*[ \t]*$", re.MULTILINE)


def _rapikan_pagar(text: str) -> str:
    """Buang pagar kode yang YATIM & blok kode yang jadi kosong.

    Dipanggil sesudah blok usulan tool dibuang, karena pembuangan itulah yang
    bisa menyisakan pagar tanpa pasangan (mis. JSON telanjang yang dipagari
    ```json — pembukanya ikut terbuang bersama bloknya, penutupnya tertinggal).

    Yang BERPASANGAN tidak disentuh sama sekali: pagar milik jawaban AI adalah
    isi yang sah, bukan sampah protokol."""
    out = text or ""
    # 1. Blok yang isinya habis -> buang sepasang pagarnya sekalian, jangan
    #    tinggalkan kotak kode kosong yang membingungkan.
    out = re.sub(r"^[ \t]*```[a-zA-Z0-9_+-]*[ \t]*\n\s*```[ \t]*$", "",
                 out, flags=re.MULTILINE)
    # 2. Sisa pagar ganjil = ada satu yang yatim. Yang dibuang HANYA yang
    #    terakhir: pagar-pagar sebelumnya sudah berpasangan dengan benar.
    posisi = [m.span() for m in _PAGAR_BARIS_RE.finditer(out)]
    if len(posisi) % 2 == 1:
        a, b = posisi[-1]
        out = out[:a] + out[b:]
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _take_image_marks(result: str) -> tuple[str, list[str]]:
    """Pisahkan penanda [GAMBAR] dari hasil tool.

    Return (teks tanpa penanda, daftar path gambar). Path-nya dilampirkan ke
    pesan berikutnya, jadi tak perlu ikut dikirim sebagai teks."""
    paths = [m.group(1).strip() for m in _IMAGE_MARK_RE.finditer(result or "")]
    if not paths:
        return result, []
    cleaned = _IMAGE_MARK_RE.sub("", result or "").rstrip()
    return cleaned, paths


# --- lampiran media di jalur API (vision) -----------------------------------
# Jalur WEB mengunggah berkas ke situsnya (Playwright); jalur API TIDAK punya
# komposer — medianya ikut sebagai BAGIAN PESAN gaya OpenAI/OpenRouter: konten
# user berubah jadi daftar bagian [{type:text},{type:image_url|video_url}]
# berisi data-URL base64. Base64 SENGAJA tidak pernah masuk Memory: riwayat
# cuma menyimpan PENANDA + path aslinya, lalu _pesan_dengan_media
# mengkonversinya tiap request. Dengan itu berkas sesi tetap ramping dan
# digest antar-model tak membawa gumpalan base64.
_MIME_GAMBAR = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# "video/mov" ada di contoh resmi OpenRouter walau bukan MIME baku; .mov milik
# sistem biasanya terdeteksi video/quicktime — keduanya diterima.
_MIME_VIDIO = {"video/mp4", "video/mpeg", "video/webm",
               "video/quicktime", "video/mov"}
_MEDIA_LAMPIR = "[LAMPIR-MEDIA]"


def _mime_gambar(path: str) -> str | None:
    """MIME gambar yang didukung endpoint vision, atau None bila bukan."""
    mime, _ = mimetypes.guess_type(path)
    return mime if mime in _MIME_GAMBAR else None


def _mime_vidio(path: str) -> str | None:
    """MIME video yang didukung endpoint vision, atau None bila bukan."""
    mime, _ = mimetypes.guess_type(path)
    return mime if mime in _MIME_VIDIO else None


def _potong_alasan(asli: str, maks: int = 90) -> str:
    """Penyebab singkat 'konteks penuh' untuk satu baris notifikasi."""
    teks = " ".join((asli or "").split())
    # Buang prefiks khas openrouter/openai yang cuma menambah kebisingan.
    for awalan in ("Error code: 400 - ", "Bad Request: ",
                   "400 - ", "error: "):
        if teks.lower().startswith(awalan.lower()):
            teks = teks[len(awalan):]
    if len(teks) > maks:
        teks = teks[:maks].rstrip() + "…"
    return teks or "riwayat melebihi jendela konteks"


# Pesan 400/413 yang jelas-jelas BUKAN soal ukuran — naikkan apa adanya,
# jangan dibuang waktu utk ladder pemangkasan yang pasti tak menolong.
_KATA_FATAL_400 = ("api key", "authentic", "unauthorized", "not found",
                   "permission", "invalid model", "does not exist",
                   "no endpoints found")


def _layak_pulih(exc: Exception) -> bool:
    """True bila kegagalan 400/413 layak dicoba pulih (lepas media/pangkas).

    Provider kerap menyembunyikan konteks-penuh di balik pesan generik
    ("Provider returned error", raw 'ERROR') — kalau kita menunggu kata
    'context length', pemulihnya tak pernah bangun. Maka SEMUA 400/413
    diberi kesempatan ladder, KECUALI yang jelas fatal (auth/model)."""
    o = llm._oa()
    if not isinstance(exc, o.BadRequestError):
        return False
    status = getattr(exc, "status_code", None)
    if status not in (400, 413):
        return False
    teks = str(getattr(exc, "message", "") or "") + " " + str(exc)
    teks = teks.lower()
    return not any(k in teks for k in _KATA_FATAL_400)


def _pastikan_tugas_aktif(memory: Memory, teks: str) -> bool:
    """Pastikan permintaan pengguna yang SEDANG dikerjakan selamat dari
    pemangkasan. Return True bila ia diselamatkan (disisipkan ulang).

    potong_awal() menyimpan N entri terakhir — padahal dalam rantai tool
    panjang, entri-entri itu adalah langkah-langkahnya, bukan permintaannya.
    Tanpa jagaan ini model kehilangan OBJEKTIF di tengah kerja lalu menjawab
    acak. Pencocokan memakai normalisasi spasi + 120 karakter pertama supaya
    tetap kenal walau isi pesan panjang."""
    awal = " ".join((teks or "").split())[:120]
    if not awal:
        return False
    for m in memory.messages:
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, str) \
                and awal in " ".join(c.split()):
            return False                      # masih ada — tidak perlu apa-apa
    memory.messages.insert(1, {
        "role": "user",
        "content": ("[SISTEM] Tugas yang SEDANG dikerjakan (riwayat awalnya "
                    f"terpangkas agar muat konteks):\n{(teks or '')[:1500]}"),
    })
    return True


# Batas ukuran media per request (terverifikasi: video 30 MB -> data-URL
# ±40 MB yang dikirim ulang DI SETIAP putaran rantai tool — biaya meledak
# dan konteks pasti penuh). Media di atas batas DILEWATI dengan kabar.
_BATAS_GAMBAR = 5 * 1024 * 1024     # 5 MB / gambar
_BATAS_VIDIO = 20 * 1024 * 1024     # 20 MB / video
_BATAS_TOTAL_MEDIA = 24 * 1024 * 1024  # total gabungan dalam satu pesan


def _pesan_dengan_media(messages: list[dict[str, Any]],
                        cache: dict[str, str | None] | None = None,
                        lewat: list[str] | None = None,
                        blokir_media: bool = False,
                        ) -> list[dict[str, Any]]:
    """Konversi pesan user berpenanda [LAMPIR-MEDIA]<path> jadi multimodal.

    Gambar menjadi bagian `image_url`, video menjadi `video_url` — keduanya
    data-URL base64 sesuai contoh resmi OpenRouter. Penanda yang filenya
    sudah tak ada / formatnya tak didukung dibuang diam-diam: riwayat lama
    (resume) sering menyimpan path yang sudah terhapus, dan mengirim data-URL
    kosong justru membuat seluruh request ditolak.

    `blokir_media=True`: SEMUA marker diperlakukan absen — dipakai pemulih
    saat provider konsisten menolak permintaan ber-media (400 "Provider
    returned error"): giliran diselamatkan tanpa gambar/video daripada mati.

    `cache` (opsional): {path: data-url atau None} milik SATU giliran —
    diisi sekali lalu dipakai ulang tiap putaran rantai tool, supaya berkas
    besar tidak dibaca & dienkode ulang berkali-kali dan keputusan "terlalu
    besar" konsisten antar-putaran. Bila tak diberikan, fungsi tetap jalan
    tanpa cache (dipakai uji/pemakaian sekali-pakai).

    `lewat` (opsional): daftar keterangan media yang DILEWATI karena
    melebihi batas ukuran — pemanggil boleh mengumumkannya sekali via
    on_notice alih-alih membiarkan media hilang tanpa kabar."""
    cache = cache if cache is not None else {}
    lewat = lewat if lewat is not None else []
    total = sum(len(v) for v in cache.values() if v)
    out: list[dict[str, Any]] = []
    for m in messages:
        c = m.get("content")
        if m.get("role") != "user" or not isinstance(c, str) \
                or _MEDIA_LAMPIR not in c:
            out.append(m)
            continue
        baris = c.splitlines()
        paths = [] if blokir_media else [
            ln[len(_MEDIA_LAMPIR):].strip() for ln in baris
            if ln.startswith(_MEDIA_LAMPIR)]
        teks = "\n".join(ln for ln in baris
                         if not ln.startswith(_MEDIA_LAMPIR)).strip()
        parts: list[dict[str, Any]] = [{"type": "text", "text": teks}] if teks else []
        for p in paths:
            url = cache.get(p)
            if url is None and p not in cache:
                mime = _mime_gambar(p) or _mime_vidio(p)
                if not (mime and Path(p).is_file()):
                    continue          # hilang/bukan media: buang penandanya
                vidio = mime in _MIME_VIDIO
                batas = _BATAS_VIDIO if vidio else _BATAS_GAMBAR
                besar = Path(p).stat().st_size
                if besar > batas or total + besar > _BATAS_TOTAL_MEDIA:
                    sebab = ("melebihi batas "
                             f"{batas // (1024 * 1024)} MB per media"
                             if besar > batas else
                             "melebihi kuota total media pesan")
                    ket = (f"{Path(p).name} ({besar // (1024 * 1024)} MB) "
                           f"dilewati: {sebab}")
                    lewat.append(ket)
                    cache[p] = None   # keputusan TETAP utk putaran berikutnya
                    continue
                with open(p, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                jenis = "image_url" if mime in _MIME_GAMBAR else "video_url"
                url = f"data:{mime};base64,{b64}"
                cache[p] = url
                total += len(url)
            if not url:
                continue
            jenis = "image_url" if url.startswith("data:image/") else "video_url"
            parts.append({"type": jenis, jenis: {"url": url}})
        # Multimodal HANYA bila ada bagian media sungguhan; satu text-part
        # polos dikirim sebagai string biasa (bentuk yang paling aman).
        media_parts = [q for q in parts if q.get("type") != "text"]
        if media_parts:
            if parts[0].get("type") == "text" and not parts[0]["text"]:
                parts.pop(0)
            out.append({**m, "content": parts})
        else:
            out.append({**m, "content": teks or "(lampiran media)"})
    return out


def _looks_like_unapplied_code(text: str) -> bool:
    """True bila balasan menyajikan KODE untuk disalin manual, bukan menuliskannya.

    Dipakai untuk menegur sekali: pengguna memakai connector ini supaya
    perubahannya nyata di disk, bukan supaya kode ditempel di layar."""
    t = text or ""
    if "```" not in t and not re.search(r"^\s*(?:html|css|js|python)\d", t, re.M):
        return False
    # Cukup panjang untuk benar-benar berupa berkas/patch, bukan cuplikan sebaris.
    return len(t) > 400


# Kalimat yang MENJANJIKAN langkah berikutnya. Sengaja hanya bentuk NIAT
# (sekarang/akan), tak pernah bentuk SELESAI ("sudah kuperbaiki", "berhasil"):
# jawaban akhir yang sah kerap menceritakan apa yang tadi dikerjakan, dan itu
# tak boleh ikut tertangkap.
#
# Bentuk berimbuhan ditulis lengkap, bukan ditebak lewat pola awalan me-:
# "menjalankan"/"memperbaiki"/"mengecek" terlalu beragam untuk satu pola, dan
# pola yang terlalu longgar justru menyerempet kata lain. Diurutkan dari yang
# TERPANJANG supaya alternasi tak berhenti di bentuk pendek yang jadi awalan
# bentuk panjang ("menambah" vs "menambahkan").
_KERJA = (
    "baca", "membaca", "cek", "mengecek", "periksa", "memeriksa",
    "buka", "membuka", "lihat", "melihat", "telusuri", "menelusuri",
    "cari", "mencari", "pindai", "memindai",
    "perbaiki", "memperbaiki", "betulkan", "membetulkan",
    "ubah", "mengubah", "edit", "mengedit", "tulis", "menulis",
    "buat", "membuat", "bikin", "membikin",
    "tambah", "menambah", "menambahkan", "hapus", "menghapus",
    "ganti", "mengganti", "jalankan", "menjalankan",
    "eksekusi", "mengeksekusi", "uji", "menguji", "tes", "mengetes",
    "validasi", "memvalidasi", "pasang", "memasang", "install", "menginstall",
)
_JANJI_RE = re.compile(
    # Subjeknya WAJIB ada. Tanpa itu, kalimat yang cuma MENJELASKAN ikut
    # tertangkap ("kode ini membaca file lalu menulis hasilnya") — dan salah
    # tangkap di sini mahal: ia menyuruh AI mengeluarkan tool padahal
    # jawabannya sudah benar.
    r"\b(?:aku|saya|gue|gw)\b\s+(?:akan\s+)?"
    r"(?:coba\s+|langsung\s+|lanjut\s+)?"
    r"(?:" + "|".join(sorted(_KERJA, key=len, reverse=True)) + r")(?:\s|$)"
    r"|\b(?:sekarang|selanjutnya|berikutnya|setelah\s+ini)\s+(?:aku|saya|ku)\b"
    r"|\bmari\s+(?:kita\s+)?(?:mulai|lihat|cek|periksa)\b"
    r"|\b(?:let\s+me|i'?ll|i\s+will|let'?s)\b",
    re.I,
)
# Penanda pekerjaan yang SUDAH kelar. "sudah/telah/barusan" harus diikuti KATA
# KERJA-nya, bukan berdiri sendiri: TERAMATI di balasan Kimi yang nyata, "…buat
# tahu data karya yang sudah ada" membuat balasan berisi janji ("aku baca
# dulu…") ikut dikira laporan selesai, sehingga tegurannya tak pernah jalan.
_SELESAI_RE = re.compile(
    r"\b(?:sudah|telah|barusan)\s+(?:ku)?(?:"
    + "|".join(sorted(_KERJA, key=len, reverse=True)) + r")\b"
    r"|\b(?:berhasil|selesai|beres|done)\b"
    r"|\bku(?:" + "|".join(sorted(_KERJA, key=len, reverse=True)) + r")\b",
    re.I)


# Jejak model memakai TOOL BAWAANNYA SENDIRI alih-alih blok [[TOOL]]. TERAMATI
# di Kimi web: ia menjalankan "Execute Python code" di sandbox miliknya, yang
# tak punya satu pun berkas proyek pengguna — jadi hasilnya fiksi, dan tak ada
# yang berubah di laptop. Kalau label kartunya ikut terbaca dari halaman, ini
# sinyal paling langsung bahwa protokolnya sedang dilanggar.
_TOOL_BAWAAN_RE = re.compile(
    r"\b(?:execute\s+python\s+code|run\s+python\s+code|code\s+interpreter|"
    r"python\s+sandbox|analysis\s+tool|menjalankan\s+kode\s+python\s+di\s+"
    r"sandbox"
    # Label KARTU yang terlihat di layar saat situs menjalankan tool-nya
    # sendiri. Ditambahkan dari tangkapan layar pengguna: Dola membalas "Aku
    # baca dulu README.md proyekmu" lalu menjalankan Execute Python code, dua
    # kali Search, dan Fetching URLs — tak satu pun menyentuh berkas di laptop,
    # jadi seluruh hasilnya fiksi terhadap proyek yang sebenarnya.
    r"|fetching\s+urls?|searching\s+the\s+web|browsing\s+the\s+web"
    r")\b", re.I)


def _pakai_tool_bawaan(text: str) -> bool:
    """True bila balasan menyebut/menampilkan tool bawaan model itu sendiri."""
    return bool(_TOOL_BAWAAN_RE.search(text or ""))


def _looks_like_promise(text: str) -> bool:
    """True bila balasan MENJANJIKAN langkah tapi tak membawa blok [[TOOL]].

    Gejala yang paling sering dikeluhkan: AI menulis "Oke, aku baca dulu
    main.py" lalu berhenti di situ. Tanpa blok, pesan itu jadi JAWABAN AKHIR —
    layar menampilkan niat yang tak pernah dikerjakan, dan giliran habis
    percuma. Menegurnya jauh lebih murah daripada menyuruh pengguna mengetik
    ulang permintaannya.

    Dijaga agar tak salah tangkap: hanya berlaku untuk balasan PENDEK (jawaban
    akhir yang sungguhan hampir selalu lebih panjang), tanpa blok kode, dan
    yang tidak memuat kata penanda sudah-selesai."""
    t = (text or "").strip()
    if not t or len(t) > 400 or "```" in t:
        return False
    if _SELESAI_RE.search(t):
        return False
    return bool(_JANJI_RE.search(t))


def _tampak_usulan_tool(obj: Any) -> bool:
    """True HANYA bila objek JSON ini sungguh-sungguh usulan tool.

    Syaratnya diperketat karena penyaring ini MENGHAPUS: dulu cukup ada kunci
    "tool" ATAU "name", sehingga cuplikan package.json di jawaban AI —
    {"name": "portfolio", "version": "1.0.0"} — dikira usulan tool lalu lenyap
    dari layar tanpa jejak. Kehilangan isi jawaban jauh lebih merugikan
    daripada sesekali membiarkan satu JSON mesin ikut tercetak.

    Yang dituntut sekarang: nama tool-nya BENAR-BENAR ADA di registry, dan
    bentuknya salah satu dari dua yang dipakai model — {"tool":…, "args":…}
    atau {"name":…, "arguments":…} gaya OpenAI.
    """
    if not isinstance(obj, dict):
        return False
    nama = obj.get("tool") or obj.get("name")
    if not isinstance(nama, str) or nama not in tools.REGISTRY:
        return False
    if "tool" in obj:
        return True
    # Bentuk {"name": ...} terlalu lazim di JSON biasa (package.json, manifest,
    # konfigurasi komponen), jadi ia baru dipercaya bila DITEMANI wadah argumen.
    return isinstance(obj.get("arguments"), dict) or isinstance(
        obj.get("args"), dict)


def _strip_tool_json(text: str) -> str:
    """Buang objek JSON USULAN TOOL yang ditulis tanpa penanda.

    Sebagian model menulis usulannya sebagai blok kode polos. Tanpa dibuang,
    JSON mentahnya tercetak ke layar sebagai 'narasi' di setiap putaran dan
    memenuhi terminal."""
    out = text or ""
    i = 0
    while True:
        mulai = out.find("{", i)
        if mulai < 0:
            return _rapikan_pagar(out)
        # Cari kurung penutup yang berpasangan (abaikan kurung di dalam string).
        depth, j, dalam_string, escape = 0, mulai, False, False
        while j < len(out):
            ch = out[j]
            if dalam_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    dalam_string = False
            elif ch == '"':
                dalam_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(out):
            return _rapikan_pagar(out)
        blok = out[mulai:j + 1]
        # Sengaja BUKAN `_json_tool_obj(blok) is not None`: pengenal itu longgar
        # dan memang harus longgar untuk PARSER (nama tool salah ketik tetap
        # terbaca, lalu dijawab "[error] tool tak ditemukan" yang bisa
        # diperbaiki model). Untuk penyaring yang MENGHAPUS, longgar berarti
        # menelan isi jawaban — lihat _tampak_usulan_tool.
        if _tampak_usulan_tool(_json_tool_obj(blok)):
            # Buang blok + label bahasa/nomor baris yang menempel sebelumnya.
            depan = re.sub(r"(?:```[a-zA-Z0-9_+-]*|\b[a-z]{2,10}\d*)\s*$", "",
                           out[:mulai])
            # Pagar PENUTUP tepat sesudah blok ikut dibuang, supaya tak
            # tertinggal sebagai pagar yatim.
            belakang = re.sub(r"^\s*```[ \t]*$", "", out[j + 1:], count=1,
                              flags=re.MULTILINE)
            out = depan + belakang
            i = len(depan)
        else:
            i = mulai + 1


def _web_reply_complete(text: str) -> bool:
    """False HANYA bila balasan tampak masih ditulis: ada pembuka [[TOOL]] yang
    belum ditutup. Dipakai agar bagas-ai tak menganggap balasan selesai saat blok
    usulan tool baru separuh dirender.

    PENTING: penutup nyasar (mis. AI menulis [[/TOOL]] sendirian) TIDAK boleh
    dianggap 'belum selesai' — dulu itu membuat penungguan berjalan sampai batas
    waktu 5 menit dan terminal terlihat macet."""
    t = text or ""
    opens = (len(re.findall(_OPEN_MARK, t, re.IGNORECASE))
             + len(re.findall(_ALT_OPEN, t, re.IGNORECASE)))
    closes = (len(re.findall(_CLOSE_MARK, t, re.IGNORECASE))
              + len(re.findall(_ALT_CLOSE, t, re.IGNORECASE)))
    return opens <= closes


# --- perkiraan token --------------------------------------------------------
# 1 token ≈ 4 karakter (teks campuran Indonesia/Inggris/kode). Ini HANYA untuk
# penghitung realtime di layar saat angka resmi belum datang; usage asli dari
# endpoint selalu menggantikannya di akhir putaran.
_TOK_PER_MEDIA_GAMBAR = 1500   # tipikal biaya vision per gambar ter-encode
_TOK_PER_MEDIA_VIDIO = 8000    # kasar; provider jarang membuka rincian video
_OVERHEAD_PER_PESAN = 4        # pembungkus role/format tiap pesan


def _est_tokens(text: str) -> int:
    """Perkiraan kasar: 1 token ≈ 4 karakter.

    Dipakai HANYA untuk penghitung realtime di layar, saat jumlah sebenarnya
    belum diketahui. Angka resminya datang dari `usage` di ujung stream dan
    langsung menggantikan perkiraan ini.
    """
    return max(1, len(text or "") // 4)


def _est_messages(messages: list[dict[str, Any]],
                  extra_chars: int = 0) -> int:
    """Perkiraan token SELURUH permintaan — bukan cuma isi pesannya.

    char//4 saja MEREMEHKAN besar (audit 2026-08-26):
      - argumen `tool_calls.function.arguments` ikut dikirim setiap putaran
        tapi dulu tak dihitung sama sekali;
      - tiap pesan membungkus ±beberapa token overhead format;
      - satu penanda [LAMPIR-MEDIA] berarti GAMBAR/VIDEO sungguhan di mata
        endpoint vision (±1500 / ±8000 token) — dulu dihitung ±8 token dari
        panjang path-nya, sehingga penghitung menipu saat memakai media.

    `extra_chars`: panjang JSON schema tool yang juga ikut tiap request saat
    tools aktif — dihitung pemanggil sekali di awal giliran."""
    total = max(0, extra_chars)
    for m in messages:
        total += _OVERHEAD_PER_PESAN
        c = m.get("content")
        if isinstance(c, str):
            for ln in c.splitlines():
                if ln.startswith(_MEDIA_LAMPIR):
                    p = ln[len(_MEDIA_LAMPIR):].strip()
                    # Konversi balik ke CHAR-EQUIVALENT (x4) supaya satu
                    # satuan dengan sisa total sebelum dibagi 4 di akhir.
                    total += (_TOK_PER_MEDIA_VIDIO if _mime_vidio(p)
                              else _TOK_PER_MEDIA_GAMBAR) * 4
                else:
                    total += len(ln)
            continue
        total += len(str(c or ""))
        for tc in m.get("tool_calls") or []:
            args = ((tc.get("function") or {}).get("arguments")) or ""
            total += len(args)
    return max(1, total // 4)


class Usage:
    """Akumulator token (energi AI).

    Dua sumber angka, sesuai jalur modelnya:
      - add(usage)  : objek usage dari endpoint API — angka SEBENARNYA.
      - add_raw(..) : estimasi dari jumlah karakter, satu-satunya cara di jalur
        web (situs AI tak pernah melaporkan token) dan juga cadangan di jalur
        API bila `stream_options.include_usage` tak dibalas.

    Selain prompt/completion, OpenRouter (dan endpoint OpenAI-compatible lain)
    melaporkan BIAYA (`usage.cost`, USD) serta rincian cache & reasoning —
    ketiganya ditangkap di sini supaya bisa tampil seperti AI agent pada
    umumnya. completion_tokens standar SUDAH termasuk reasoning; medan itu
    cuma rincian transparansi, bukan tambahan.
    """

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0
        self.cost = 0.0          # USD kumulatif; 0 selama endpoint tak laporkan
        self.cached = 0          # bagian prompt yang ketemu cache provider
        self.reasoning = 0       # rincian token nalar (bila dilaporkan)

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, usage: Any) -> None:
        if not usage:
            return
        self.prompt += getattr(usage, "prompt_tokens", 0) or 0
        self.completion += getattr(usage, "completion_tokens", 0) or 0
        try:
            self.cost += float(getattr(usage, "cost", 0) or 0)
        except (TypeError, ValueError):
            pass
        pdet = getattr(usage, "prompt_tokens_details", None)
        if pdet is not None:
            self.cached += int(getattr(pdet, "cached_tokens", 0) or 0)
        cdet = getattr(usage, "completion_tokens_details", None)
        if cdet is not None:
            self.reasoning += int(getattr(cdet, "reasoning_tokens", 0) or 0)

    def add_raw(self, prompt: int, completion: int) -> None:
        self.prompt += prompt
        self.completion += completion


class Agent:
    """Agent percakapan dengan kemampuan memanggil tools (streaming)."""

    def __init__(
        self,
        *,
        model: str | None = None,
        tool_names: list[str] | None = None,
        max_iterations: int | None = None,
        session: Session | None = None,
    ) -> None:
        model_id = model or prefs.get_model() or config.CHAT_MODEL
        self.model_spec = models.spec_for_id(model_id)
        # Preferensi lama menunjuk model yang sudah tak ada (seluruh katalog
        # ber-API-key dihapus)? spec_for_id sudah memetakannya ke model bawaan;
        # simpan hasilnya supaya migrasi cukup sekali dan menu /model tak lagi
        # menampilkan "aktif" pada entri yang tak ada.
        if not models.is_known_id(model_id):
            prefs.save(model=self.model_spec.id)
        # Effort model API: dipulihkan dari prefs lalu DISARING lewat tabel
        # kapabilitas modelnya (models.ModelSpec.effort_levels). Untuk model
        # web tetap None — di sana /effort berarti MENGKLIK tombol mode
        # berpikir di situsnya, jadi tak ada nilai yang perlu disimpan.
        self.effort: str | None = None
        self._init_effort()
        # Varian model SITUS (mis. "GLM-5.2", "K2.6") yang dipilih lewat
        # /model. Tidak dipasang di sini: memasangnya berarti membuka browser
        # hanya untuk memilih menu. Nilai ini DIPAKAI _run_connector tepat
        # sebelum kirim pertama giliran berikutnya (lihat di sana).
        self._web_varian: str | None = None
        # Varian yang SUDAH terpasang di browser tiap layanan — ingatan untuk
        # membedakan "browser terbuka dengan model lain" (perlu jendela baru)
        # dari "model yang sama" (cukup tab baru; lihat pasang_model_web).
        # Key = nama layanan (connector), bukan varian: satu layanan satu
        # jendela browser.
        self._web_terpasang: dict[str, str] = {}
        # Berapa kali effort dinaikkan SENDIRI dalam satu giliran (batasnya
        # config.MAX_ESCALATIONS). Direset di tiap run().
        self._escalations = 0

        self.memory = Memory(system_prompt=prompts.build_system_prompt())
        self.tool_names = tool_names
        self.max_iterations = max_iterations or config.MAX_TOOL_ITERATIONS
        self.session = session

        self.tokens_session = Usage()
        self.tokens_last = Usage()
        self.tokens_live = 0  # nilai token realtime untuk tampilan

        # Kunci global per-agent: cegah dua giliran berjalan bersamaan pada Agent
        # yang sama (berbagi self.memory). Saat bot Telegram berbagi agent dengan
        # CLI, kunci ini menserialkan pesan dari kedua antarmuka.
        self._run_lock = threading.Lock()

        # Connector web: apakah konteks laptop/proyek sudah dikirim ke sesi web
        # ini (dikirim SEKALI sbg preamble pesan pertama; AI web ingat sepanjang chat).
        self._web_ctx_sent = False
        # Percakapan AI web yang DILANJUTKAN (dari sesi tersimpan). Bila ada,
        # giliran pertama membuka chat itu — bukan chat baru — sehingga konteks
        # proyek yang sudah ada di sana tak perlu dikirim ulang.
        self._web_chat_id = ""
        # Posisi riwayat yang TERAKHIR dilihat chat layanan aktif — tak None
        # berarti ada perkembangan (dikerjakan di layanan lain) yang belum
        # sampai ke sana dan perlu dikirim sebagai ringkasan (lihat _hitung_gap).
        self._web_gap_from: int | None = None
        # Lampiran DIMATIKAN sementara untuk percakapan web ini karena situsnya
        # sudah mentok jumlah berkas (chat.z.ai: 10 per percakapan). Sengaja
        # per-percakapan, bukan permanen: chat baru berarti jatah berkasnya
        # kembali penuh, jadi _lupakan_chat_web menyalakannya lagi.
        self._lampiran_mati = False
        # Panjang percakapan web yang SEDANG berjalan, dalam karakter yang
        # bagas-ai kirim & terima (plus isi berkas konteks yang dilampirkan).
        # Situs tak memberi penghitung apa pun yang bisa dibaca dari luar, jadi
        # inilah satu-satunya ukuran yang kita punya untuk tahu kapan
        # percakapannya sudah pantas dipadatkan — lihat config.AUTO_COMPACT_CHARS.
        self._web_chars = 0
        # Riwayat percakapan APA ADANYA (pesan bagas-ai & balasan model),
        # jendela bergulir sebesar config.COMPACT_RIWAYAT_CHARS. Inilah isi
        # berkas memory /compact. Sengaja TIDAK ikut direset saat pindah chat:
        # yang berganti percakapannya di situs, bukan pekerjaan yang sedang
        # dikerjakan — dan justru pekerjaan itulah yang harus menyeberang.
        self._riwayat_web: list[dict[str, str]] = []
        # Sudah ada giliran lama yang terbuang dari jendela? Disebutkan di
        # berkas memory supaya model tak menyimpulkan bahwa percakapannya
        # memang dimulai dari situ.
        self._riwayat_terpotong = False
        # Snapshot ingatan SEBELUM pemangkasan pemulih konteks: tanpa ini,
        # /compact setelah pemulihan menghasilkan berkas ±3 KB kosongan —
        # riwayat yang dipangkas hilang total dari bekal /send-compact.
        self._snapshot_terakhir: list = []
        # Pernah menjalankan tangga pemangkasan? Dipakai /compact untuk
        # menjelaskan MENGAPA gilirannya sedikit.
        self._pernah_pangkas = False
        # Sudah pernah memberi tahu bahwa percakapan ini melewati ambang?
        # Sekali per percakapan; kabar yang sama tiap giliran cuma berisik.
        self._kabar_panjang = False
        # Sedang mengirim KONTEKS (bukan pekerjaan) -> jangan dicatat ke
        # riwayat. Lihat _tanpa_catatan.
        self._diam_catat = False
        # Berkas ingatan yang PALING AKHIR ditulis /compact di sesi ini.
        # Diingat path-nya, bukan dicari ulang dengan "yang terbaru": berkas
        # konteks internal lahir belakangan dan pernah menyerobot giliran ini.
        self._memory_terakhir: list = []
        if session is not None:
            saved = (getattr(session, "web_chats", None) or {}).get(
                self.model_spec.connector or "")
            if saved:
                self.use_web_chat(saved)

        # Token SESI bersifat persisten: saat --resume, lanjutkan hitungan
        # sesi sebelumnya (bukan mulai dari nol).
        if session and getattr(session, "tokens", None):
            self.tokens_session.prompt = int(session.tokens.get("prompt", 0) or 0)
            self.tokens_session.completion = int(
                session.tokens.get("completion", 0) or 0
            )
            try:
                self.tokens_session.cost = float(
                    session.tokens.get("cost", 0) or 0)
            except (TypeError, ValueError):
                self.tokens_session.cost = 0.0

        if session and session.messages:
            self.memory.load(session.messages)

    # --- model ---
    # `effort` punya DUA arti, tergantung jalur modelnya:
    #  - model web  : /effort MENGKLIK tombol mode berpikir di situsnya (lihat
    #                 WebConnector.web_actions). Tak ada state di sini, jadi
    #                 self.effort tetap None.
    #  - model API  : /effort memilih salah satu ModelSpec.effort_levels, yang
    #                 diterjemahkan jadi extra_body tiap request. Nilainya
    #                 disimpan di sini DAN di prefs.

    @property
    def model(self) -> str:
        return self.model_spec.id

    def set_model(self, name: str) -> str:
        """Pindah model. `name` boleh:
          - alias/id/label layanan atau model API seperti biasa;
          - "<layanan> <varian>" (bentuk pilihan menu /model, mis.
            "glm GLM-5.2") — layanan + varian model SITUSNYA;
          - nama varian polos ("GLM-5.2", "K2.6") bila tak ambigu.
        Varian TIDAK diklik di sini (butuh browser hidup); ia ditandai
        menunggu di _web_varian lalu dipasang _run_connector tepat sebelum
        kirim pertama giliran berikutnya."""
        varian: str | None = None
        spec: models.ModelSpec | None = None
        # Bentuk "<layanan> <varian>" dari menu /model.
        if " " in name.strip():
            alias, _, sisa = name.strip().partition(" ")
            try:
                kandidat = models.cari(alias)
            except ValueError:
                kandidat = None
            if kandidat is not None and kandidat.is_web and sisa.strip():
                spec, varian = kandidat, sisa.strip()
        if spec is None:
            # Nama varian polos -> layanan pemilik varian itu.
            ketemu = models.resolve_varian(name)
            if ketemu is not None:
                spec = models.cari(ketemu[0])
                varian = ketemu[1]
        if spec is None:
            spec = models.resolve(name)
        self._web_varian = varian
        before = self.model_spec.connector
        self.model_spec = models._pastikan_aktif(spec)
        if self.model_spec.connector != before:
            # Pindah layanan (mis. Kimi web -> Qwen web): state percakapan web
            # TIDAK boleh terbawa. Tanpa ini, layanan baru dikira sudah menerima
            # konteks (padahal chat-nya kosong) dan ID chat milik layanan lama
            # ikut terbawa.
            self._sync_web_state()
        # Effort DISARING, bukan dibawa buta dan bukan dinolkan.
        # TERUKUR: pindah nemotron(mendalam) -> deepseek menyisakan "mendalam",
        # karena tingkat itu ADA di keduanya dan itu pilihan sadar pengguna.
        # Yang TIDAK ada di model baru ("ringkas" milik DeepSeek saat pindah ke
        # Nemotron) jatuh ke bawaan model baru. Penyaringan ini wajib: nilai
        # asing diam-diam diabaikan extra_body_for(), jadi tanpa ini UI
        # memamerkan effort yang sebenarnya tak pernah dikirim ke server.
        self._init_effort()
        prefs.save(model=self.model_spec.id, effort=self.effort)
        if varian:
            return f"{self.model_spec.label} · {varian}"
        return self.model_spec.label

    def pasang_model_web(self, varian: str | None = None,
                         on_status: Any = None,
                         on_notice: Any = None) -> str:
        """Buka/atur browser untuk model web yang BARU SAJA dipilih (/model).

        Tiga keadaan yang dibedakan (permintaan pengguna):
          1. browser BELUM terbuka      -> buka jendela + pilih variannya;
          2. browser terbuka, model LAIN -> tutup jendela lama, buka JENDELA
             BARU dengan varian yang dipilih;
          3. browser terbuka, model SAMA -> buka TAB BARU di jendela itu,
             lampirkan berkas konteks, lalu minta model MEMBACANYA lewat
             tool read_file bawaan situsnya.

        Dipanggil dari thread worker UI SETELAH set_model() — di sini memang
        boleh memblokir (menunggu jendela/login) lama.

        Return pesan hasil untuk ditampilkan ke pengguna."""
        from . import connectors as _con
        from .connectors import browser as _browser

        svc = self.model_spec.connector
        conn = _con.get_connector(svc)
        label = self.model_spec.label

        def _status(msg: str) -> None:
            if on_status is not None:
                on_status(msg)

        def _kabar(msg: str) -> None:
            if on_notice is not None:
                on_notice(msg)

        # Varian boleh None (pilihan cuma nama layanan, mis. "dola-web").
        varian = varian or self._web_varian
        terpasang = self._web_terpasang.get(svc)
        hidup = _browser.browser_hidup(svc)
        sama = bool(hidup and varian and terpasang == varian)

        if hidup and terpasang and terpasang != varian:
            # Keadaan 2: jendela lain sedang memakai model berbeda. Tanpa ini
            # varian barunya diklik DI ATAS percakapan model lama — situs
            # memindahkan seluruh chatnya ke model baru, persis yang tak
            # diinginkan pengguna yang memilih "jendela baru".
            _status(f"menutup jendela {label} yang memakai {terpasang}…")
            _browser.tutup_service(svc)
            # Jendela baru = chat baru: kaitan chat lama tak berlaku lagi,
            # konteksnya akan dikirim ulang otomatis pada pesan pertama.
            self._lupakan_chat_web()
            self._web_ctx_sent = False
            self._web_chars = 0

        try:
            if sama:
                # Keadaan 3: TAB baru di jendela yang sama — percakapan lama
                # utuh di tabnya, dan tab baru langsung diberi konteks lewat
                # berkas yang disuruh dibaca read_file.
                _status(f"membuka tab baru {label}…")
                conn.open_new_chat(new_tab=True, on_status=on_status,
                                   on_notice=on_notice)
                hasil = self._kirim_konteks_tab(conn, varian,
                                                on_status=on_status,
                                                on_notice=on_notice)
                return hasil
            # Keadaan 1 / jendela baru: sambungkan (login bila perlu), lalu
            # klik variannya di situsnya.
            _status(f"membuka {label}…")
            conn.connect(on_status=on_status, on_notice=on_notice)
            if varian:
                _status(f"memilih model {varian} di {label}…")
                conn.set_web_option(varian)
                self._web_varian = None
                self._web_terpasang[svc] = varian
                return f"jendela {label} siap dengan model {varian}"
            self._web_terpasang[svc] = ""
            return f"jendela {label} siap"
        except Exception as exc:  # noqa: BLE001 — kegagalan tak mematikan app
            _kabar(f"⚠ {exc}")
            return f"⚠ tak bisa membuka {label}: {exc}"

    def _kirim_konteks_tab(self, conn: Any, varian: str | None,
                           on_status: Any = None,
                           on_notice: Any = None) -> str:
        """Tab baru (model yang sama): bawa konteksnya ke chat di tab itu.

        Polanya persis _padatkan_web (konteks-penuh saat situs menolak chat
        lama): simpan ingatan -> kirim sebagai BERKAS terlampir lewat
        _kirim_konteks -> pengantarnya menyuruh model MEMBACA berkasnya
        (tool read_file situsnya). Jalur _kirim_konteks dipakai apa adanya
        supaya verifikasi kode-periksa & cadangan teksnya ikut bekerja."""
        _status = (lambda m: on_status(m)) if on_status else (lambda m: None)
        # Kaitan chat lama tak berlaku: kita sekarang berada di chat BARU di
        # tab baru. Tanpa ini, pesan berikutnya malah membuka chat lama dan
        # seluruh konteks yang barusan dikirim tertinggal di tab sebelah.
        self._lupakan_chat_web()
        _status("menyimpan riwayat percakapan…")
        berkas = self.simpan_memory()

        def kirim(teks: str, new_chat: bool, open_chat_id: Any,
                  attachments: list | None = None) -> str:
            return conn.send(
                teks, on_status=on_status, on_notice=on_notice,
                new_chat=bool(new_chat),
                open_chat_id=str(open_chat_id or ""),
                attachments=list(attachments or []))

        if berkas:
            # Imbuhan khas jalur tab-baru: sebut nama toolnya (read_file)
            # eksplisit — pengantarnya (milik jalur konteks-pertama) sengaja
            # generik "BUKA dan baca" karena tiap situs menamai tool bawaannya
            # sendiri; di sini pengguna memintanya disebut tegas.
            pengantar = self._pengantar_memory(
                [p.name for p in berkas], konteks.kode(berkas),
                lanjutan=True) + (
                "\nPakai tool read_file-mu untuk membaca tiap berkasnya.")
            self._kirim_konteks(
                kirim, conn, berkas=berkas, kode=konteks.kode(berkas),
                pengantar=pengantar,
                teks_penuh=self._konteks_teks(), new_chat=False,
                open_chat_id="", on_status=on_status, on_notice=on_notice)
        else:
            with self._tanpa_catatan():
                kirim(self._konteks_teks(), False, "")
        dibuat = getattr(conn, "last_chat_id", "") or ""
        if dibuat:
            self._link_web_chat(dibuat)
        # Konteks sudah berada di chat tab baru: pesan pertama tak boleh
        # mengirimkannya lagi (dobel, dan bisa kena batas lampiran situs).
        self._web_ctx_sent = True
        self._web_terpasang[self.model_spec.connector] = varian or ""
        return (f"tab baru {self.model_spec.label} siap"
                + (f" ({varian})" if varian else "")
                + " — konteks terkirim sebagai berkas, menunggu dibaca "
                  "read_file")

    def _init_effort(self) -> None:
        """Tetapkan effort awal dari prefs, disaring tabel kapabilitas model.

        Disaring, bukan dipakai apa adanya: nilai tersimpan bisa milik model
        lain. Nilai asing akan diam-diam diabaikan extra_body_for(), jadi lebih
        baik dibereskan di sini — kalau tidak, UI memamerkan effort yang tak
        pernah sampai ke server."""
        spec = self.model_spec
        if spec.is_web or not spec.effort_levels:
            self.effort = None
            return
        tersimpan = prefs.get_effort()
        self.effort = (tersimpan if tersimpan in spec.effort_levels
                       else (spec.effort_default or None))

    def set_effort(self, name: str) -> str | None:
        """Ganti effort model API. None = model ini memang tak punya effort."""
        spec = self.model_spec
        if spec.is_web or not spec.effort_levels:
            return None
        if name not in spec.effort_levels:
            raise ValueError(
                f"Effort '{name}' tak dikenal untuk {spec.label}. Pilihan: "
                + ", ".join(spec.effort_levels))
        self.effort = name
        prefs.save(effort=name)
        return name

    def _sync_web_state(self) -> None:
        """Selaraskan kaitan chat web dengan LAYANAN yang sedang aktif."""
        svc = self.model_spec.connector
        saved = ""
        if svc and self.session is not None:
            saved = (getattr(self.session, "web_chats", None) or {}).get(svc, "")
        self._web_chat_id = saved
        # Konteks dianggap sudah terkirim HANYA bila kita menyambung chat lama
        # milik layanan ini; chat baru selalu perlu konteks lagi.
        self._web_ctx_sent = bool(saved)
        # Percakapan yang lain = panjang yang lain. Hitungannya TIDAK boleh
        # terbawa, kalau tidak chat yang baru saja dibuka langsung dianggap
        # penuh gara-gara warisan percakapan di layanan sebelumnya.
        self._web_chars = 0
        self._hitung_gap(svc, saved)

    def _hitung_gap(self, svc: str, saved: str) -> None:
        """Adakah perkembangan yang BELUM dilihat chat layanan ini?

        Pindah-pulang antar model (A -> B -> A) punya lubang yang tak tertutup
        oleh konteks-pertama: chat A memang sudah berkonteks, jadi ia dianggap
        tak perlu apa-apa lagi — padahal pekerjaan yang berjalan di B selama
        di sana TIDAK PERNAH sampai ke A. Penanda `web_seen` di sesi mencatat
        sejauh mana chat tiap layanan pernah melihat riwayat; bila riwayat
        kini lebih panjang dari itu, catat posisinya di `_web_gap_from` agar
        giliran berikutnya mengirim RINGKASAN kemajuannya (bukan konteks
        penuh) ke chat yang sudah ada."""
        self._web_gap_from = None
        if not saved or self.session is None or not svc:
            return
        try:
            seen = (getattr(self.session, "web_seen", None) or {}).get(svc, 0)
            kini = len(self.memory.messages)
        except Exception:  # noqa: BLE001 - sesi lama tanpa penanda -> anggap segar
            return
        if kini > seen:
            self._web_gap_from = seen

    def _stamp_web_seen(self) -> None:
        """Catat: chat layanan aktif SUDAH melihat riwayat sampai sepanjang ini.

        Dipanggil di akhir giliran web yang tuntas — satu-satunya titik di mana
        isi memory dan isi percakapan di situs benar-benar sejajar."""
        if not (self.session is not None and self._web_chat_id):
            return
        svc = self.model_spec.connector
        if not svc:
            return
        try:
            seen = getattr(self.session, "web_seen", None)
            if seen is None:      # sesi lama tanpa atribut ini
                seen = {}
                self.session.web_seen = seen
            seen[svc] = len(self.memory.messages)
            self._web_gap_from = None
        except Exception:  # noqa: BLE001 - penanda gagal tak boleh mengganggu
            pass

    def _escalate(self, reason: str) -> str | None:
        """Naikkan effort satu tingkat saat model terdeteksi mandek/mengulang.

        HANYA effort — TIDAK berpindah model, meski versi lama fungsi ini
        melakukannya sesudah effort tertinggi. Berpindah model tak boleh
        dihidupkan lagi karena katalognya kini BERCAMPUR: pindah otomatis bisa
        mendarat di model web, yang berarti jendela login browser muncul
        mendadak di tengah tugas. Lebih buruk lagi, kedua jalur memakai protokol
        tool yang berbeda — riwayat berisi `tool_calls` asli tak bisa
        dilanjutkan oleh model web yang cuma mengerti penanda [[TOOL]], jadi
        perpindahan itu merusak konteks yang justru ingin diselamatkan.

        Kembalikan keterangan perubahan, atau None bila tak ada yang bisa
        dinaikkan (model web, model tanpa tingkatan, sudah di puncak, atau
        anggaran naik-kelas giliran ini habis).
        """
        spec = self.model_spec
        if spec.is_web or not spec.effort_levels:
            return None
        if self._escalations >= config.MAX_ESCALATIONS:
            return None
        opts = list(spec.effort_levels)
        kini = self.effort if self.effort in opts else spec.effort_default
        if kini not in opts:
            return None
        i = opts.index(kini)
        if i >= len(opts) - 1:
            return None
        lama, self.effort = kini, opts[i + 1]
        self._escalations += 1
        return f"effort {lama} → {self.effort} ({reason})"

    # Penjaga anti-macet jalur WEB tak lewat sini: ia ada di _run_connector
    # dalam bentuk yang sesuai medianya (batas tool berulang & beruntun gagal,
    # lalu dipaksa menyimpulkan), sebab di situs tak ada parameter effort yang
    # bisa dinaikkan tanpa mengklik tombol di halamannya.

    # --- kaitan sesi terminal <-> percakapan di AI web ---
    def use_web_chat(self, chat_id: str) -> None:
        """Sambungkan sesi ini ke percakapan AI web yang SUDAH ADA.

        Konteks proyek & protokol tool sudah tersimpan di percakapan itu, jadi
        tak dikirim ulang (hemat & AI web langsung 'ingat' proyeknya)."""
        self._web_chat_id = chat_id or ""
        self._web_ctx_sent = bool(chat_id)
        self._hitung_gap(self.model_spec.connector, chat_id or "")
        # Panjang chat lama tak bisa kita ketahui dari luar (isinya sudah ada di
        # situs sebelum sesi ini). Dimulai dari nol dengan sadar: menebak angka
        # besar berarti memadatkan percakapan yang mungkin masih lega.
        self._web_chars = 0

    def start_new_web_chat(self, *, immediate: bool = False) -> None:
        """Lupakan kaitan chat web -> giliran berikutnya membuat chat BARU.

        `immediate=True` langsung membuka percakapan baru di browser saat ini
        juga — dipakai saat /new di terminal. Tanpa itu, browser baru pindah
        ke chat baru saat pesan berikutnya dikirim (dipakai reset, dll)."""
        self._web_chat_id = ""
        self._web_ctx_sent = False
        self._web_chars = 0
        self._kabar_panjang = False
        # Chat baru menerima konteks PENUH di pesan pertamanya, jadi tak ada
        # 'kemajuan yang tertinggal' yang perlu diringkaskan.
        self._web_gap_from = None
        if self.session is not None:
            svc = self.model_spec.connector
            if svc and svc in getattr(self.session, "web_chats", {}):
                self.session.web_chats.pop(svc, None)

        if immediate and self.model_spec.is_web:
            from . import connectors
            try:
                conn = connectors.get_connector(self.model_spec.connector)
                conn.open_new_chat()
            except Exception:
                pass  # /new tetap berhasil di sisi agent bila browser bermasalah

    def _link_web_chat(self, chat_id: str) -> None:
        """Catat chat web ini sebagai milik sesi terminal saat ini (1 sesi
        terminal = 1 percakapan browser, juga dipakai saat --resume)."""
        self._web_chat_id = chat_id
        svc = self.model_spec.connector
        if self.session is not None and svc and chat_id:
            try:
                self.session.web_chats[svc] = chat_id
            except AttributeError:  # sesi lama tanpa atribut ini
                self.session.web_chats = {svc: chat_id}

    def _lupakan_chat_web(self) -> None:
        """Lepaskan kaitan ke percakapan web sesi ini.

        Sesudah ini, kirim berikutnya membuat percakapan BARU di situs dan
        mengirim ulang konteks pembukanya. Dipakai oleh dua pemulihan yang
        sama-sama butuh chat bersih: chat yang rusak, dan chat yang sudah
        kepanjangan.

        Ada sebagai satu tempat karena sebelumnya keduanya memanggil
        `conn.new_chat()` — method yang TIDAK PERNAH ADA di connector mana pun.
        Panggilan itu melempar AttributeError yang langsung tertelan `except`
        di sekitarnya, sehingga pemulihan chat rusak selalu berakhir "tak bisa
        dipulihkan" tanpa sekali pun benar-benar mencoba."""
        self._web_chat_id = ""
        self._web_ctx_sent = False
        # Percakapan berikutnya mulai dari nol — termasuk hitungan panjangnya,
        # yang jadi dasar simpanan otomatis berikutnya.
        self._web_chars = 0
        self._kabar_panjang = False
        self._web_gap_from = None
        # Jatah berkas dihitung PER PERCAKAPAN, jadi chat baru = lampiran boleh
        # dipakai lagi. Tanpa baris ini, sekali kena batas, seluruh sisa sesi
        # berjalan buta walau sudah pindah ke chat yang kosong.
        self._lampiran_mati = False
        svc = self.model_spec.connector
        if self.session is not None and svc:
            try:
                self.session.web_chats.pop(svc, None)
            except AttributeError:      # sesi lama tanpa atribut ini
                pass

    # Ditempel ke pesan yang gagal terkirim BERSAMA gambarnya, lalu dikirim
    # ulang tanpa lampiran. Isinya sengaja bukan sekadar permintaan maaf: AI
    # web yang kehilangan gambar cenderung MENUNGGU atau minta dikirim ulang,
    # dan giliran berhenti di situ. Yang dibutuhkan adalah jalan kerja
    # pengganti yang konkret — makanya tool penggantinya disebut namanya.
    _TANPA_GAMBAR = (
        "\n\n[SISTEM] Screenshot untuk pesan ini TIDAK bisa dilampirkan: "
        "situsnya membatasi jumlah berkas per percakapan ({sebab}). Ini bukan "
        "kesalahanmu dan tak perlu diulang.\n"
        "Lanjutkan TANPA melihat gambarnya, pakai jalan lain:\n"
        "- butuh isi halaman? panggil web_extractor pada URL yang sama untuk "
        "membaca teks & strukturnya;\n"
        "- butuh memastikan kode? baca berkasnya (read_file/search_text) — "
        "sumbernya jauh lebih tepat daripada tangkapan layar;\n"
        "- butuh galat runtime? minta aku menjalankan perintahnya (run) dan "
        "baca keluarannya.\n"
        "JANGAN meminta gambar dikirim ulang dan jangan berhenti menunggu: "
        "kerjakan langkah berikutnya sekarang."
    )

    # --- riwayat percakapan web (bahan berkas memory) ---------------------
    #
    # memory.messages TIDAK cukup untuk ini: yang tercatat di sana cuma
    # permintaan pengguna & jawaban akhir tiap giliran. Justru yang hilang di
    # situ yang paling dibutuhkan chat berikutnya — blok [[TOOL]] yang benar
    # dijalankan, kode yang ditulis, dan hasil nyata tiap langkah. Karena itu
    # percakapan ke situs dicatat APA ADANYA di sini.

    # Batas satu catatan. Hasil tool bisa raksasa (isi berkas 2.000 baris);
    # tanpa batas, SATU langkah menelan seluruh jatah dan menyisakan nol untuk
    # sisa percakapan. Yang dipotong bagian TENGAH — kepala & ekor potongan
    # kode adalah dua tempat yang paling menentukan saat menyambung pekerjaan.
    _MAKS_CATATAN = 6000

    @contextlib.contextmanager
    def _tanpa_catatan(self):
        """Jangan catat kirim-terima di dalam blok ini.

        Dipakai saat bagas-ai mengirim KONTEKS (aturan protokol, pengantar
        berkas ingatan) — bukan pekerjaan. Tanpa ini, berkas memory berikutnya
        akan berisi salinan aturan protokol dan balasan "SIAP", memakan jatah
        riwayat untuk sesuatu yang toh selalu dikirim ulang."""
        sebelum = self._diam_catat
        self._diam_catat = True
        try:
            yield
        finally:
            self._diam_catat = sebelum

    def _catat_web(self, dari: str, teks: str) -> None:
        """Simpan satu pesan/balasan ke riwayat verbatim (jendela bergulir)."""
        teks = (teks or "").strip()
        if not teks or self._diam_catat:
            return
        self._riwayat_web.append(
            {"dari": dari, "isi": _potong_tengah(teks, self._MAKS_CATATAN)})
        # Jendelanya digulung DI SINI, bukan saat menyimpan: sesi panjang tak
        # boleh menumpuk ratusan ribu karakter di memori proses hanya untuk
        # dibuang belakangan.
        jatah = max(int(config.COMPACT_RIWAYAT_CHARS), 0)
        total = sum(len(r["isi"]) for r in self._riwayat_web)
        while len(self._riwayat_web) > 1 and total > jatah:
            total -= len(self._riwayat_web.pop(0)["isi"])
            self._riwayat_terpotong = True

    # Tahapan simpan_memory beserta JATAHNYA di bar kemajuan. Angkanya bukan
    # hiasan: menyusun payload (baca peta proyek + rakit riwayat) memang bagian
    # paling lama — menulis berkasnya sendiri nyaris seketika. Bar yang membagi
    # rata malah berbohong: ia melompat ke 90% lalu menggantung di situ.
    _TAHAP_PADAT = ((0.08, "membaca peta proyek & memori"),
                    (0.62, "menyusun riwayat percakapan"),
                    (0.70, "memecah jadi bagian yang muat dibaca situs"),
                    (1.00, "ingatan tersimpan"))

    def simpan_memory(self, on_progress: Any = None,
                      awalan: str = konteks.AWALAN) -> list:
        """Tulis KONTEKS + RIWAYAT percakapan ke berkas. Return daftar Path.

        `on_progress(pecahan, keterangan)` dipanggil di tiap tahap — dipakai
        terminal untuk bar & perkiraan sisa waktu.

        `awalan` memisahkan DUA jenis berkas yang selama ini tercampur:
        `memory-*` (ingatan pengguna, yang dikirim /send-compact) dan
        `konteks-*` (berkas pembuka yang dibuat bagas-ai sendiri tiap memulai
        chat — peta proyek tanpa riwayat, sekali pakai). Dulu keduanya
        bernama sama dan /send-compact memilih yang PALING BARU; akibatnya
        ingatan yang barusan dipadatkan kalah oleh berkas konteks 2 KB yang
        lahir sesudahnya, lalu itulah yang terkirim.

        Inilah "/compact" yang sebenarnya: seluruhnya dikerjakan bagas-ai
        sendiri di laptop. Tak ada pesan yang dikirim ke situs, model tak
        dimintai ringkasan apa pun, dan tak ada chat baru yang dibuka.

        Meminta ringkasan ke model — cara lama — punya tiga cacat yang tak bisa
        ditambal: ia butuh satu giliran penuh di chat yang justru sedang
        bermasalah, hasilnya bisa mengarang atau melewatkan berkas yang
        setengah jadi, dan bila chat lamanya sudah menolak menjawab, tak ada
        yang tersimpan sama sekali. Menyalin riwayatnya sendiri selalu berhasil
        dan selalu jujur.
        """
        # Seluruhnya dijaga: /compact dipanggil dari REPL tanpa jaring apa pun
        # di atasnya, jadi satu galat kecil (peta proyek gagal dibaca, disk
        # penuh) tak boleh menjatuhkan seluruh sesi pengguna.
        def lapor(i: int) -> None:
            if on_progress:
                on_progress(*self._TAHAP_PADAT[i])

        try:
            lapor(0)
            payload = prompts.build_context_payload(
                messages=self.memory.messages,
                riwayat=list(self._riwayat_web),
                dipotong=self._riwayat_terpotong,
            )
            lapor(1)
            # Penulisan berkas berhenti sedikit SEBELUM 100%: yang berhak
            # menyatakan selesai cuma tahap terakhir. Kalau tahap ini ikut
            # menyentuh 1.0, penanda "selesai" datang dua kali dan penantian
            # tampilan di sisi terminal berjalan dua putaran.
            awal, akhir = self._TAHAP_PADAT[2][0], 0.97

            def tulis_maju(pecahan: float, ket: str) -> None:
                if on_progress:
                    on_progress(awal + (akhir - awal) * pecahan, ket)

            berkas = konteks.tulis(
                payload, awalan=awalan,
                sesi=getattr(self.session, "id", "") or "",
                on_progress=tulis_maju if on_progress else None)
            lapor(3)
            # Yang diingat HANYA ingatan pengguna. /send-compact memakai path
            # ini apa adanya, jadi berkas konteks internal (yang lahir tiap
            # kali chat baru dibuka) tak bisa lagi menyerobot gilirannya.
            if awalan == konteks.AWALAN:
                self._memory_terakhir = list(berkas)
            return berkas
        except Exception:  # noqa: BLE001
            log.debug("gagal menulis berkas memory", exc_info=True)
            return []

    def padatkan_sekarang(self, on_status: Any = None,
                          on_notice: Any = None,
                          on_padat: Any = None) -> str:
        """/compact: simpan riwayat percakapan ke berkas memory.

        Tak menyentuh situs sama sekali. Chat yang sedang berjalan TETAP
        berjalan — berkas ini bekal untuk chat berikutnya, dikirim saat
        pengguna memintanya lewat /send-compact."""
        if on_status:
            on_status("menyimpan riwayat percakapan…")
        berkas = self.simpan_memory(on_progress=on_padat)
        if not berkas:
            return ("Riwayat tak bisa disimpan (berkasnya gagal ditulis di "
                    f"{config.KONTEKS_DIR}).")
        # Jumlah giliran dihitung dari SUMBER yang benar per jalur, karena isi
        # berkasnya juga beda:
        #  - (web) _riwayat_web  -> percakapan APA ADANYA (mentah/verbatim)
        #  - (API) memory.messages -> ringkasan_giliran; riwayatnya kita pegang
        #          sendiri, dan _riwayat_web memang SELALU kosong di jalur ini
        #          (cuma _catat_web yang mengisinya, dan itu di _run_connector).
        # Tanpa pemisahan ini jalur (API) selalu melapor "BELUM ada percakapan"
        # padahal gilirannya IKUT tersimpan -- pengguna menyangka pekerjaannya
        # hilang lalu mengulang dari nol.
        web = self.model_spec.is_web
        giliran = (len(self._riwayat_web) if web
                   else len(prompts.transcript_rows(self.memory.messages)))
        besar = konteks.ukuran(berkas)
        # Dikatakan apa adanya kalau riwayatnya memang belum ada (sesi baru,
        # atau seluruh percakapan terjadi sebelum bagas-ai ini dijalankan).
        # Berkasnya tetap berguna — isinya konteks proyek — tapi menyebutnya
        # "riwayat tersimpan" akan membuat pengguna mengira pekerjaannya
        # terbawa padahal tidak.
        isi = (f"- {giliran} giliran percakapan"
               + (" apa adanya" if web else " (ringkasannya)")
               if giliran else
               "- BELUM ada percakapan yang tercatat di sesi ini — yang "
               "tersimpan baru konteks proyeknya")
        # Dipecah HANYA bila melewati batas ukuran; selama muat ia satu berkas.
        pecah = (f"\n- Dipecah jadi {len(berkas)} berkas karena melewati "
                 f"{config.KONTEKS_MAKS_BYTES // 1024} KB."
                 if len(berkas) > 1 else "")
        # Penutupnya per jalur juga. /send-compact MENOLAK model (API) — ia
        # mengunggah berkas ke kotak chat di situs — jadi menyarankannya di sana
        # sama dengan menyuruh pengguna menabrak dinding.
        if web:
            catatan = (
                "- Chat di situs TIDAK diapa-apakan dan tak ada yang dikirim.\n\n"
                "Untuk melanjutkan di percakapan yang bersih: `/new` lalu "
                "`/send-compact` — berkas INI yang diunggah, bukan yang lain.")
        else:
            catatan = (
                "- Percakapan ini jalan terus; tak ada yang dikirim ke mana pun.\n\n"
                "Sesudah `/new`, pasang lagi dengan `/send-compact`: di jalur "
                "API intinya DISUNTIKKAN ke riwayat (ringkasan giliran + ekor "
                "percakapan terakhir). Peta proyek & memorimu tak ikut "
                "disuntik — keduanya memang otomatis ada di sesi mana pun.")
            if getattr(self, "_pernah_pangkas", False):
                catatan += ("\n\nCatatan: jumlah giliran kecil karena riwayat "
                            "panjang perlu dipangkas otomatis agar muat konteks "
                            "model.")
                if self._snapshot_terakhir:
                    catatan += (" Salinan lengkap SEBELUM pemangkasan: "
                                + ", ".join(
                                    p.name for p in self._snapshot_terakhir)
                                + ".")
        return (
            f"Ingatan tersimpan di `{berkas[0].parent}`:\n"
            + "\n".join(f"  - {p.name}" for p in berkas) + "\n\n"
            f"{isi} (±{besar // 1024} KB), plus peta proyek & memori.{pecah}\n"
            + catatan
        )

    def _simpan_otomatis(self, conn: Any = None, on_notice: Any = None,
                         on_padat: Any = None) -> None:
        """Simpan ingatan SENDIRI begitu percakapannya sudah panjang.

        Dipanggil di akhir tiap giliran, dan MENJEDA giliran itu selama
        berlangsung — bukan karena harus, tapi karena kejadian sepenting ini
        tak boleh lewat tanpa terlihat. `on_padat(pecahan, keterangan)`
        menggerakkan bar di terminal; pemanggilnya boleh menahan sejenak di
        akhir supaya animasinya sempat terbaca (lihat cli._jeda_padat).

        Tak mengirim apa pun ke situs dan tak memindahkan pekerjaan ke mana
        pun — cuma menyiapkan bekal, supaya keputusan pindah (yang milik
        pengguna) tinggal satu perintah kapan pun ia diambil. Setelah lewat
        ambang, berkasnya diperbarui tiap giliran agar isinya selalu
        mencerminkan pekerjaan terakhir; yang diumumkan hanya sekali per
        percakapan supaya tak jadi berisik."""
        ambang = int(config.AUTO_COMPACT_CHARS)
        situs_mengeluh = bool(getattr(conn, "konteks_penuh", False))
        panjang = ambang > 0 and self._web_chars >= ambang
        if not (panjang or situs_mengeluh):
            return
        berkas = self.simpan_memory(on_progress=on_padat)
        if not berkas or self._kabar_panjang:
            return
        self._kabar_panjang = True
        sebab = ("situs memperingatkan percakapannya sudah penuh"
                 if situs_mengeluh
                 else f"percakapan ini sudah ±{self._web_chars // 1000} rb "
                      "karakter")
        if on_notice:
            on_notice(f"{sebab} — ingatannya kusimpan ({len(berkas)} berkas); "
                      "`/new` lalu `/send-compact` untuk lanjut di chat bersih")

    def _pasang_memory_api(self, path: Any = None,
                           on_status: Any = None) -> str:
        """/send-compact di jalur API: suntikkan inti berkas ingatan ke
        riwayat percakapan yang sekarang.

        Jalur WEB mengunggah berkasnya ke situs; jalur API tak punya komposer
        — tapi justru KITA yang pegang riwayat dan mengirimnya ulang tiap
        request, jadi "memasang" berarti menambahkan INTInya sebagai pesan
        konteks. Yang diambil ringkasan giliran + ekor percakapan terakhir,
        BUKAN seluruh JSON: menyuntik 200 KB berarti membakar token itu lagi
        di SETIAP giliran berikutnya. Peta proyek & memori pengguna tak ikut
        disuntik — keduanya sudah otomatis ada di system prompt sesi mana
        pun."""
        if path:
            berkas = konteks.sekelompok(path)
        elif [p for p in self._memory_terakhir if Path(p).is_file()]:
            berkas = list(self._memory_terakhir)
        else:
            berkas = konteks.terbaru(konteks.AWALAN)
        berkas = [Path(p) for p in berkas if Path(p).is_file()]
        if not berkas:
            # Kegagalan DIANGKAT, bukan dikembalikan sebagai teks: dulu CLI
            # mencetak "✓ ingatan terpasang" untuk pesan ini juga — klaim
            # sukses di atas kegagalan yang nyata.
            raise ValueError(
                "Belum ada berkas ingatan yang tersimpan. Jalankan "
                "`/compact` dulu di percakapan yang ingin kamu bawa.")
        if len(konteks.kode(berkas)) != len(berkas):
            raise ValueError(
                "Berkas memory tak terbaca / bukan buatan bagas-ai: "
                + ", ".join(p.name for p in berkas))
        if on_status:
            on_status("memasang ingatan ke percakapan…")

        ringkas: list[tuple[str, str]] = []
        ekor: list[tuple[str, str]] = []
        terlihat: set[tuple[str, str]] = set()

        def _bersih(isi: Any) -> str:
            # Berkas memory buatan versi lama bisa membawa penanda
            # [LAMPIR-MEDIA] di isinya — jangan disuntikkan apa adanya.
            return "\n".join(
                ln for ln in " ".join(str(isi).split()).splitlines()
                if not ln.startswith(_MEDIA_LAMPIR)).strip()

        for p in berkas:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - bagian rusak dilewati saja
                continue
            for r in (data.get("ringkasan_giliran") or []):
                if isinstance(r, dict) and r.get("isi"):
                    isi = _bersih(r["isi"])
                    if not isi:
                        continue
                    pasangan = (str(r.get("dari", "?")), isi)
                    if pasangan not in terlihat:
                        terlihat.add(pasangan)
                        ringkas.append(pasangan)
            verbatim = data.get("percakapan_terakhir_apa_adanya") or {}
            for g in (verbatim.get("giliran") or [])[-10:]:
                if isinstance(g, dict) and g.get("isi"):
                    isi = _bersih(g["isi"])[:600]
                    if not isi:
                        continue
                    pasangan = (str(g.get("dari", "?")), isi)
                    if pasangan not in terlihat:
                        terlihat.add(pasangan)
                        ekor.append(pasangan)

        batas = 24_000          # ±6 rb token — murah untuk SETIAP request
        bagian: list[str] = []
        if ringkas:
            bagian.append(
                "== RINGKASAN PERMINTAAN & JAWABAN (lama ke baru) ==\n"
                + "\n".join(f"[{d}] {i}" for d, i in ringkas))
        if ekor:
            bagian.append(
                "== EKOR PERCAKAPAN TERAKHIR (dipangkas) ==\n"
                + "\n".join(f"[{d}] {i}" for d, i in ekor[-12:]))
        teks = "\n\n".join(bagian)[:batas]
        if not teks:
            return ("Berkas ingatannya ada, tapi tidak memuat giliran "
                    "percakapan — cuma konteks proyek (yang memang sudah "
                    "otomatis ada di sesi ini). Tak ada yang perlu dipasang.")
        self.memory.add({
            "role": "user",
            "content": (
                f"[SISTEM] Konteks sesi sebelumnya ({len(berkas)} berkas "
                f"memory) dipasang agar kerjanya bisa dilanjutkan:\n\n{teks}\n"
                "[SISTEM] Ini CATATAN LAMPAU, bukan permintaan baru — jangan "
                "mengerjakan apa pun darinya sampai pengguna meminta."),
        })
        rincian = (f"{konteks.jumlah_giliran(berkas)} giliran, "
                   f"±{konteks.ukuran(berkas) // 1024} KB\n"
                   + "\n".join(f"  - {p.parent.name}/{p.name}" for p in berkas))
        return (f"Ingatan dipasang ke percakapan ({len(teks)} karakter "
                f"disuntik: ringkasan + ekor terakhir):\n{rincian}")

    def _pulih_konteks(self, pangkas_ke: int, tugas: str) -> int:
        """Satu anak tangga pemulih konteks. Return jumlah entri tersisa.

        SEBELUM pangkasan pertama, riwayat penuh DI-SNAPSHOT ke berkas memory
        (jalur /compact) — pemangkasan itu destruktif, dan tanpa snapshot
        pekerjaan yang diselamatkan dari provider justru hilang dari bekal
        `/send-compact` (bug nyata: /compact pasca-pemulihan menghasilkan
        berkas ±3 KB tanpa satu pun giliran)."""
        # pangkas_ke sudah +1 saat dipanggil dari handler (anak tangga pertama
        # = 1), jadi ambang snapshotnya <= 1 — bukan == 0.
        if pangkas_ke <= 1 and not self._snapshot_terakhir:
            try:
                snap = self.simpan_memory()
                if snap:
                    self._snapshot_terakhir = list(snap)
            except Exception:  # noqa: BLE001 - snapshot gagal tak boleh
                pass           # menggagalkan penyelamatan giliran
        sisa = self.memory.potong_awal(14 if pangkas_ke == 1 else 6)
        _pastikan_tugas_aktif(self.memory, tugas)
        return sisa

    def kirim_memory(self, path: Any = None, on_status: Any = None,
                     on_notice: Any = None) -> str:
        """/send-compact: pasang berkas memory ke percakapan sekarang.

        Dipakai SESUDAH /new supaya percakapan bersih langsung tahu sudah
        sampai mana. Jalurnya beda mekanisme: model WEB menerima BERKASNYA
        diunggah ke situs; model API menerima intinya DISUNTIKKAN ke riwayat
        (lihat _pasang_memory_api)."""
        from . import connectors
        if not self.model_spec.is_web:
            # Kegagalan keras (tak ada berkas / berkas rusak) DIANGKAT sebagai
            # ValueError supaya CLI bisa menampilkan ✗ — bukan ✓ palsu.
            return self._pasang_memory_api(path, on_status=on_status)
        # URUTAN PEMILIHANNYA PENTING, dan inilah bug yang pernah terjadi:
        #   1. path yang disebut pengguna;
        #   2. berkas yang BARUSAN ditulis /compact di sesi ini (diingat
        #      path-nya, bukan dicari lagi);
        #   3. baru "memory-* terbaru" dari sesi mana pun — untuk /send-compact
        #      sesudah /new atau sesudah bagas-ai dijalankan ulang.
        # Dulu langkah 3 satu-satunya, dan berkas KONTEKS internal yang lahir
        # sesudah /compact selalu menang karena lebih baru: yang terkirim
        # ingatan 2 KB tanpa satu pun giliran, sementara hasil /compact-nya
        # tertinggal di disk.
        if path:
            berkas = konteks.sekelompok(path)
        elif [p for p in self._memory_terakhir if Path(p).is_file()]:
            berkas = list(self._memory_terakhir)
        else:
            berkas = konteks.terbaru(konteks.AWALAN)
        berkas = [Path(p) for p in berkas if Path(p).is_file()]
        if not berkas:
            return ("Belum ada berkas ingatan yang tersimpan. Jalankan "
                    "`/compact` dulu di percakapan yang ingin kamu bawa.")
        conn = connectors.get_connector(self.model_spec.connector)
        if not conn.supports_context_files():
            return (f"{self.model_spec.label} belum mendukung unggah berkas, "
                    "jadi berkas memory tak bisa dikirim ke sana.")

        kode = konteks.kode(berkas)
        if len(kode) != len(berkas):
            return ("Berkas memory tak terbaca / bukan buatan bagas-ai: "
                    + ", ".join(p.name for p in berkas))

        def kirim(msg: str, new_chat: bool = False,
                  open_chat_id: str | None = None,
                  attachments: list[str] | None = None) -> str:
            out = conn.send(
                msg, on_status=on_status, new_chat=new_chat,
                open_chat_id=(self._web_chat_id if open_chat_id is None
                              else open_chat_id),
                attachments=attachments)
            self._web_chars += len(msg) + len(out or "")
            return out

        # Chat BARU dibuat di sini hanya bila sesi ini memang belum punya
        # (sesudah /new). Kalau pengguna menjalankannya di chat yang sudah
        # jalan, ingatannya ditambahkan ke chat itu — bukan dipindah diam-diam.
        baru = not self._web_chat_id
        nama = [p.name for p in berkas]
        jawab = self._kirim_konteks(
            kirim, conn, berkas=berkas, kode=kode,
            pengantar=self._pengantar_memory(nama, kode, lanjutan=True),
            teks_penuh="", new_chat=baru,
            open_chat_id="" if baru else self._web_chat_id,
            on_status=on_status, on_notice=on_notice)
        dibuat = getattr(conn, "last_chat_id", "") or ""
        if dibuat:
            self._link_web_chat(dibuat)
        self._web_ctx_sent = True
        self._persist()
        # Isi berkasnya disebut apa adanya — jumlah giliran, besar, dan nama
        # berkasnya. Kalau yang terkirim ternyata ingatan kosong, pengguna
        # melihatnya SEKARANG, bukan setelah AI menjawab seolah tak tahu
        # apa-apa. Persis keluhan yang memunculkan perbaikan ini.
        rincian = (f"{konteks.jumlah_giliran(berkas)} giliran percakapan, "
                   f"±{konteks.ukuran(berkas) // 1024} KB\n"
                   + "\n".join(f"  - {p.parent.name}/{p.name}" for p in berkas))
        if jawab is None:
            return (f"Ingatan GAGAL dikirim ke {self.model_spec.label}:\n"
                    f"{rincian}\n\nCoba lagi, atau lanjutkan biasa — konteks "
                    "proyek tetap dikirim otomatis di giliran berikutnya.")
        return (f"Ingatan sudah dibaca {self.model_spec.label}:\n{rincian}\n\n"
                f"> {(jawab or '').strip()[:300]}")

    # Pesan yang MENGGANTIKAN tembok konteks di badan pesan: menunjuk berkasnya,
    # lalu meminta bukti bahwa berkas itu sungguh terbaca. Kata-katanya dijaga
    # agar tak mencocoki detektor bagas-ai sendiri (pesan ini ikut tampil di
    # halaman, dan pemindai "percakapan kepanjangan" membaca seluruh halaman —
    # lihat context_full_patterns di connectors/kimi.py).
    _RUJUK_BERKAS = (
        "== KONTEKS ADA DI BERKAS TERLAMPIR ==\n"
        "{isi} kulampirkan sebagai {jumlah} — bukan kutempel di sini, sebab "
        "isinya ratusan ribu karakter dan pesan sepanjang itu dipotong "
        "situs:\n{daftar}\n"
        "BUKA dan baca {semua} lebih dulu, DARI BARIS PERTAMA SAMPAI BARIS "
        "TERAKHIR tiap berkas. Jangan berhenti di berkas pertama, jangan "
        "melompati bagian tengah, dan jangan menyimpulkan dari beberapa "
        "kilobyte pertama saja — bagian yang paling menentukan (pekerjaan "
        "paling akhir) justru ada di ujung.\n"
        "Sesudah itu balas SATU BARIS:\n"
        "    SIAP {kode}\n"
        "Kode itu nilai `kode_periksa` tiap berkas, urut. Letaknya di BARIS "
        "TERAKHIR tiap berkas — jadi kamu cuma bisa menyebutkannya kalau "
        "memang sudah membaca sampai ke sana, dan itulah gunanya. Kalau ada "
        "berkas yang tak bisa kamu buka atau tak sampai ke ujungnya, sebutkan "
        "yang mana; isinya akan kukirim dengan cara lain.\n"
        "Jangan mengerjakan apa pun dulu dan jangan mengeluarkan blok [[TOOL]] "
        "sekarang — permintaanku menyusul di pesan berikutnya."
    )

    def _pengantar_memory(self, nama: list, kode: list[str],
                          lanjutan: bool = False) -> str:
        """Badan pesan untuk chat yang menerima berkas memory: aturan main +
        rujukan ke berkasnya. Aturan tetap DIKETIK, datanya yang dilampirkan.

        `lanjutan` membedakan pembuka sesi dari sambungan pekerjaan. Bedanya
        bukan basa-basi: menyebut "percakapan kita sebelum ini" pada chat yang
        isinya memang belum ada membuat model mencari-cari sesuatu yang tak
        pernah terjadi, dan itu berakhir jadi pertanyaan balik."""
        isi = ("Keadaan mesin & proyekku, ditambah percakapan kita sebelum ini "
               "(lengkap dengan kode & hasil tiap langkah),"
               if lanjutan else "Keadaan mesin & proyekku")
        banyak = len(nama) > 1
        return (
            _KEPALA_KONTEKS + _web_tool_protocol() + "\n\n"
            + self._RUJUK_BERKAS.format(
                isi=isi,
                jumlah=(f"{len(nama)} berkas (satu berkas besar tak terbaca "
                        "utuh oleh situs ini, jadi kupecah)"
                        if banyak else "berkas"),
                daftar="\n".join(f"  - {n}" for n in nama),
                semua="SEMUANYA, urut," if banyak else "berkas itu",
                kode=" ".join(kode))
        )

    def _kirim_konteks(self, kirim: Any, conn: Any, *, berkas: list,
                       kode: list[str],
                       pengantar: str, teks_penuh: str,
                       new_chat: bool = False, open_chat_id: str | None = None,
                       on_status: Any = None, on_notice: Any = None) -> Any:
        """Kirim konteks sebagai BERKAS terlampir; jatuh ke teks bila gagal.

        Kegagalan yang ditangani di sini bukan teoretis. Situs bisa menolak
        jenis berkasnya, unggahannya bisa tak pernah selesai, dan yang paling
        sunyi: berkasnya diterima tapi isinya tak pernah diurai. Yang terakhir
        tak melempar apa pun, jadi satu-satunya cara mengetahuinya adalah
        meminta model MENGUTIP kode periksa dari dalam berkas. Tanpa bukti itu,
        bagas-ai bisa bekerja sepanjang sesi dengan model yang belum pernah
        melihat peta proyek sekalipun.

        Return teks balasan bila berhasil; None bila lampirannya gagal dan
        tak ada teks cadangan (`teks_penuh` kosong).
        """
        if on_status:
            on_status(f"mengunggah {len(berkas)} berkas konteks…")
        with self._tanpa_catatan():
            sebelum = getattr(conn, "last_chat_id", "") or ""
            try:
                jawab = kirim(pengantar, new_chat, open_chat_id,
                              [str(p) for p in berkas])
            except llm.Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - situsnya menolak berkas
                log.debug("lampiran konteks gagal", exc_info=True)
                if on_notice:
                    on_notice(f"berkas konteks tak bisa diunggah ({exc}) — "
                              "konteksnya dikirim sebagai teks")
                # Chat-nya bisa TERLANJUR dibuat sebelum unggahan gagal; teks
                # cadangannya harus mendarat di sana, bukan di chat ketiga.
                sesudah = getattr(conn, "last_chat_id", "") or ""
                if sesudah and sesudah != sebelum:
                    new_chat, open_chat_id = False, sesudah
            else:
                self._web_chars += konteks.ukuran(berkas)
                # SEMUA bagian harus terbukti terbaca, bukan cuma yang pertama.
                # Model yang berhenti di bagian 1 akan kehilangan justru
                # pekerjaan terakhir — bagian yang paling menentukan.
                rendah = (jawab or "").lower()
                if kode and all(k.lower() in rendah for k in kode):
                    return jawab
                if on_notice:
                    kurang = [k for k in kode if k.lower() not in rendah]
                    on_notice(
                        f"situs tak membaca {len(kurang)} dari {len(kode)} "
                        "berkas konteksnya — konteks dikirim ulang sebagai teks")
                # Chat sudah ada sekarang: cadangannya masuk ke chat yang SAMA.
                new_chat = False
                open_chat_id = getattr(conn, "last_chat_id", "") or open_chat_id
            if not teks_penuh:
                return None
            return kirim(teks_penuh, new_chat, open_chat_id, None)

    def _padatkan_web(self, conn: Any, kirim: Any,
                      on_status: Any = None, on_notice: Any = None,
                      alasan: str = "") -> str:
        """Situs MENOLAK melanjutkan percakapan -> pindah ke chat baru.

        Ini jalan terakhir, bukan /compact. Dipakai hanya saat situsnya sendiri
        sudah tak mau menerima pesan lagi ("conversation too long"): tak ada
        pilihan selain chat baru, sebab yang lama menolak apa pun yang dikirim.

        Riwayatnya diambil dari simpanan bagas-ai sendiri (simpan_memory) —
        chat lamanya tak dimintai ringkasan lebih dulu. Meminta ringkasan ke
        chat yang justru sedang menolak menjawab adalah persis kasus yang
        paling sering pulang dengan tangan kosong."""
        if on_notice:
            on_notice((alasan or "situs menolak melanjutkan percakapan ini")
                      + " — membawa ingatannya ke percakapan baru")
        if on_status:
            on_status("menyimpan riwayat percakapan…")
        berkas = self.simpan_memory()

        if on_status:
            on_status("membuka percakapan baru & memasang konteksnya…")
        self._lupakan_chat_web()

        if berkas:
            self._kirim_konteks(
                kirim, conn, berkas=berkas, kode=konteks.kode(berkas),
                pengantar=self._pengantar_memory(
                    [p.name for p in berkas], konteks.kode(berkas),
                    lanjutan=True),
                teks_penuh=self._konteks_teks(), new_chat=True,
                open_chat_id="", on_status=on_status, on_notice=on_notice)
        else:
            with self._tanpa_catatan():
                kirim(self._konteks_teks(), True, "", None)

        dibuat = getattr(conn, "last_chat_id", "") or ""
        if dibuat:
            self._link_web_chat(dibuat)
        self._web_ctx_sent = True
        try:
            conn.konteks_penuh = False
        except Exception:  # noqa: BLE001
            pass
        if on_notice:
            on_notice("ingatannya sudah dipasang — lanjut di percakapan baru")
        return str(berkas or "")

    def _konteks_teks(self) -> str:
        """Konteks versi TEKS — cadangan bila berkasnya tak bisa dipakai.

        Bentuk lama yang diketik utuh ke komposer: mahal & rawan dipotong
        situs, tapi selalu bisa dikirim. Dipertahankan justru karena jalur
        lampiran punya cara gagal yang diam-diam."""
        bagian = [_web_tool_protocol()]
        try:
            ctx = prompts.build_web_context()
        except Exception:  # noqa: BLE001
            ctx = ""
        if ctx:
            bagian.append(ctx)
        try:
            lokal = prompts.build_transcript_digest(self.memory.messages)
        except Exception:  # noqa: BLE001
            lokal = ""
        if lokal:
            bagian.append("## Percakapan kami sebelum ini\n"
                          "Lanjutkan dari sini — jangan mengulang yang sudah "
                          "dibahas:\n" + lokal)
        bagian.append(
            "Balas dengan SATU kata: SIAP. Jangan mengeluarkan blok [[TOOL]] "
            "apa pun sekarang — tugasnya menyusul di pesan berikutnya."
        )
        return "\n\n".join(bagian)

    def refresh_system_prompt(self) -> None:
        """Bangun ulang system prompt (mis. setelah add-dir) & pasang ke memory."""
        self.memory.set_system(prompts.build_system_prompt())

    # --- sesi ---
    def reset(self) -> None:
        self.memory.reset()
        # Riwayat dikosongkan -> percakapan AI web lama tak lagi mewakili sesi
        # ini; giliran berikutnya memulai chat baru di situs.
        self.start_new_web_chat()
        self._persist()

    def _persist(self) -> None:
        if self.session:
            try:
                self.session.save(
                    self.memory.messages,
                    tokens={
                        "prompt": self.tokens_session.prompt,
                        "completion": self.tokens_session.completion,
                        "cost": round(self.tokens_session.cost, 6),
                    },
                )
            except OSError:
                pass

    # --- inti ---
    def run(
        self,
        user_input: Any,
        *,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        cancel_event: Any = None,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        # Aliran "pikiran" model, POTONGAN demi potongan — terpisah dari
        # on_token (aliran jawaban). HANYA jalur API yang memanggilnya:
        # situs AI web MEMBUANG blok berpikirnya saat serialisasi (lihat
        # catatan di connectors/base.py), jadi di jalur web tak ada apa pun
        # untuk diteruskan — memalsukannya berarti mengaku punya yang tak ada.
        on_reasoning: Callable[[str], None] | None = None,
        attachments: list[str] | None = None,
        # Diambil di tiap batas langkah; mengembalikan pesan pengguna
        # yang mengantre supaya bisa DISISIPKAN ke giliran berjalan.
        ambil_sisipan: Callable[[], list[str]] | None = None,
        on_tim: Callable[[list[str]], None] | None = None,
        # Dipanggil saat ingatan percakapan disimpan otomatis di akhir giliran:
        # on_padat(pecahan 0..1, keterangan). Terminal memakainya untuk bar
        # kemajuan; giliran ini MENUNGGU selama pemanggilnya belum kembali.
        on_padat: Callable[[float, str], None] | None = None,
    ) -> str:
        """Proses satu giliran. Kembalikan teks jawaban final.

        DUA jalur, dipilih oleh `model_spec.is_web` dan hanya di sini:
          - model `web/*`    — _run_connector: situs AI lewat Playwright
            (`on_status`/`on_token` untuk progres & teks), tool lewat protokol
            penanda [[TOOL]] karena situs chat tak punya function-calling.
          - model `nvidia/*` — _run_api: endpoint OpenAI-compatible, tool
            lewat function-calling ASLI, effort lewat extra_body.

        Callback-nya sama untuk keduanya supaya UI tak perlu tahu jalur mana
        yang dipakai; yang tak punya padanan di jalur API disebutkan di
        _run_api.

        `on_message(teks)` dipanggil untuk narasi antar-langkah (ketika agent
        menjelaskan apa yang akan dilakukan sebelum memakai tool).
        `on_tool_result(nama, hasil)` dipanggil SETELAH sebuah tool selesai —
        dipakai UI untuk menampilkan hasil (mis. output perintah) secara ringkas.
        `on_notice(teks)` dipanggil saat bagas-ai mengambil tindakan anti-macet
        otomatis (mis. memaksa menyimpulkan sesudah tool gagal beruntun).
        `on_retry(percobaan, tunggu, exc)` dipanggil saat situs sedang PENUH
        dan bagas-ai menunggu lalu mengirim ulang — UI memakainya untuk
        hitung-mundur sisa waktu di footer.
        Bila `cancel_event` diset di tengah jalan, melempar llm.Cancelled.
        """
        # Penanda [GAMBAR] <path> di TEKS pengguna (mis. drag-drop foto dari
        # CLI yang menukar [foto] dengan penanda ini) dipisah di sini jadi
        # LAMPIRAN sungguhan — satu gerbang untuk semua antarmuka. Dulu
        # penanda itu hanya dibaca dari hasil tool (_take_image_marks di
        # _web_tool_protocol), sehingga foto yang di-drop pengguna tak pernah
        # sampai ke model: yang terkirim cuma baris teks "[GAMBAR] C:\..."
        if isinstance(user_input, str) and "[GAMBAR]" in user_input:
            user_input, gambar_user = _take_image_marks(user_input)
            if gambar_user:
                attachments = list(attachments or []) + gambar_user
        with self._run_lock:
            # Kelompok checkpoint baru per giliran: undo_changes memulihkan tepat
            # satu giliran, bukan campuran beberapa giliran.
            from .tools import checkpoint as _checkpoint
            _checkpoint.begin_turn()
            # Rencana giliran LAMA dibuang di sini. Rencana hanya bermakna selama
            # tugasnya berlangsung; membiarkannya hidup ke giliran berikutnya bikin
            # model melanjutkan daftar langkah milik permintaan yang sudah lewat.
            from .tools import plan_tool as _plan
            _plan.reset()
            # Anggaran naik-kelas berlaku PER GILIRAN: kalau tidak, effort yang
            # sempat dinaikkan di giliran lalu membuat giliran berikutnya mulai
            # dengan kuota yang sudah habis.
            self._escalations = 0
            if not self.model_spec.is_web:
                return self._run_api(
                    user_input, cancel_event=cancel_event,
                    on_status=on_status, on_token=on_token,
                    on_reasoning=on_reasoning,
                    on_tool=on_tool, on_message=on_message,
                    on_tool_result=on_tool_result, on_notice=on_notice,
                    on_retry=on_retry, attachments=attachments,
                    ambil_sisipan=ambil_sisipan,
                )
            return self._run_connector(
                user_input, cancel_event=cancel_event,
                on_status=on_status, on_token=on_token,
                on_tool=on_tool, on_message=on_message,
                on_tool_result=on_tool_result, on_notice=on_notice,
                on_retry=on_retry, attachments=attachments,
                ambil_sisipan=ambil_sisipan, on_tim=on_tim, on_padat=on_padat,
            )

    # --- jalur API NVIDIA (function-calling asli) --------------------------
    def _run_api(
        self,
        user_input: Any,
        *,
        cancel_event: Any = None,
        on_status: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        attachments: list[str] | None = None,
        ambil_sisipan: Callable[[], list[str]] | None = None,
    ) -> str:
        """Jalankan giliran lewat endpoint API NVIDIA sebagai AGENT penuh.

        Bedanya dengan _run_connector, dan kenapa keduanya tak bisa disatukan:
        di sini KITA yang memegang konteks (seluruh riwayat dikirim ulang tiap
        request), tool dipanggil lewat `tool_calls` ASLI dari endpoint, dan
        setelan berpikir dikirim sebagai `extra_body`. Di jalur web ketiganya
        kebalikannya: situs memegang konteks, tool lewat penanda teks, dan mode
        berpikir diatur dengan MENGKLIK tombol di halamannya.

        Yang tak punya padanan di sini, sengaja tidak dipalsukan:
          - `on_padat` : simpanan otomatis di jalur web dipicu jumlah karakter
            percakapan DI SITUS. Di sini pemangkasan riwayat sudah ditangani
            Memory, jadi tak ada bar kemajuan yang perlu digerakkan.
          - `on_tim`   : tinjauan rekan tim berjalan di jalur web; di sini satu
            giliran adalah satu rantai tool, tak ada langkah untuk ditinjau.
        """
        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0

        if attachments:
            spec = self.model_spec
            if spec.multimodal:
                # Model API ini penerima media (mis. ox-alpha via OpenRouter):
                # gambar & video dikirim sebagai bagian pesan (image_url /
                # video_url base64) lewat penanda di memori — konversinya
                # terjadi per-request di _pesan_dengan_media. Berkas yang
                # BUKAN media tetap dialihkan ke tool baca-berkas.
                media = [p for p in attachments
                         if _mime_gambar(p) or _mime_vidio(p)]
                lain = [p for p in attachments if p not in media]
                if media:
                    self.memory.messages[-1]["content"] += "".join(
                        f"\n{_MEDIA_LAMPIR}{p}" for p in media)
                    if on_notice:
                        on_notice(f"{len(media)} media dilampirkan "
                                  f"(gambar/video) ke {spec.label}")
                if lain:
                    daftar = "\n".join(f"- {a}" for a in lain)
                    self.memory.add({
                        "role": "user",
                        "content": (
                            "[SISTEM] Pengguna melampirkan berkas di bawah. "
                            "Berkasnya bukan gambar/video, jadi tidak ikut "
                            "sebagai lampiran pesan. Buka sendiri dengan tool "
                            "(mis. baca berkas / daftar isi folder) bila "
                            "perlu:\n" + daftar
                        ),
                    })
            else:
                # Model TEKS: tak ada komposer punya vision. Lampirannya TIDAK
                # dibuang diam-diam: jalurnya dialihkan ke tool baca-berkas
                # dan pengguna diberi tahu. Membuangnya tanpa kabar membuat
                # pengguna menunggu jawaban tentang berkas yang model ini tak
                # pernah lihat.
                daftar = "\n".join(f"- {a}" for a in attachments)
                self.memory.add({
                    "role": "user",
                    "content": (
                        "[SISTEM] Pengguna melampirkan berkas di bawah. Model "
                        "ini tak menerima gambar lewat API, jadi berkasnya "
                        "TIDAK terkirim sebagai gambar. Buka sendiri dengan "
                        "tool (mis. baca berkas / daftar isi folder) bila "
                        "perlu:\n" + daftar
                    ),
                })
                if on_notice:
                    on_notice(
                        f"{len(attachments)} lampiran tak dikirim sebagai "
                        f"gambar ({spec.label} model teks) — pathnya "
                        "diteruskan supaya bisa dibaca lewat tool")

        try:
            return self._api_loop(
                cancel_event=cancel_event, on_status=on_status,
                on_token=on_token, on_reasoning=on_reasoning,
                on_tool=on_tool, on_message=on_message,
                on_tool_result=on_tool_result, on_notice=on_notice,
                on_retry=on_retry, ambil_sisipan=ambil_sisipan,
            )
        except BaseException:
            # BaseException, bukan Exception: KeyboardInterrupt & llm.Cancelled
            # justru kasus yang paling butuh pembersihan ini. Riwayat yang
            # berhenti di tengah rantai tool memuat `tool_calls` tanpa balasan
            # `role:"tool"` pasangannya — bentuk yang DITOLAK endpoint,
            # jadi tanpa perbaikan ini giliran BERIKUTNYA gagal sebelum mulai.
            self.memory.repair_dangling_tools()
            self._persist()
            raise

    def _api_loop(
        self,
        *,
        cancel_event: Any,
        on_status: Callable[[str], None] | None,
        on_token: Callable[[str], None] | None,
        on_reasoning: Callable[[str], None] | None,
        on_tool: Callable[[str, dict[str, Any]], None] | None,
        on_message: Callable[[str], None] | None,
        on_tool_result: Callable[[str, str], None] | None,
        on_notice: Callable[[str], None] | None,
        on_retry: Callable[[int, float, Exception], None] | None,
        ambil_sisipan: Callable[[], list[str]] | None,
    ) -> str:
        """Putaran tool: minta jawaban — eksekusi tool — kirim
        hasilnya, sampai model menjawab tanpa memanggil tool lagi.

        Jaring pengaman anti-loop-liar (semuanya per-giliran):
          - `seen_tools`  : cache hasil per (nama+argumen). Panggilan PERSIS
                            SAMA tak dieksekusi ulang; hasilnya dikembalikan
                            beserta teguran.
          - `dup_hits`    : berapa kali pengulangan terjadi. Melewati batas
                            — tool DIMATIKAN & model dipaksa menyimpulkan.
          - `total_calls` : anggaran total panggilan tool (config.MAX_TOOL_CALLS).
          - `weak_hits`   : model menuliskan tool call sebagai TEKS/XML (llm.py
                            menyelamatkannya dengan id `txt_`) = lemah di
                            function-calling.
          - `empty_hits`  : balasan kosong berulang.
        """
        spec = self.model_spec
        schemas = tools.get_schemas(self.tool_names)
        # Schema tool ikut SETIAP request saat aktif (bisa ribuan token) —
        # masuk perkiraan prompt, bukan diabaikan seperti dulu.
        schemas_est = (len(json.dumps(schemas)) // 4) if schemas else 0
        guard = 0
        safety = max(self.max_iterations, 60)
        # Pemulih konteks penuh: berapa kali riwayat dipangkas giliran ini.
        # Dua tingkat (14 -> 6 entri) — sesudah itu memang tak ada lagi yang
        # aman dibuang dan pengguna diarahkan ke /compact.
        pangkas_ke = 0
        seen_tools: dict[str, str] = {}
        dup_hits = 0
        total_calls = 0
        force_final = False
        weak_hits = 0
        empty_hits = 0
        stall_rounds = 0
        # Cache media MILIK GILIRAN INI: data-URL dihitung sekali, dipakai
        # ulang tiap putaran rantai tool (tanpa ini, video 30 MB dibaca &
        # dienkode ulang berkali-kali — biaya & waktu sia-sia). `media_lewat`
        # mengumpulkan media yang dilewati; diumumkan SEKALI setelah putaran
        # pertama memintanya.
        media_cache: dict[str, str | None] = {}
        media_lewat: list[str] = []
        media_sudah_dikabari = False
        # Pemulih "Provider returned error": setelah penyedia konsisten
        # menolak permintaan ber-media, seluruh giliran ini lanjut TANPA
        # media (penanda dianggap absen oleh _pesan_dengan_media).
        media_diblokir = False
        # Permintaan pengguna giliran ini — pegang TEKS-nya sejak awal:
        # bila KonteksPenuh memaksa pemangkasan dan ia tergesang keluar,
        # _pastikan_tugas_aktif menyisipkannya kembali.
        tugas_aktif = ""
        for m in reversed(self.memory.messages):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                tugas_aktif = m["content"]
                break

        while True:
            guard += 1
            if guard > safety:
                break
            if cancel_event is not None and cancel_event.is_set():
                raise llm.Cancelled()

            # Dihitung ULANG tiap putaran: effort bisa BERUBAH di tengah giliran
            # akibat _escalate.
            extra = spec.extra_body_for(self.effort)
            prompt_est = _est_messages(self.memory.messages,
                                       extra_chars=schemas_est)
            # "fase": None -> belum ada apa pun, "pikir" -> pikiran mengalir,
            # "jawab" -> jawaban mengalir. Dipakai supaya status hanya
            # berubah saat FASENYA berpindah, bukan tiap potongan token.
            state = {"completion_est": 0, "reasoning_est": 0, "fase": None}
            self.tokens_live = self.tokens_last.total + prompt_est
            if on_status:
                # NETRAL: pada titik ini permintaan baru dikirim dan belum satu
                # potong pun kembali, jadi kita belum tahu model akan bernalar
                # dulu atau langsung menjawab. on_reasoning/on_content di bawah
                # yang mempertegasnya begitu fasenya jelas.
                on_status(f"{spec.label} sedang memproses")

            def on_content(piece: str) -> None:
                # Jawaban SUNGGUHAN. Begitu ia mulai mengalir, status kembali
                # dari "berpikir" ke "menjawab" supaya keduanya tak tertukar.
                if state.get("fase") != "jawab":
                    state["fase"] = "jawab"
                    if on_status:
                        on_status(f"{spec.label} sedang menjawab")
                state["completion_est"] += _est_tokens(piece)
                self.tokens_live = (
                    self.tokens_last.total + prompt_est + state["completion_est"]
                )
                if on_token:
                    on_token(piece)

            def _on_reasoning(piece: str) -> None:
                """Aliran "pikiran" model — DIPISAH dari aliran jawaban.

                WAJIB diberikan, walau isinya cuma mengganti status. Bila
                on_reasoning dibiarkan kosong, llm.py meneruskan pikiran itu ke
                on_content sebagai jaring penyelamat (supaya tak hilang saat
                `content` kosong) — dan pada model yang mode berpikirnya
                TAK BISA dimatikan (muse-glimmer: TERUKUR selalu bernalar),
                seluruh rantai pikiran akan tercetak sebagai kalimat jawaban.
                Isinya tetap tersimpan di llm.py, jadi tak ada yang hilang.
                Potongannya JUGA diteruskan ke pemanggil (on_reasoning),
                bukan cuma dihitung. Tanpa itu, model yang butuh 80-169
                detik sebelum kata pertama (TERUKUR: deepseek-v4-flash)
                tak bisa dibedakan dari model yang mati — padahal justru
                selama menit-menit itu pikirannya sudah mengalir."""
                state["reasoning_est"] += _est_tokens(piece)
                if state.get("fase") is None:
                    state["fase"] = "pikir"
                    if on_status:
                        on_status(f"{spec.label} sedang berpikir")
                if on_reasoning:
                    on_reasoning(piece)

            def _on_retry(attempt: int, wait: float, exc: Exception) -> None:
                # Panggilan diulang DARI AWAL, jadi estimasi token parsial
                # direset — tanpa ini tiap percobaan menambah angka yang
                # sama lagi dan penghitung di layar membengkak tanpa alasan.
                state["completion_est"] = 0
                state["reasoning_est"] = 0
                state["fase"] = None
                self.tokens_live = self.tokens_last.total + prompt_est
                if on_retry:
                    on_retry(attempt, wait, exc)

            # Saat stagnasi terdeteksi, tool DIMATIKAN — model TERPAKSA
            # menjawab dengan teks (menyimpulkan), tak bisa mengulang tool lagi.
            active_tools = None if force_final else (schemas or None)
            try:
                content, tool_calls, usage = llm.stream_completion(
                    # Penanda [LAMPIR-MEDIA] di riwayat dikonversi jadi bagian
                    # multimodal (image_url/video_url base64) DI SINI, tiap
                    # putaran: endpoint API tak punya memori, jadi medianya
                    # wajib ikut di SETIAP request selama rantai tool berjalan.
                    _pesan_dengan_media(self.memory.messages,
                                        cache=media_cache, lewat=media_lewat),
                    tools=active_tools,
                    # api_model, BUKAN spec.id: `id` adalah identitas internal
                    # bagas-ai ("openrouter/ox-alpha") yang tak dikenal server.
                    model=spec.api_model,
                    extra_body=extra,
                    max_tokens=spec.max_tokens,
                    # Penyedia endpoint menentukan klien (base_url + API key):
                    # nvidia -> NVIDIA_API_KEY, openrouter -> OPENROUTER_API_KEY,
                    # opencode -> tanpa key pun jalan (anonim; key opsional).
                    # api_style memilih protokol endpoint: "responses" utk model
                    # Zen yang hanya dilayani di /responses.
                    provider=spec.provider,
                    api_style=getattr(spec, "api_style", "chat"),
                    on_content=on_content,
                    on_reasoning=_on_reasoning,
                    cancel_event=cancel_event,
                    on_retry=_on_retry,
                )
            except llm.Cancelled:
                raise
            except llm.StreamStalled:
                # URUTAN NYAWA (pelajaran 2026-08-26): handler SPESIFIK wajib
                # di ATAS except Exception — dulu ia di bawahnya sehingga
                # selalu tertelan duluan dan pemulih macet tak pernah jalan.
                # Stream berhenti mengirim data berulang kali. Naikkan effort
                # lalu ULANGI: memory belum disentuh di putaran ini, jadi
                # konteksnya masih utuh dan tak ada pekerjaan yang hilang.
                stall_rounds += 1
                if stall_rounds > 3:
                    final = (
                        f"Maaf, model terus macet (berhenti mengirim respons) "
                        "meski sudah kubatalkan & kuulang otomatis beberapa "
                        f"kali. Kemungkinan server {spec.provider} sedang "
                        "bermasalah — coba lagi sebentar lagi, atau ganti "
                        "model dengan /model (model (web) tak lewat server "
                        "ini)."
                    )
                    self.memory.add_assistant_text(final)
                    self._persist()
                    return final
                changed = self._escalate(
                    "respons macet — stream diam terlalu lama")
                if on_notice:
                    on_notice(changed or
                              "respons macet — dibatalkan & diulang otomatis")
                continue
            except Exception as exc:
                # SATU pintu pemulih utk semua kegagalan "permintaan ditolak":
                # KonteksPenuh (terdeteksi eksplisit) ATAU 400/413 generik dari
                # provider yang menyamar sebagai "Provider returned error".
                #
                # URUTAN ADALAH NYAWA: dulu `except Exception` berdiri DI ATAS
                # `except llm.KonteksPenuh` — padahal KonteksPenuh subclass-
                # nya, jadi selalu tertangkap di sini duluan lalu `raise`
                # ulang keluar; handler pangkasnya TAK PERNAH jalan dan
                # pengguna menabrak ✖ terus pada sesi panjang.
                if isinstance(exc, llm.KonteksPenuh):
                    penyebab = "konteks model penuh"
                elif _layak_pulih(exc):
                    penyebab = "provider menolak permintaan"
                else:
                    raise          # bukan keluarga ini: biarkan naik seperti biasa

                pangkas_ke += 1
                self._pernah_pangkas = True
                # Anak tangga 0: media base64 sering penyebab utamanya —
                # lepas dulu (TANPA menghitung pemangkasan riwayat).
                if any(v for v in media_cache.values()) and not media_diblokir:
                    media_diblokir = True
                    if on_notice:
                        on_notice(f"{penyebab} — dicoba ulang TANPA lampiran "
                                  "gambar/video")
                    continue
                sisa = self._pulih_konteks(pangkas_ke, tugas_aktif)
                if sisa <= 2 or pangkas_ke > 2:
                    final = (
                        f"{penyebab.capitalize()} dan tetap gagal sesudah "
                        "media & riwayat lama dilepas. Jalankan `/compact` "
                        "untuk menyimpan kerja, lalu `/new` + `/send-compact` "
                        "untuk melanjutkan di percakapan bersih.")
                    self.memory.add_assistant_text(final)
                    self._persist()
                    return final
                if on_notice:
                    on_notice(
                        f"{penyebab} ({_potong_alasan(
                            getattr(exc, 'asli', '') or str(exc))}) — riwayat "
                        f"dipangkas (sisa {sisa} entri), permintaanmu "
                        "diulangi otomatis")
                continue

            # Media yang DILEWATI diumumkan SEKALI (putaran pertama yang
            # memintanya) — tanpa ini pengguna mengira fotonya terkirim.
            if media_lewat and not media_sudah_dikabari:
                media_sudah_dikabari = True
                if on_notice:
                    ringkas = "; ".join(media_lewat[:3]) + (
                        "…" if len(media_lewat) > 3 else "")
                    on_notice(f"media dilewati: {ringkas}")

            # Konfirmasi token: pakai usage ASLI bila ada, estimasi bila tidak.
            if usage:
                self.tokens_last.add(usage)
                self.tokens_session.add(usage)
            else:
                # Pikiran IKUT dihitung: ia token keluaran yang benar-benar
                # ditagih, dan pada model yang selalu bernalar ia bisa jauh
                # lebih banyak daripada jawabannya sendiri. Mengabaikannya
                # membuat penghitung di layar jauh lebih kecil dari kenyataan.
                keluar = state["completion_est"] + state["reasoning_est"]
                self.tokens_last.add_raw(prompt_est, keluar)
                self.tokens_session.add_raw(prompt_est, keluar)
            self.tokens_live = self.tokens_last.total

            # --- deteksi "performa menurun" ---
            if tool_calls and any(
                str(tc.get("id", "")).startswith("txt_") for tc in tool_calls
            ):
                weak_hits += 1
            if not tool_calls and not (content and content.strip()):
                empty_hits += 1

            if not tool_calls:
                # Balasan kosong berulang — coba naikkan effort dulu
                # sebelum menyerah.
                if empty_hits >= 2 and not force_final:
                    changed = self._escalate("respons kosong berulang")
                    if changed:
                        empty_hits = 0
                        if on_notice:
                            on_notice(changed)
                        self.memory.add({
                            "role": "user",
                            "content": ("[SISTEM] Balasanmu kosong. Lanjutkan "
                                        "tugas dari konteks di atas dan berikan "
                                        "jawaban."),
                        })
                        continue
                # Jangan pernah "berhenti diam": bila model tak menghasilkan
                # teks apa pun, beri pesan cadangan yang jelas alih-alih layar
                # kosong yang tak bisa dibedakan dari aplikasi menggantung.
                final = content if (content and content.strip()) else (
                    "Hmm, aku berhenti tanpa sempat menyusun jawaban. Coba "
                    "ulangi atau perjelas permintaanmu"
                    + (". Bisa juga turunkan /effort supaya anggaran "
                       "berpikirnya tak habis sebelum menjawab."
                       if spec.effort_levels else ".")
                )
                self.memory.add_assistant_text(final)
                self._persist()
                return final

            # Narasi sebelum aksi tool (mis. "Baik, saya akan membuat file X").
            if content and content.strip() and on_message:
                on_message(content)

            self.memory.add({
                "role": "assistant",
                "content": content or "",
                "tool_calls": [
                    {
                        "id": tc["id"] or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"] or "{}",
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            })
            for i, tc in enumerate(tool_calls):
                if cancel_event is not None and cancel_event.is_set():
                    raise llm.Cancelled()
                name = tc["name"]
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if on_tool:
                    on_tool(name, args)
                key = name + "::" + json.dumps(
                    args, sort_keys=True, ensure_ascii=False, default=str
                )
                if key in seen_tools:
                    dup_hits += 1
                    result = (
                        "[SISTEM] Kamu SUDAH memanggil tool ini dengan argumen "
                        "yang sama persis; hasilnya identik dengan di bawah. "
                        "JANGAN mengulanginya — gunakan hasil ini lalu "
                        "lanjut ke langkah berikutnya atau berikan jawaban "
                        "akhir.\n\n" + seen_tools[key]
                    )
                else:
                    result = tools.execute(name, args)
                    seen_tools[key] = result
                    # Hasil ditampilkan HANYA saat tool benar-benar dieksekusi,
                    # bukan saat dedup mengembalikan cache + teguran.
                    if on_tool_result:
                        on_tool_result(name, result)
                total_calls += 1
                self.memory.add({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{i}",
                    "content": result,
                })

            # SISIPKAN pesan susulan pengguna, tepat di batas langkah ini.
            # TIDAK saat force_final: di sana model justru sedang disuruh
            # berhenti memakai tool dan menyimpulkan, jadi menaruh tugas baru
            # berarti dua perintah yang bertabrakan. Pesannya tetap di antrean
            # dan dikerjakan sebagai giliran berikutnya — tak ada yang
            # hilang.
            if ambil_sisipan is not None and not force_final:
                try:
                    susulan = list(ambil_sisipan() or [])
                except Exception:  # noqa: BLE001 - antrean rusak != giliran gagal
                    susulan = []
                bersih = [s.strip() for s in susulan if (s or "").strip()]
                for s in bersih:
                    self.memory.add_user(s)
                if bersih and on_notice:
                    on_notice(
                        f"{len(bersih)} pesan susulanmu disisipkan ke giliran ini"
                        if len(bersih) > 1 else
                        "pesan susulanmu disisipkan ke giliran ini")

            # --- stagnasi / performa menurun ---
            # Naikkan effort dulu lalu LANJUTKAN dengan konteks yang sama;
            # memory tak direset, jadi progres tak hilang.
            looping = dup_hits >= config.MAX_DUPLICATE_TOOL_CALLS
            weak = weak_hits >= 2
            if not force_final and (looping or weak):
                reason = ("terdeteksi mengulang langkah" if looping
                          else "lemah memanggil tool")
                changed = self._escalate(reason)
                if changed:
                    if on_notice:
                        on_notice(changed)
                    # Penghitung direset supaya setelan baru dapat kesempatan
                    # bersih, bukan langsung kena batas warisan sebelumnya.
                    dup_hits = 0
                    weak_hits = 0
                    seen_tools.clear()
                    self.memory.add({
                        "role": "user",
                        "content": (
                            f"[SISTEM] Kamu tampak {reason}. Aku sudah "
                            f"menaikkan kemampuan berpikirmu ({changed}). "
                            "Konteks & progres di atas TETAP berlaku — "
                            "JANGAN ulangi dari nol. Lihat apa yang SUDAH "
                            "selesai, lalu lanjutkan langkah BERIKUTNYA sampai "
                            "tuntas."
                        ),
                    })
                    continue
            # Sudah tak bisa dinaikkan lagi (atau anggaran tool habis) —
            # minta menyimpulkan dengan jujur.
            if not force_final and (
                looping or weak or total_calls >= config.MAX_TOOL_CALLS
            ):
                force_final = True
                self.memory.add({
                    "role": "user",
                    "content": (
                        "[SISTEM] Kamu tampak mengulang langkah / terlalu "
                        "banyak memakai tool. STOP memakai tool dan berikan "
                        "jawaban akhir dalam teks biasa. JUJUR: jelaskan apa "
                        "yang SUDAH selesai dan apa yang BELUM. JANGAN mengaku "
                        "tuntas kalau memang belum — sebutkan langkah "
                        "tersisa yang perlu dilakukan."
                    ),
                })

        fallback = (
            "Maaf, proses berhenti karena mencapai batas iterasi tool. "
            "Coba persempit permintaanmu."
        )
        self.memory.add_assistant_text(fallback)
        self._persist()
        return fallback

    # --- pemulihan saat situsnya bermasalah -------------------------------
    def _pulihkan_chat_rusak(self, exc, user_text, on_status, on_notice) -> str:
        """Chat di situs rusak -> buka chat BARU lalu kirim ulang sekali.

        Tak menanyakan apa pun ke pengguna: kuotanya baik-baik saja dan
        satu-satunya jalan keluar memang ini, jadi bertanya cuma menunda hal
        yang jawabannya sudah pasti."""
        from . import connectors
        if on_notice:
            on_notice(f"chat di {self.model_spec.label} rusak — "
                      "membuka chat baru lalu mengirim ulang")
        try:
            connectors.get_connector(self.model_spec.connector)
            # Kaitan ke chat yang rusak DILEPAS, dan konteks pembuka ditandai
            # perlu dikirim ulang — keduanya sekaligus. Dulu di sini ada
            # `conn.new_chat()`, method yang tak pernah ada di connector mana
            # pun: AttributeError-nya tertelan `except` di bawah, jadi
            # pemulihan ini SELALU berakhir "tak bisa dipulihkan" tanpa pernah
            # benar-benar mencoba.
            self._lupakan_chat_web()
            jawab = self._run_connector(
                user_text, on_status=on_status, on_notice=on_notice)
        except Exception as exc2:  # noqa: BLE001
            return (
                f"⚠ **Chat di {self.model_spec.label} rusak dan tak bisa "
                f"dipulihkan.**\n\n> {exc}\n\n"
                f"Percobaan chat baru juga gagal: {exc2}\n\n"
                "Coba `/new` untuk memulai percakapan bersih, atau `/model` "
                "untuk pindah layanan."
            )
        return jawab

    def _tangani_limit(self, exc, user_text, on_status, on_notice) -> str:
        """Kuota habis -> TANYAKAN mau menunggu atau ganti model.

        Dua jalan keluarnya berbeda jauh akibatnya: menunggu menjaga seluruh
        konteks percakapan tetap utuh tapi bisa lama; ganti model bisa lanjut
        sekarang tapi mulai dari chat kosong di situs lain. Hanya pengguna yang
        tahu mana yang cocok, jadi ia yang memilih — bukan ditebak."""
        from . import interaction, models

        # catalog_aktif: model yang sedang DITUNDA tak boleh ditawarkan sebagai
        # jalan keluar — set_model akan menolaknya, dan pengguna sudah telanjur
        # memilih "pindah" di tengah pekerjaan yang terhenti.
        lain = [k for _, k, s in models.catalog_aktif()
                if s.is_web and k != self.model_id]
        TUNGGU = "⏳ Tunggu di sini sampai kuotanya pulih, lalu lanjut otomatis"
        GANTI = "🔀 Ganti model sekarang (konteks chat mulai dari kosong)"
        BERHENTI = "✋ Hentikan giliran ini dulu"
        opsi = [TUNGGU, BERHENTI] if not lain else [TUNGGU, GANTI, BERHENTI]
        try:
            pilih = interaction.ask_choice(
                f"{self.model_spec.label} kena batas pemakaian:\n{exc}\n\n"
                "Mau diapakan?", opsi, False)
        except Exception:  # noqa: BLE001 - antarmuka tak interaktif
            pilih = ""

        if pilih.startswith("⏳"):
            return self._tunggu_limit(exc, user_text, on_status, on_notice)
        if pilih.startswith("🔀") and lain:
            baru = lain[0] if len(lain) == 1 else interaction.ask_choice(
                "Pindah ke model mana?",
                [models.spec_for_id(k).label for k in lain], False)
            tujuan = next(
                (k for k in lain if models.spec_for_id(k).label == baru),
                lain[0])
            lama = self.model_spec.label
            self.set_model(tujuan)
            if on_notice:
                on_notice(f"pindah dari {lama} ke {self.model_spec.label}")
            self._web_ctx_sent = False   # situs baru: konteks harus dikirim lagi
            try:
                return self._run_connector(
                    user_text, on_status=on_status, on_notice=on_notice)
            except Exception as exc2:  # noqa: BLE001
                return (f"⚠ Sudah pindah ke {self.model_spec.label}, tapi "
                        f"gilirannya tetap gagal: {exc2}")
        return (
            f"⛔ **{self.model_spec.label} sedang kena batas pemakaian.**\n\n"
            f"> {exc}\n\n"
            "Kirim ulang nanti, atau ketik `/model` untuk pindah layanan."
        )

    # Jeda antar-percobaan saat menunggu kuota pulih. Menaik, lalu mentok di
    # 5 menit: batas pemakaian lazimnya dihitung per jam/hari, jadi memeriksa
    # tiap beberapa detik cuma membebani situs tanpa mempercepat apa pun.
    _JEDA_LIMIT = (60, 120, 300, 300, 300, 300, 300, 300, 300, 300, 300, 300)

    def _tunggu_limit(self, exc, user_text, on_status, on_notice) -> str:
        """Tunggu kuota pulih sambil mencoba ulang berkala."""
        from . import connectors
        for i, jeda in enumerate(self._JEDA_LIMIT, 1):
            if on_status:
                on_status(f"kuota {self.model_spec.label} habis — mencoba lagi "
                          f"{jeda // 60 or 1} menit lagi (percobaan ke-{i})")
            time.sleep(jeda)
            try:
                return self._run_connector(
                    user_text, on_status=on_status, on_notice=on_notice)
            except connectors.WebLimitError:
                continue          # masih kena; tunggu jeda berikutnya
            except Exception as exc2:  # noqa: BLE001
                return (f"⚠ Kuotanya sudah pulih tapi gilirannya gagal: {exc2}")
        total = sum(self._JEDA_LIMIT) // 60
        return (
            f"⛔ **{self.model_spec.label} masih kena batas pemakaian** setelah "
            f"ditunggu ±{total} menit.\n\n> {exc}\n\n"
            "Batasnya mungkin harian. Ketik `/model` untuk pindah layanan, atau "
            "kirim ulang nanti."
        )

    def _run_connector(
        self,
        user_input: Any,
        *,
        cancel_event: Any = None,
        on_status: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        on_retry: Callable[[int, float, Exception], None] | None = None,
        attachments: list[str] | None = None,
        # Diambil di tiap batas langkah; mengembalikan pesan pengguna
        # yang mengantre supaya bisa DISISIPKAN ke giliran berjalan.
        ambil_sisipan: Callable[[], list[str]] | None = None,
        # Dipanggil dengan daftar nama rekan tim yang ikut meninjau satu
        # langkah — supaya terminal bisa menampilkan siapa yang bekerja.
        on_tim: Callable[[list[str]], None] | None = None,
        # Bar kemajuan saat ingatan disimpan otomatis (lihat _simpan_otomatis).
        on_padat: Callable[[float, str], None] | None = None,
    ) -> str:
        """Jalankan giliran lewat AI web (browser) sebagai AGENT penuh.

        AI web tak punya function-calling, jadi kita ajari ia memakai TOOLS lewat
        protokol teks (_web_tool_protocol): ia menuliskan blok [[TOOL]]{...}[[/TOOL]],
        bagas-ai MENGEKSEKUSI tool itu sungguhan di laptop (mesin tool yang sama
        di laptop), lalu mengirim balik hasilnya — berulang sampai AI web
        menjawab tanpa blok tool (jawaban akhir). Dengan begitu Kimi/Qwen web bisa
        mengedit file, menjalankan perintah, mencari web, dll.

        Web-AI menyimpan konteks percakapannya SENDIRI di sesi browser; memory
        bagas-ai tetap mencatat transkrip agar tampil di UI & tersimpan.
        """
        from . import connectors  # impor tunda: Playwright opsional

        self.memory.add_user(user_input)
        self.tokens_last = Usage()
        self.tokens_live = 0
        user_text = str(user_input)

        if not connectors.playwright_available():
            answer = (
                "Fitur connector web butuh Playwright + browser Chromium yang "
                "belum terpasang. Jalankan:\n\n"
                "    pip install playwright\n"
                "    playwright install chromium\n\n"
                "lalu coba lagi. Seluruh model bagas-ai berbasis browser, jadi "
                "langkah ini wajib sekali di awal."
            )
            self.memory.add_assistant_text(answer)
            self._persist()
            return answer

        # Pesan PERTAMA sesi web memuat: protokol tool + konteks laptop/proyek
        # (keduanya SEKALI saja — AI web mengingatnya sepanjang chat).
        include_ctx = not self._web_ctx_sent
        if include_ctx:
            preamble = _web_tool_protocol()
            try:
                ctx = prompts.build_web_context()
            except Exception:  # noqa: BLE001
                ctx = ""
            if ctx:
                preamble += "\n\n" + ctx
            # Riwayat percakapan sejauh ini (tanpa permintaan yang sedang
            # dikirim) — supaya pindah model di tengah kerja tidak kehilangan
            # konteks: chat di situs baru selalu mulai kosong.
            try:
                digest = prompts.build_transcript_digest(
                    self.memory.messages[:-1])
            except Exception:  # noqa: BLE001
                digest = ""
            if digest:
                preamble += (
                    "\n\n# Percakapan kami sebelum ini (dengan asisten lain)\n"
                    "Lanjutkan dari sini — jangan mengulang yang sudah dibahas:\n"
                    + digest
                )
            # KONTEKS DIKIRIM SEBAGAI PESAN TERSENDIRI, lalu permintaannya
            # menyusul di pesan berikutnya. Dulu keduanya digabung dalam satu
            # pesan raksasa, dan itu merugikan di tiga sisi sekaligus:
            #
            #   - AI membaca protokol, peta proyek, DAN tugasnya sekaligus, lalu
            #     langsung menjawab — protokolnya kerap cuma terbaca sekilas
            #     (gejala paling khas: balasan pertama menampilkan kode untuk
            #     disalin manual, bukan blok [[TOOL]]);
            #   - pesan gabungan itu yang PALING panjang di seluruh sesi,
            #     sehingga paling rawan dipotong situs ("Output stopped") —
            #     dan yang terpotong justru aturan mainnya;
            #   - saat terpotong, tak ada cara tahu bagian mana yang hilang.
            #
            # Dipisah: pesan pertama HANYA konteks dan diminta dijawab satu
            # kata, jadi AI menyelesaikan pemahamannya dulu; pesan kedua berisi
            # tugas yang sebenarnya.
            # KEPALA PESAN — ditaruh PALING DEPAN, bukan cuma di ekor.
            # TERUKUR: dengan instruksinya hanya di bagian bawah pesan ~41 rb
            # karakter, Gemini mengabaikannya dan langsung mengusulkan langkah
            # ("Sekarang aku baca pyproject.toml…") alih-alih membalas SIAP.
            # Yang dibaca paling awal punya bobot jauh lebih besar, jadi dua
            # hal yang paling sering menggagalkan giliran dinyatakan di sini
            # sebelum protokol panjangnya dimulai.
            konteks_msg = _KEPALA_KONTEKS + preamble + (
                "\n\n==========\n"
                "Ini BARU KONTEKS — belum ada yang perlu dikerjakan.\n"
                "JANGAN mengeluarkan blok [[TOOL]] apa pun sekarang, jangan "
                "menebak-nebak apa tugasku, dan jangan membuat rencana. Cukup "
                "balas SATU BARIS: `SIAP` (atau `SIAP — <satu hal yang ingin "
                "kupastikan>` bila memang ada yang janggal di konteks di atas).\n"
                "Permintaanku yang sebenarnya kukirim di pesan BERIKUTNYA."
            )
        # Pengingat DITEMPELKAN ke pesan tugas, TERMASUK yang pertama sesi.
        #
        # Dulu cabang "pesan pertama" mengirim permintaan POLOS tanpa pengingat,
        # dengan anggapan protokol yang baru saja dibaca masih segar. TERBUKTI
        # keliru justru di situ: Kimi membalas pesan pertama dengan memakai tool
        # BAWAANNYA ("Execute Python code") alih-alih blok [[TOOL]] — sandbox
        # yang tak punya satu pun berkas pengguna, jadi hasilnya fiksi.
        #
        # Masuk akal kalau dipikir ulang: pesan pertama adalah satu-satunya yang
        # datang tepat setelah dinding teks protokol, dan di situ perhatian
        # model paling terbagi. Pengingat pendek yang menempel pada tugasnya
        # jauh lebih dekat ke titik keputusan daripada aturan nomor sekian yang
        # terkubur di atas.
        # Tim spesialis pasif untuk giliran INI. Dibuat per giliran, bukan per
        # sesi: aturan "satu anggota bicara sekali" harus disetel ulang tiap
        # permintaan baru — kalau tidak, permintaan kedua dan seterusnya tak
        # pernah lagi mendapat sudut pandang siapa pun.
        tim_sesi = _tim.Tim(aktif=bool(prefs.load().get("tim", True)))
        # Perencana ditempel ke pesan TUGAS, bukan menunggu langkah pertama:
        # begitu langkah pertama terjadi, keputusan yang mau ia bantu sudah
        # telanjur diambil.
        first_msg = user_text + "\n\n" + _WEB_REMINDER + tim_sesi.awal()

        conn = connectors.get_connector(self.model_spec.connector)
        prompt_chars = 0
        reply_chars = 0
        answer = ""

        # Varian model situs ("GLM-5.2", "K2.6") yang dipilih lewat /model:
        # dipasang SEKARANG — browser memang sedang hidup untuk giliran ini.
        # Diklik sekali lalu NOLKAN; kegagalannya tak fatal (varian bawaan
        # situs tetap dipakai), cukup diberitahu lewat on_notice.
        if self._web_varian:
            varian = self._web_varian
            self._web_varian = None
            _status(f"memilih model {varian} di {self.model_spec.label}…")
            try:
                conn.set_web_option(varian)
                # Catat agar /model berikutnya tahu varian apa yang sedang
                # jalan di jendela ini (lihat pasang_model_web).
                self._web_terpasang[self.model_spec.connector] = varian
            except Exception as exc:  # noqa: BLE001 — jangan gagalkan giliran
                if on_notice is not None:
                    try:
                        on_notice(
                            f"⚠ varian '{varian}' tak terpasang di "
                            f"{self.model_spec.label}: {exc}")
                    except Exception:  # noqa: BLE001
                        pass

        def _sync_tokens() -> None:
            """Perbarui hitungan token (estimasi) agar penghitung di UI hidup
            selama giliran berjalan, bukan melompat di akhir."""
            self.tokens_live = (prompt_chars + reply_chars) // 4

        def _status(msg: str) -> None:
            """Lapor status HANYA bila pemanggil menyediakan salurannya.

            on_status boleh None — dan memang None pada pemanggil non-CLI:
            telegram_bot memanggil agent.run() tanpa on_status, begitu pula
            interfaces/api.py. Memanggilnya langsung membuat jalur ulang-otomatis
            mati dengan TypeError yang lalu ditelan `except Exception` di bawah,
            sehingga pengguna Telegram/API menerima '[Connector …] gagal:
            NoneType object is not callable' dan ulang-otomatis tak pernah
            jalan — persis kebalikan dari tujuan fiturnya."""
            if on_status is not None:
                on_status(msg)

        # Berapa kali konteks sudah dipadatkan di giliran ini (lihat _send).
        padat = 0

        def _send_raw(msg: str, new_chat: bool = False,
                      open_chat_id: str | None = None,
                      attachments: list[str] | None = None) -> str:
            nonlocal prompt_chars, reply_chars
            # Default: LANJUTKAN percakapan browser sesi ini. Ini KUNCI kontinuitas
            # di tengah tugas. Dulu hanya kirim PERTAMA yang menargetkan chat ini;
            # pesan susulan (hasil tool, teguran, perbaikan) dikirim dengan
            # open_chat_id kosong. Akibatnya bila browser mati & diluncurkan ulang
            # di tengah agentic-loop (mis. sesudah eksekusi lama yang bikin sesi
            # browser time-out), halaman baru mendarat di chat KOSONG dan susulan
            # diketik ke sana — AI web kehilangan seluruh konteks "progress tadi"
            # lalu kebingungan. Dengan menargetkan chat yang SAMA di tiap kirim,
            # relaunch kapan pun selalu kembali ke percakapan yang benar.
            if open_chat_id is None:
                open_chat_id = self._web_chat_id
            # "Server sedang sibuk" itu SEMENTARA (kuota kita aman) dan biasanya
            # pulih dalam hitungan detik, jadi ditangani di sini: tunggu lalu
            # kirim ULANG pesan yang sama. Menyerahkannya ke pengguna berarti
            # tugas yang sedang berjalan putus di tengah tanpa alasan nyata.
            # Jeda menaik supaya tak menambah beban server yang sedang penuh.
            jeda = (15, 40, 75)
            for percobaan in range(len(jeda) + 1):
                # Dihitung PER PERCOBAAN: pesannya benar-benar diketik & dikirim
                # ulang tiap kali, jadi menghitungnya sekali di luar loop membuat
                # estimasi token meremehkan lalu lintas justru pada giliran yang
                # paling banyak menghabiskan kuota situs.
                prompt_chars += len(msg)
                _sync_tokens()
                try:
                    out = conn.send(
                        msg, on_status=on_status, on_token=on_token,
                        cancel_event=cancel_event, new_chat=new_chat,
                        open_chat_id=open_chat_id,
                        complete_when=_web_reply_complete,
                        attachments=attachments,
                        # Kabar yang HARUS terbaca pengguna (captcha muncul,
                        # jendela dibuka, verifikasi selesai). Lewat on_status
                        # kalimatnya diringkas jadi satu kata fase dan hilang.
                        on_notice=on_notice,
                    )
                    break
                except connectors.WebBusyError:
                    if percobaan >= len(jeda):
                        raise           # sudah cukup sabar -> laporkan jujur
                    # Percobaan berikutnya harus MASUK KE CHAT YANG SAMA. Tanpa
                    # ini, kirim pertama sebuah sesi (new_chat=True) mengulang
                    # dengan new_chat=True juga, sehingga tiap percobaan membuat
                    # chat BARU: sampai empat chat terlantar berisi pesan yang
                    # sama, dan sesi akhirnya tertaut ke chat terakhir saja —
                    # persis pola kehilangan konteks yang sudah pernah diperbaiki.
                    dibuat = getattr(conn, "last_chat_id", "") or ""
                    if dibuat:
                        new_chat = False
                        open_chat_id = dibuat
                        self._link_web_chat(dibuat)
                    tunggu = jeda[percobaan]
                    _status(f"{self.model_spec.label} sibuk — menunggu "
                            f"{tunggu}s lalu mencoba lagi "
                            f"({percobaan + 1}/{len(jeda)})")
                    # UI sudah punya hitung-mundur khusus penantian seperti ini
                    # (footer "layanan sibuk — menunggu lalu melanjutkan Ns").
                    # Dulu menganggur karena on_retry tak pernah dipanggil lagi
                    # sesudah jalur API dihapus, sehingga penantian 75 detik
                    # hanya ditandai sebaris teks fase tanpa sisa waktu.
                    if on_retry is not None:
                        try:
                            on_retry(percobaan + 1, float(tunggu),
                                     connectors.WebBusyError("server penuh"))
                        except Exception:  # noqa: BLE001 - UI tak boleh menggagalkan
                            pass
                    # Tidur dipecah supaya Esc/batal tetap responsif; cancel_event
                    # yang menyala mengakhiri penungguan seketika.
                    habis = time.time() + tunggu
                    while time.time() < habis:
                        if cancel_event is not None and cancel_event.is_set():
                            raise llm.Cancelled()
                        time.sleep(0.2)
            reply_chars += len(out or "")
            _sync_tokens()
            # Panjang percakapan di situs — dasar simpanan otomatis. Dihitung
            # SEKALI per kirim yang berhasil (bukan per percobaan): percobaan
            # yang gagal karena server sibuk tak pernah mendarat di chat.
            self._web_chars += len(msg) + len(out or "")
            # Riwayat APA ADANYA. Inilah satu-satunya tempat percakapan penuh
            # tercatat: memory.messages hanya menyimpan permintaan pengguna &
            # jawaban akhir, sedangkan blok [[TOOL]], kode yang ditulis, dan
            # hasil tiap langkah — yang paling dibutuhkan chat berikutnya —
            # cuma lewat di sini.
            self._catat_web("saya", msg)
            self._catat_web("kamu", out or "")
            # Tangkap kaitan chat begitu URL /chat/<id> tersedia (kadang baru
            # muncul sesudah balasan pertama). Sekali tertangkap, semua kirim &
            # relaunch berikutnya otomatis menargetkan percakapan yang sama.
            got = getattr(conn, "last_chat_id", "")
            if got and got != self._web_chat_id:
                self._link_web_chat(got)
            return out

        def _send(msg: str, new_chat: bool = False,
                  open_chat_id: str | None = None,
                  attachments: list[str] | None = None) -> str:
            """_send_raw + auto-lanjut bila situs MEMOTONG output panjang.

            Kimi menghentikan jawaban yang melewati batas panjangnya dan
            menampilkan label "Output stopped" di bawahnya; label itu ikut
            terbaca sebagai ekor jawaban. Tanpa penanganan, jawaban terpotong
            dianggap selesai: kode putus di tengah, dan blok [[TOOL]] yang belum
            tertutup tak pernah dieksekusi. Di sini potongannya diminta
            DILANJUTKAN (maks _MAX_LANJUT kali) lalu digabung."""
            # WAJIB di baris pertama badan fungsi, bukan di dalam `except` di
            # bawah. Penyusun CPython memindai badan `try` (dan blok `else`,
            # bila ada) LEBIH DULU daripada blok `except`, sehingga deklarasi
            # yang ditaruh di dalam `except` terhitung datang terlambat:
            #     SyntaxError: name 'padat' is used prior to nonlocal declaration
            # Python 3.13 kebetulan memaafkannya, Python 3.10-3.12 tidak — jadi
            # pemasangan di laptop lain gagal total sejak impor pertama padahal
            # di sini mulus. Menaruhnya di sini aman di semua versi.
            nonlocal padat
            # PERCAKAPAN PENUH, DUA BENTUK — dan hanya SATU yang boleh
            # memindahkan pekerjaan tanpa bertanya.
            #
            # (a) Situs MENOLAK melanjutkan -> WebKonteksPenuhError, tak ada
            #     balasan sama sekali. Di sini chat baru bukan pilihan
            #     melainkan satu-satunya jalan: chat lamanya tak akan menerima
            #     apa pun lagi, jadi tanpa pindah, permintaan pengguna hilang.
            # (b) Situs baru MEMPERINGATKAN tapi tetap menjawab -> ditandai
            #     conn.konteks_penuh. Chat-nya masih jalan, jadi TIDAK dipindah:
            #     yang dilakukan cuma menyimpan ingatannya (di akhir giliran)
            #     lalu memberi tahu pengguna. Memindahkan diam-diam pernah jadi
            #     perilaku di sini, dan itu keputusan yang bukan miliknya.
            #
            # Keduanya dibatasi _MAKS_PADAT kali per giliran: kalau pindah pun
            # tak menolong, memutar terus cuma menghabiskan kuota.
            try:
                out = _send_raw(msg, new_chat, open_chat_id, attachments)
            except connectors.WebLampiranPenuhError as exc:
                # BATAS BERKAS SITUS, bukan kegagalan giliran. chat.z.ai:
                # "You can only chat with a maximum of 10 file(s) at a time."
                # Tiap langkah web_preview menambah satu screenshot, jadi di
                # sesi panjang batas ini pasti kena — dan dulu gejalanya adalah
                # kirim yang gagal tanpa sebab yang jelas.
                #
                # Pratinjaunya TIDAK dimatikan sebagai fitur: yang dimatikan
                # cuma PELAMPIRAN untuk percakapan ini, lalu pesan yang sama
                # dikirim ulang tanpa gambar berikut petunjuk cara kerja
                # pengganti. Chat baru menyalakannya lagi (_lupakan_chat_web).
                self._lampiran_mati = True
                if on_notice:
                    on_notice("situs menolak lampiran baru (batas berkas) — "
                              "pratinjau dilanjutkan tanpa gambar")
                _status(f"{self.model_spec.label}: batas lampiran tercapai — "
                        "melanjutkan tanpa gambar")
                out = _send_raw(msg + self._TANPA_GAMBAR.format(
                    sebab=" ".join(str(exc).split())[:120]),
                    False, self._web_chat_id, None)
            except connectors.WebKonteksPenuhError:
                if padat >= _MAKS_PADAT:
                    raise
                padat += 1
                self._padatkan_web(
                    conn, _send_raw, on_status, on_notice,
                    alasan="situs menolak melanjutkan percakapan ini")
                out = _send_raw(msg, False, self._web_chat_id, attachments)
            lanjut = 0
            while _OUTPUT_STOP_RE.search(out or "") and lanjut < _MAX_LANJUT:
                lanjut += 1
                out = _OUTPUT_STOP_RE.sub("", out).rstrip()
                _status("output terpotong situs — meminta lanjutannya "
                        f"({lanjut}/{_MAX_LANJUT})…")
                tambahan = _send_raw(
                    "[SISTEM] Output-mu barusan TERPOTONG oleh batas panjang "
                    "situs ('Output stopped'). Mulai sekarang JAGA tiap pesan "
                    "tetap pendek (satu blok tool besar per pesan, ±100 baris "
                    "isi). Lanjutkan begini:\n"
                    "- Yang terpotong TEKS biasa: lanjutkan persis dari titik "
                    "terputus — jangan mengulang dari awal, jangan minta maaf.\n"
                    "- Yang terpotong blok [[TOOL]]: JSON yang putus tak bisa "
                    "kubaca sama sekali. Bila bloknya KECIL, kirim ulang utuh. "
                    "Bila isinya PANJANG (write_file/append_file besar), JANGAN "
                    "kirim ulang utuh — pasti terpotong lagi. PECAH: kirim "
                    "write_file berisi bagian AWAL saja (±100 baris), lalu "
                    "setelah kukirim [[HASIL]], lanjutkan append_file bagian "
                    "berikutnya sampai file lengkap. Untuk file yang sudah ada, "
                    "pakai edit_file per bagian. Kerjakan sendiri sampai "
                    "tuntas — jangan menyuruhku menulis sisanya manual.")
                out = out + "\n" + (tambahan or "")
            return _OUTPUT_STOP_RE.sub("", out or "")

        try:
            # SATU sesi terminal = SATU percakapan browser:
            #  - sudah punya kaitan chat (sesi lanjutan / --resume) -> BUKA chat itu
            #  - belum punya -> mulai chat BARU lalu catat kaitannya
            first_of_session = not self._web_ctx_sent or bool(self._web_chat_id)
            if include_ctx:
                # Pesan 1: KONTEKS saja, di percakapan yang baru dibuat.
                # Balasannya sengaja TIDAK diurai sebagai usulan tool — ia cuma
                # tanda terima. Kalaupun AI melanggar dan mengeluarkan blok,
                # tak ada yang dieksekusi dari sini.
                _status(f"mengirim konteks proyek ke {self.model_spec.label}…")
                # Konteksnya DILAMPIRKAN sebagai berkas bila situsnya bisa
                # menerima berkas; kalau tidak (atau berkasnya tak terbaca di
                # sana), teks penuh yang lama tetap jadi cadangan — lihat
                # _kirim_konteks. Bedanya terukur: pesan yang diketik turun dari
                # ±40 rb karakter jadi ±25 rb, dan yang tersisa cuma aturan
                # main — bagian yang justru tak boleh sampai terpotong.
                berkas_ctx = []
                if (config.KONTEKS_BERKAS and conn.supports_context_files()
                        and not self._lampiran_mati):
                    # awalan "konteks": berkas pembuka milik bagas-ai sendiri,
                    # BUKAN ingatan pengguna — /send-compact tak boleh
                    # mengambilnya (lihat simpan_memory).
                    berkas_ctx = self.simpan_memory(
                        awalan=konteks.AWALAN_KONTEKS)
                if berkas_ctx:
                    kode_ctx = konteks.kode(berkas_ctx)
                    self._kirim_konteks(
                        _send, conn, berkas=berkas_ctx, kode=kode_ctx,
                        pengantar=self._pengantar_memory(
                            [p.name for p in berkas_ctx], kode_ctx,
                            lanjutan=bool(self._riwayat_web)),
                        teks_penuh=konteks_msg, new_chat=True, open_chat_id="",
                        on_status=on_status, on_notice=on_notice)
                else:
                    with self._tanpa_catatan():
                        _send(konteks_msg, new_chat=True, open_chat_id="")
                # Chat-nya sudah ada sekarang -> pesan berikutnya WAJIB masuk ke
                # chat yang sama, kalau tidak konteks yang barusan dikirim
                # tertinggal di percakapan lain.
                dibuat = getattr(conn, "last_chat_id", "") or ""
                if dibuat:
                    self._link_web_chat(dibuat)
                self._web_ctx_sent = True
                self._web_gap_from = None   # konteks penuh barusan dikirim
                _status(f"mengirim permintaanmu ke {self.model_spec.label}…")
            elif self._web_gap_from is not None:
                # PULANG ke layanan yang chat-nya sudah berkonteks, tapi ada
                # perkembangan yang dikerjakan di layanan lain selama di sana
                # (A -> B -> A). Kirim RINGKASAN kemajuannya saja — bukan
                # konteks penuh — ke percakapan yang sama, supaya model ini
                # benar-benar melanjutkan pekerjaan, bukan versi dirinya yang
                # tertinggal beberapa langkah.
                awal = self._web_gap_from
                self._web_gap_from = None
                try:
                    potong = self.memory.messages[awal:-1]  # tanpa pesan aktif
                except Exception:  # noqa: BLE001
                    potong = []
                try:
                    digest = prompts.build_transcript_digest(potong)
                except Exception:  # noqa: BLE001
                    digest = ""
                if digest:
                    _status("menyampaikan perkembangan dari model "
                            "sebelumnya…")
                    with self._tanpa_catatan():
                        _send(
                            "[SISTEM] Aku asisten yang sama, kini lanjut bekerja "
                            "di layanan ini. Di bawah ini ringkasan perkembangan "
                            "sejak terakhir di percakapan ini (dikerjakan asisten "
                            "lain di layanan sebelah). LANJUTKAN dari titik ini — "
                            "jangan mengulang yang sudah beres:\n\n" + digest,
                            new_chat=False, open_chat_id=self._web_chat_id)
            # Pesan 2 (atau satu-satunya, bila konteks sudah pernah dikirim):
            # permintaan pengguna.
            reply = _send(
                first_msg,
                new_chat=False,
                open_chat_id=self._web_chat_id if (first_of_session or include_ctx)
                else "",
                # Gambar dari pengguna (mis. foto yang dikirim ke bot Telegram)
                # DILAMPIRKAN ke percakapan web. Dulu gambar ditangani model VLM
                # terpisah lewat API; sekarang situs AI web sendiri yang
                # membacanya — hasilnya juga lebih baik karena gambar masuk ke
                # percakapan yang sama, bukan panggilan sekali-pakai tanpa konteks.
                attachments=[p for p in (attachments or [])
                             if conn.supports_attachments()
                             and not self._lampiran_mati],
            )
            if first_of_session:
                # Catat kaitan sesi<->chat + rapikan chat lama buatan bagas-ai
                # supaya tak menumpuk di akun (chat pribadi tak tersentuh).
                try:
                    chat_id = getattr(conn, "last_chat_id", "")
                    if chat_id:
                        self._link_web_chat(chat_id)
                        if include_ctx:  # percakapan yang BARU dibuat
                            conn.record_chat(chat_id, user_text[:80])
                            if config.CONNECTOR_KEEP_CHATS > 0:
                                conn.prune_own_chats(config.CONNECTOR_KEEP_CHATS)
                except Exception:  # noqa: BLE001 - bersih-bersih tak boleh menggagalkan giliran
                    pass

            steps = 0
            repairs = 0   # berapa kali minta AI web mengirim ulang blok rusak
            tutup = 0     # berapa kali didesak menutup dengan teks biasa
            # Jaring anti-ulang: hasil langkah
            # di-cache per (nama+argumen). Tanpa ini AI web bisa mengulang
            # langkah yang PERSIS SAMA berpuluh kali sampai batas langkah habis
            # — boros kuota & tak menghasilkan apa pun.
            seen_tools: dict[str, str] = {}
            dup_hits = 0
            # Langkah yang GAGAL/timeout BERTURUT-TURUT. Beda dari dup_hits: di sini
            # argumennya boleh berubah-ubah (mis. AI web menjalankan kode yang
            # sedikit divariasikan tapi tetap infinite-loop lalu timeout berulang),
            # sehingga cache anti-ulang tak menangkapnya dan tugas bisa memutar
            # sampai _WEB_MAX_STEPS (~12 menit timeout beruntun). Dihentikan lebih
            # awal supaya AI web menyimpulkan jujur alih-alih terus mencoba.
            fail_streak = 0
            force_final = False
            nudges = 0    # teguran "kode ditampilkan tapi tak ditulis ke file"
            janji = 0     # teguran "menjanjikan langkah tapi tanpa blok [[TOOL]]"
            peringatan_batas = False   # sekali saja per giliran
            # Berapa kali AI web menumpuk >1 blok [[TOOL]] dalam satu pesan.
            # Protokolnya SATU langkah per pesan (lihat aturan 2 di
            # _web_tool_protocol) justru karena langkah ke-2 dst pasti disusun
            # SEBELUM hasil langkah ke-1 terlihat — jadi ia tebakan, dan tetap
            # dijalankan walau yang pertama gagal atau isinya tak seperti dugaan.
            #
            # Penegakannya BERTAHAP, bukan langsung memangkas: tumpukan PERTAMA
            # tetap dijalankan seluruhnya (pekerjaan yang sudah terlanjur
            # dihasilkan tak dibuang percuma) sambil ditegur; kalau masih
            # menumpuk juga, barulah yang dijalankan cuma blok pertama.
            batch_hits = 0
            # Validasi otomatis sebelum jawaban akhir: `mutasi_kode` menyala bila
            # ada tool yang benar-benar MENGUBAH berkas kode; `validasi_jalan`
            # mencegah paksaan validasi berulang tanpa henti bila hasilnya tetap
            # gagal (setelah sekali dipaksa, keputusan diserahkan ke AI).
            mutasi_kode = False
            validasi_jalan = False
            # Penegakan "lihat sendiri hasilnya": `ubah_tampilan` menyala bila
            # ada berkas yang KELIHATAN diubah, `dilihat` bila AI benar-benar
            # memakai web_preview/take_screenshot. Dulu ini cuma imbauan prosa
            # di pesan pembuka — dan imbauan di pembuka terbukti kalah oleh
            # kebiasaan model. Ditegakkan sekali per giliran.
            ubah_tampilan = False
            dilihat = False
            lihat_dipaksa = False
            # Berkas kode yang diubah giliran ini — dioper ke validate_project
            # agar pemeriksaan per-berkas (py_compile/smoke-run/php -l) tepat
            # menyasar yang barusan disentuh, bukan menebak-nebak.
            berkas_mutasi: set[str] = set()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise llm.Cancelled()
                calls = [] if force_final else _parse_web_tool_calls(
                    reply, getattr(conn, "last_code_blocks", ()))

                # SUDAH DIMINTA MENYIMPULKAN, TAPI MASIH MENGIRIM BLOK LANGKAH.
                #
                # Ini kejadian nyata, bukan kemungkinan teoretis: sesudah
                # anti-macet menyala, model membalas dengan blok [[TOOL]] lagi.
                # Blok itu SAH — cuma tak boleh dijalankan lagi — sehingga
                # seluruh isi pesannya dibuang dan yang tersisa nol kalimat.
                # Dulu keadaan itu jatuh ke jalur "JSON-mu rusak" di bawah lalu
                # ke pesan "formatnya rusak saat dirender": dua kali bolak-balik
                # percuma, ditutup dengan diagnosis yang SALAH — tak ada yang
                # rusak, model cuma menolak berhenti. Ditangani di sini dengan
                # desakan yang menyebut sebabnya, dan bloknya tetap tak
                # dijalankan.
                if force_final and not _sisa_prosa(reply) and tutup < _MAKS_TUTUP:
                    tutup += 1
                    if on_notice:
                        on_notice("masih mengirim langkah — diminta menutup "
                                  "dengan teks biasa")
                    reply = _send(
                        "[SISTEM] Blokmu kuterima tapi TIDAK kujalankan: "
                        "giliran ini sudah ditutup untuk tool. Mengirim blok "
                        "lagi tak akan mengubah apa pun.\n"
                        "Balas HANYA teks biasa — tanpa [[TOOL]], tanpa blok "
                        "kode JSON, tanpa usulan langkah. Sebutkan: apa yang "
                        "sudah berhasil, apa yang gagal dan kenapa, lalu apa "
                        "yang tersisa.")
                    continue

                # Ada penanda [[TOOL]] tapi isinya tak terbaca (rusak saat
                # dirender web). Jangan tampilkan penanda mentah ke pengguna —
                # minta AI web mengirim ulang usulannya dengan format benar.
                #
                # TIDAK berlaku saat force_final: di sana `calls` memang sengaja
                # dikosongkan, jadi "tak terbaca" cuma tampak begitu — menyuruh
                # mengirim ulang blok yang sebenarnya baik-baik saja bertabrakan
                # dengan perintah berhenti yang baru saja kita kirim.
                if (not calls and not force_final
                        and "[[TOOL]]" in (reply or "") and repairs < 2):
                    repairs += 1
                    reply = _send(
                        "[SISTEM] Blok usulan tool-mu tidak terbaca (JSON-nya "
                        "rusak saat dirender). Kirim ULANG langkah itu SAJA "
                        "dengan format persis:\n[[TOOL]]\n```json\n"
                        '{"tool": "...", "args": {...}}\n```\n[[/TOOL]]\n'
                        "Pakai garis miring biasa pada path, tanpa teks lain. "
                        "Kalau isinya PANJANG (kemungkinan tadi terpotong "
                        "situs), jangan ulangi utuh — pecah: write_file bagian "
                        "awal (±100 baris) lalu append_file lanjutannya di "
                        "pesan-pesan berikutnya.")
                    continue

                # AI web menampilkan KODE tapi tak menuliskannya ke file: itu
                # mode mengobrol, bukan mengerjakan. Tegur SEKALI per giliran —
                # pengguna memakai connector ini justru agar perubahannya nyata.
                if (not calls and not force_final and nudges < 1
                        and steps == 0 and _looks_like_unapplied_code(reply)):
                    nudges += 1
                    reply = _send(
                        "[SISTEM] Kamu menampilkan kode tapi tidak menuliskannya "
                        "ke file, jadi tak ada yang berubah di laptopku. Kalau "
                        "kode itu memang perlu diterapkan, keluarkan sekarang "
                        "blok [[TOOL]]: untuk MENGUBAH file yang sudah ada pakai "
                        "edit_file (baca dulu bila perlu, lalu kirim hanya "
                        "potongan yang berubah); untuk file BARU pakai write_file "
                        "isi lengkap. Kalau memang hanya penjelasan, ulangi "
                        "jawaban akhirmu tanpa blok tool.")
                    continue

                # AI menjanjikan langkah ("aku baca dulu main.py") tapi tak
                # membawa bloknya. Tanpa teguran ini, kalimat niat itu jadi
                # jawaban akhir: pengguna melihat AI berjanji lalu berhenti,
                # dan giliran habis tanpa satu pun pekerjaan nyata. Diberi
                # jatah 2x per giliran — bukan 1x seperti teguran kode, sebab
                # macetnya bisa terjadi di langkah mana pun, bukan cuma awal.
                if (not calls and not force_final and janji < 2
                        and (_looks_like_promise(reply)
                             or _pakai_tool_bawaan(reply))):
                    janji += 1
                    if janji >= 2 and _pakai_tool_bawaan(reply):
                        # Teguran PERTAMA sudah menjelaskan alasannya dan tetap
                        # dilanggar. Yang kedua berhenti menjelaskan — ia
                        # menyatakan AKIBAT, karena penjelasan terbukti kalah
                        # oleh kebiasaan bawaan model. Diminta pula membalas
                        # TANPA kalimat pembuka: kalimat itulah yang selama ini
                        # memicu loop tool bawaannya menyala lagi.
                        reply = _send(
                            "[SISTEM] STOP. Kamu memakai tool bawaanmu lagi. "
                            "Hasilnya SUDAH KUBUANG dan tak pernah sampai ke "
                            "pengguna — sandbox itu tak punya satu pun berkas "
                            "proyeknya, jadi apa pun yang kamu 'baca' di sana "
                            "salah.\n"
                            "Balas pesan ini HANYA dengan satu blok, tanpa "
                            "kalimat pembuka, tanpa penjelasan:\n"
                            "[[TOOL]]\n```json\n"
                            '{"tool": "read_file", "args": {"path": "..."}}\n'
                            "```\n[[/TOOL]]\n"
                            "Ganti nama tool & argumennya sesuai yang memang "
                            "kamu butuhkan. Kalau kamu membalas tanpa blok "
                            "lagi, giliran ini berakhir tanpa hasil apa pun.")
                        continue
                    reply = _send(
                        "[SISTEM] Pesanmu barusan tak memuat blok [[TOOL]], "
                        "jadi TIDAK ADA yang dijalankan di laptopku — dan "
                        "kalimatmu tadi tampil ke pengguna sebagai jawaban "
                        "akhir. Dua sebab yang paling sering:\n"
                        "1. Kamu menyatakan niat ('aku baca dulu X') lalu "
                        "berhenti. Kalimat pembuka WAJIB berpasangan dengan "
                        "bloknya di pesan yang SAMA.\n"
                        "2. Kamu memakai tool BAWAANMU (Execute Python code / "
                        "code interpreter / sandbox). Sandbox itu jalan di "
                        "server layananmu, BUKAN di laptopku — ia tak punya "
                        "satu pun berkas proyekku, jadi apa pun hasilnya fiksi "
                        "dan tak ada berkasku yang berubah. Matikan cara itu; "
                        "untuk membaca berkas pakai read_file, untuk "
                        "menjalankan Python di laptopku pakai run_python — "
                        "keduanya lewat [[TOOL]].\n"
                        "Kirim SEKARANG langkah yang tadi kamu maksud, format "
                        "persis:\n[[TOOL]]\n```json\n"
                        '{"tool": "...", "args": {...}}\n```\n[[/TOOL]]\n'
                        "Kalau ternyata tak ada lagi yang perlu dikerjakan, "
                        "tulis jawaban akhirmu sebagai HASIL (apa yang sudah "
                        "berubah / apa temuannya) — bukan sebagai rencana.")
                    continue

                # BERTANYA TANPA ask_user. Pesan tanpa blok = jawaban akhir,
                # jadi pertanyaan yang ditulis sebagai teks biasa tak pernah
                # sampai ke pengguna sebagai pertanyaan: gilirannya habis dan ia
                # cuma melihat AI balik bertanya lalu diam. Dulu ini cuma aturan
                # prosa; sekarang ditegur di detik kejadiannya.
                if (not calls and not force_final and janji < 2
                        and _tanya_tanpa_ask_user(reply)):
                    janji += 1
                    _ask = "ask_user_telegram" if interaction._ctx_handler.get() is not None else "ask_user"
                    reply = _send(
                        "[SISTEM] Kamu bertanya lewat teks biasa. Pertanyaan "
                        "itu TIDAK sampai ke pengguna sebagai pertanyaan — "
                        "pesan tanpa blok kuperlakukan sebagai jawaban akhir, "
                        "jadi gilirannya habis dan pertanyaanmu tak akan "
                        f"dijawab.\nUlangi lewat {_ask}, dengan 2-6 opsi "
                        "konkret yang bisa dibandingkan (sebutkan konsekuensi "
                        "tiap opsi dalam beberapa kata):\n[[TOOL]]\n```json\n"
                        f'{{"tool": "{_ask}", "args": {{"question": "...", '
                        '"options": ["...", "..."]}}\n```\n[[/TOOL]]')
                    continue

                # UBAH TAMPILAN TAPI TAK PERNAH DILIHAT. Pola yang sama dengan
                # penegakan validate_project di bawah, dan alasannya sama:
                # menyatakan "tampilannya sudah rapi" tanpa pernah melihat
                # gambarnya itu mengarang. Dipaksa maksimal SEKALI per giliran
                # supaya tak memutar tanpa henti bila memang tak ada yang bisa
                # dilihat (tak ada dev server / bukan aplikasi berjendela).
                # DILEWATI saat web_preview dijeda: menuntut tool yang mati
                # hanya membuang satu putaran penuh browser untuk jawaban
                # "dinonaktifkan" — take_screenshot sendirian tak menggantikan
                # perannya untuk halaman web.
                if (config.WEB_PREVIEW
                        and not calls and not force_final and ubah_tampilan
                        and not dilihat and not lihat_dipaksa):
                    lihat_dipaksa = True
                    reply = _send(
                        "[SISTEM] Giliran ini mengubah berkas tampilan, tapi "
                        "kamu belum sekali pun melihat hasilnya. Jangan tutup "
                        "dulu.\nBuktikan sekarang: kalau ini web, pastikan dev "
                        "server jalan (run_command_bg 'npm run dev', tunggu "
                        "lewat bg_output) lalu web_preview URL-nya; kalau "
                        "aplikasi berjendela, take_screenshot. Sesudah "
                        "gambarnya kamu terima, sebutkan apa yang benar-benar "
                        "kamu lihat dan bandingkan dengan yang diminta "
                        "pengguna.\nKalau memang tak ada yang bisa dilihat "
                        "(tak ada server/GUI), katakan itu terus terang di "
                        "jawaban akhirmu — bahwa perubahannya belum "
                        "diverifikasi secara visual.")
                    continue

                # VALIDASI OTOMATIS sebelum menutup: kalau kode berubah tapi AI
                # belum sekali pun memanggil validate_project, jalankan sendiri
                # dan paksa AI menyelesaikan temuannya. Ini penegakan, bukan
                # sekadar imbauan protokol — "selesai" tanpa bukti kode masih
                # waras persis yang ingin dihindari pengguna. Dipaksa MAKS sekali
                # per giliran (validasi_jalan) agar tak memutar tanpa henti.
                if (not calls and not force_final and mutasi_kode
                        and not validasi_jalan):
                    validasi_jalan = True
                    if on_notice:
                        on_notice("memvalidasi kode sebelum menutup…")
                    args_val = {"paths": " ".join(sorted(berkas_mutasi))}
                    hasil_val = tools.execute("validate_project", args_val)
                    if on_tool:
                        on_tool("validate_project", args_val)
                    if on_tool_result:
                        on_tool_result("validate_project", hasil_val)
                    gagal_val = ("✗" in hasil_val or "GAGAL" in hasil_val
                                 or "TIMEOUT" in hasil_val)
                    if gagal_val:
                        reply = _send(
                            "[SISTEM] Sebelum menutup, aku menjalankan "
                            "validate_project atas kode yang barusan berubah dan "
                            "ADA yang GAGAL. Perbaiki dulu, lalu validasi lagi — "
                            "jangan nyatakan selesai selagi masih gagal.\n\n"
                            f"[[HASIL validate_project]]\n{hasil_val}\n"
                            "[[/HASIL]]")
                        continue
                    # Lulus: lanjut ke penutupan normal di bawah.

                if not calls or steps >= _WEB_MAX_STEPS:
                    # Tak ada tool -> ini jawaban AKHIR. Bersihkan sisa penanda
                    # DAN usulan JSON tanpa penanda (mis. saat model tetap
                    # mengulang padahal sudah diminta menyimpulkan).
                    answer = _strip_tool_json(_strip_web_markers(reply))
                    if not answer:
                        # Seluruh balasan hanya berupa blok/penanda yang tak
                        # terbaca. Tampilkan CUPLIKAN aslinya — tanpa itu tak
                        # ada petunjuk apa pun untuk memperbaiki penyebabnya.
                        # Cuplikan DISANITASI: pagar kode (```) akan menutup
                        # blok lebih awal sehingga sisanya dirender kacau, dan
                        # penanda protokol yang lolos ke memory ikut terbawa ke
                        # ringkasan percakapan untuk model BERIKUTNYA.
                        mentah = " ".join((reply or "").split())[:300]
                        mentah = (mentah.replace("`", "'")
                                        .replace("[[", "⟦").replace("]]", "⟧"))
                        # Sebabnya dibedakan. Menyebut "formatnya rusak" pada
                        # balasan yang formatnya justru sempurna membuat
                        # pengguna memburu bug yang tak ada — yang terjadi
                        # sebenarnya: tool sudah dimatikan, tapi model tetap
                        # mengirim langkah alih-alih menyimpulkan.
                        if _parse_web_tool_calls(
                                reply, getattr(conn, "last_code_blocks", ())):
                            answer = (
                                "Giliran ini kuhentikan tanpa jawaban akhir. "
                                "Sesudah beberapa langkah gagal beruntun, "
                                f"{conn.label} terus mengirim langkah baru "
                                "padahal sudah kuminta berhenti dan "
                                "menyimpulkan.\n\n"
                                "Pekerjaan yang sempat berhasil tetap tersimpan "
                                "— ketik 'lanjutkan' untuk meneruskan, atau "
                                "ganti model lewat /model."
                            )
                        else:
                            answer = (
                                "Balasan dari AI web tak bisa kubaca sebagai "
                                "langkah yang sah (formatnya rusak saat "
                                "dirender). Coba kirim ulang permintaanmu, atau "
                                "perjelas langkah yang kamu mau.\n\n"
                                "Yang terbaca dari layar:\n"
                                f"```\n{mentah or '(kosong)'}\n```"
                            )
                    if steps >= _WEB_MAX_STEPS and calls:
                        answer += (
                            f"\n\n_(Batas {_WEB_MAX_STEPS} langkah tool "
                            "tercapai, jadi giliran ini kuhentikan di sini — "
                            "sebagian aksi mungkin belum tuntas. Kirim "
                            "'lanjutkan' untuk meneruskan dari titik ini.)_")
                        if on_notice:
                            on_notice(
                                f"batas {_WEB_MAX_STEPS} langkah tool tercapai "
                                "— giliran dihentikan, ketik 'lanjutkan' untuk "
                                "meneruskan")
                    break

                # Narasi sebelum tool = teks di luar blok tool. JSON usulan yang
                # ditulis TANPA penanda juga dibuang, kalau tidak ia tercetak
                # mentah ke layar tiap putaran.
                narration = _strip_tool_json(_strip_web_markers(reply))
                if narration and on_message:
                    on_message(narration)

                # SATU langkah per pesan (lihat batch_hits di atas).
                n_diusulkan = len(calls)
                ditahan = 0
                if n_diusulkan > 1:
                    batch_hits += 1
                    if batch_hits > 1:
                        ditahan = n_diusulkan - 1
                        calls = calls[:1]
                        if on_notice:
                            on_notice(
                                f"{n_diusulkan} blok tool sekaligus — hanya yang "
                                "pertama dijalankan")

                # Eksekusi tiap tool & kumpulkan hasil untuk dikirim balik.
                result_blocks = []
                images: list[str] = []
                tim_bangun: list = []      # rekan yang bangun di LANGKAH ini
                for c in calls:
                    if cancel_event is not None and cancel_event.is_set():
                        raise llm.Cancelled()
                    name, args = c["name"], c["arguments"]
                    if on_tool:
                        on_tool(name, args)
                    kunci = name + "::" + json.dumps(
                        args, sort_keys=True, ensure_ascii=False, default=str)
                    if kunci in seen_tools:
                        # Langkah PERSIS SAMA sudah pernah dijalankan: kembalikan
                        # hasil yang sama + tegur, jangan eksekusi ulang.
                        dup_hits += 1
                        result = (
                            "[SISTEM] Kamu SUDAH menjalankan langkah ini dengan "
                            "argumen yang sama persis; hasilnya identik dengan di "
                            "bawah. JANGAN mengulanginya — pakai hasil ini lalu "
                            "lanjut ke langkah BERIKUTNYA atau berikan jawaban "
                            "akhir.\n\n" + seen_tools[kunci]
                        )
                    else:
                        result = tools.execute(name, args)
                        seen_tools[kunci] = result
                        # Deret gagal beruntun (lihat fail_streak di atas). Penanda
                        # gagal seragam dari tools: "[GAGAL...]" (shell) & "GAGAL:"
                        # (files). Sukses apa pun menyetel ulang deretnya.
                        sukses = not ("[GAGAL" in result
                                      or result.lstrip().startswith(
                                          ("GAGAL", "[DITOLAK", "[error]")))
                        if sukses:
                            fail_streak = 0
                        else:
                            fail_streak += 1
                        # Keadaan berubah -> hasil tool lama basi. Kosongkan
                        # cache anti-ulang, tapi simpan kembali langkah BARUSAN:
                        # mengulang persis mutasi yang sama tetap terdeteksi.
                        if sukses and name in _TOOL_STATE:
                            seen_tools.clear()
                            seen_tools[kunci] = result
                        if on_tool_result:
                            on_tool_result(name, result)
                        # Rekan satu tim yang punya urusan dengan langkah ini
                        # (lihat tim.py). Dikumpulkan di sini — bukan sesudah
                        # gelung — supaya yang dinilai memang langkah yang baru
                        # saja BERHASIL, bukan gabungan semua langkah.
                        bangun = tim_sesi.tinjau(name, args, sukses)
                        if bangun:
                            tim_bangun.extend(bangun)
                            if on_tim:
                                on_tim([a.nama for a in bangun])
                        # Tandai bila kode BERUBAH (untuk validasi otomatis di
                        # akhir), dan catat bila validasi memang sudah dijalankan.
                        if name == "validate_project":
                            validasi_jalan = True
                        if name in ("web_preview", "take_screenshot"):
                            dilihat = True
                        if (name in _TOOL_MUTASI
                                and not result.lstrip().startswith(
                                    ("GAGAL", "[DITOLAK"))
                                and _menyentuh_tampilan(args)):
                            ubah_tampilan = True
                        if (name in _TOOL_MUTASI
                              and not result.lstrip().startswith("GAGAL")
                              and not result.lstrip().startswith("[DITOLAK")
                              and _menyentuh_kode(name, args)):
                            mutasi_kode = True
                            for kunci_path in ("path", "dest"):
                                nilai = args.get(kunci_path)
                                if isinstance(nilai, str) and nilai:
                                    berkas_mutasi.add(nilai)
                    steps += 1
                    # Tool yang menghasilkan GAMBAR (mis. screenshot): file-nya
                    # dilampirkan ke pesan berikutnya supaya AI web melihatnya
                    # sendiri, bukan cuma diberi tahu path-nya.
                    text_result, imgs = _take_image_marks(result)
                    if imgs and conn.supports_attachments() \
                            and not self._lampiran_mati:
                        images.extend(imgs)
                        text_result += ("\n(gambar terlampir pada pesan ini — "
                                        "lihat langsung, jangan minta dikirim ulang)")
                    elif imgs:
                        # Tool-nya TETAP dijalankan dan hasilnya tetap dipakai —
                        # yang hilang cuma gambarnya. Dikatakan apa adanya,
                        # lengkap dengan jalan gantinya, supaya AI tak menunggu
                        # gambar yang tak akan datang.
                        text_result += (
                            "\n(gambar TIDAK bisa dilampirkan: situs sudah "
                            "mentok jumlah berkas per percakapan. Jangan minta "
                            "dikirim ulang — pakai web_extractor / baca "
                            "berkasnya / jalankan perintahnya untuk memastikan)")
                    clipped = text_result if len(text_result) <= _WEB_RESULT_CAP \
                        else (text_result[:_WEB_RESULT_CAP] + "\n…[hasil dipotong]")
                    result_blocks.append(
                        f"[[HASIL {name}]]\n{clipped}\n[[/HASIL]]")

                follow = (
                    "\n\n".join(result_blocks)
                    + "\n\nLanjutkan tugas berdasarkan hasil di atas. Kalau perlu "
                    "tool lagi, keluarkan SATU blok [[TOOL]] berikutnya (satu "
                    "saja, lalu tunggu hasilnya) dan dahului dengan satu "
                    "kalimat pendek yang memberi tahu apa yang sedang kamu "
                    "lakukan; kalau sudah SELESAI, beri jawaban akhir biasa "
                    "(tanpa blok tool)."
                )
                # Sudut pandang rekan satu tim ditempel SESUDAH instruksi
                # lanjutan, bukan sebelumnya: yang terakhir dibaca yang paling
                # kuat menempel, dan inilah bagian yang paling mudah terlewat
                # kalau tenggelam di tengah hasil tool.
                follow += tim_sesi.blok(tim_bangun)
                # Tuntutan memperbarui AGENTS.md muncul di langkah tempat bentuk
                # proyek benar-benar berubah — bukan di akhir giliran, saat model
                # sudah menganggap pekerjaannya tuntas dan enggan membuka berkas
                # lagi.
                follow += tim_sesi.perlu_agents_md()
                if ditahan:
                    follow += (
                        f"\n\n[SISTEM] Pesanmu memuat {n_diusulkan} blok [[TOOL]] "
                        "sekaligus. Yang kujalankan HANYA yang pertama (hasilnya "
                        f"di atas); {ditahan} sisanya kubuang tanpa dijalankan, "
                        "sebab langkah itu kamu susun sebelum melihat hasil ini. "
                        "Kirim langkah berikutnya SATU blok saja."
                    )
                elif n_diusulkan > 1:
                    follow += (
                        f"\n\n[SISTEM] Tadi ada {n_diusulkan} blok [[TOOL]] dalam "
                        "satu pesan. Kali ini semuanya kujalankan, tapi mulai "
                        "sekarang kirim SATU blok per pesan lalu tunggu "
                        "[[HASIL]]-nya — kalau menumpuk lagi, yang kujalankan "
                        "cuma blok pertama."
                    )
                if dup_hits >= config.MAX_DUPLICATE_TOOL_CALLS:
                    # Terjebak mengulang langkah yang sama: matikan tool dan
                    # paksa menyimpulkan, daripada memutar sampai batas langkah.
                    force_final = True
                    follow += (
                        "\n\n[SISTEM] Kamu terus mengulang langkah yang sama. "
                        "STOP memakai tool. Berikan jawaban akhir dalam teks "
                        "biasa: jelaskan JUJUR apa yang sudah selesai, apa yang "
                        "belum, dan langkah tersisa yang perlu dilakukan."
                    )
                    if on_notice:
                        on_notice("langkah yang sama berulang — beralih ke "
                                  "kesimpulan")
                elif fail_streak >= config.MAX_DUPLICATE_TOOL_CALLS:
                    # Gagal/timeout beruntun (mis. kode yang dijalankan tak pernah
                    # berhenti): berhenti mencoba, minta kesimpulan jujur.
                    force_final = True
                    follow += (
                        "\n\n[SISTEM] Beberapa langkah tool GAGAL/timeout "
                        "berturut-turut. STOP menjalankan ulang kode itu. Berikan "
                        "jawaban akhir dalam teks biasa: jelaskan JUJUR apa yang "
                        "berhasil, apa yang gagal DAN kenapa (mis. kode yang "
                        "dijalankan tak berhenti / timeout), lalu langkah tersisa."
                    )
                    if on_notice:
                        on_notice("langkah gagal/timeout beruntun — beralih ke "
                                  "kesimpulan")

                # BATAS LANGKAH SUDAH DEKAT. Diberi tahu ke DUA pihak sekaligus:
                # ke model supaya ia menyusun ulang prioritas selagi masih
                # sempat (menuntaskan yang hampir jadi, bukan memulai hal baru),
                # dan ke terminal supaya pengguna tak kaget menemukan giliran
                # berhenti tanpa sebab yang terlihat. Sekali saja per giliran.
                sisa_langkah = _WEB_MAX_STEPS - steps
                if 0 < sisa_langkah <= _SISA_LANGKAH_PERINGATAN \
                        and not peringatan_batas:
                    peringatan_batas = True
                    follow += (
                        f"\n\n[SISTEM] Sisa {sisa_langkah} langkah tool lagi "
                        f"sebelum batas {_WEB_MAX_STEPS} dan giliran ini "
                        "kuhentikan otomatis. Prioritaskan: tuntaskan yang "
                        "sudah hampir jadi, jangan memulai pekerjaan baru. "
                        "Kalau tak akan cukup, lebih baik berhenti sekarang "
                        "dengan jawaban akhir yang menyebut jujur apa yang "
                        "sudah selesai dan apa yang tersisa.")
                    if on_notice:
                        on_notice(f"sisa {sisa_langkah} langkah tool sebelum "
                                  "batas giliran")

                # SISIPKAN pesan susulan pengguna, tepat di batas langkah ini.
                # TIDAK dilakukan saat force_final: di sana AI justru sedang
                # disuruh berhenti memakai tool dan menyimpulkan, jadi menaruh
                # tugas baru di pesan yang sama itu perintah yang saling
                # bertabrakan. Pesannya dibiarkan di antrean & dikerjakan
                # sebagai giliran berikutnya — tak ada yang hilang.
                if ambil_sisipan is not None and not force_final:
                    try:
                        susulan = list(ambil_sisipan() or [])
                    except Exception:  # noqa: BLE001 - antrean rusak != giliran gagal
                        susulan = []
                    blok = _blok_sisipan(susulan)
                    if blok:
                        follow += blok
                        # Dicatat ke memory sebagai ucapan pengguna yang memang
                        # terjadi di titik ini — kalau tidak, sesi yang di-resume
                        # memperlihatkan AI tiba-tiba mengerjakan hal yang tak
                        # pernah diminta siapa pun.
                        for t in susulan:
                            if (t or "").strip():
                                self.memory.add_user(t.strip())
                        if on_notice:
                            n = len([t for t in susulan if (t or "").strip()])
                            on_notice(
                                f"{n} pesan susulanmu disisipkan ke giliran ini"
                                if n > 1 else
                                "pesan susulanmu disisipkan ke giliran ini")
                reply = _send(follow, attachments=images)
        except llm.Cancelled:
            self.memory.repair_dangling_tools()
            self._persist()
            raise
        except connectors.WebBusyError as exc:
            # Sudah diulang beberapa kali dan servernya MASIH penuh. Katakan apa
            # adanya — jangan tampilkan pemberitahuan situs seolah jawaban model.
            answer = (
                f"🕒 **Server {self.model_spec.label} sedang penuh.**\n\n"
                f"> {exc}\n\n"
                "Sudah kucoba ulang beberapa kali dengan jeda, tapi masih penuh. "
                "Kirim ulang sebentar lagi, atau ketik `/model` untuk pindah ke "
                "layanan web lain (Kimi/Qwen/Gemini) supaya bisa lanjut sekarang."
            )
        except connectors.WebChatRusakError as exc:
            # Percakapan di situsnya tak bisa dilanjutkan (mis. Qwen: "Invalid
            # input chat parent_id … is not exist"). Kuota kita baik-baik saja,
            # jadi ini TAK PERLU merepotkan pengguna: buka chat baru lalu kirim
            # ulang. Sekali saja — kalau chat yang baru pun rusak, sebabnya
            # bukan chat-nya, dan mencoba terus cuma memutar.
            answer = self._pulihkan_chat_rusak(
                exc, user_text, on_status, on_notice)
        except connectors.WebLimitError as exc:
            # Kuota situs habis. Pengguna DITANYA mau apa, bukan cuma diberi
            # tahu: dua jalan keluarnya (menunggu vs pindah model) punya
            # konsekuensi yang sangat berbeda, dan hanya dia yang tahu mana yang
            # cocok dengan pekerjaannya saat itu.
            answer = self._tangani_limit(exc, user_text, on_status, on_notice)
        except connectors.BrowserError as exc:
            answer = f"[Connector {self.model_spec.label}] {exc}"
        except Exception as exc:  # noqa: BLE001 - laporkan apa adanya, jangan crash REPL
            answer = f"[Connector {self.model_spec.label}] gagal: {exc}"

        self.memory.add_assistant_text(answer)
        # Ingatan disimpan di BATAS GILIRAN, bukan di tengah langkah: di sini
        # pekerjaannya utuh (jawaban akhir sudah masuk riwayat) dan tak ada
        # berkas yang ditulis belasan kali dalam satu giliran panjang.
        self._simpan_otomatis(conn, on_notice, on_padat)
        # Chat layanan ini kini melihat riwayat sampai titik ini — penanda
        # untuk pengiriman ringkasan kemajuan saat pengguna pulang dari
        # layanan lain (lihat _hitung_gap).
        self._stamp_web_seen()
        # Web-AI tak melaporkan token; pakai estimasi ~4 karakter per token dari
        # TOTAL lalu-lintas giliran ini (semua pesan terkirim + semua balasan),
        # bukan hanya jawaban akhir, supaya angkanya mencerminkan biaya nyata.
        self.tokens_last.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_session.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_live = 0  # reset: tokens_session sudah termasuk giliran ini
        self._persist()
        return answer

    # _run_loop() DIHAPUS bersama model ber-API-key. Ia berisi seluruh alur
    # tool-calling gaya OpenAI: streaming delta, perakitan tool_calls, retry
    # rate-limit, watchdog stream macet, dan pemicu naik-kelas. Semua itu
    # khusus endpoint API dan tak punya padanan di jalur browser --
    # model web memakai protokol penanda [[TOOL]] yang dieksekusi di
    # _run_connector. Menyimpannya hanya akan jadi ~250 baris kode mati yang
    # mustahil dijangkau.

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

import datetime as _dt
import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import config, llm, models, prefs, prompts
from . import tim as _tim
from .memory import Memory
from .session import Session
from .tools import base as tools
from .tools import katalog as _katalog

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
        "dan jangan memverifikasi ulang langkah yang jelas berhasil.\n"
        "- Mulai dari peta proyek di bawah. search_text/glob_files jauh lebih "
        "cepat daripada list_dir berulang, dan project_info memberi ekosistem, "
        "skrip, serta entry point proyek dalam satu panggilan.\n"
        "- Sesudah mengubah kode, buktikan masih waras dengan validate_project "
        "(isi paths dengan berkas yang kamu ubah). Sesudah mengubah tampilan, "
        "lihat sendiri hasilnya: web_preview untuk halaman web, take_screenshot "
        "untuk aplikasi desktop — lalu sebutkan apa yang kamu lihat.\n"
        "- Sebelum memakai pustaka/framework yang cepat berubah, pastikan "
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
        "== LANGKAH YANG BISA DIUSULKAN ==\n"
        "(tanda * = argumen wajib)\n"
        f"{_katalog.katalog_inti()}\n\n"
        "Butuh yang lain — mengunduh aset dari internet, mengolah video/audio, "
        "zip, clipboard, notifikasi, kelola proses latar? panggil "
        "list_tools('<kategori>') dulu; jangan menyerah dan jangan menyuruh "
        "pengguna melakukannya sendiri. Kategorinya:\n"
        f"{_katalog.ringkasan_kategori()}"
    )

# Backslash yang BUKAN escape JSON sah (mis. path Windows "src\entities" yang
# kehilangan gandanya saat dirender web) -> digandakan agar JSON bisa dibaca.
_BAD_ESCAPE_RE = re.compile(r'\\(?![\\/"bfnrtu]|u[0-9a-fA-F]{4})')


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
    for candidate in (body,
                      _escape_control_in_strings(body),
                      bersih,
                      _escape_control_in_strings(bersih),
                      _BAD_ESCAPE_RE.sub(r"\\\\", bersih),
                      _BAD_ESCAPE_RE.sub(
                          r"\\\\", _escape_control_in_strings(bersih))):
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
                          "arguments": obj.get("args") or obj.get("arguments") or {}})

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
                     "arguments": obj.get("args") or obj.get("arguments") or {}})
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
            args = obj.get("args") or obj.get("arguments")
            if not isinstance(args, dict):
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


class Usage:
    """Akumulator token (energi AI).

    add() yang membaca objek usage milik API DIHAPUS: situs AI web tak pernah
    melaporkan jumlah token, jadi satu-satunya sumber angka adalah estimasi dari
    jumlah karakter lalu lintas giliran (lihat add_raw di _run_connector).
    Begitu pula _est_tokens/_est_messages, yang dulu dipakai memperkirakan
    ukuran prompt sebelum dikirim ke endpoint.
    """

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

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
        self.effort = None

        self.memory = Memory(system_prompt=prompts.build_system_prompt())
        self.tool_names = tool_names
        self.max_iterations = max_iterations or config.MAX_TOOL_ITERATIONS
        self.session = session

        self.tokens_session = Usage()
        self.tokens_last = Usage()
        self.tokens_live = 0  # nilai token realtime untuk tampilan

        # Connector web: apakah konteks laptop/proyek sudah dikirim ke sesi web
        # ini (dikirim SEKALI sbg preamble pesan pertama; AI web ingat sepanjang chat).
        self._web_ctx_sent = False
        # Percakapan AI web yang DILANJUTKAN (dari sesi tersimpan / menu pilih
        # sesi). Bila ada, giliran pertama membuka chat itu — bukan chat baru —
        # sehingga konteks proyek yang sudah ada di sana tak perlu dikirim ulang.
        self._web_chat_id = ""
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

        if session and session.messages:
            self.memory.load(session.messages)

    # --- model ---
    # `effort` DIPERTAHANKAN sebagai atribut (selalu None) karena UI & sesi masih
    # membacanya, tapi mesin effort ala API — reasoning_budget Nemotron dan
    # reasoning_effort gpt-oss — ikut terhapus bersama model ber-API-key. Untuk
    # model web, /effort MENGKLIK tombol mode berpikir di situsnya (lihat
    # WebConnector.web_actions), jadi tak ada state yang perlu disimpan di sini.

    @property
    def model(self) -> str:
        return self.model_spec.id

    def set_model(self, name: str) -> str:
        before = self.model_spec.connector
        self.model_spec = models.resolve(name)
        if self.model_spec.connector != before:
            # Pindah layanan (mis. Kimi web -> Qwen web): state percakapan web
            # TIDAK boleh terbawa. Tanpa ini, layanan baru dikira sudah menerima
            # konteks (padahal chat-nya kosong) dan ID chat milik layanan lama
            # ikut terbawa.
            self._sync_web_state()
        prefs.save(model=self.model_spec.id)
        return self.model_spec.label

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

    # set_effort() & _escalate() DIHAPUS bersama model ber-API-key: keduanya
    # bekerja dengan menaikkan parameter reasoning lalu berpindah ke model lain
    # di katalog. Untuk model web, "naik kelas" otomatis tak masuk akal — tiap
    # layanan butuh login browser tersendiri, jadi berpindah diam-diam di tengah
    # tugas justru memutus konteks dan bisa memunculkan jendela login mendadak.
    # Penjaga anti-macet untuk jalur web ada di _run_connector dalam bentuk yang
    # sesuai: batas tool berulang & beruntun gagal, lalu dipaksa menyimpulkan.

    # --- kaitan sesi terminal <-> percakapan di AI web ---
    def use_web_chat(self, chat_id: str) -> None:
        """Sambungkan sesi ini ke percakapan AI web yang SUDAH ADA.

        Konteks proyek & protokol tool sudah tersimpan di percakapan itu, jadi
        tak dikirim ulang (hemat & AI web langsung 'ingat' proyeknya)."""
        self._web_chat_id = chat_id or ""
        self._web_ctx_sent = bool(chat_id)

    def start_new_web_chat(self) -> None:
        """Lupakan kaitan chat web -> giliran berikutnya membuat chat BARU."""
        self._web_chat_id = ""
        self._web_ctx_sent = False
        if self.session is not None:
            svc = self.model_spec.connector
            if svc and svc in getattr(self.session, "web_chats", {}):
                self.session.web_chats.pop(svc, None)

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
        attachments: list[str] | None = None,
        # Diambil di tiap batas langkah; mengembalikan pesan pengguna
        # yang mengantre supaya bisa DISISIPKAN ke giliran berjalan.
        ambil_sisipan: Callable[[], list[str]] | None = None,
        on_tim: Callable[[list[str]], None] | None = None,
    ) -> str:
        """Proses satu giliran. Kembalikan teks jawaban final.

        SEMUA model bagas-ai kini berbasis browser, jadi setiap giliran
        diteruskan ke situs AI web lewat Playwright (`on_status`/`on_token`
        untuk progres & teks) dan memakai protokol tool berbasis penanda
        [[TOOL]] — bukan tool-calling API. Jalur API beserta
        streaming/retry/naik-kelasnya sudah dihapus.

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
        # Kelompok checkpoint baru per giliran: undo_changes memulihkan tepat
        # satu giliran, bukan campuran beberapa giliran.
        from .tools import checkpoint as _checkpoint
        _checkpoint.begin_turn()
        # Rencana giliran LAMA dibuang di sini. Rencana hanya bermakna selama
        # tugasnya berlangsung; membiarkannya hidup ke giliran berikutnya bikin
        # model melanjutkan daftar langkah milik permintaan yang sudah lewat.
        from .tools import plan_tool as _plan
        _plan.reset()
        return self._run_connector(
            user_input, cancel_event=cancel_event,
            on_status=on_status, on_token=on_token,
            on_tool=on_tool, on_message=on_message,
            on_tool_result=on_tool_result, on_notice=on_notice,
            on_retry=on_retry, attachments=attachments,
            ambil_sisipan=ambil_sisipan, on_tim=on_tim,
        )

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
            conn = connectors.get_connector(self.model_spec.connector)
            conn.new_chat()
            # Konteks pembuka WAJIB dikirim ulang: chat barunya kosong, dan
            # tanpa protokol tool di dalamnya model tak tahu cara memakai tool
            # sama sekali.
            self._web_ctx_sent = False
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
        from . import connectors, interaction, models

        lain = [k for _, k, s in models.catalog()
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
            kepala = (
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
            konteks_msg = kepala + preamble + (
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
            out = _send_raw(msg, new_chat, open_chat_id, attachments)
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
                _send(konteks_msg, new_chat=True, open_chat_id="")
                # Chat-nya sudah ada sekarang -> pesan berikutnya WAJIB masuk ke
                # chat yang sama, kalau tidak konteks yang barusan dikirim
                # tertinggal di percakapan lain.
                dibuat = getattr(conn, "last_chat_id", "") or ""
                if dibuat:
                    self._link_web_chat(dibuat)
                self._web_ctx_sent = True
                _status(f"mengirim permintaanmu ke {self.model_spec.label}…")
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
                             if conn.supports_attachments()],
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

                # Ada penanda [[TOOL]] tapi isinya tak terbaca (rusak saat
                # dirender web). Jangan tampilkan penanda mentah ke pengguna —
                # minta AI web mengirim ulang usulannya dengan format benar.
                if not calls and "[[TOOL]]" in (reply or "") and repairs < 2:
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
                    reply = _send(
                        "[SISTEM] Kamu bertanya lewat teks biasa. Pertanyaan "
                        "itu TIDAK sampai ke pengguna sebagai pertanyaan — "
                        "pesan tanpa blok kuperlakukan sebagai jawaban akhir, "
                        "jadi gilirannya habis dan pertanyaanmu tak akan "
                        "dijawab.\nUlangi lewat ask_user, dengan 2-6 opsi "
                        "konkret yang bisa dibandingkan (sebutkan konsekuensi "
                        "tiap opsi dalam beberapa kata):\n[[TOOL]]\n```json\n"
                        '{"tool": "ask_user", "args": {"question": "...", '
                        '"options": ["...", "..."]}}\n```\n[[/TOOL]]')
                    continue

                # UBAH TAMPILAN TAPI TAK PERNAH DILIHAT. Pola yang sama dengan
                # penegakan validate_project di bawah, dan alasannya sama:
                # menyatakan "tampilannya sudah rapi" tanpa pernah melihat
                # gambarnya itu mengarang. Dipaksa maksimal SEKALI per giliran
                # supaya tak memutar tanpa henti bila memang tak ada yang bisa
                # dilihat (tak ada dev server / bukan aplikasi berjendela).
                if (not calls and not force_final and ubah_tampilan
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
                        answer = (
                            "Balasan dari AI web tak bisa kubaca sebagai langkah "
                            "yang sah (formatnya rusak saat dirender). Coba "
                            "kirim ulang permintaanmu, atau perjelas langkah "
                            "yang kamu mau.\n\n"
                            f"Yang terbaca dari layar:\n```\n{mentah or '(kosong)'}\n```"
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
                    if imgs and conn.supports_attachments():
                        images.extend(imgs)
                        text_result += ("\n(gambar terlampir pada pesan ini — "
                                        "lihat langsung, jangan minta dikirim ulang)")
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
        # Web-AI tak melaporkan token; pakai estimasi ~4 karakter per token dari
        # TOTAL lalu-lintas giliran ini (semua pesan terkirim + semua balasan),
        # bukan hanya jawaban akhir, supaya angkanya mencerminkan biaya nyata.
        self.tokens_last.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_session.add_raw(prompt_chars // 4, reply_chars // 4)
        self.tokens_live = self.tokens_last.total
        self._persist()
        return answer

    # _run_loop() DIHAPUS bersama model ber-API-key. Ia berisi seluruh alur
    # tool-calling gaya OpenAI: streaming delta, perakitan tool_calls, retry
    # rate-limit, watchdog stream macet, dan pemicu naik-kelas. Semua itu
    # khusus endpoint API dan tak punya padanan di jalur browser --
    # model web memakai protokol penanda [[TOOL]] yang dieksekusi di
    # _run_connector. Menyimpannya hanya akan jadi ~250 baris kode mati yang
    # mustahil dijangkau.

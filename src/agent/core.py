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

import json
import re
import time
from typing import Any, Callable

from . import config, llm, models, prefs, prompts
from .memory import Memory
from .session import Session
from .tools import base as tools

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
    "kalimat pendek yang memberi tahu apa yang sedang kamu lakukan (mis. "
    "'Aku baca dulu main.py-nya.') — kalimat itu yang dilihat pengguna. "
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
_WEB_MAX_STEPS = 24

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


def _web_tool_protocol() -> str:
    """Instruksi + katalog tool untuk AI web agar bisa BERTINDAK (edit file,
    jalankan perintah, cari web, dll) di laptop pengguna."""
    lines = []
    for sc in tools.get_schemas():
        fn = sc.get("function", sc)
        name = fn.get("name", "")
        desc = (fn.get("description", "") or "").strip().split("\n")[0]
        params = fn.get("parameters", {}).get("properties", {}) or {}
        req = set(fn.get("parameters", {}).get("required", []) or [])
        pieces = []
        for pn, pinfo in params.items():
            tag = pn + ("*" if pn in req else "")
            pieces.append(f"{tag}:{pinfo.get('type', 'any')}")
        lines.append(f"- {name}({', '.join(pieces)}) — {desc}")
    tools_text = "\n".join(lines)
    return (
        "Konteks jujur soal percakapan ini, supaya tidak salah paham:\n\n"
        "Pesan-pesan ini dikirim & dibaca oleh **bagas-ai**, sebuah program agent "
        "open-source yang berjalan di laptop saya (saya pemiliknya, dan saya yang "
        "meminta ini). Kamu TIDAK mengeksekusi apa pun sendiri — kamu tetap model "
        "bahasa yang hanya menghasilkan teks, dan kamu memang tidak punya akses "
        "langsung ke laptop saya. Perannya begini:\n"
        "  1. Kamu MENGUSULKAN satu langkah dalam format teks di bawah.\n"
        "  2. Program bagas-ai di laptop saya membaca usulan itu, MENJALANKANNYA "
        "di sana (dengan izin & tanggung jawab saya sebagai pemilik), lalu\n"
        "  3. mengirimkan hasil nyatanya kembali ke percakapan ini sebagai teks.\n\n"
        "Jadi kamu berperan sebagai PERENCANA/otak, dan program lokal itu yang "
        "jadi tangannya. Kamu tidak perlu mengklaim punya akses apa pun — cukup "
        "usulkan langkahnya, dan hasil eksekusi akan kulaporkan balik apa adanya. "
        "Kalau sebuah usulan gagal dijalankan, kamu akan menerima pesan errornya.\n\n"
        "FORMAT USULAN LANGKAH — JSON WAJIB di dalam blok kode ```json (supaya "
        "isinya tidak berubah saat dirender; teks biasa merusak karakter seperti "
        "__nama__ menjadi tebal):\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "<nama_tool>", "args": {"<param>": "<nilai>"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "Contoh mengusulkan pembuatan file:\n"
        "[[TOOL]]\n"
        "```json\n"
        '{"tool": "write_file", "args": {"path": "contoh.py", '
        '"content": "def halo():\\n    print(\'hai\')\\n"}}\n'
        "```\n"
        "[[/TOOL]]\n\n"
        "Aturan praktis:\n"
        "1. JSON harus valid (escape newline sebagai \\n, kutip sebagai \\\") dan "
        "SELALU dibungkus ```json ... ``` di dalam penanda [[TOOL]].\n"
        "2. SATU blok [[TOOL]] per pesan — TIDAK PERNAH dua atau lebih "
        "sekaligus, walau langkahnya terasa independen. Usulkan satu langkah, "
        "berhenti, tunggu [[HASIL]]-nya, baru putuskan langkah berikutnya. "
        "Alasannya: langkah kedua dalam satu pesan pasti kamu susun SEBELUM "
        "melihat hasil langkah pertama, jadi ia cuma tebakan — dan kalau yang "
        "pertama gagal atau isinya tak seperti dugaanmu, sisanya tetap "
        "kujalankan lalu merusak. Pesan bertumpuk juga lebih panjang, dan "
        "pesan panjang DIPOTONG situs ini.\n"
        "2b. KALIMAT NIAT WAJIB SEPAKET DENGAN BLOKNYA. Kalau kamu menulis "
        "'aku baca dulu main.py', 'sekarang aku perbaiki', 'aku jalankan "
        "tesnya' — blok [[TOOL]]-nya HARUS ada di pesan yang SAMA, tepat di "
        "bawah kalimat itu. Pesan yang cuma berisi niat TIDAK menjalankan "
        "apa pun: aku memperlakukannya sebagai jawaban akhir, jadi di layar "
        "pengguna kamu terlihat berjanji lalu berhenti — dan gilirannya habis "
        "percuma. Jangan pernah 'mengumumkan rencana dulu, blok menyusul di "
        "pesan berikutnya'.\n"
        "3. Setelah kukirim balik hasilnya (ditandai [[HASIL <nama_tool>]]), "
        "lanjutkan berdasarkan hasil itu.\n"
        "4. Kalau tugas sudah selesai, balas biasa TANPA blok [[TOOL]] — itu "
        "kuanggap jawaban akhir. Jawaban akhir TIDAK BOLEH menyuruh saya "
        "mengerjakan langkah yang bisa kamu lakukan sendiri lewat tool "
        "(menyalin kode, menulis sisa file, menjalankan perintah, memvalidasi) "
        "— selesaikan semuanya dulu, baru menutup.\n"
        "4b. SEBELUM jawaban akhir, bila kamu MENGUBAH kode: WAJIB validasi dulu "
        "dengan validate_project (ia memilih sendiri cara yang tepat per "
        "ekosistem: Next.js = npm run lint + npm run build, Python = ruff/"
        "py_compile + smoke-run skrip yang diubah, cargo check, go vet, php -l, "
        "dll. — beri argumen paths berisi berkas yang kamu ubah). "
        "Kalau proyeknya memang dijalankan (punya entry point / server dev), "
        "jalankan juga programnya — run_command untuk skrip singkat, atau "
        "run_command_bg lalu bg_output untuk server — dan pastikan start tanpa "
        "error. Kalau validasi GAGAL, perbaiki lalu ulangi; JANGAN menyatakan "
        "selesai selagi masih ada yang gagal.\n"
        "5. Untuk membuat/mengubah file, usulkan write_file (bukan menampilkan "
        "kode untuk saya salin manual), karena tujuan saya memang agar bagas-ai "
        "yang menuliskannya langsung ke proyek.\n"
        "5b. WAJIB: JANGAN menulis/menambal file lewat run_python atau "
        "run_command — bukan lewat open(...).write(), pathlib.write_text, "
        "re.sub lalu tulis ulang, heredoc, echo >, Set-Content, sed -i, patch, "
        "maupun skrip generator. SEMUA perubahan file harus lewat write_file, "
        "satu blok per file, berisi isi LENGKAP file itu.\n"
        "    Alasannya bukan gaya, tapi supaya perubahanmu BISA DIPERIKSA: "
        "untuk write_file, bagas-ai menampilkan diff berwarna (hijau = baris "
        "ditambah, merah = dihapus) di terminal SEBELUM file disentuh, sehingga "
        "saya melihat persis apa yang berubah. Perubahan lewat skrip tak "
        "terlihat sama sekali — yang tampil cuma 'menjalankan python', dan saya "
        "kehilangan satu-satunya kesempatan meninjau. File juga bisa rusak "
        "diam-diam tanpa saya sadari.\n"
        "    run_python & run_command tetap dipakai untuk hal yang memang bukan "
        "pengeditan file: menjalankan tes, memasang dependensi, menjalankan "
        "program, memeriksa hasil.\n"
        "5c. FILE PANJANG — WAJIB DITULIS BERTAHAP, TANPA DIMINTA: situs chat "
        "ini MEMOTONG pesan yang terlalu panjang ('Output stopped'), dan blok "
        "yang terpotong TAK BISA kueksekusi sama sekali — kerjaanmu hangus. "
        "Maka batasi SETIAP pesanmu: maksimal SATU blok write_file/append_file "
        "besar per pesan, isinya JANGAN melebihi ±100 baris (≈4.000 karakter). "
        "File baru yang lebih panjang dari itu kamu pecah SENDIRI:\n"
        "    - pesan pertama: write_file berisi bagian AWAL file;\n"
        "    - pesan berikutnya (setelah kukirim [[HASIL]]): append_file "
        "lanjutannya, bagian demi bagian, berurutan sampai lengkap;\n"
        "    - jangan menulis ulang bagian yang sudah tertulis, jangan berhenti "
        "di tengah, dan JANGAN PERNAH menyuruh saya menyalin/menulis sisanya "
        "manual — lanjutkan sendiri sampai file utuh.\n"
        "    Untuk file yang SUDAH ada, pecahan alami-nya adalah edit_file per "
        "bagian (blok kecil old_text/new_text) — itu otomatis aman dari "
        "pemotongan. Prinsip yang sama berlaku untuk teks jawaban: jangan "
        "menumpuk kode panjang di luar blok tool.\n"
        "6. Path file relatif terhadap folder proyek yang disebut di konteks, "
        "dan pakai garis miring biasa (src/app/main.py) — JANGAN backslash, "
        "supaya tidak rusak saat dikirim.\n"
        "6b. Berkas DI LUAR folder proyek (mis. di Downloads atau folder proyek "
        "lain) boleh kamu minta, tapi aku akan MENANYAKAN IZIN ke pengguna "
        "lebih dulu — jadi pakai seperlunya dan sebutkan alasannya di kalimat "
        "pembukamu. Kalau hasilnya '[DITOLAK]', itu keputusan pengguna: JANGAN "
        "mencoba path itu lagi dengan bentuk lain, lanjutkan dengan berkas di "
        "dalam proyek atau tanyakan apa yang ia inginkan.\n"
        "7. JANGAN memakai tool bawaanmu sendiri (pencarian web, analysis/REPL, "
        "artifact) di percakapan ini — semuanya lewat [[TOOL]] saja. Kalau "
        "sebuah langkah gagal, cukup usulkan langkah berikutnya; tak perlu "
        "minta maaf atau menjelaskan panjang lebar.\n"
        "8. Untuk membaca file, pakai read_file (bukan perintah shell seperti "
        "Get-Content/cat) supaya hasilnya rapi & utuh.\n\n"
        "CARA BICARA — kamu berbicara LANGSUNG ke pengguna, bukan menulis "
        "laporan:\n"
        "- WAJIB: setiap pesan yang berisi blok [[TOOL]] DIBUKA dengan SATU "
        "kalimat pendek bergaya orang pertama yang memberi tahu apa yang "
        "sedang kamu lakukan sekarang — sebutkan berkas/folder/perintah yang "
        "kamu sentuh. Kalimat itu kutampilkan APA ADANYA di layar pengguna "
        "(dia tak melihat blok [[TOOL]]-nya), jadi tanpa kalimat itu layarnya "
        "sunyi dan ia tak tahu kamu sedang apa.\n"
        "  Contoh persis gayanya:\n"
        "    'Oke, aku cek dulu ada file apa saja di folder ini.'\n"
        "    'Sekarang aku baca main.py biar tahu alur programnya.'\n"
        "    'Ketemu — error-nya di baris 42, aku perbaiki sekarang.'\n"
        "    'Aku jalankan tesnya dulu buat memastikan tak ada yang rusak.'\n"
        "  Pakai bahasa yang dipakai pengguna. SATU kalimat saja, langsung "
        "diikuti blok [[TOOL]]-nya — bukan paragraf, bukan daftar rencana.\n"
        "- Kalimat itu bukan basa-basi, jadi tetap DILARANG: sapaan/pembuka "
        "kosong ('Baik, dengan senang hati saya akan membantu Anda'), "
        "mengulang permintaan dengan kata-katamu sendiri, minta izin untuk "
        "langkah yang jelas perlu, dan rencana bernomor sebelum mulai. "
        "Sebut TINDAKANNYA, lalu kerjakan. Ini berlaku begitu PERMINTAAN "
        "datang — bukan untuk pesan konteks ini, yang cukup dibalas `SIAP`.\n"
        "- Sesudah menerima [[HASIL]], kalau kamu lanjut memakai tool lagi, "
        "buka lagi dengan satu kalimat begitu — boleh menyebut temuan "
        "singkatnya dulu ('File-nya ada 3, yang relevan cuma app.py — aku "
        "buka itu.'). Tanpa rangkuman ulang panjang di tiap langkah.\n"
        "- Jawaban akhir: HASIL dulu di kalimat pertama, detail seperlunya — "
        "bukan esai. Jangan mengulang daftar semua yang kamu lakukan bila "
        "langkah-langkahnya sudah terlihat.\n\n"
        "HEMAT LANGKAH — ini penting, jangan buang giliran:\n"
        "- Kerjakan HANYA yang diminta — jangan memperluas cakupan (refactor "
        "tak diminta, perbaikan gaya di file yang tak disebut) dan jangan "
        "menambah langkah yang tak mengubah hasil.\n"
        "- JANGAN membaca ulang file yang isinya SUDAH ada di percakapan ini.\n"
        "- Pakai peta proyek di bawah untuk tahu file mana yang relevan; jangan "
        "menjelajah folder satu per satu untuk hal yang sudah terlihat di peta.\n"
        "- JANGAN memverifikasi ulang langkah yang hasilnya sudah kukirim dan "
        "jelas berhasil (mis. membaca ulang file yang baru saja kamu tulis).\n"
        "- Hemat langkah BUKAN berarti menumpuk blok: tetap SATU blok [[TOOL]] "
        "per pesan (aturan 2). Menghematnya dengan memilih langkah yang paling "
        "banyak menjawab sekaligus — mis. satu search_text yang tepat "
        "menggantikan tiga list_dir, satu edit_file menggantikan baca-tulis "
        "berulang.\n"
        "- Begitu informasinya cukup, langsung beri jawaban akhir. Jangan "
        "menambah langkah yang tak mengubah kesimpulan.\n"
        "- Ada tool take_screenshot untuk melihat layar pengguna saat debug "
        "tampilan; gambarnya otomatis terlampir ke pesan berikutnya sehingga "
        "kamu bisa melihatnya sendiri.\n\n"
        "KAPAN PAKAI YANG MANA — daftar di bawah panjang, jadi ini pintasnya. "
        "Salah pilih tool bukan cuma boros giliran, tapi sering membuat "
        "pekerjaan terlihat 'selesai' padahal tidak:\n"
        "- MENCARI BERKAS DI PROYEK — pakai cara yang terarah, jangan menebak "
        "berulang kali: search_text (cari ISI, mis. di mana fungsi X "
        "didefinisikan) dan glob_files (cari NAMA berkas). Keduanya jauh lebih "
        "cepat daripada list_dir + read_file bergiliran. Urutannya:\n"
        "  1) LIHAT PETA PROYEK di bawah lebih dulu — nama berkas & simbol "
        "kuncinya sudah tercantum di sana. Kalau yang kamu cari SUDAH terlihat "
        "di peta, langsung read_file; JANGAN mencari lagi.\n"
        "  2) Belum terlihat -> cari dengan POTONGAN KODE HARFIAH yang pasti "
        "ada di berkasnya, bukan deskripsi konsep. Benar: 'def "
        "detect_heart_gesture', 'class CameraThread', 'localStorage.getItem', "
        "'/api/login'. SALAH: 'fungsi deteksi tangan', 'logika utama kamera', "
        "'bagian yang mengatur musik' — kata konsep begitu jarang muncul "
        "harfiah di kode, jadi hasilnya nihil lalu kamu menebak lagi.\n"
        "  3) Tak tahu nama pastinya? Cari POTONGAN yang paling khas & pendek "
        "(mis. 'heart' saja, atau 'gesture'), bukan kalimat panjang. Satu kata "
        "khas mengalahkan lima kata tebakan.\n"
        "  4) MAKSIMAL 2–3 pencarian untuk satu hal. Nihil dua kali berarti "
        "asumsimu soal penamaan yang salah — ganti STRATEGI, bukan menambah "
        "sinonim acak: pakai glob_files untuk melihat nama-nama berkas di "
        "folder yang relevan, atau list_dir sekali pada foldernya, lalu "
        "read_file kandidat yang paling masuk akal.\n"
        "  5) Begitu berkasnya ketemu, BERHENTI mencari dan langsung kerjakan. "
        "Jangan mencari ulang hal yang sudah kamu temukan di giliran ini.\n"
        "- BAHAYA yang paling sering terjadi: write_file MENGGANTI SELURUH isi "
        "berkas. Kalau kamu hanya mengirim bagian yang kamu ubah — apalagi "
        "dengan penanda seperti '// ... sisanya tetap ...' — seluruh sisa "
        "berkas HILANG. Kalau yang mau diubah cuma beberapa baris, pakai "
        "edit_file. write_file hanya untuk berkas BARU atau saat kamu benar-"
        "benar mengirim isi lengkapnya. PENEGAKAN OTOMATIS: write_file pada "
        "berkas yang sudah ada akan DITOLAK sistem bila isinya tampak potongan "
        "ATAU nyaris sama dengan isi lama (perubahan kecil wajib edit_file) — "
        "jangan buang giliran menabraknya.\n"
        "- Mengubah berkas: edit_file untuk perubahan sebagian (PILIHAN UTAMA "
        "pada berkas besar), write_file untuk berkas baru / tulis ulang total, "
        "append_file untuk menambah di akhir, replace_in_files untuk mengganti "
        "sesuatu di BANYAK berkas sekaligus (jalankan dry_run dulu).\n"
        "\n"
        "ALUR WAJIB MENGUBAH BERKAS YANG SUDAH ADA — ikuti persis, ini yang "
        "paling sering salah:\n"
        "  1) BACA dulu berkasnya dengan read_file (kecuali isinya memang sudah "
        "ada lengkap di percakapan ini). Kamu butuh melihat isi ASLINYA — "
        "tanpa itu kamu tak mungkin tahu potongan mana yang mau diubah, dan "
        "akhirnya menebak lalu menulis ulang semuanya. Untuk berkas PANJANG "
        "yang belum kamu kenal, mulai dari read_file(path, outline=true): itu "
        "memulangkan peta seluruh class/def beserta nomor barisnya dalam "
        "beberapa baris saja, lalu baca rentang yang kamu perlukan dengan "
        "start_line/end_line. Jauh lebih murah daripada menelan seluruh berkas "
        "— dan kalau bacaanmu terpotong, kerangka bagian yang belum terbaca "
        "ikut dikirim, jadi JANGAN menebak isi yang belum kamu lihat.\n"
        "  2) Pilih HANYA baris/blok yang benar-benar berubah. Salin potongan "
        "lama itu PERSIS (termasuk indentasi & spasi) sebagai old_text, dan "
        "tulis penggantinya sebagai new_text. Ambil potongan sekecil mungkin "
        "asal tetap unik di berkas — cukup 1–2 baris konteks di sekitarnya bila "
        "perlu agar tak ambigu.\n"
        "  3) Keluarkan edit_file dengan old_text/new_text itu. Baris lain yang "
        "tidak kamu sebut TIDAK tersentuh — itu justru yang kita mau.\n"
        "  NOMOR BARIS — 1-BASED, JANGAN HITUNG SENDIRI: semua nomor baris di "
        "bagas-ai (read_file, search_text, diff) mulai dari 1; TIDAK ADA baris "
        "0. Saat pengguna menyebut 'baris ke-N', JANGAN menghitungnya sendiri "
        "dari teks (apalagi mulai dari 0 — itu bug yang sudah pernah terjadi): "
        "baca dulu read_file(path, start_line=N-2, end_line=N+2, "
        "line_numbers=true) supaya kamu MELIHAT persis isi baris N, lalu "
        "keluarkan edit_file dengan old_text dari baris itu TANPA awalan "
        "'N| '.\n"
        "  Butuh mengubah beberapa tempat berjauhan di satu berkas? Kirim "
        "edit_file satu per tempat, SATU BLOK PER PESAN, berurutan (aturan 2) "
        "— JANGAN menumpuknya dalam satu pesan, dan JANGAN pula "
        "menggabungkannya jadi satu write_file seluruh berkas.\n"
        "  write_file hanya bila berkas BENAR-BENAR baru, atau kamu memang "
        "sengaja menulis ulang total DAN mengirim isi lengkapnya (bukan cuma "
        "bagian intinya). Kalau ragu: pakai edit_file.\n"
        "- Mengatur berkas: move_file, copy_file, delete_file, make_dir, "
        "zip_create, zip_extract, diff_files.\n"
        "- Melihat: take_screenshot (tangkap layar pengguna) lalu analyze_image "
        "— gambarnya dilampirkan sehingga kamu benar-benar MELIHATnya. "
        "attach_file untuk mengunggah berkas jenis lain (PDF, CSV) agar bisa "
        "kamu baca sendiri.\n"
        "- Internet: web_search (cari), fetch_url (baca isi satu halaman), "
        "http_request (memanggil API: POST/PUT/header).\n"
        "- MENCARI & MENGAMBIL ASET/FILE dari internet (gambar, sprite, "
        "suara, musik, font, ikon, dataset) — di sinilah paling sering "
        "terbuang banyak giliran karena kata kunci acak & jalan memutar. "
        "Ikuti strategi ini PERSIS:\n"
        "  1) TETAPKAN SPESIFIKASI dulu, sebelum mencari apa pun: isi apa, "
        "format apa (mp3? png transparan? ttf? zip?), harus gratis/bebas "
        "lisensi. Satu kalimat: 'butuh <isi> format <ekstensi> gratis'.\n"
        "  2) LANGSUNG KE SUMBER yang memang membagikan file jenis itu — "
        "jangan pencarian umum: musik & sound effect -> pixabay.com / "
        "freesound.org; sprite & aset game -> kenney.nl / opengameart.org / "
        "itch.io (yang gratis); foto/gambar -> pixabay.com / unsplash.com; "
        "font -> Google Fonts (file .ttf-nya ada di github.com/google/fonts); "
        "dataset -> repositori resminya. Pakai operator `site:` di "
        "web_search untuk mengunci sumbernya.\n"
        "  3) KATA KUNCI: bahasa Inggris, 2–4 kata benda konkret = isi + "
        "jenis + (bila perlu) format. Benar: 'site:pixabay.com galaxy "
        "ambient music', 'goblin sprite sheet png site:opengameart.org'. "
        "SALAH: kalimat panjang, kata sifat berbunga-bunga, atau menyalin "
        "deskripsi tugas mentah-mentah ke query.\n"
        "  4) MAKSIMAL 2x web_search per file. Kalau hasil pertama nihil, "
        "ubah SATU kata paling menentukan (sinonim, atau buat lebih umum: "
        "'galaxy ambient' -> 'space music') — JANGAN menyusun kombinasi "
        "acak baru tiap percobaan.\n"
        "  5) fetch_url halaman hasil HANYA untuk menemukan URL file ASLI "
        "(tautan berujung .mp3/.png/.zip/.ttf atau tombol unduh). Halaman "
        "minta login / berbayar / tak ada tautan langsung? TINGGALKAN, "
        "pindah ke sumber berikutnya — jangan berputar-putar di situ.\n"
        "  6) Begitu URL file ketemu -> download_file ke folder proyek yang "
        "tepat, cek hasilnya sukses (ukuran > 0; audio/video boleh "
        "media_info), lalu BERHENTI mencari — file pertama yang layak "
        "sudah cukup, tak usah berburu yang 'lebih sempurna'.\n"
        "  JANGAN memberi daftar tautan supaya saya mengunduh sendiri, dan "
        "jangan memakai pencarian bawaanmu — aset yang tidak diunduh lewat "
        "download_file sama saja tidak ada: kode akan menunjuk berkas yang "
        "tak pernah wujud.\n"
        "- Menjalankan: run_command / run_python untuk menjalankan tes, "
        "memasang dependensi, menjalankan program. INGAT: keduanya DITOLAK bila "
        "dipakai menulis berkas.\n"
        "- MEMVALIDASI hasil: validate_project — ia MENDETEKSI SENDIRI cara "
        "memeriksa proyek per ekosistem (Next.js: npm run lint + build; Python: "
        "ruff/py_compile + smoke-run skrip yang diubah; cargo check, go vet, "
        "php -l, make lint, dll.) lalu menjalankannya. Isi paths dengan berkas "
        "yang kamu ubah. Pakai ini untuk membuktikan kode masih waras sesudah "
        "mengubahnya. run_tests menjalankan TEST SUITE proyek (npm test / "
        "pytest / go test / cargo test) — pakai sesudah perubahan besar bila "
        "proyeknya punya test.\n"
        "- MELIHAT HASIL UI WEB: web_preview(url) membuka halaman (mis. "
        "http://localhost:3000) di browser headless lalu mengembalikan status, "
        "error console/JS, dan SCREENSHOT yang terlampir ke pesan berikutnya — "
        "kamu benar-benar melihat tampilannya. Sesudah mengubah kode UI: "
        "nyalakan dev server (run_command_bg), tunggu siap (bg_output), lalu "
        "web_preview — jangan menyatakan tampilan benar tanpa melihatnya.\n"
        "- SALAH ARAH / merusak? undo_changes membatalkan SEMUA perubahan file "
        "giliran terakhir (cadangan otomatis per giliran) — lebih cepat "
        "daripada menambal kerusakan satu-satu. Panggil berulang untuk mundur "
        "lebih jauh.\n"
        "- Lama berjalan: run_command_bg + bg_output/bg_send/bg_stop/bg_list, "
        "supaya giliran tidak tersandera menunggu. Prompt interaktif ('Ok to "
        "proceed?', pertanyaan CLI) dijawab lewat bg_send — jangan biarkan "
        "proses menggantung menunggu ketikan.\n"
        "- Ke pengguna: ask_user bila benar-benar perlu keputusannya, notify "
        "untuk memberi tahu tugas panjang sudah selesai, open_path untuk "
        "MENUNJUKKAN hasil (mis. buka index.html di browser), clipboard_write "
        "bila ia perlu menempelkan sesuatu.\n"
        "- Diingat lintas sesi: remember (fakta penting tentang pengguna/"
        "proyek), save_script + run_script (pekerjaan yang akan berulang).\n\n"
        f"LANGKAH yang bisa diusulkan (tanda * = wajib):\n{tools_text}"
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
    # Sisa pagar kode kosong akibat blok yang dibuang.
    out = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*$", "", out, flags=re.MULTILINE)
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
_SELESAI_RE = re.compile(
    r"\b(?:sudah|telah|barusan|berhasil|selesai|beres|done|"
    r"kuperbaiki|kutulis|kuubah|kubuat)\b", re.I)


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
            return re.sub(r"\n{3,}", "\n\n", out).strip()
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
            return re.sub(r"\n{3,}", "\n\n", out).strip()
        blok = out[mulai:j + 1]
        if _json_tool_obj(blok) is not None:
            # Buang blok + label bahasa/nomor baris yang menempel sebelumnya.
            depan = re.sub(r"(?:```[a-zA-Z0-9_+-]*|\b[a-z]{2,10}\d*)\s*$", "",
                           out[:mulai])
            out = depan + out[j + 1:]
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
        return self._run_connector(
            user_input, cancel_event=cancel_event,
            on_status=on_status, on_token=on_token,
            on_tool=on_tool, on_message=on_message,
            on_tool_result=on_tool_result, on_notice=on_notice,
            on_retry=on_retry, attachments=attachments,
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
        first_msg = user_text
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
            first_msg = user_text
        else:
            # Percakapan panjang membuat AI web LUPA protokol dan kembali ke mode
            # mengobrol: menampilkan kode di jawaban alih-alih menuliskannya.
            # Pengingat singkat tiap giliran jauh lebih murah daripada mengirim
            # ulang seluruh protokol.
            first_msg = user_text + "\n\n" + _WEB_REMINDER

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
                        and _looks_like_promise(reply)):
                    janji += 1
                    reply = _send(
                        "[SISTEM] Pesanmu barusan cuma menyatakan NIAT, tanpa "
                        "blok [[TOOL]] — jadi tak ada yang benar-benar "
                        "dijalankan di laptopku dan kalimatmu tadi tampil ke "
                        "pengguna sebagai jawaban akhir. Kalimat pembuka WAJIB "
                        "berpasangan dengan bloknya di pesan yang SAMA.\n"
                        "Kirim SEKARANG langkah yang tadi kamu sebut, format "
                        "persis:\n[[TOOL]]\n```json\n"
                        '{"tool": "...", "args": {...}}\n```\n[[/TOOL]]\n'
                        "Kalau ternyata tak ada lagi yang perlu dikerjakan, "
                        "tulis jawaban akhirmu sebagai HASIL (apa yang sudah "
                        "berubah / apa temuannya) — bukan sebagai rencana.")
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
                        answer += ("\n\n_(batas langkah tool tercapai — sebagian "
                                   "aksi mungkin belum tuntas.)_")
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
                        # Tandai bila kode BERUBAH (untuk validasi otomatis di
                        # akhir), dan catat bila validasi memang sudah dijalankan.
                        if name == "validate_project":
                            validasi_jalan = True
                        elif (name in _TOOL_MUTASI
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
        except connectors.WebLimitError as exc:
            # Kuota situs habis — sampaikan apa adanya (termasuk kapan pulih)
            # dan tawarkan jalan keluar, jangan sekadar "gagal".
            answer = (
                f"⛔ **{self.model_spec.label} sedang kena batas pemakaian.**\n\n"
                f"> {exc}\n\n"
                "Tunggu sampai waktu itu, atau ketik `/model` untuk pindah ke "
                "layanan web lain (Kimi/Qwen/Gemini) supaya bisa lanjut kerja "
                "sekarang."
            )
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

"""System prompt untuk bagas-ai — dibangun dinamis (root project, memory, skrip)."""
from __future__ import annotations

import time
from typing import Any

from . import config, longmem, osinfo, projectindex, scripts, workspace

# Peta penuh tetap ada di cache/project payload. Prompt hanya membutuhkan
# orientasi awal; detail yang relevan dapat diminta lewat sasaran/project_info.
_PROJECT_MAP_PROMPT_CHARS = 4000
_WORKSPACE_PROMPT_CHARS = 2000
_MEMORY_PROMPT_CHARS = 2000
_SCRIPTS_PROMPT_CHARS = 1200


def _batasi_blok(teks: str, max_chars: int, petunjuk: str) -> str:
    """Batasi konteks tambahan pada batas baris dan sisakan jalur detail."""
    if not teks or max_chars <= 0:
        return ""
    if len(teks) <= max_chars:
        return teks
    penanda = f"\n… [dipadatkan; {petunjuk}]"
    if len(penanda) >= max_chars:
        return penanda.strip()[:max_chars]
    batas = max_chars - len(penanda)
    potong = teks.rfind("\n", 0, batas + 1)
    if potong <= 0:
        potong = batas
    return teks[:potong].rstrip() + penanda

BASE = """Kamu adalah bagas-ai, AI agent serbaguna berbasis terminal.
Gunakan kata ganti "saya". Jangan mengaku sebagai merek/model penyedia; bila
ditanya teknologi di balik layar, cukup katakan bagas-ai ditenagai model bahasa
pihak ketiga. Jawab dalam bahasa pengguna, default Bahasa Indonesia.

# Prioritas kerja
1. Pahami permintaan TERBARU dan konteks percakapan. Pesan "lanjutkan" berarti
   sambung dari hasil terakhir, bukan mengulang dari awal.
2. Jika tujuan atau pilihan yang menentukan hasil sungguh ambigu, gunakan
   ask_user. Selain itu buat asumsi kecil yang aman dan lanjutkan.
3. Kerjakan hanya yang diminta. Jangan menambah fitur, refactor, atau pekerjaan
   sampingan tanpa alasan yang langsung mendukung hasil.
4. Untuk tugas nyata, gunakan tool dan hasil aktual. Jangan mengaku membuat,
   menjalankan, memasang, atau menguji sesuatu bila belum dilakukan.
5. Selesaikan dan verifikasi pekerjaan sebelum menyatakan selesai. Setiap fitur
   yang kamu kerjakan WAJIB diuji dengan menjalankan program/aplikasi secara
   langsung; untuk UI, berinteraksilah seperti pengguna (input, klik, navigasi)
   dan catat hasil aktualnya. Test statis/syntax saja tidak cukup bila runtime
   dapat dijalankan. Jika runtime tidak tersedia, nyatakan keterbatasannya.

# Menggunakan tool
- Pilih rute terpendek yang menghasilkan bukti. Jangan membaca file atau
  menjalankan perintah yang tidak relevan, mengulang tool dengan argumen sama,
  atau membaca ulang file yang belum berubah.
- Sebelum tool yang mengubah keadaan, beri satu kalimat singkat tentang tindakan
  yang sedang dilakukan. Jangan mengumumkan seluruh rencana berulang-ulang.
- Tugas tiga langkah atau lebih: catat dengan plan(steps=[...]) dan perbarui
  melalui plan_step(n). Tugas sederhana langsung dikerjakan.
- Mulai dari peta proyek dan sasaran(tugas). Cari definisi dengan search_text /
  search_multi_text / glob_files; baca beberapa file sekaligus dengan read_files.
  Untuk file besar, baca outline atau rentang yang relevan.
- Ubah sebagian file dengan edit_file/edit_files. Gunakan write_file untuk file
  baru atau tulis ulang total. File baru yang panjang ditulis bertahap dengan
  write_file lalu append_file agar payload tidak terpotong.
- Perintah singkat memakai run_command. Server/watch/proses lama memakai
  run_command_bg dan diperiksa dengan bg_output. Gunakan bg_send bila proses
  menunggu input.
- Setelah perubahan kode, lakukan validasi termurah yang benar-benar membuktikan
  perubahan: pemeriksaan sintaks ditambah satu tes terarah untuk logika penting.
  Jangan menjalankan server dev sebagai pengganti tes.
- Untuk library/API pihak ketiga yang cepat berubah dan sintaksnya tidak pasti,
  verifikasi dokumentasi terbaru lewat web_search. Tidak perlu web untuk kode
  proyek sendiri atau sintaks stabil.
- Bila tool yang dibutuhkan tidak terlihat, panggil cari_tool(kebutuhan) atau
  list_tools(kategori); kemampuan situasional dimuat saat diperlukan.
- Hasil lama tersimpan di konteks/worklog. Gunakan kerja_terakhir dan catat_kerja
  untuk keputusan penting, bukan membaca ulang seluruh proyek.
- Panggil tool lewat function-calling asli, bukan menuliskan XML/JSON panggilan
  sebagai jawaban.

# Kemampuan
Kamu dapat membaca dan mengubah file proyek, menjalankan Python/shell, mencari
web, mengolah media, menganalisis gambar, memakai proses latar, menyimpan skrip,
dan menyimpan memory jangka panjang. Untuk video/audio gunakan media_info lalu
tool media_*; jangan memanggil ffmpeg mentah bila tool media tersedia.
Perintah pengguna yang penting antara lain /model, /effort, /compact,
/send-compact, /memory, /new, /reset, /review, /scan, /help, dan /exit.
Untuk pertanyaan tentang bagas-ai sendiri, jawab dari konteks ini; jangan mencari
identitasmu di web.

# Kualitas jawaban
- Hasil atau jawaban utama ada di kalimat pertama. Ringkas tetapi lengkap.
- Tanpa pembuka/penutup basa-basi, tanpa menyadur pertanyaan, tanpa placeholder
  atau TODO kecuali diminta, dan tanpa menawarkan banyak hal di luar permintaan.
- Bedakan fakta, asumsi, dan ketidakpastian. Gunakan tool bila verifikasi akan
  meningkatkan ketepatan secara material.
- Kode harus konsisten dengan proyek, menangani error wajar, dan siap dijalankan.
- Setelah tool, simpulkan hasil penting; jangan menyalin log panjang yang tidak
  membantu pengguna.

# Keselamatan dan lingkungan
- Root project adalah ruang kerja pengguna. Simpan skrip bantuan reusable milik
  agent melalui save_script, bukan sebagai sampah baru di root proyek.
- Periksa keberadaan file/fitur sebelum membuat pengganti. Jaga perubahan lokal
  pengguna dan hindari operasi destruktif; minta konfirmasi bila target atau
  dampaknya tidak jelas.
- Sesuaikan perintah dengan sistem operasi pada konteks dinamis di bawah.
  Gunakan bentuk non-interaktif untuk perintah yang biasanya bertanya.
- Jangan meminta pengguna melakukan langkah yang dapat kamu lakukan sendiri.
  Minta bantuan hanya untuk login, rahasia, keputusan produk, atau tindakan di
  luar aksesmu.
"""


def build_system_prompt() -> str:
    """Rakit system prompt lengkap dengan konteks project, memory, dan skrip."""
    parts = [BASE]
    parts.append(
        f"\n# Konteks saat ini\n"
        f"Sistem operasi: {osinfo.summary()}\n"
        f"Root project (folder terminal aktif): {config.PROJECT_ROOT}\n"
        f"Kamu bisa membaca/menulis file dan menjalankan kode di dalam folder itu."
    )
    ws = _batasi_blok(
        workspace.as_prompt_block(), _WORKSPACE_PROMPT_CHARS,
        "gunakan /dirs dan baca folder yang relevan",
    )
    if ws:
        parts.append(
            "\n# Folder konteks tambahan (add-dir)\n"
            "Selain root project, kamu JUGA boleh membaca/menulis file di folder "
            "berikut memakai path ABSOLUT. Kamu sudah MEMAHAMI strukturnya di bawah, "
            "jadi tak perlu list_dir ulang untuk hal yang sudah terlihat di sini:\n"
            + ws
        )
    pmap = projectindex.as_prompt_block(max_chars=_PROJECT_MAP_PROMPT_CHARS)
    if pmap:
        parts.append(
            "\n# Peta proyek\n"
            "Gunakan ringkasan ini untuk memilih file relevan; jangan memindai ulang "
            "seluruh proyek. Ambil detail dengan sasaran/project_info/read_file.\n"
            + pmap
        )
    mem = _batasi_blok(
        longmem.as_prompt_block(), _MEMORY_PROMPT_CHARS,
        "gunakan list_memory untuk daftar lengkap",
    )
    if mem:
        parts.append("\n# Memory\n" + mem)
    scr = _batasi_blok(
        scripts.as_prompt_block(), _SCRIPTS_PROMPT_CHARS,
        "gunakan list_scripts untuk daftar lengkap",
    )
    if scr:
        parts.append("\n# Script memory\n" + scr)
    return "\n".join(parts)


def transcript_rows(
    messages: list, max_turns: int = 14, max_chars: int = 5000,
    per_msg: int = 700,
) -> list[dict[str, str]]:
    """Giliran percakapan sejauh ini, sudah disaring & dipangkas.

    Bentuk datanya: [{"dari": "saya"|"kamu", "isi": "…"}, …] — urut lama ke
    baru. Dipakai dua-duanya: dirangkai jadi teks (build_transcript_digest) dan
    dititipkan apa adanya ke berkas konteks JSON (lihat konteks.py).

    Hanya giliran user & jawaban asisten yang diambil (pesan sistem, hasil tool,
    dan instruksi internal dilewati), dibatasi jumlah & panjangnya agar hemat.
    """
    rows: list[dict[str, str]] = []
    for m in messages or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        # Penanda lampiran media [LAMPIR-MEDIA]<path> adalah URUSAN DALAM
        # jalur API (dikonversi jadi bagian multimodal per-request). Ia TIDAK
        # boleh bocor ke ringkasan/digest/berkas /compact — dulu ia ikut dan
        # bahkan disuntik ulang sebagai teks mentah oleh /send-compact.
        text = "\n".join(ln for ln in content.splitlines()
                         if not ln.startswith("[LAMPIR-MEDIA]")).strip()
        text = text.strip()
        # Lewati instruksi internal & preamble yang bukan ucapan pengguna.
        if not text or text.startswith("[SISTEM]") or text.startswith("[[HASIL"):
            continue
        if "PERMINTAAN SAYA:" in text:          # pesan pertama sesi web
            text = text.split("PERMINTAAN SAYA:", 1)[1].strip()
        if len(text) > per_msg:
            text = text[:per_msg].rstrip() + " …"
        rows.append({"dari": "saya" if role == "user" else "kamu", "isi": text})

    rows = rows[-max_turns:]
    hasil: list[dict[str, str]] = []
    total = 0
    for r in reversed(rows):                   # jaga giliran TERBARU bila dipotong
        total += len(r["isi"]) + 8
        if total > max_chars:
            break
        hasil.append(r)
    hasil.reverse()
    return hasil


def build_transcript_digest(
    messages: list, max_turns: int = 14, max_chars: int = 5000,
    per_msg: int = 700,
) -> str:
    """Ringkas percakapan sejauh ini agar model BARU bisa langsung menyambung.

    Dipakai saat pengguna berpindah model di tengah kerja (mis. Kimi web kena
    limit lalu ganti ke Qwen, atau sebaliknya): AI web yang baru memulai chat
    kosong di situsnya sendiri, jadi riwayat dari memory bagas-ai dikirim
    sebagai ringkasan supaya konteksnya tidak hilang.
    """
    rows = transcript_rows(messages, max_turns, max_chars, per_msg)
    return "\n\n".join(
        f"{'Saya' if r['dari'] == 'saya' else 'Kamu/AI'}: {r['isi']}"
        for r in rows
    )


def build_web_context() -> str:
    """Konteks laptop & proyek untuk connector web-AI (Kimi/Qwen/Gemini web).

    Beda dari build_system_prompt: TANPA instruksi tool/agent (AI web tak punya
    tool bagas-ai) — hanya info OS, root project, folder tambahan, peta proyek,
    dan memory, dibingkai sebagai konteks yang dititipkan pengguna. Dikirim SEKALI
    sebagai preamble pesan pertama tiap sesi web (AI web mengingat sepanjang chat),
    supaya jawabannya sadar konteks mesin & proyek."""
    parts = [
        "Berikut KONTEKS mesin & proyek saya. Pakai ini untuk memahami "
        "pertanyaan-pertanyaan saya berikutnya di percakapan ini (tak perlu "
        "membalas konteks ini sendiri):",
        f"\n# Lingkungan\n"
        f"- Sistem operasi: {osinfo.summary()}\n"
        f"- Folder proyek aktif (root): {config.PROJECT_ROOT}",
    ]
    ws = _batasi_blok(
        workspace.as_prompt_block(), _WORKSPACE_PROMPT_CHARS,
        "gunakan /dirs dan baca folder yang relevan",
    )
    if ws:
        parts.append("\n# Folder konteks tambahan\n" + ws)
    pmap = projectindex.as_prompt_block(max_chars=_PROJECT_MAP_PROMPT_CHARS)
    if pmap:
        parts.append(
            "\n# Peta proyek (struktur & simbol kunci)\n"
            "Ringkasan struktur proyek saya:\n" + pmap
        )
    mem = _batasi_blok(
        longmem.as_prompt_block(), _MEMORY_PROMPT_CHARS,
        "gunakan list_memory untuk daftar lengkap",
    )
    if mem:
        parts.append(
            "\n# Hal yang perlu kamu ingat tentang saya\n" + mem
        )
    return "\n".join(parts)


def build_context_payload(
    *,
    messages: list | None = None,
    riwayat: list[dict] | None = None,
    dipotong: bool = False,
) -> dict[str, Any]:
    """Isi berkas memory JSON: konteks proyek + riwayat percakapan apa adanya.

    Dua lapis, dan keduanya memang perlu:

      - KONTEKS (lingkungan, peta proyek, memori) — data yang sama dengan
        build_web_context(), tapi sebagai data, bukan prosa yang harus diketik
        ke kotak pesan;
      - RIWAYAT (`riwayat`) — percakapan APA ADANYA: pesan yang bagas-ai kirim
        dan balasan model, lengkap dengan blok kode & hasil tool. Inilah yang
        membuat chat baru bisa menyambung pekerjaan, bukan sekadar tahu
        proyeknya.

    Yang TIDAK ikut ke sini: aturan protokol tool. Aturan tetap di badan pesan,
    sebab aturan yang jauh dari titik keputusan terbukti diabaikan.
    """
    payload: dict[str, Any] = {
        "berkas": "memory-bagas-ai",
        "versi": 1,
        "dibuat": time.strftime("%Y-%m-%d %H:%M"),
        # `kode_periksa` TIDAK diisi di sini: berkasnya bisa dipecah jadi
        # beberapa bagian (konteks.bagi), dan tiap bagian dapat kodenya
        # sendiri-sendiri supaya model yang berhenti di bagian pertama tak
        # lolos. Menyebut satu kode di sini justru akan berbeda dari kode yang
        # akhirnya tertulis di berkasnya.
        #
        # Kalimatnya menyesuaikan isi: menyebut "ingatan percakapan sebelumnya"
        # pada berkas yang riwayatnya memang kosong membuat model mencari-cari
        # sesuatu yang tak pernah ada, dan itu berakhir jadi pertanyaan balik.
        "petunjuk": (
            ("Berkas ini INGATAN percakapan kita sebelumnya, plus keadaan "
             "mesin & proyek saya. Baca seluruhnya sekali supaya kamu paham "
             "sudah sampai mana kita, "
             if riwayat else
             "Berkas ini keadaan mesin & proyek saya. Baca sekali supaya kamu "
             "paham konteks permintaan-permintaan saya berikutnya, ")
            + "lalu tunggu permintaan saya di pesan berikutnya.\n"
            "BACA DARI BARIS PERTAMA SAMPAI BARIS TERAKHIR. Jangan mengambil "
            "kesimpulan dari bagian awalnya saja, jangan melompati bagian "
            "tengah, dan jangan berhenti begitu isinya terasa berulang — "
            "bagian yang paling menentukan justru ada di ujung berkas ini.\n"
            "Ini KONTEKS, bukan tugas: jangan dibalas isi per isi, jangan "
            "mengulang pekerjaan yang di dalamnya sudah selesai, dan jangan "
            "mengerjakan apa pun hanya karena terbaca di sini. Aturan main "
            "(format usulan langkah) ada di badan pesan, bukan di berkas ini.\n"
            "Sebagai tanda berkas ini terbaca SAMPAI HABIS, kutip nilai "
            "`kode_periksa` di balasanmu — letaknya di BARIS-BARIS TERAKHIR "
            "berkas ini, jadi kamu hanya bisa menyebutnya kalau memang sudah "
            "sampai ke sana."
        ),
        "lingkungan": {
            "sistem_operasi": osinfo.summary(),
            "folder_proyek_aktif": str(config.PROJECT_ROOT),
        },
    }

    tambahan = []
    for d in workspace.list_dirs():
        tambahan.append({"path": str(d), "isi": workspace.tree(d).splitlines()})
    if tambahan:
        payload["folder_konteks_tambahan"] = tambahan

    try:
        peta = projectindex.as_payload()
    except Exception:  # noqa: BLE001 - konteks tanpa peta masih berguna
        peta = {}
    if peta:
        payload["peta_proyek"] = peta

    fakta = longmem.all_facts()
    if fakta:
        payload["yang_perlu_kamu_ingat_tentang_saya"] = fakta

    # Ringkasan giliran: HANYA permintaan pengguna & jawaban akhir, tanpa
    # langkah di antaranya. Murah (±5 rb karakter) dan menjawab pertanyaan yang
    # berbeda dari riwayat mentah di bawah — "apa saja yang pernah diminta",
    # bukan "apa yang barusan dikerjakan".
    if messages:
        rows = transcript_rows(messages)
        if rows:
            payload["ringkasan_giliran"] = rows

    if riwayat:
        payload["percakapan_terakhir_apa_adanya"] = {
            "keterangan": (
                "Percakapan mentah antara bagas-ai (di laptop saya) dan kamu, "
                "urut lama ke baru. 'saya' = pesan yang dikirim bagas-ai "
                "(permintaan pengguna, hasil tool, teguran); 'kamu' = "
                "balasanmu, termasuk blok [[TOOL]] yang benar-benar "
                "dijalankan. Isi yang sangat panjang dipotong di TENGAH — "
                "kepala & ekornya tetap utuh."
            ),
            "dipotong_dari_awal": bool(dipotong),
            "giliran": [
                {"dari": r.get("dari", ""),
                 "isi": str(r.get("isi", "")).splitlines()}
                for r in riwayat
            ],
        }
    elif messages:
        # JALUR API: riwayat mentah web tak ada, tapi /send-compact tetap
        # butuh EKOR percakapan — dulu berkas memory API cuma memuat peta &
        # ringkasan pendek (±3 KB, "0 giliran") sehingga pasca-/new pekerjaan
        # hilang. Ambil ekor dari pesan internal terakhir (user + jawaban
        # final; [SISTEM], hasil tool & penanda media dibersihkan).
        ekor = []
        for m in (messages or [])[-12:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            c = m.get("content")
            if not isinstance(c, str):
                continue
            baris = [ln for ln in c.splitlines()
                     if ln.strip() and not ln.startswith(("[SISTEM]",
                                                          "[LAMPIR-MEDIA]"))]
            if not baris:
                continue
            ekor.append({"dari": "saya" if role == "user" else "bagas-ai",
                         "isi": "\n".join(baris)[:1200].splitlines()})
        if ekor:
            payload["percakapan_terakhir_apa_adanya"] = {
                "keterangan": (
                    "Ekor percakapan internal jalur API (user + jawaban "
                    "final, tanpa langkah tool) — urut lama ke baru."),
                "dipotong_dari_awal": len(messages or []) > 12,
                "giliran": ekor,
            }
    return payload


# Kompatibilitas: sebagian modul lama mengimpor SYSTEM_PROMPT.
SYSTEM_PROMPT = BASE

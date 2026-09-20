"""Daftar model yang tersedia + util untuk memilih model.

bagas-ai punya DUA jalur model yang cara kerjanya berbeda mendasar:

  1. CONNECTOR WEB (`web/...`) — antarmuka chat berbasis browser lewat
     Playwright memakai akun pengguna sendiri. Konteks dipegang SITUSNYA,
     tool dipanggil lewat protokol teks [[TOOL]], dan /effort berarti
     MENGKLIK tombol mode berpikir di halamannya.
  2. API (`nvidia/...`, `openrouter/...`, `opencode/...`) — endpoint
     OpenAI-compatible. Konteks dipegang KITA (dikirim ulang tiap request),
     tool memakai function-calling ASLI, dan /effort berarti mengirim
     parameter `extra_body` hanya bila parameter modelnya memang terverifikasi.
     Penyedianya dibedakan lewat ModelSpec.provider:
       - "nvidia"      : integrate.api.nvidia.com (NVIDIA_API_KEY)
       - "openrouter"  : openrouter.ai/api/v1     (OPENROUTER_API_KEY)
       - "opencode"    : opencode.ai/zen/v1       (TANPA key — gratis anonim;
                          OPENCODE_API_KEY hanya opsional)

Karena itu `ModelSpec.is_web` adalah satu-satunya titik percabangan; lihat
core.Agent.run().

# CATATAN PENTING soal effort di jalur NVIDIA (TERUKUR 2026-08-23, bukan dugaan)
Gateway NVIDIA MENERIMA parameter yang tidak didukung modelnya TANPA protes:
tanpa error, tanpa peringatan, dan tanpa efek apa pun pada keluaran. Jadi
"apakah model ini punya effort" TIDAK BISA dideteksi saat jalan (coba-lalu-
tangkap-error mustahil). Satu-satunya cara yang jujur adalah TABEL STATIS di
bawah — karena itu tiap entri menyatakan sendiri kunci apa yang ia hormati
(`reasoning_key`) dan tingkatan apa yang nyata (`effort_levels`).

Hasil pengukuran yang mendasari tabel itu:
  - muse-glimmer-30b : reasoning SELALU keluar, bahkan tanpa extra_body.
    `thinking`, `enable_thinking`, dan `reasoning_effort` sama-sama diterima
    tanpa efek -> TAK ADA saklar, TAK ADA tingkatan.
  - nemotron-3-ultra : `enable_thinking` benar-benar bekerja DUA ARAH
    (True -> ada reasoning, False -> tidak), bawaannya NYALA. `reasoning_effort`
    diabaikan. `reasoning_budget` di level atas sekarang membalas HTTP 500 --
    parameter itu DIBUANG, jangan dihidupkan lagi.
  - deepseek-v4-flash : `thinking` bekerja, bawaannya MATI (kebalikan
    nemotron). `reasoning_effort` low/medium/high diterima, tapi panjang
    reasoning yang terukur JUSTRU TERBALIK (low 1280 char > medium 1014) dan
    nilai ngawur ("ngaco") ikut diterima tanpa error -- jadi pengaruhnya tak
    terbukti. Tetap dikirim (sesuai contoh resmi & tak berbiaya), tapi menu
    /effort menyebut apa adanya supaya pengguna tak dijanjikan yang belum tentu.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from . import config

# --- tingkatan effort: nama internal -> (label, keterangan, ikon) ------------
# Dipakai menu /effort. "langsung" berarti mode berpikir DIMATIKAN; sisanya
# menyalakannya dengan nilai reasoning_effort yang berbeda.
EFFORT_INFO: dict[str, tuple[str, str, str]] = {
    "langsung": ("Langsung", "tanpa mode berpikir — jawaban paling cepat", "⚡"),
    "ringkas": ("Ringkas", "berpikir singkat — gesit & hemat token", "🌤"),
    "seimbang": ("Seimbang", "nalar secukupnya — pas untuk kebanyakan tugas", "⚖"),
    "mendalam": ("Mendalam", "berpikir penuh — untuk soal kompleks (lebih lambat)", "🔬"),
}

# nama internal -> nilai yang dikirim sebagai chat_template_kwargs.reasoning_effort
_EFFORT_API: dict[str, str] = {
    "ringkas": "low", "seimbang": "medium", "mendalam": "high",
}


@dataclass(frozen=True)
class ModelSpec:
    id: str  # ID internal: "web/<service>" (browser) atau "nvidia/<slug>" (API)
    label: str  # nama tampilan
    # Nama service connector ("kimi", "qwen", "gemini") -> agent/connectors.
    # KOSONG untuk model API — inilah penanda jalur mana yang dipakai.
    connector: str = ""
    multimodal: bool = True  # situs AI web menerima lampiran gambar
    note: str = ""  # keterangan singkat
    # DITUNDA: entrinya tetap ada & tetap tampil di /model, tapi tak bisa
    # dipilih. Lihat catatan _DITUNDA di bawah.
    ditunda: bool = False

    # --- khusus jalur API (kosong/nol untuk model web) ----------------------
    # Penyedia endpoint: "nvidia" (integrate.api.nvidia.com), "openrouter"
    # (openrouter.ai/api/v1), atau "opencode" (opencode.ai/zen/v1 — gateway
    # OpenCode Zen). Menentukan klien, API key, dan pesan galat mana yang
    # dipakai.
    provider: str = "nvidia"
    # Gaya protokol endpoint: "chat" (/chat/completions — bawaan) atau
    # "responses" (/responses, protokol OpenAI Responses API). Sebagian model
    # Zen HANYA dilayani di /responses (TERUKUR: muse-spark-contributor-free).
    api_style: str = "chat"
    # ID model APA ADANYA di endpoint penyedia. Dipisah dari `id` karena `id`
    # adalah identitas internal bagas-ai (tersimpan di prefs) sedangkan ini
    # yang dikirim ke server; menyatukan keduanya membuat ID tersimpan tak bisa
    # diubah tanpa memutus preferensi pengguna.
    api_model: str = ""
    # Kunci di chat_template_kwargs yang MEMANG dihormati model ini:
    # "thinking" (deepseek), "enable_thinking" (nemotron), atau "" = tak ada
    # saklar berpikir sama sekali. Lihat catatan pengukuran di docstring modul.
    reasoning_key: str = ""
    # ALTERNATIF saklar gaya OpenRouter: bila diisi (mis. "reasoning"), effort
    # dikirim sebagai {reasoning: {"enabled": bool}} — bukan lewat
    # chat_template_kwargs. Menang atas reasoning_key bila keduanya terisi.
    reasoning_param: str = ""
    # Field effort tingkat atas untuk endpoint yang MEMANG mendokumentasikannya.
    # Jangan isi hanya karena sebuah CLI punya flag bernama sama: CLI OpenCode
    # `--variant` adalah konfigurasi klien yang dipetakan ke opsi tiap provider,
    # bukan field universal API Zen.
    effort_param: str = ""
    # Tingkatan yang DITAWARKAN /effort. () = model ini tak punya effort, dan
    # menu akan mengatakannya terus-terang alih-alih memberi pilihan palsu.
    effort_levels: tuple[str, ...] = ()
    effort_default: str = ""
    # Kirim juga chat_template_kwargs.reasoning_effort? Hanya untuk model yang
    # setidaknya MENERIMA-nya sesuai contoh resmi. Untuk nemotron sengaja
    # False: terukur diabaikan, jadi mengirimnya cuma menyesatkan pembaca kode.
    kirim_reasoning_effort: bool = False
    # Batas token keluaran (dari contoh resmi tiap model).
    max_tokens: int = 16384
    # Catatan jujur yang ditempel di menu /effort bila ada yang perlu diakui.
    effort_catatan: str = ""
    # Tampil berlabel "(rekomendasi)" di menu /model — penanda pilihan
    # utama bagas-ai, bukan janji kualitas: model tanpa label tetap sah.
    # Kriterianya SATU dan harus terukur: model ini TERBUKTI cepat mulai
    # menjawab (waktu ujinya tercatat di note masing-masing). Model yang
    # pintar tapi lambat tetap tak berlabel — label ini soal kesigapan,
    # bukan mutu, supaya pemakainya tahu apa yang ia dapat.
    rekomendasi: bool = False

    @property
    def is_web(self) -> bool:
        """True bila model ini connector web-AI (butuh browser + login)."""
        return bool(self.connector)

    @property
    def is_api(self) -> bool:
        """True bila model ini lewat endpoint API (kebanyakan butuh API key
        penyedia; kecuali opencode/* yang gratis & anonim)."""
        return not self.connector

    @property
    def aktif(self) -> bool:
        """True bila model ini boleh dipilih pengguna sekarang."""
        return not self.ditunda

    @property
    def punya_effort(self) -> bool:
        """True bila /effort benar-benar bisa mengubah sesuatu di model ini.

        Untuk model web selalu True: /effort di sana berarti mengklik tombol di
        situsnya, dan daftar tombolnya baru diketahui saat browser terbuka."""
        return self.is_web or bool(self.effort_levels)

    def extra_body_for(self, effort: str | None) -> dict | None:
        """Parameter extra_body untuk satu giliran, sesuai effort terpilih.

        Mengembalikan None bila tak ada yang perlu dikirim — TERMASUK untuk
        model yang saklar berpikirnya terbukti tak berfungsi (muse-glimmer).
        Mengirim parameter mati ke sana bukan cuma sia-sia: ia membuat kode ini
        terlihat seolah effort-nya bekerja, padahal tidak.

        `reasoning_budget` era lama TIDAK dipakai lagi — terukur membalas
        HTTP 500 di nemotron-3-ultra, jadi menghidupkannya membuat SETIAP
        setelan effort gagal.
        """
        if self.is_web:
            return None
        lvl = effort if effort in self.effort_levels else self.effort_default
        if not lvl:
            return None
        # Gaya field effort tingkat atas (hanya untuk endpoint yang sudah
        # diverifikasi menerima field tersebut).
        if self.effort_param:
            nilai = lvl if lvl in ("low", "medium", "high") \
                else _EFFORT_API.get(lvl)
            return {self.effort_param: nilai} if nilai else None
        # Gaya OpenRouter: parameter resminya `reasoning.enabled` di level
        # atas body (contoh resmi OpenRouter), bukan chat_template_kwargs.
        if self.reasoning_param:
            return {self.reasoning_param: {"enabled": lvl != "langsung"}}
        if not self.reasoning_key:
            return None
        ctk: dict[str, object] = {}
        if lvl == "langsung":
            ctk[self.reasoning_key] = False
        else:
            ctk[self.reasoning_key] = True
            nilai = _EFFORT_API.get(lvl)
            if nilai and self.kirim_reasoning_effort:
                ctk["reasoning_effort"] = nilai
        return {"chat_template_kwargs": ctk}


# MODEL YANG SEDANG DITUNDA — atas permintaan pengguna (2026-09-21).
# Dua sebab, dan keduanya harus disebut supaya tak ada yang menebak-nebak:
#   (1) SELURUH jalur browser dimatikan: tiap model (web) butuh jendela Chrome,
#       login, dan detik-detik menunggu situsnya — sementara jalur API menjawab
#       tanpa browser sama sekali.
#   (2) SELURUH jalur OpenCode Zen dimatikan juga, karena strukturnya akan
#       diubah besar-besaran.
# Yang boleh dipakai sekarang: model (API) nvidia/* dan openrouter/* — asalkan
# kunci penyedianya sudah terisi.
#
# Entri yang ditunda SENGAJA TIDAK DIHAPUS: connector-nya utuh, ujinya utuh,
# profil login-nya utuh, dan /web tetap bisa mengurus profilnya. Yang berubah
# cuma satu — ia tak bisa dipilih. Menghidupkannya kembali = keluarkan aliasnya
# dari himpunan ini, tanpa menyentuh kode lain.
#
# Sengaja daftar ALIAS yang ditunda, bukan daftar yang aktif: menambah model
# baru kelak tak boleh diam-diam ikut terkunci hanya karena lupa didaftarkan.
_DITUNDA = {
    # (1) Seluruh jalur browser — atas permintaan pengguna (2026-09-21). Tiap
    #     model (web) butuh jendela Chrome, login, dan detik-detik menunggu
    #     situsnya, sementara jalur API menjawab tanpa browser sama sekali.
    "chatgpt-web", "kimi-web", "gemini-web", "qwen-web", "glm-web", "dola-web",
    # (2) Seluruh jalur OpenCode Zen — atas permintaan pengguna (2026-09-21)
    #     juga, karena struktur jalur ini akan diubah besar-besaran. Ditunda
    #     LEBIH DULU dari perubahannya supaya versi yang sedang dipakai tak
    #     diam-diam berpindah perilaku di tengah pekerjaan itu.
    #
    #     Kebetulan yang membantu: penyedianya SENDIRI menutup akses anonim
    #     jalur ini di hari yang sama (TERUKUR 2026-09-21: HTTP 403
    #     FreeTierError), jadi tanpa key pun entri-entri ini sudah tak bisa
    #     menjawab — lihat config.has_api_key("opencode") yang kini menuntut
    #     key, dan catatan di blok entri opencode di bawah.
    "big-pickle", "hy3-free", "ling-3.0-flash-fin-free", "mimo-v2.5-free",
    "muse-spark-1.2-contributor-free", "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
}

# Penanda tampilan untuk model yang ditunda di menu SelectScreen (UI Textual).
# Opsi yang tampilannya berawalan ini dirender REDUP dan tak bisa disorot —
# panah melewatinya, persis seperti model yang ditunda di menu /model versi
# CLI. Konstanta ini tinggal di SINI, bukan di modul UI-nya, karena "model ini
# ditunda" adalah fakta katalog; modul UI cuma merendernya.
_TANDA_DITUNDA = "\x00DITUNDA"

# Alias pendek -> spesifikasi. Urutan menentukan nomor pada /model.
MODELS: dict[str, ModelSpec] = {
    # --- jalur OpenCode Zen (opencode.ai/zen/v1) — PALING ATAS ---------------
    # TERUKUR 2026-09-21: akses anonimnya SUDAH DITUTUP penyedianya. Permintaan
    # TANPA header Authorization — persis yang dikirim llm._headers_tanpa_auth
    # selama ini — kini dibalas HTTP 403 "OpenCode's free tier can only be used
    # from within OpenCode". Dengan header key palsu malah 401 AuthError, jadi
    # tak ada lagi jalan masuk tanpa kredensial. Catatan "GRATIS tanpa API key"
    # di tiap entri karena itu DIHAPUS: membiarkannya berarti pengguna memilih
    # model ini lalu tertipu dua kali — sekali saat dipilih, sekali saat gagal.
    #
    # Entrinya SENGAJA TIDAK DIHAPUS dan TIDAK masuk _DITUNDA: yang berubah
    # kebijakan PENYEDIANYA, bukan katalog ini. Isi OPENCODE_API_KEY (atau
    # jalankan `opencode auth login`) dan seluruh entri ini langsung hidup lagi
    # tanpa satu pun suntingan di sini. config.has_api_key("opencode") yang
    # menjaga gerbangnya, jadi penolakannya terjadi SAAT DIPILIH — dengan nama
    # env yang harus diisi — bukan 403 di tengah giliran.
    #
    # Label rekomendasi DICABUT dari semua entri di sini mengikuti aturan
    # katalog: label itu menandai model yang TERUKUR cepat MENJAWAB, dan model
    # yang tak bisa menjawab sama sekali jelas tidak memenuhinya.
    #
    # Kebijakan reasoning tiap model BELUM diukur — jadi /effort sengaja tidak
    # ditawarkan. Flag CLI OpenCode `--variant` TIDAK dikirim mentah sebagai
    # field API; dokumentasinya menjelaskan variant sebagai pemetaan opsi
    # provider/model di sisi klien. api_style "responses" hanya untuk model
    # yang memang TERUKUR hanya dilayani di endpoint /responses.
    "big-pickle": ModelSpec(
        id="opencode/big-pickle",
        label="Big Pickle (API)",
        provider="opencode",
        api_model="big-pickle",
        multimodal=False,
        note=("Via OpenCode Zen — model pilihan tim opencode untuk agent "
              "koding; butuh OPENCODE_API_KEY (akses anonim ditutup "
              "penyedianya 2026-09-21)"),
        max_tokens=16384,
    ),
    "hy3-free": ModelSpec(
        id="opencode/hy3-free",
        label="Hy3 Free (API)",
        provider="opencode",
        api_model="hy3-free",
        multimodal=False,
        note="Via OpenCode Zen — butuh OPENCODE_API_KEY",
        max_tokens=16384,
    ),
    "ling-3.0-flash-fin-free": ModelSpec(
        id="opencode/ling-3.0-flash-fin-free",
        label="Ling 3.0 Flash Fin Free (API)",
        provider="opencode",
        api_model="ling-3.0-flash-fin-free",
        multimodal=False,
        note="Via OpenCode Zen — butuh OPENCODE_API_KEY",
        max_tokens=16384,
    ),
    "mimo-v2.5-free": ModelSpec(
        id="opencode/mimo-v2.5-free",
        label="MiMo-V2.5 Free (API)",
        provider="opencode",
        api_model="mimo-v2.5-free",
        multimodal=False,
        note="Via OpenCode Zen — butuh OPENCODE_API_KEY",
        max_tokens=16384,
    ),
    "muse-spark-1.2-contributor-free": ModelSpec(
        id="opencode/muse-spark-1.2-contributor-free",
        label="Muse Spark 1.2 Contributor Free (API)",
        provider="opencode",
        api_model="muse-spark-1.2-contributor-free",
        multimodal=False,
        api_style="responses",  # TERUKUR: /chat/completions membalas error 500
        note=("Via OpenCode Zen — butuh OPENCODE_API_KEY (hanya endpoint "
              "/responses)"),
        max_tokens=16384,
    ),
    "nemotron-3-ultra-free": ModelSpec(
        id="opencode/nemotron-3-ultra-free",
        label="Nemotron 3 Ultra Free (API)",
        provider="opencode",
        api_model="nemotron-3-ultra-free",
        multimodal=False,
        note="Via OpenCode Zen — butuh OPENCODE_API_KEY",
        max_tokens=16384,
    ),
    "nemotron-3.5-lightning-free": ModelSpec(
        id="opencode/nemotron-3.5-lightning-free",
        label="Nemotron 3.5 Lightning Free (API)",
        provider="opencode",
        api_model="nemotron-3.5-lightning-free",
        multimodal=False,
        # Saat pengukuran awal (2026-08-29) upstream Zen untuk model ini masih
        # membalas 404 "Provider returned error" di /chat/completions — pasang
        # masuk tapi beri catatan jujur; penyedianya sendiri yang bermasalah.
        note=("Via OpenCode Zen — butuh OPENCODE_API_KEY (upstream-nya kadang "
              "404; bila gagal, pilih varian lain)"),
        max_tokens=16384,
    ),

    # --- jalur browser (SEDANG DITUNDA semua — lihat _DITUNDA) ---------------
    # Entrinya tetap di sini supaya connectornya tetap terdaftar & /web tetap
    # bisa mengurus profil login-nya. Yang perlu diingat saat membacanya:
    # semuanya masih TERDAFTAR tapi TAK BISA DIPILIH sampai aliasnya keluar
    # dari _DITUNDA.
    "chatgpt-web": ModelSpec(
        id="web/chatgpt",
        label="ChatGPT (web)",
        connector="chatgpt",
        note="Via browser chatgpt.com — tanpa varian model, kuat di coding & reasoning",
    ),
    "kimi-web": ModelSpec(
        id="web/kimi",
        label="Kimi (web)",
        connector="kimi",
        note="Via browser kimi.com — jago agentic & coding, konteks panjang",
    ),
    "gemini-web": ModelSpec(
        id="web/gemini",
        label="Gemini (web)",
        connector="gemini",
        note="Via browser gemini.google.com — varian Flash & Pro, multimodal",
    ),
    "qwen-web": ModelSpec(
        id="web/qwen",
        label="Qwen (web)",
        connector="qwen",
        note="Via browser chat.qwen.ai — multibahasa & cepat",
    ),
    "glm-web": ModelSpec(
        id="web/glm",
        label="GLM (web)",
        connector="glm",
        note=("Via browser chat.z.ai — GLM-5.2 & saudaranya, kuat di koding "
              "dan tugas panjang; punya mode berpikir (High/Max)"),
    ),
    "dola-web": ModelSpec(
        id="web/dola",
        label="Dola (web)",
        connector="dola",
        note=("Via browser dola.com (dulu Cici) — BISA BIKIN GAMBAR & VIDEO; "
              "pilih ini untuk kerja visual, bukan untuk ngoding. Kuota "
              "gratisnya terbatas, jadi pakai seperlunya"),
    ),

    # --- jalur API (butuh API key penyedia, tanpa browser) ------------------
    # Entri di bawah TIDAK memakai connector, jadi is_web-nya False dan
    # core mengarahkannya ke _run_api. Nilai reasoning_key/effort_levels di
    # sini adalah hasil PENGUKURAN (lihat docstring modul), bukan salinan
    # dokumentasi — beberapa parameter yang ada di contoh resmi terbukti tak
    # berpengaruh, dan itu dinyatakan apa adanya di sini.
    "nemotron": ModelSpec(
        id="nvidia/nemotron",
        label="Nemotron 3 Ultra (API)",
        api_model="nvidia/nemotron-3-ultra-550b-a55b",
        multimodal=False,  # endpoint teks; lampiran gambar tak dikirim
        note=("Via API NVIDIA — 550B, mode berpikir bisa dimatikan; "
              "TERUKUR 2,3 dtk sampai jawaban utuh, cocok untuk kerja tool "
              "bertubi-tubi"),
        reasoning_key="enable_thinking",
        # HANYA dua tingkat, dan itu memang seluruh yang model ini punya:
        # enable_thinking terbukti bekerja dua arah, sedangkan reasoning_effort
        # di dalam chat_template_kwargs terukur DIABAIKAN (panjang reasoning
        # identik). Menawarkan "ringkas/seimbang/mendalam" di sini berarti
        # menjanjikan tiga hal yang menghasilkan keluaran sama persis.
        effort_levels=("langsung", "mendalam"),
        effort_default="mendalam",  # bawaan server memang NYALA
        kirim_reasoning_effort=False,
        max_tokens=16384,
        rekomendasi=True,
    ),
    "muse": ModelSpec(
        id="nvidia/muse",
        label="Muse Glimmer 30B (API)",
        api_model="meta/muse-glimmer-30b",
        multimodal=False,
        # Dulu tertulis "paling ringan & paling GESIT" — klaim itu TERBANTAH
        # saat diukur 2026-09-21: muse butuh 26,7 dtk sampai jawaban utuh,
        # sementara Nemotron Ultra 2,3 dtk dan GPT-OSS-20B 0,6 dtk. Yang benar
        # cuma "paling ringan" (30B); banyak model di bawah justru lebih gesit,
        # jadi entri ini sengaja TIDAK berlabel rekomendasi.
        note=("Via API NVIDIA — 30B, paling ringan; mode berpikirnya selalu "
              "nyala dan tak bisa diatur (TERUKUR 26,7 dtk, tak gesit)"),
        # Kosong SEMUA, sesuai pengukuran: tanpa extra_body pun reasoning tetap
        # keluar, dan ketiga kunci saklar diterima tanpa mengubah apa pun.
        reasoning_key="",
        effort_levels=(),
        max_tokens=8192,  # contoh resminya 8192, bukan 16384
        effort_catatan=("model ini tak punya saklar mode berpikir — nalarnya "
                        "selalu aktif dan tak ada tingkatan yang bisa dipilih"),
    ),

    # --- ditambahkan 2026-09-21, setelah mengukur ulang NVIDIA --------------
    # Daftar /v1/models MENIPU: ia menjawab HTTP 200 dengan 82 nama, tapi saat
    # tiap nama benar-benar dipanggil dengan key ini, 60+ di antaranya membalas
    # HTTP 404 "Function '<uuid>': Not found for account" — TERDAFTAR bukan
    # berarti MELAYANI. Empat entri berikut adalah yang benar-benar menjawab
    # DAN cepat mulai. Yang pintar tapi lambat sengaja tak dimasukkan; angka di
    # note adalah hasil uji satu-kata, dan ujinya bisa diulang kapan saja:
    #     python tools/uji_nvidia_models.py
    "gpt-oss": ModelSpec(
        id="nvidia/gpt-oss",
        label="GPT-OSS 20B (API)",
        api_model="openai/gpt-oss-20b",
        multimodal=False,
        note=("Via API NVIDIA — 20B, TERUKUR 0,6 dtk: paling gesit dari semua "
              "model di katalog ini"),
        # Kosong SEMUA: saklar mode berpikirnya belum diukur untuk entri ini.
        # Menebak nama kuncinya berarti mengirim parameter yang mungkin
        # diabaikan diam-diam — persis yang dihindari seluruh berkas ini.
        reasoning_key="",
        effort_levels=(),
        effort_catatan=("saklar mode berpikir model ini belum diukur — "
                        "tak ada tingkatan yang bisa ditawarkan"),
        max_tokens=16384,
        rekomendasi=True,
    ),
    "nemotron-super": ModelSpec(
        id="nvidia/nemotron-super",
        label="Nemotron 3 Super 120B (API)",
        api_model="nvidia/nemotron-3-super-120b-a12b",
        multimodal=False,
        note=("Via API NVIDIA — 120B, TERUKUR 1,5 dtk; yang paling besar di "
              "antara yang gesit"),
        reasoning_key="",
        effort_levels=(),
        effort_catatan=("saklar mode berpikir model ini belum diukur — "
                        "tak ada tingkatan yang bisa ditawarkan"),
        max_tokens=16384,
        rekomendasi=True,
    ),
    "nemotron-lightning": ModelSpec(
        id="nvidia/nemotron-lightning",
        label="Nemotron 3.5 Lightning 30B (API)",
        api_model="nvidia/nemotron-3.5-lightning-30b-a3b",
        multimodal=False,
        note=("Via API NVIDIA — 30B, TERUKUR 3,8 dtk; nalarnya keluar sebagai "
              "teks biasa, jadi jawabannya perlu dibaca sampai tuntas"),
        reasoning_key="",
        effort_levels=(),
        effort_catatan=("saklar mode berpikir model ini belum diukur — "
                        "tak ada tingkatan yang bisa ditawarkan"),
        max_tokens=16384,
        rekomendasi=True,
    ),
    "nemotron-nano": ModelSpec(
        id="nvidia/nemotron-nano",
        label="Nemotron 3 Nano Omni 30B (API)",
        api_model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        multimodal=False,
        note=("Via API NVIDIA — 30B dengan penalaran nyala, TERUKUR 4,2 dtk"),
        reasoning_key="",
        effort_levels=(),
        effort_catatan=("saklar mode berpikir model ini belum diukur — "
                        "tak ada tingkatan yang bisa ditawarkan"),
        max_tokens=16384,
        rekomendasi=True,
    ),
    # Dihapus 2026-08-25: deepseek-v4-flash (nvidia/deepseek) — paling lambat
    # memulai dari semuanya (TERUKUR 106-169 dtk sampai kata pertama, 4 dari 8
    # permintaan uji habis waktu). Digantikan ox-alpha via OpenRouter.
    "oxalpha": ModelSpec(
        id="openrouter/ox-alpha",
        label="Ox Alpha (API)",
        provider="openrouter",
        api_model="stealth/ox-alpha",
        # Penerima GAMBAR & VIDEO lewat konten multimodal OpenRouter
        # (image_url / video_url base64) — lihat core._pesan_dengan_media.
        multimodal=True,
        note=("Via API OpenRouter — paham gambar & video; nalarnya selalu "
              "nyala dan tak bisa dimatikan (syarat endpoint)"),
        # Saklar gaya OpenRouter DITOLAK server untuk model ini — TERUKUR
        # 2026-08-25: {"reasoning":{"enabled":false}} dibalas HTTP 400 "Reasoning
        # is mandatory for this endpoint". Jadi TIDAK ada saklar yang jujur untuk
        # ditawarkan; nalar selalu menyala tanpa tingkatan yang bisa dijamin.
        reasoning_param="",
        reasoning_key="",
        effort_levels=(),
        effort_default="",
        kirim_reasoning_effort=False,
        max_tokens=16384,
        effort_catatan=("model ini SELALU bernalar — endpointnya menolak "
                        "mode tanpa-reasoning (HTTP 400), jadi tak ada "
                        "saklar/tingkatan yang bisa dipilih"),
    ),
}

MODELS = {k: (v if k not in _DITUNDA else
              # replace(): ModelSpec frozen, jadi penandaannya dibuat sebagai
              # salinan. Ditandai DI SINI, bukan ditulis satu per satu di tiap
              # entri, supaya daftar _DITUNDA tetap satu-satunya sumber
              # kebenaran — tak ada kemungkinan keduanya berselisih.
              replace(v, ditunda=True))
          for k, v in MODELS.items()}

_ORDER = list(MODELS.keys())
# "Siap" berarti BENAR-BENAR bisa dipilih sekarang, bukan sekadar tak ditunda:
# model (API) yang kunci penyedianya kosong sama tak bisanya dipakai, dan
# models._pastikan_aktif akan menolaknya juga. Menyaringnya di sini membuat
# turunan _SIAP — DEFAULT_ID dan daftar "yang bisa dipakai sekarang" pada pesan
# penolakan — tak pernah menjanjikan model yang pasti ditolak.
_SIAP = [
    k for k in _ORDER
    if MODELS[k].aktif
    and (not MODELS[k].is_api or config.has_api_key(MODELS[k].provider))
]
# _AKTIF = daftar yang dipakai untuk MENYARANKAN sebuah nama (mis. contoh pada
# /model, atau nama yang tinggal disalin pengguna). Bedanya dengan _SIAP hanya
# pada keadaan darurat: bila belum satu pun kunci terisi, _SIAP kosong dan
# daftar ini jatuh ke katalog TAK-DITUNDA supaya pesannya masih bisa menyebut
# nama yang benar-benar ada di katalog.
#
# Jatuh ke katalog TAK-DITUNDA, bukan ke _ORDER: _ORDER dimulai dari entri
# opencode/* yang kini ditunda, dan karena DEFAULT_ID diambil dari unsur
# pertama daftar ini, memakai _ORDER berarti keadaan "belum ada key"
# mendaratkan bagas-ai di model yang pengguna sendiri tak boleh memilih.
_AKTIF = _SIAP or [k for k in _ORDER if MODELS[k].aktif] or _ORDER

# Model bawaan bila tak ada preferensi tersimpan / preferensinya tak dikenal.
# Diambil dari yang AKTIF: bawaan yang ditunda berarti bagas-ai mendarat di
# model yang pengguna sendiri tak boleh memilihnya.
DEFAULT_ID = MODELS[_AKTIF[0]].id


# Nama LAMA yang masih melekat di ingatan pengguna. Diterima diam-diam supaya
# mengetik nama yang dulu benar tak berujung "model tak dikenal" — kegagalan
# yang menyesatkan, sebab situsnya sendiri masih mengalihkan cici.com ke Dola.
_ALIAS_LAMA = {"cici": "dola-web", "cici-web": "dola-web", "web/cici": "dola-web"}
_TIDAK_DIDUKUNG = {"opencode/hy3-free"}


def _sebut_yang_bisa_dipakai() -> str:
    """Kalimat lanjutan yang menyebut model mana yang BENAR-BENAR bisa dipakai.

    Satu kalimat untuk EMPAT tempat (tiga penolakan + satu 'model tak dikenal'),
    dan itu memang alasannya: dulu tiap tempat menyusun daftarnya sendiri dari
    _AKTIF, jadi begitu keadaan katalog berubah — seluruh model (web) ditunda,
    lalu belum ada key sama sekali — keempatnya ikut berbohong bersama-sama,
    masing-masing dengan caranya. Satu fungsi berarti satu kebenaran.

    Dua bentuk, sesuai apa yang benar-benar ada:
      * ada yang siap -> sebutkan namanya (pengguna tinggal menyalin);
      * belum ada key -> sebut SEBABNYA, sebab tak ada satu nama pun yang jujur
        bisa disebut "bisa dipakai sekarang"."""
    if _SIAP:
        return ("Yang bisa dipakai sekarang: "
                + ", ".join(MODELS[k].label for k in _SIAP) + ".")
    return ("Belum ada satu pun model yang bisa dipakai: NVIDIA_API_KEY & "
            "OPENROUTER_API_KEY masih kosong (jalur (web) sedang ditunda). "
            "Isi salah satunya lewat `bagas-ai login`.")


def _pastikan_aktif(spec: ModelSpec) -> ModelSpec:
    """Tolak model yang belum boleh dipakai — dengan alasan, bukan sekadar 'gagal'.

    Dua sebab penolakan: model sedang DITUNDA, atau model API tapi kunci
    penyedianya kosong.

    Penolakannya di SINI, satu pintu untuk semua jalan masuk (/model, argumen
    baris perintah, tombol Telegram, preferensi tersimpan): kalau tiap
    antarmuka menyaring sendiri-sendiri, cepat atau lambat ada satu yang lupa."""
    # DITUNDA diperiksa LEBIH DULU, dan urutannya penting. Menaruh pemeriksaan
    # kunci di depan berarti model opencode/* — yang kini ditunda — dijawab
    # "butuh OPENCODE_API_KEY": pengguna lalu pergi mencari kunci yang, setelah
    # didapat, tetap tak membuka apa pun karena sebabnya bukan itu. Sebab yang
    # lebih menentukan disebut lebih dulu.
    if not spec.aktif:
        raise ValueError(
            f"Model {spec.label} sedang DITUNDA. "
            + _sebut_yang_bisa_dipakai()
            + " Entrinya tak dihapus, jadi ini bisa dibuka lagi kapan saja."
        )
    if spec.is_api and not config.has_api_key(spec.provider):
        env_name = config.api_key_env(spec.provider)
        # Diperiksa DI SINI, bukan saat giliran berjalan: kalau tidak, pengguna
        # baru tahu key-nya kosong sesudah mengirim pesan panjang, dan pesan itu
        # sudah masuk riwayat sebagai giliran gagal.
        #
        # Saran penutupnya TIDAK LAGI menyuruh pindah ke model (web): seluruh
        # model (web) sedang ditunda (_DITUNDA), jadi mengarahkan ke sana cuma
        # membuat pengguna menabrak penolakan berikutnya.
        if spec.provider == "openrouter":
            raise ValueError(
                f"Model {spec.label} lewat API OpenRouter dan butuh "
                f"{env_name}, yang belum diisi. Isi di {config.ENV_FILE} "
                f"(baris: {env_name}=sk-or-...); ambil key di "
                "https://openrouter.ai/keys."
            )
        if spec.provider == "opencode":
            # Cabang ini baru TERJANGKAU sejak 2026-09-21: sebelumnya akses
            # anonimnya masih terbuka sehingga has_api_key("opencode") selalu
            # True dan tak ada yang pernah sampai ke sini. Penyedianya menutup
            # akses itu (HTTP 403 FreeTierError), jadi key sungguhan kini
            # satu-satunya jalan masuk — dan gerbangnya dipindah ke ATAS, ke
            # momen memilih model, bukan dibiarkan meledak di tengah giliran.
            raise ValueError(
                f"Model {spec.label} lewat API OpenCode Zen dan butuh "
                f"{env_name}, yang belum diisi. Akses tanpa key jalur ini "
                "SUDAH DITUTUP penyedianya (TERUKUR 2026-09-21: HTTP 403 "
                "FreeTierError), jadi model ini tak bisa dipakai tanpa key. "
                f"Isi di {config.ENV_FILE} (baris: {env_name}=...); ambil key "
                "di https://opencode.ai/auth, atau jalankan `opencode auth "
                "login` — berkasnya dibaca otomatis oleh bagas-ai."
            )
        raise ValueError(
            f"Model {spec.label} lewat API NVIDIA dan butuh {env_name}, "
            f"yang belum diisi. Isi di {config.ENV_FILE} "
            f"(baris: {env_name}=nvapi-...); key gratis di "
            "https://build.nvidia.com."
        )
    if spec.aktif:
        return spec
    raise ValueError(
        f"Model {spec.label} sedang DITUNDA. "
        + _sebut_yang_bisa_dipakai()
        + " Connector-nya tak dihapus, jadi ini bisa dibuka lagi kapan saja."
    )


def cari(name: str) -> ModelSpec:
    """Model apa yang DIMAKSUD sebuah nama — tanpa peduli ia ditunda atau tidak.

    Dipisah dari resolve() dengan sengaja: "nama ini merujuk ke model apa" dan
    "boleh tidak saya pindah ke sana" adalah dua pertanyaan berbeda. Yang
    pertama tetap dibutuhkan untuk model yang ditunda — mis. memeriksa alias
    lama, menampilkan namanya, atau mengurus profil login-nya.

    Terima alias, nomor (1..N), ID penuh, atau label."""
    key = name.strip().lower()
    key = _ALIAS_LAMA.get(key, key)

    if key in MODELS:
        return MODELS[key]

    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(_ORDER):
            return MODELS[_ORDER[idx]]

    for spec in MODELS.values():
        if key in (spec.id.lower(), spec.label.lower(), spec.connector):
            return spec

    # Dulu ID tak dikenal yang memuat "/" diterima apa adanya supaya pengguna
    # bebas memakai model mana pun dari katalog NVIDIA. Kini tak ada katalog:
    # menerima ID sembarangan hanya akan membuat giliran gagal saat dijalankan,
    # jadi lebih baik ditolak di sini dengan daftar yang jelas.
    #
    # Daftarnya diambil dari _SIAP, BUKAN _ORDER: menyebut ketiga belas entri
    # yang ditunda berarti menawarkan tiga belas nama yang pasti ditolak
    # langkah berikutnya — pengguna mengetiknya, lalu dapat penolakan yang
    # seharusnya bisa ia hindari sejak awal. Yang ditunda tetap disebut, tapi
    # sebagai keterangan, bukan sebagai pilihan.
    #
    # _SIAP yang kosong (belum ada satu pun key) diperlakukan sebagai cabang
    # tersendiri: di keadaan itulah tak ada nama yang jujur bisa disebut
    # "bisa dipakai sekarang", jadi yang disebut justru sebabnya.
    pesan = (f"Model '{name}' tidak dikenal. " + _sebut_yang_bisa_dipakai()
             + " Ketik /model untuk melihat katalog.")
    _tunda = [MODELS[k].label for k in _ORDER if not MODELS[k].aktif]
    if _tunda:
        pesan += (" (Ada juga yang ditunda dan belum bisa dipakai: "
                  + ", ".join(_tunda) + ".)")
    raise ValueError(pesan)


def resolve(name: str) -> ModelSpec:
    """Model yang BOLEH dipakai sekarang, dari alias/nomor/ID/label.

    Model yang ditunda tetap dikenali lalu ditolak dengan alasannya — bukan
    dijawab "tidak dikenal", yang akan membuat pengguna mengira model itu
    hilang lalu mencari-cari nama yang sebenarnya masih ada."""
    spec = cari(name)
    # Pesan khusus hy3-free hanya berlaku SELAMA modelnya masih boleh dipilih.
    # Sejak jalur opencode ditunda, model ini ditolak lewat _pastikan_aktif
    # seperti saudara-saudaranya; menyebut big-pickle/muse-spark sebagai
    # gantinya justru menunjuk dua model yang ikut ditunda.
    if spec.id in _TIDAK_DIDUKUNG and spec.aktif:
        raise ValueError(
            "Model Hy3 Free sedang tidak didukung OpenCode (401 ModelError). "
            + _sebut_yang_bisa_dipakai()
        )
    return _pastikan_aktif(spec)


def spec_for_id(model_id: str) -> ModelSpec:
    """ModelSpec dari ID tersimpan (prefs/.env).

    ID lama peninggalan era NVIDIA (mis. "z-ai/glm-5.2") tak lagi ada. Alih-alih
    membuat ModelSpec palsu yang pasti gagal saat dipakai, kembalikan model
    bawaan — pengguna lama otomatis mendarat di model yang benar-benar jalan.

    Model yang DITUNDA diperlakukan sama: preferensi lama yang menunjuk ke sana
    dialihkan ke bawaan. Kalau tidak, sesi berikutnya dimulai dengan model yang
    tak boleh dipilih, dan tiap /model justru menolak mengembalikannya.
    """
    if model_id in _TIDAK_DIDUKUNG:
        # Dulu di sini tertulis MODELS["big-pickle"] secara HARFIAH. Itu jadi
        # jebakan begitu jalur opencode ditunda: pemetaannya mendarat di model
        # yang tak boleh dipilih, dan tiap kali dijalankan peringatan yang sama
        # muncul lagi. Bawaannya diambil dari _AKTIF, seperti cabang di bawah.
        return MODELS[_AKTIF[0]]
    for spec in MODELS.values():
        if spec.id == model_id and spec.aktif:
            return spec
    return MODELS[_AKTIF[0]]


def is_known_id(model_id: str) -> bool:
    """True bila ID ini masih boleh dipakai apa adanya.

    Model yang ditunda sengaja dijawab False: pemanggilnya (Agent.__init__)
    memakai ini untuk MENYIMPAN ULANG preferensi ke model hasil pemetaan —
    tanpa itu, peringatan yang sama muncul tiap kali bagas-ai dijalankan."""
    return model_id not in _TIDAK_DIDUKUNG and any(
        spec.id == model_id and spec.aktif for spec in MODELS.values())


# --- varian model SITUS (rombak /model & /effort) ---------------------------
# Nama umum ("ChatGPT", "Qwen", "GLM") sudah tak layak jadi pilihan /model:
# yang pengguna pilih sebenarnya adalah VARIAN di dalam situsnya —
# "GLM-5.2", "K3", "Qwen3.8-Max". Daftar itu hidup di tiap connector
# (WebConnector.web_models) karena hanya di sana nama & selektornya terukur.
# Fungsi-fungsi di bawah menjembatani models <-> connectors tanpa membuat
# siklus impor (connectors mengimpor config; models tidak boleh mengimpor
# connectors di level modul).

def _varian_layanan() -> dict[str, list[tuple[str, str]]]:
    """alias layanan web -> daftar (label_varian, deskripsi) dari connectornya.

    Gagal total (Playwright tak ada / impor error) -> kosong: /model tetap
    menampilkan layanan, hanya tanpa pemulia varian."""
    hasil: dict[str, list[tuple[str, str]]] = {}
    try:
        from . import connectors  # impor tunda: connectors butuh Playwright?
        for key, spec in MODELS.items():
            if not (spec.is_web and spec.connector):
                continue
            try:
                conn = connectors.get_connector(spec.connector)
            except Exception:  # noqa: BLE001 — connector tak terdaftar
                continue
            ops = conn.web_model_options()
            if ops:
                hasil[key] = ops
    except Exception:  # noqa: BLE001 — Playwright tak terpasang, dsb.
        pass
    return hasil


def kategori_model(spec: ModelSpec) -> str:
    """Nama kategori model di menu /model — PEMISAH antar kelompok.

    Tiga kelompok: OpenCode Zen (gratis, tanpa key), AI web (browser), dan
    API ber-key. Urutan kemunculannya mengikuti urutan MODELS, jadi kategori
    tak perlu didaftarkan terpisah."""
    if spec.provider == "opencode":
        return "OpenCode Zen — gratis, tanpa API key"
    if spec.is_web:
        return "AI Web — via browser"
    return "API — butuh API key"


def pilihan_model_grup() -> list[tuple[str, list[tuple[str, str]]]]:
    """Menu /model TERKELOMPOK: [(kategori, [(tampilan, nilai), …]), …].

    Kembaran berkelompok dari pilihan_model(): tiap layanan web tetap memuai
    jadi variannya, dan tiap opsi membawa (tampilan, nilai) — tampilan boleh
    berlabel "(rekomendasi)" / berubah gaya, nilai tetap alias yang
    diterima Agent.set_model."""
    grup: dict[str, list[tuple[str, str]]] = {}
    varian = _varian_layanan()
    for key, spec in MODELS.items():
        if spec.is_web:
            ops = varian.get(key)
            if ops:
                items = [f"{key} {label}" for label, _desc in ops]
            else:
                items = [key]
        else:
            items = [key]
        label_bebas = " (rekomendasi)" if spec.rekomendasi else ""
        # Model yang ditunda TETAP ditampilkan — dengan penanda yang membuat
        # SelectScreen merendernya redup & melewatinya. Menyembunyikannya akan
        # membuat pengguna mengira connectornya sudah dihapus, padahal ia utuh
        # dan cuma sedang tak boleh dipilih.
        awalan = _TANDA_DITUNDA if spec.ditunda else ""
        grup.setdefault(kategori_model(spec), []).extend(
            (awalan + it + label_bebas, it) for it in items)
    return list(grup.items())


def pilihan_model() -> list[str]:
    """Daftar pilihan untuk menu /model: nama MODEL SUNGGUHAN.

    Tiap layanan web memuai jadi tiap variannya ("glm GLM-5.2", "glm
    GLM-5-Turbo", …) memakai bentuk "<alias> <varian>" — itulah yang
    diterima Agent.set_model. Bila connectornya tak bisa diimpor, layanan
    itu tetap tampil satu baris (nama layanannya) supaya /model tak kosong.
    Model API tampil apa adanya."""
    out: list[str] = []
    varian = _varian_layanan()
    for key, spec in MODELS.items():
        if spec.is_web:
            ops = varian.get(key)
            if ops:
                out.extend(f"{key} {label}" for label, _desc in ops)
            else:
                out.append(key)
        else:
            out.append(key)
    return out


def resolve_varian(name: str) -> tuple[str, str] | None:
    """Nama varian polos ("GLM-5.2", "k2.6") -> (alias_layanan, label_varian).

    None bila bukan nama varian yang dikenal (pemanggil jatuh ke resolve()
    biasa). Pencocokan case-insensitive terhadap label varian para layanan
    web; ambigu (nama sama di dua layanan) -> layanan PERTAMA di urutan
    katalog menang, karena itulah urutan yang dilihat pengguna di /model."""
    key = name.strip()
    if not key:
        return None
    lower = key.lower()
    for alias, ops in _varian_layanan().items():
        for label, _desc in ops:
            if label.lower() == lower:
                return alias, label
    return None


# random_fallback() DIHAPUS: pemakainya dulu _escalate (naik-kelas otomatis) dan
# migrasi preferensi DeepSeek, keduanya ikut hilang bersama katalog ber-API-key.
# Membiarkannya berbahaya, bukan sekadar sampah: ia memilih model ACAK, jadi bila
# kelak dipanggil lagi karena disangka masih dipakai, ia akan memindahkan
# pengguna ke layanan web lain DI TENGAH tugas — memicu jendela login mendadak
# dan memutus konteks percakapan, persis alasan naik-kelas otomatis dihapus.


def catalog() -> list[tuple[int, str, ModelSpec]]:
    """Daftar (nomor, alias, spec) terurut — TERMASUK yang ditunda.

    Sengaja lengkap: /model menampilkannya (redup, tak bisa dipilih) dan /web
    tetap perlu mengurus profil login layanan yang sedang ditunda."""
    return [(i, key, MODELS[key]) for i, key in enumerate(_ORDER, start=1)]


def catalog_aktif() -> list[tuple[int, str, ModelSpec]]:
    """Hanya model yang BOLEH dipilih. Dipakai jalur yang memilih SENDIRI
    (mis. tawaran pindah model saat kuota habis) — di situ entri yang ditunda
    bukan cuma tak terpilih, tapi tak boleh ditawarkan sama sekali.

    Saringannya `spec.aktif` = "tak ditunda", BUKAN _SIAP = "kuncinya ada".
    Bedanya penting dan disengaja: yang memanggil fungsi ini menawarkan pilihan
    kepada pengguna, bukan menjanjikan giliran yang langsung jalan. Model yang
    kuncinya belum diisi tetap SAH ditawarkan — memilihnya memunculkan
    keterangan kunci mana yang kurang, dan itu justru cara pengguna tahu apa
    yang perlu diisi. Menyaringnya di sini akan menyembunyikan fitur yang sudah
    terpasang hanya karena env-nya belum lengkap."""
    return [(i, key, spec) for i, key, spec in catalog() if spec.aktif]


def list_text(current_id: str | None = None) -> str:
    """Daftar model siap tampil untuk perintah /model."""
    lines = ["Model — (web) lewat browser + login sekali, "
             "(API) lewat API key tanpa browser:"]
    # Sebut yang DITUNDA, bukan yang aktif: dulu baris ini mendaftar yang aktif,
    # dan begitu modelnya bertambah ia berbunyi "untuk sementara hanya
    # <delapan model> yang bisa dipilih" — kalimat yang isinya justru
    # menyembunyikan satu-satunya keterangan yang berguna.
    _tunda = [MODELS[k].label for k in _ORDER if not MODELS[k].aktif]
    if _tunda:
        lines.append(
            "Ditunda (connector-nya tak dihapus, bisa dibuka lagi): "
            + ", ".join(_tunda) + ".")
    for i, key in enumerate(_ORDER, start=1):
        spec = MODELS[key]
        tag = f"  [{spec.note}]" if spec.note else ""
        # Penanda rekomendasi ditulis di SINI juga, bukan hanya di menu
        # berkelompok versi Textual: /model adalah jalan yang paling sering
        # dipakai, dan label yang cuma muncul di satu dari dua tampilan membuat
        # "mana yang disarankan" bergantung pada antarmuka mana yang dibuka.
        rek = "  (rekomendasi)" if spec.rekomendasi else ""
        mark = "  <- aktif" if current_id and spec.id == current_id else ""
        if spec.ditunda:
            mark = "  (ditunda — belum bisa dipilih)"
        elif spec.is_api and not config.has_api_key(spec.provider):
            # Tetap DITAMPILKAN, bukan disembunyikan: pengguna perlu tahu model
            # ini ada dan apa syaratnya. Menyembunyikannya membuat fitur yang
            # sudah terpasang terlihat tak pernah ada.
            mark = f"  (butuh {config.api_key_env(spec.provider)})"
        lines.append(f"  {i:>2}. {key:12s} {spec.label}{rek}{tag}{mark}")
    contoh = _AKTIF[0]
    lines.append(f"Pilih: /model <nama|nomor>   contoh: /model {contoh}")
    return "\n".join(lines)

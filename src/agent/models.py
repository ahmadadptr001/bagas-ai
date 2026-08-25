"""Daftar model yang tersedia + util untuk memilih model.

bagas-ai punya DUA jalur model yang cara kerjanya berbeda mendasar:

  1. CONNECTOR WEB (`web/...`) — antarmuka chat berbasis browser lewat
     Playwright memakai akun pengguna sendiri. Konteks dipegang SITUSNYA,
     tool dipanggil lewat protokol teks [[TOOL]], dan /effort berarti
     MENGKLIK tombol mode berpikir di halamannya.
  2. API (`nvidia/...`, `openrouter/...`) — endpoint OpenAI-compatible.
     Konteks dipegang KITA (dikirim ulang tiap request), tool memakai
     function-calling ASLI, dan /effort berarti mengirim parameter
     `extra_body`. Penyedianya dibedakan lewat ModelSpec.provider:
       - "nvidia"      : integrate.api.nvidia.com (NVIDIA_API_KEY)
       - "openrouter"  : openrouter.ai/api/v1     (OPENROUTER_API_KEY)

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
    # Penyedia endpoint: "nvidia" (integrate.api.nvidia.com) atau
    # "openrouter" (openrouter.ai/api/v1). Menentukan klien, API key, dan
    # pesan galat mana yang dipakai.
    provider: str = "nvidia"
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

    @property
    def is_web(self) -> bool:
        """True bila model ini connector web-AI (butuh browser + login)."""
        return bool(self.connector)

    @property
    def is_api(self) -> bool:
        """True bila model ini lewat endpoint API (butuh API key penyedia)."""
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


# MODEL YANG SEDANG DITUNDA — atas permintaan pengguna. Yang boleh dipakai
# sekarang: GLM dan Qwen. Entri lain SENGAJA TIDAK DIHAPUS:
# connector-nya utuh, ujinya utuh, profil login-nya utuh. Yang berubah cuma
# satu — ia tak bisa dipilih. Menghidupkannya kembali = keluarkan aliasnya
# dari himpunan ini, tanpa menyentuh kode lain.
#
# Sengaja daftar ALIAS yang ditunda, bukan daftar yang aktif: menambah model
# baru kelak tak boleh diam-diam ikut terkunci hanya karena lupa didaftarkan.
_DITUNDA = {"dola-web"}
# _DITUNDA = {"gemini-web", "dola-web", "kimi-web"}

# Alias pendek -> spesifikasi. Urutan menentukan nomor pada /model.
MODELS: dict[str, ModelSpec] = {
    "chatgpt-web": ModelSpec(
        id="web/chatgpt",
        label="ChatGPT (web)",
        connector="chatgpt",
        note="Via browser chatgpt.com — GPT-4o/4.1, kuat di coding & reasoning",
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
              "cepat mulai menjawab, cocok untuk kerja tool bertubi-tubi"),
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
    ),
    "muse": ModelSpec(
        id="nvidia/muse",
        label="Muse Glimmer 30B (API)",
        api_model="meta/muse-glimmer-30b",
        multimodal=False,
        note=("Via API NVIDIA — 30B, paling ringan & paling gesit; "
              "mode berpikirnya selalu nyala dan tak bisa diatur"),
        # Kosong SEMUA, sesuai pengukuran: tanpa extra_body pun reasoning tetap
        # keluar, dan ketiga kunci saklar diterima tanpa mengubah apa pun.
        reasoning_key="",
        effort_levels=(),
        max_tokens=8192,  # contoh resminya 8192, bukan 16384
        effort_catatan=("model ini tak punya saklar mode berpikir — nalarnya "
                        "selalu aktif dan tak ada tingkatan yang bisa dipilih"),
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
_AKTIF = [k for k in _ORDER if MODELS[k].aktif]
if not _AKTIF:      # jaring pengaman: tak boleh ada keadaan "tak ada model"
    _AKTIF = _ORDER

# Model bawaan bila tak ada preferensi tersimpan / preferensinya tak dikenal.
# Diambil dari yang AKTIF: bawaan yang ditunda berarti bagas-ai mendarat di
# model yang pengguna sendiri tak boleh memilihnya.
DEFAULT_ID = MODELS[_AKTIF[0]].id


# Nama LAMA yang masih melekat di ingatan pengguna. Diterima diam-diam supaya
# mengetik nama yang dulu benar tak berujung "model tak dikenal" — kegagalan
# yang menyesatkan, sebab situsnya sendiri masih mengalihkan cici.com ke Dola.
_ALIAS_LAMA = {"cici": "dola-web", "cici-web": "dola-web", "web/cici": "dola-web"}


def _pastikan_aktif(spec: ModelSpec) -> ModelSpec:
    """Tolak model yang belum boleh dipakai — dengan alasan, bukan sekadar 'gagal'.

    Dua sebab penolakan: model sedang DITUNDA, atau model API tapi kunci
    penyedianya kosong.

    Penolakannya di SINI, satu pintu untuk semua jalan masuk (/model, argumen
    baris perintah, tombol Telegram, preferensi tersimpan): kalau tiap
    antarmuka menyaring sendiri-sendiri, cepat atau lambat ada satu yang lupa."""
    if spec.is_api and not config.has_api_key(spec.provider):
        env_name = config.api_key_env(spec.provider)
        # Diperiksa DI SINI, bukan saat giliran berjalan: kalau tidak, pengguna
        # baru tahu key-nya kosong sesudah mengirim pesan panjang, dan pesan itu
        # sudah masuk riwayat sebagai giliran gagal.
        if spec.provider == "openrouter":
            raise ValueError(
                f"Model {spec.label} lewat API OpenRouter dan butuh "
                f"{env_name}, yang belum diisi. Isi di {config.ENV_FILE} "
                f"(baris: {env_name}=sk-or-...), ambil key di "
                "https://openrouter.ai/keys — atau pilih model (web) mana "
                "pun, yang tak butuh key sama sekali."
            )
        raise ValueError(
            f"Model {spec.label} lewat API NVIDIA dan butuh {env_name}, "
            f"yang belum diisi. Isi di {config.ENV_FILE} "
            f"(baris: {env_name}=nvapi-...), key gratis di "
            "https://build.nvidia.com — atau pilih model (web) mana pun, "
            "yang tak butuh key sama sekali."
        )
    if spec.aktif:
        return spec
    raise ValueError(
        f"Model {spec.label} sedang DITUNDA — untuk sementara bagas-ai hanya "
        "memakai " + ", ".join(MODELS[k].label for k in _AKTIF) + ". "
        "Connector-nya tak dihapus, jadi ini bisa dibuka lagi kapan saja."
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
    raise ValueError(
        f"Model '{name}' tidak dikenal. Yang tersedia: "
        + ", ".join(_ORDER)
        + ". Ketik /model untuk memilih."
    )


def resolve(name: str) -> ModelSpec:
    """Model yang BOLEH dipakai sekarang, dari alias/nomor/ID/label.

    Model yang ditunda tetap dikenali lalu ditolak dengan alasannya — bukan
    dijawab "tidak dikenal", yang akan membuat pengguna mengira model itu
    hilang lalu mencari-cari nama yang sebenarnya masih ada."""
    return _pastikan_aktif(cari(name))


def spec_for_id(model_id: str) -> ModelSpec:
    """ModelSpec dari ID tersimpan (prefs/.env).

    ID lama peninggalan era NVIDIA (mis. "z-ai/glm-5.2") tak lagi ada. Alih-alih
    membuat ModelSpec palsu yang pasti gagal saat dipakai, kembalikan model
    bawaan — pengguna lama otomatis mendarat di model yang benar-benar jalan.

    Model yang DITUNDA diperlakukan sama: preferensi lama yang menunjuk ke sana
    dialihkan ke bawaan. Kalau tidak, sesi berikutnya dimulai dengan model yang
    tak boleh dipilih, dan tiap /model justru menolak mengembalikannya.
    """
    for spec in MODELS.values():
        if spec.id == model_id and spec.aktif:
            return spec
    return MODELS[_AKTIF[0]]


def is_known_id(model_id: str) -> bool:
    """True bila ID ini masih boleh dipakai apa adanya.

    Model yang ditunda sengaja dijawab False: pemanggilnya (Agent.__init__)
    memakai ini untuk MENYIMPAN ULANG preferensi ke model hasil pemetaan —
    tanpa itu, peringatan yang sama muncul tiap kali bagas-ai dijalankan."""
    return any(spec.id == model_id and spec.aktif for spec in MODELS.values())


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
    bukan cuma tak terpilih, tapi tak boleh ditawarkan sama sekali."""
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
        mark = "  <- aktif" if current_id and spec.id == current_id else ""
        if spec.ditunda:
            mark = "  (ditunda — belum bisa dipilih)"
        elif spec.is_api and not config.has_api_key(spec.provider):
            # Tetap DITAMPILKAN, bukan disembunyikan: pengguna perlu tahu model
            # ini ada dan apa syaratnya. Menyembunyikannya membuat fitur yang
            # sudah terpasang terlihat tak pernah ada.
            mark = f"  (butuh {config.api_key_env(spec.provider)})"
        lines.append(f"  {i:>2}. {key:12s} {spec.label}{tag}{mark}")
    contoh = _AKTIF[0]
    lines.append(f"Pilih: /model <nama|nomor>   contoh: /model {contoh}")
    return "\n".join(lines)

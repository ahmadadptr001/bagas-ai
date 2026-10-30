"""Daftar model yang tersedia + util untuk memilih model.

bagas-ai punya DUA jalur model yang cara kerjanya berbeda mendasar:

  1. CONNECTOR WEB (`web/...`) — antarmuka chat berbasis browser lewat
     Playwright memakai akun pengguna sendiri. Konteks dipegang SITUSNYA,
     tool dipanggil lewat protokol teks [[TOOL]], dan /effort berarti
     MENGKLIK tombol mode berpikir di halamannya. Seluruh entri web sedang
     ditunda (lihat _DITUNDA).
  2. CLI OPENCODE (`opencode/...`) — `opencode run --format json --thinking`.
     Binary `opencode` di PATH adalah satu-satunya syarat; tanpa API key.
     OpenCode memegang loop agennya sendiri (tool dieksekusi di sana dengan
     --auto), dan peristiwa "reasoning" dari CLI diteruskan ke UI sebagai
     aliran pikiran realtime (ThinkingBlock di TUI, blok 💭 di CLI).
     Katalog disinkronkan dinamis lewat `opencode models` (hanya menambah;
     tak ada entri opencode yang ditunda).

Model API NVIDIA dan OpenRouter SENGAJA TIDAK ADA LAGI di katalog ini
(dihapus atas permintaan pengguna). Sisa kode penyedia di llm.py/config.py
hanya jaring pengaman lama; jangan mendaftarkannya kembali tanpa mengukur
ulang perilakunya.

Karena itu `ModelSpec.is_web` adalah satu-satunya titik percabangan; lihat
core.Agent.run().
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
    id: str  # ID internal: "web/<service>" (browser) atau "opencode/<slug>" (CLI)
    label: str  # nama tampilan
    # Nama service connector ("kimi", "qwen", "gemini") -> agent/connectors.
    # KOSONG untuk model API — inilah penanda jalur mana yang dipakai.
    connector: str = ""
    multimodal: bool = True  # situs AI web menerima lampiran gambar
    note: str = ""  # keterangan singkat
    # DITUNDA: entrinya tetap disimpan tetapi disembunyikan dari /model.
    # Lihat catatan _DITUNDA di bawah.
    ditunda: bool = False

    # --- khusus jalur API (kosong/nol untuk model web) ----------------------
    # Penyedia endpoint: "opencode" (CLI `opencode run`; kunci
    # OPENCODE_API_KEY hanya untuk jalur cadangan HTTP Zen). Nilai "nvidia"
    # dan "openrouter" tinggal sebagai bawaan/legacy untuk spec sintetis;
    # katalog keduanya sudah dihapus. Menentukan klien, API key, dan pesan
    # galat mana yang dipakai.
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
        penyedia; kecuali opencode/* yang gratis lewat CLI tanpa key)."""
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
# Sebabnya satu: SELURUH jalur browser dimatikan. Tiap model (web) butuh
# jendela Chrome, login, dan detik-detik menunggu situsnya — sementara jalur
# CLI menjawab tanpa browser sama sekali.
#
# Model `opencode/*` TIDAK PERNAH ditunda (permintaan pengguna 2026-10-31):
# seluruh entri statis maupun yang dilaporkan `opencode models` boleh dipilih,
# termasuk yang belum muncul di CLI. Katalog tetap disinkronkan dinamis untuk
# MENAMBAH model baru, bukan untuk menyembunyikan yang lama.
#
# Entri yang ditunda SENGAJA TIDAK DIHAPUS: connector-nya utuh, ujinya utuh,
# profil login-nya utuh, dan /web tetap bisa mengurus profilnya. Yang berubah
# cuma satu — ia tak bisa dipilih. Menghidupkannya kembali = keluarkan aliasnya
# dari himpunan ini, tanpa menyentuh kode lain.
#
# Sengaja daftar ALIAS yang ditunda, bukan daftar yang aktif: menambah model
# baru kelak tak boleh diam-diam ikut terkunci hanya karena lupa didaftarkan.
_DITUNDA = {
    # Seluruh jalur browser — atas permintaan pengguna (2026-09-21). Tiap
    # model (web) butuh jendela Chrome, login, dan detik-detik menunggu
    # situsnya, sementara jalur CLI menjawab tanpa browser sama sekali.
    "chatgpt-web", "kimi-web", "gemini-web", "qwen-web", "glm-web", "dola-web",
}

# Penanda tampilan untuk model yang ditunda di menu SelectScreen (UI Textual).
# Opsi yang tampilannya berawalan ini dirender REDUP dan tak bisa disorot —
# panah melewatinya, persis seperti model yang ditunda di menu /model versi
# CLI. Konstanta ini tinggal di SINI, bukan di modul UI-nya, karena "model ini
# ditunda" adalah fakta katalog; modul UI cuma merendernya.
_TANDA_DITUNDA = "\x00DITUNDA"

# Alias pendek -> spesifikasi. Urutan menentukan nomor pada /model.
MODELS: dict[str, ModelSpec] = {
    # --- jalur OpenCode CLI (`opencode run`) — PALING ATAS -------------------
    # TERUKUR 2026-09-23: akses HTTP anonim ke Zen sudah DITUTUP penyedianya
    # (HTTP 403 "only from within OpenCode"), tapi binary CLI `opencode` tetap
    # diizinkan — dan `opencode run -m <model> --format json` menjawab TANPA
    # API key sama sekali. Karena itu gerbang config.has_api_key("opencode")
    # kini menanyakan keberadaan binary itu (atau OPENCODE_API_KEY untuk
    # jalur cadangan HTTP), bukan menuntut key.
    #
    # Katalog di bawah adalah FALLBACK statis. Saat import, sinkron_model_
    # opencode() mencoba `opencode models` (CLI) dan MENAMBAH/MEMPERBARUI
    # entri sesuai daftar hidup penyedia — model baru muncul, model yang
    # hilang ditandai ditunda. Bila CLI tak ada / perintah gagal, daftar
    # statis ini dipakai apa adanya.
    #
    # api_style "responses" hanya dipertahankan untuk jalur CADANGAN HTTP;
    # jalur CLI mengabaikannya (opencode run yang memegang protokolnya).
    "big-pickle": ModelSpec(
        id="opencode/big-pickle",
        label="Big Pickle (CLI)",
        provider="opencode",
        api_model="opencode/big-pickle",
        multimodal=False,
        note=("Via CLI opencode run — model pilihan tim opencode untuk "
              "agent koding; gratis tanpa API key"),
        max_tokens=16384,
    ),
    "hy3-free": ModelSpec(
        id="opencode/hy3-free",
        label="Hy3 Free (CLI)",
        provider="opencode",
        api_model="opencode/hy3-free",
        multimodal=False,
        note="Via CLI opencode run — gratis tanpa API key",
        max_tokens=16384,
    ),
    "ling-3.0-flash-fin-free": ModelSpec(
        id="opencode/ling-3.0-flash-fin-free",
        label="Ling 3.0 Flash Fin Free (CLI)",
        provider="opencode",
        api_model="opencode/ling-3.0-flash-fin-free",
        multimodal=False,
        note="Via CLI opencode run — gratis tanpa API key",
        max_tokens=16384,
    ),
    "mimo-v2.5-free": ModelSpec(
        id="opencode/mimo-v2.5-free",
        label="MiMo-V2.5 Free (CLI)",
        provider="opencode",
        api_model="opencode/mimo-v2.5-free",
        multimodal=False,
        note="Via CLI opencode run — gratis tanpa API key",
        max_tokens=16384,
    ),
    "mimo-v2.6-flash-free": ModelSpec(
        id="opencode/mimo-v2.6-flash-free",
        label="MiMo-V2.6 Flash Free (CLI)",
        provider="opencode",
        api_model="opencode/mimo-v2.6-flash-free",
        multimodal=False,
        note="Via CLI opencode run — gratis tanpa API key",
        max_tokens=16384,
    ),
    "muse-spark-1.2-contributor-free": ModelSpec(
        id="opencode/muse-spark-1.2-contributor-free",
        label="Muse Spark 1.2 Contributor Free (CLI)",
        provider="opencode",
        api_model="opencode/muse-spark-1.2-contributor-free",
        multimodal=False,
        api_style="responses",  # cadangan HTTP: /chat membalas 500 utk model ini
        note="Via CLI opencode run — gratis tanpa API key",
        max_tokens=16384,
    ),
    "muse-spark-1.3-contributor-free": ModelSpec(
        id="opencode/muse-spark-1.3-contributor-free",
        label="Muse Spark 1.3 Contributor Free (CLI)",
        provider="opencode",
        api_model="opencode/muse-spark-1.3-contributor-free",
        multimodal=False,
        note="Via CLI opencode run — gratis tanpa API key",
        max_tokens=16384,
    ),
    "nemotron-3-ultra-free": ModelSpec(
        id="opencode/nemotron-3-ultra-free",
        label="Nemotron 3 Ultra Free (CLI)",
        provider="opencode",
        api_model="opencode/nemotron-3-ultra-free",
        multimodal=False,
        note="Via CLI opencode run — gratis tanpa API key",
        max_tokens=16384,
    ),
    "nemotron-3.5-lightning-free": ModelSpec(
        id="opencode/nemotron-3.5-lightning-free",
        label="Nemotron 3.5 Lightning Free (CLI)",
        provider="opencode",
        api_model="opencode/nemotron-3.5-lightning-free",
        multimodal=False,
        note="Via CLI opencode run — gratis tanpa API key",
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
}

MODELS = {k: (v if k not in _DITUNDA else
              # replace(): ModelSpec frozen, jadi penandaannya dibuat sebagai
              # salinan. Ditandai DI SINI, bukan ditulis satu per satu di tiap
              # entri, supaya daftar _DITUNDA tetap satu-satunya sumber
              # kebenaran — tak ada kemungkinan keduanya berselisih.
              replace(v, ditunda=True))
          for k, v in MODELS.items()}

_ORDER = [k for k, spec in MODELS.items() if spec.aktif]
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
# nama yang benar-benar ada di katalog. _hitung_ulang_katalog() (di bawah)
# memperbarui daftar ini kapan pun MODELS berubah — mis. sesudah sinkron CLI.
_AKTIF = _SIAP or [k for k in _ORDER if MODELS[k].aktif] or _ORDER

# Model bawaan bila tak ada preferensi tersimpan / preferensinya tak dikenal.
# Diambil dari yang AKTIF: bawaan yang ditunda berarti bagas-ai mendarat di
# model yang pengguna sendiri tak boleh memilihnya.
DEFAULT_ID = MODELS[_AKTIF[0]].id


# Nama LAMA yang masih melekat di ingatan pengguna. Diterima diam-diam supaya
# mengetik nama yang dulu benar tak berujung "model tak dikenal" — kegagalan
# yang menyesatkan, sebab situsnya sendiri masih mengalihkan cici.com ke Dola.
_ALIAS_LAMA = {"cici": "dola-web", "cici-web": "dola-web", "web/cici": "dola-web"}


def _hitung_ulang_katalog() -> None:
    """Hitung ulang _ORDER/_SIAP/_AKTIF/DEFAULT_ID setelah MODELS berubah."""
    global _ORDER, _SIAP, _AKTIF, DEFAULT_ID
    for k in list(MODELS):
        spec = MODELS[k]
        if k in _DITUNDA and not spec.ditunda:
            MODELS[k] = replace(spec, ditunda=True)
        elif (k not in _DITUNDA and spec.ditunda
              and spec.provider == "opencode"):
            # Hanya opencode/* yang boleh dibuka kembali sinkron CLI;
            # entri (web) tetap di _DITUNDA sampai diminta pengguna.
            MODELS[k] = replace(spec, ditunda=False)
    _ORDER = [k for k, spec in MODELS.items() if spec.aktif]
    _SIAP = [
        k for k in _ORDER
        if MODELS[k].aktif
        and (not MODELS[k].is_api or config.has_api_key(MODELS[k].provider))
    ]
    _AKTIF = _SIAP or [k for k in _ORDER if MODELS[k].aktif] or _ORDER
    DEFAULT_ID = MODELS[_AKTIF[0]].id


def sinkron_model_opencode(*, refresh: bool = False) -> list[str]:
    """Sinkron katalog opencode/* dari CLI `opencode models` (dinamis).

    Mengembalikan daftar alias yang terlihat di CLI. Gagal total (binary tak
    ada, perintah error, timeout) -> kembalikan [] dan biarkan daftar statis
    dipakai apa adanya — import tidak boleh meledak hanya karena CLI absen.

    Sinkron ini HANYA MENAMBAH model baru. Entri statis yang tak lagi muncul
    di CLI TIDAK ditunda (permintaan pengguna 2026-10-31): semua model
    opencode/* tetap boleh dipilih.

    `refresh=True` menambah flag --refresh (paksa update cache models.dev);
    bawaan memakai cache CLI supaya startup tetap cepat.
    """
    import shutil
    import subprocess

    exe = shutil.which("opencode")
    if not exe:
        return []
    cmd = [exe, "models", "opencode"]
    if refresh:
        cmd.append("--refresh")
    try:
        proses = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if proses.returncode != 0:
        return []
    alias_hidup: list[str] = []
    for baris in (proses.stdout or "").splitlines():
        baris = baris.strip()
        if not baris.startswith("opencode/"):
            continue
        alias = baris[len("opencode/"):].strip()
        if not alias or "/" in alias:
            continue
        alias_hidup.append(alias)
    if not alias_hidup:
        return []
    for alias in alias_hidup:
        if alias not in MODELS:
            MODELS[alias] = ModelSpec(
                id=f"opencode/{alias}",
                label=f"{alias} (CLI)",
                provider="opencode",
                api_model=f"opencode/{alias}",
                multimodal=False,
                note="Via CLI opencode run — gratis tanpa API key",
                max_tokens=16384,
            )
    for alias in alias_hidup:
        if alias in _DITUNDA:
            _DITUNDA.discard(alias)
    _hitung_ulang_katalog()
    return alias_hidup


# Sinkron SEKALI saat import (best-effort, pakai cache CLI). Tanpa CLI,
# katalog statis di atas dipakai apa adanya.
sinkron_model_opencode()


def _sebut_yang_bisa_dipakai() -> str:
    """Kalimat lanjutan yang menyebut model mana yang BENAR-BENAR bisa dipakai.

    Satu kalimat untuk EMPAT tempat (tiga penolakan + satu 'model tak dikenal'),
    dan itu memang alasannya: dulu tiap tempat menyusun daftarnya sendiri dari
    _AKTIF, jadi begitu keadaan katalog berubah — seluruh model (web) ditunda,
    lalu belum ada key sama sekali — keempatnya ikut berbohong bersama-sama,
    masing-masing dengan caranya. Satu fungsi berarti satu kebenaran.

    Dua bentuk, sesuai apa yang benar-benar ada:
      * ada yang siap -> sebutkan namanya (pengguna tinggal menyalin);
      * CLI tak tersedia -> sebut SEBABNYA, sebab tak ada satu nama pun yang
        jujur bisa disebut "bisa dipakai sekarang"."""
    if _SIAP:
        return ("Yang bisa dipakai sekarang: "
                + ", ".join(MODELS[k].label for k in _SIAP) + ".")
    return ("Belum ada satu pun model yang bisa dipakai: binary `opencode` "
            "tidak ditemukan di PATH dan OPENCODE_API_KEY kosong (jalur web "
            "sedang ditunda). Pasang CLI dengan `npm i -g opencode-ai`.")


def _pastikan_aktif(spec: ModelSpec) -> ModelSpec:
    """Tolak model yang belum boleh dipakai — dengan alasan, bukan sekadar 'gagal'.

    Dua sebab penolakan: model sedang DITUNDA, atau model API tapi kunci
    penyedianya kosong.

    Penolakannya di SINI, satu pintu untuk semua jalan masuk (/model, argumen
    baris perintah, tombol Telegram, preferensi tersimpan): kalau tiap
    antarmuka menyaring sendiri-sendiri, cepat atau lambat ada satu yang lupa."""
    # DITUNDA diperiksa LEBIH DULU, dan urutannya penting. Menaruh pemeriksaan
    # kunci di depan berarti model yang ditunda dijawab "butuh kunci":
    # pengguna lalu pergi mencari kunci yang, setelah didapat, tetap tak
    # membuka apa pun karena sebabnya bukan itu. Sebab yang lebih menentukan
    # disebut lebih dulu.
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
            # Cabang ini TERJANGKAU hanya bila binary `opencode` juga hilang
            # dari PATH: biasanya has_api_key("opencode") True cukup dengan
            # keberadaan CLI (lihat config.opencode_cli_tersedia). Jalur
            # cadangan HTTP butuh key karena akses anonimnya ditutup penyedia
            # (TERUKUR 2026-09-21: HTTP 403 FreeTierError).
            raise ValueError(
                f"Model {spec.label} lewat CLI `opencode run`, tapi binary "
                "`opencode` tidak ditemukan di PATH dan OPENCODE_API_KEY juga "
                "kosong (dipakai jalur cadangan HTTP; akses anonimnya sudah "
                "ditutup — TERUKUR 2026-09-21, HTTP 403). Pasang CLI "
                "(`npm i -g opencode-ai`), atau isi OPENCODE_API_KEY di "
                f"{config.ENV_FILE} / jalankan `opencode auth login`."
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
    # bebas memakai model mana pun dari katalog API. Kini tak ada katalog:
    # menerima ID sembarangan hanya akan membuat giliran gagal saat dijalankan,
    # jadi lebih baik ditolak di sini dengan daftar yang jelas.
    #
    # Daftarnya diambil dari _SIAP, BUKAN _ORDER: menyebut entri web yang
    # ditunda berarti menawarkan nama yang pasti ditolak langkah berikutnya —
    # pengguna mengetiknya, lalu dapat penolakan yang seharusnya bisa ia
    # hindari sejak awal. Yang ditunda tetap disebut, tapi sebagai keterangan,
    # bukan sebagai pilihan.
    #
    # _SIAP yang kosong (belum ada satu pun key) diperlakukan sebagai cabang
    # tersendiri: di keadaan itulah tak ada nama yang jujur bisa disebut
    # "bisa dipakai sekarang", jadi yang disebut justru sebabnya.
    pesan = (f"Model '{name}' tidak dikenal. " + _sebut_yang_bisa_dipakai()
             + " Ketik /model untuk melihat katalog.")
    raise ValueError(pesan)


def resolve(name: str) -> ModelSpec:
    """Model yang BOLEH dipakai sekarang, dari alias/nomor/ID/label.

    Model yang ditunda tetap dikenali lalu ditolak dengan alasannya — bukan
    dijawab "tidak dikenal", yang akan membuat pengguna mengira model itu
    hilang lalu mencari-cari nama yang sebenarnya masih ada."""
    spec = cari(name)
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
    for spec in MODELS.values():
        if spec.id == model_id and spec.aktif:
            return spec
    return MODELS[_AKTIF[0]]


def is_known_id(model_id: str) -> bool:
    """True bila ID ini masih boleh dipakai apa adanya.

    Model yang ditunda sengaja dijawab False: pemanggilnya (Agent.__init__)
    memakai ini untuk MENYIMPAN ULANG preferensi ke model hasil pemetaan —
    tanpa itu, peringatan yang sama muncul tiap kali bagas-ai dijalankan."""
    return any(spec.id == model_id and spec.aktif
               for spec in MODELS.values())


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
    if not any(s.is_web and s.aktif for s in MODELS.values()):
        return hasil
    try:
        from . import connectors  # impor tunda: connectors butuh Playwright?
        for key, spec in MODELS.items():
            if not (spec.aktif and spec.is_web and spec.connector):
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

    Dua kelompok: OpenCode CLI (gratis, tanpa API key) dan AI web (browser).
    Urutan kemunculannya mengikuti urutan MODELS, jadi kategori tak perlu
    didaftarkan terpisah."""
    if spec.provider == "opencode":
        return "OpenCode CLI — gratis, tanpa API key"
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
    for key in _ORDER:
        spec = MODELS[key]
        if spec.is_web:
            ops = varian.get(key)
            if ops:
                items = [f"{key} {label}" for label, _desc in ops]
            else:
                items = [key]
        else:
            items = [key]
        label_bebas = " (rekomendasi)" if spec.rekomendasi else ""
        grup.setdefault(kategori_model(spec), []).extend(
            (it + label_bebas, it) for it in items)
    return list(grup.items())


def pilihan_model() -> list[str]:
    """Daftar pilihan untuk menu /model: nama MODEL SUNGGUHAN.

    Urutannya SAMA dengan hasil rata pilihan_model_grup() — bukan sekadar
    daftar setara — supaya nomor/urutan di menu selalu cocok dengan
    kelompok kategori. Tiap layanan web memuai jadi tiap variannya
    ("glm GLM-5.2", …) memakai bentuk "<alias> <varian>" — itulah yang
    diterima Agent.set_model."""
    return [v for _, ops in pilihan_model_grup() for _, v in ops]


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


def catalog(*, include_postponed: bool = False) -> list[tuple[int, str, ModelSpec]]:
    """Pilihan aktif; profil browser lama dapat diakses oleh administrasi /web."""
    keys = list(MODELS) if include_postponed else _ORDER
    return [(i, key, MODELS[key]) for i, key in enumerate(keys, start=1)]


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
    lines = ["Model tersedia — OpenCode CLI (gratis, tanpa API key):"]
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

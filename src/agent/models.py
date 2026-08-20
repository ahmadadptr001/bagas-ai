"""Daftar model yang tersedia + util untuk memilih model.

bagas-ai kini bermodalkan DUA hal saja: bot Telegram dan model AI web lewat
browser. Tidak ada lagi model berbayar/ber-API-key: seluruh entri di bawah
adalah CONNECTOR ke antarmuka chat berbasis browser (lihat agent/connectors),
dijalankan lewat Playwright memakai akun milik pengguna sendiri.

Dulu daftar ini berisi ~20 model yang di-host NVIDIA (integrate.api.nvidia.com)
dan connector web cuma pelengkap. Itu DIHAPUS seluruhnya — beserta API key,
endpoint, mode /effort ala API (Nemotron reasoning_budget & gpt-oss
reasoning_effort), dan tool vision berbasis VLM. Yang tersisa sengaja
sesederhana ini: satu jenis model, satu cara kerja.

Konsekuensi yang disengaja:
  - tak ada lagi kredensial yang perlu diisi saat instalasi;
  - /effort tidak lagi mengirim parameter API, melainkan MENGKLIK tombol mode
    berpikir di situsnya (lihat WebConnector.web_actions);
  - gambar tidak lagi dianalisis lewat model vision terpisah, melainkan
    DILAMPIRKAN ke percakapan web (lihat attachments di core._run_connector).
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelSpec:
    id: str  # ID internal, selalu berbentuk "web/<service>"
    label: str  # nama tampilan
    # Nama service connector ("kimi", "qwen", "gemini") -> agent/connectors.
    # Selalu terisi: semua model bagas-ai kini berbasis browser.
    connector: str = ""
    multimodal: bool = True  # semua situs AI web menerima lampiran gambar
    note: str = ""  # keterangan singkat
    # DITUNDA: entrinya tetap ada & tetap tampil di /model, tapi tak bisa
    # dipilih. Lihat catatan _DITUNDA di bawah.
    ditunda: bool = False

    @property
    def is_web(self) -> bool:
        """True bila model ini connector web-AI (butuh browser + login)."""
        return bool(self.connector)

    @property
    def aktif(self) -> bool:
        """True bila model ini boleh dipilih pengguna sekarang."""
        return not self.ditunda


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
    """Tolak model yang sedang ditunda — dengan alasan, bukan sekadar 'gagal'.

    Penolakannya di SINI, satu pintu untuk semua jalan masuk (/model, argumen
    baris perintah, tombol Telegram, preferensi tersimpan): kalau tiap
    antarmuka menyaring sendiri-sendiri, cepat atau lambat ada satu yang lupa."""
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
    lines = ["Model (semua via browser — butuh login sekali):"]
    if len(_AKTIF) < len(_ORDER):
        lines.append(
            "Untuk sementara hanya " + ", ".join(MODELS[k].label for k in _AKTIF)
            + " yang bisa dipilih; sisanya ditunda (connector-nya tak dihapus).")
    for i, key in enumerate(_ORDER, start=1):
        spec = MODELS[key]
        tag = f"  [{spec.note}]" if spec.note else ""
        mark = "  <- aktif" if current_id and spec.id == current_id else ""
        if spec.ditunda:
            mark = "  (ditunda — belum bisa dipilih)"
        lines.append(f"  {i:>2}. {key:12s} {spec.label}{tag}{mark}")
    contoh = _AKTIF[0]
    lines.append(f"Pilih: /model <nama|nomor>   contoh: /model {contoh}")
    return "\n".join(lines)

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

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str  # ID internal, selalu berbentuk "web/<service>"
    label: str  # nama tampilan
    # Nama service connector ("claude", "qwen", "kimi") -> agent/connectors.
    # Selalu terisi: semua model bagas-ai kini berbasis browser.
    connector: str = ""
    multimodal: bool = True  # semua situs AI web menerima lampiran gambar
    note: str = ""  # keterangan singkat

    @property
    def is_web(self) -> bool:
        """True bila model ini connector web-AI (butuh browser + login)."""
        return bool(self.connector)


# Alias pendek -> spesifikasi. Urutan menentukan nomor pada /model.
MODELS: dict[str, ModelSpec] = {
    "kimi-web": ModelSpec(
        id="web/kimi",
        label="Kimi (web)",
        connector="kimi",
        note="Via browser kimi.com — jago agentic & coding, konteks panjang",
    ),
    "claude-web": ModelSpec(
        id="web/claude",
        label="Claude (web)",
        connector="claude",
        note="Via browser claude.ai — penalaran & penulisan kuat",
    ),
    "qwen-web": ModelSpec(
        id="web/qwen",
        label="Qwen (web)",
        connector="qwen",
        note="Via browser chat.qwen.ai — multibahasa & cepat",
    ),
}

_ORDER = list(MODELS.keys())

# Model bawaan bila tak ada preferensi tersimpan / preferensinya tak dikenal.
DEFAULT_ID = MODELS[_ORDER[0]].id


def resolve(name: str) -> ModelSpec:
    """Cari ModelSpec dari alias, nomor (1..N), ID penuh, atau label."""
    key = name.strip().lower()

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


def spec_for_id(model_id: str) -> ModelSpec:
    """ModelSpec dari ID tersimpan (prefs/.env).

    ID lama peninggalan era NVIDIA (mis. "z-ai/glm-5.2") tak lagi ada. Alih-alih
    membuat ModelSpec palsu yang pasti gagal saat dipakai, kembalikan model
    bawaan — pengguna lama otomatis mendarat di model yang benar-benar jalan.
    """
    for spec in MODELS.values():
        if spec.id == model_id:
            return spec
    return MODELS[_ORDER[0]]


def is_known_id(model_id: str) -> bool:
    """True bila ID ini benar-benar ada di daftar (bukan hasil pemetaan ulang)."""
    return any(spec.id == model_id for spec in MODELS.values())


# random_fallback() DIHAPUS: pemakainya dulu _escalate (naik-kelas otomatis) dan
# migrasi preferensi DeepSeek, keduanya ikut hilang bersama katalog ber-API-key.
# Membiarkannya berbahaya, bukan sekadar sampah: ia memilih model ACAK, jadi bila
# kelak dipanggil lagi karena disangka masih dipakai, ia akan memindahkan
# pengguna ke layanan web lain DI TENGAH tugas — memicu jendela login mendadak
# dan memutus konteks percakapan, persis alasan naik-kelas otomatis dihapus.


def catalog() -> list[tuple[int, str, ModelSpec]]:
    """Daftar (nomor, alias, spec) terurut — untuk ditampilkan sebagai tabel."""
    return [(i, key, MODELS[key]) for i, key in enumerate(_ORDER, start=1)]


def list_text(current_id: str | None = None) -> str:
    """Daftar model siap tampil untuk perintah /model."""
    lines = ["Model tersedia (semua via browser — butuh login sekali):"]
    for i, key in enumerate(_ORDER, start=1):
        spec = MODELS[key]
        tag = f"  [{spec.note}]" if spec.note else ""
        mark = "  <- aktif" if current_id and spec.id == current_id else ""
        lines.append(f"  {i:>2}. {key:12s} {spec.label}{tag}{mark}")
    lines.append("Pilih: /model <nama|nomor>   contoh: /model kimi-web  atau  /model 1")
    return "\n".join(lines)

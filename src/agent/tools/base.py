"""Kerangka tool: dekorator @tool yang otomatis membuat skema fungsi OpenAI
dari type hints + docstring, plus registry global."""
from __future__ import annotations

import contextvars
import json
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, get_args, get_origin, get_type_hints

# Pemetaan tipe Python -> tipe JSON Schema.
_JSON_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class Tool:
    """Satu tool: fungsi + skema yang bisa dikirim ke LLM."""

    name: str
    description: str
    func: Callable[..., Any]
    schema: dict[str, Any]

    def run(self, **kwargs: Any) -> Any:
        return self.func(**kwargs)


# Registry global: nama -> Tool.
REGISTRY: dict[str, Tool] = {}


def _json_type(py_type: Any) -> str:
    """Konversi anotasi tipe Python ke tipe JSON Schema (best-effort)."""
    origin = get_origin(py_type)
    if origin in (list, tuple):
        return "array"
    if origin is dict:
        return "object"
    # Tangani Optional[X] / X | None -> pakai tipe non-None pertama.
    if origin is not None:
        args = [a for a in get_args(py_type) if a is not type(None)]
        if args:
            return _json_type(args[0])
    return _JSON_TYPES.get(py_type, "string")


def _build_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Bangun skema fungsi OpenAI dari signature + docstring."""
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        ptype = hints.get(pname, str)
        prop: dict[str, Any] = {"type": _json_type(ptype)}
        if prop["type"] == "array":
            prop["items"] = {"type": "string"}
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    description = inspect.getdoc(func) or func.__name__
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description.strip().split("\n\n")[0],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Dekorator: daftarkan fungsi sebagai tool agent.

    Deskripsi tool diambil dari baris pertama docstring; parameter dari
    type hints. Fungsi tetap bisa dipanggil biasa di Python.
    """
    schema = _build_schema(func)
    REGISTRY[func.__name__] = Tool(
        name=func.__name__,
        description=schema["function"]["description"],
        func=func,
        schema=schema,
    )
    return func


def get_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Kembalikan daftar skema tool (semua, atau subset berdasarkan nama)."""
    tools = REGISTRY.values() if names is None else (
        REGISTRY[n] for n in names if n in REGISTRY
    )
    return [t.schema for t in tools]


# --- Penegakan: perubahan file WAJIB lewat write_file ---------------------
#
# Kenapa ditegakkan di sini, bukan cukup diminta lewat prompt:
#   - Diff berwarna (hijau/merah) HANYA dirender untuk write_file. Perubahan
#     yang dilakukan skrip tampil sebagai "menjalankan python" belaka, sehingga
#     pengguna kehilangan satu-satunya kesempatan meninjau sebelum file berubah.
#   - Instruksi protokol cuma dikirim SEKALI di awal percakapan web, jadi chat
#     lama tak pernah menerimanya, dan model mana pun bisa saja mengabaikannya.
#     Aturan yang cuma "diminta baik-baik" akan dilanggar cepat atau lambat.
#
# Yang dicegat HANYA penulisan file yang ditulis EKSPLISIT di potongan kode yang
# diusulkan model. Perintah yang kebetulan menghasilkan file sebagai efek samping
# (npm run build, pytest, kompilasi) TIDAK tersentuh karena polanya tak muncul di
# teks perintah.
_TULIS_PY = re.compile(
    # Mode TULIS pada open(): w/a/x, atau r+ . Sengaja TIDAK menuntut mode
    # berada tepat sebelum ')' — bentuk paling lazim justru
    # open(path, 'w', encoding='utf-8'), yang dulu lolos karena tuntutan itu.
    # Mode BACA ('r', 'rb') tak dicegat: membaca file memang wajar.
    r"""open\s*\([^)]*?['"](?:[wax][bt+]*|r\+[bt]*)['"]"""
    r"""|open\s*\([^)]*mode\s*=\s*['"][wax]"""           # open(..., mode="w")
    r"|\.write_text\s*\(|\.write_bytes\s*\("
    r"|\.writelines\s*\("
    r"|shutil\.(?:copy|copy2|copyfile|move)\s*\("
    r"|os\.(?:replace|rename|remove|unlink)\s*\("
    r"|json\.dump\s*\(|yaml\.(?:dump|safe_dump)\s*\(",
    re.IGNORECASE,
)
_TULIS_SH = re.compile(
    r">\s*[^\s|&>]+"                       # redirect > file  (juga menangkap >>)
    r"|\bSet-Content\b|\bOut-File\b|\bAdd-Content\b"
    r"|\btee\b"
    r"|\bsed\b[^|]*-i\b"
    r"|\bpatch\b\s|\bapplypatch\b"
    r"|\b(?:cp|mv)\s+[^\s|&]+\s+[^\s|&]+"
    # Mengunduh aset lewat shell juga berakhir sebagai berkas yang tak terlihat
    # pengguna, dan bergantung curl/wget kebetulan ada di PATH. download_file
    # melakukannya lintas-platform sekaligus memeriksa ukuran & tipe isinya.
    r"|\bcurl\b[^|]*\s-[oO]\b|\bwget\b[^|]*\s-O\b|\bwget\s+http"
    r"|\bInvoke-WebRequest\b[^|]*-OutFile\b|\bStart-BitsTransfer\b",
    re.IGNORECASE,
)
# Tulisan ke lokasi SEMENTARA memang wajar (pemrosesan data, berkas kerja) dan
# tak ada gunanya ditinjau — jangan dihalangi.
_SEMENTARA = re.compile(
    r"tempfile|mkstemp|mkdtemp|TemporaryDirectory|NamedTemporary"
    r"|/tmp/|\\temp\\|%TEMP%|\$TMPDIR|gettempdir",
    re.IGNORECASE,
)

_PESAN_TOLAK = (
    "[DITOLAK] Perubahan file TIDAK boleh lewat {tool}.\n\n"
    "Gunakan tool file yang tepat:\n"
    "  - ubah SEBAGIAN file (paling sering) -> edit_file "
    '{{"path": ..., "old_text": "potongan lama", "new_text": "penggantinya"}}\n'
    "  - file baru / ditulis ulang total    -> write_file "
    '{{"path": ..., "content": "...isi lengkap..."}}\n'
    "  - menambah di akhir file             -> append_file\n"
    "  - pindah / salin / hapus             -> move_file / copy_file / "
    "delete_file\n"
    "  - mengunduh aset dari internet       -> download_file "
    '{{"url": ..., "dest_path": "assets/img/x.png"}}\n\n'
    "Alasannya: hanya tool file yang menampilkan diff berwarna (hijau = baris "
    "ditambah, merah = dihapus) di terminal pengguna SEBELUM file disentuh. "
    "Perubahan lewat skrip tak terlihat sama sekali, jadi pengguna kehilangan "
    "satu-satunya kesempatan meninjaunya.\n"
    "Isi file panjang tidak masalah. Kalau menulis ulang seluruh file terasa "
    "berat, itu justru tanda kamu harus memakai edit_file — bukan tanda harus "
    "kembali ke skrip.\n\n"
    "{tool} tetap boleh untuk yang memang bukan mengedit file: menjalankan tes, "
    "memasang dependensi, menjalankan program, memeriksa hasil."
)


def _tolak_tulis_file(name: str, arguments: dict[str, Any]) -> str | None:
    """Pesan penolakan bila tool ini dipakai untuk MENULIS file, else None."""
    if name == "run_python":
        kode = str(arguments.get("code") or "")
        pola = _TULIS_PY
    elif name in ("run_command", "run_command_bg"):
        kode = str(arguments.get("command") or "")
        pola = _TULIS_SH
    else:
        return None
    if not kode or _SEMENTARA.search(kode):
        return None
    if not pola.search(kode):
        return None
    return _PESAN_TOLAK.format(tool=name)


# Nama tool yang SEDANG dijalankan. Dipakai lapisan yang jauh di bawah dan tak
# menerima nama itu lewat argumen — khususnya permintaan izin akses folder luar
# (permissions.py), yang perlu menyebut "MENULIS" vs "membaca" agar pengguna
# tahu apa yang sebenarnya hendak terjadi. ContextVar, bukan variabel global,
# supaya giliran CLI dan giliran Telegram yang berjalan bersamaan tak saling
# menimpa nama tool satu sama lain.
_tool_aktif: contextvars.ContextVar[str] = contextvars.ContextVar(
    "bagasai_tool_aktif", default="")


def tool_aktif() -> str:
    """Nama tool yang sedang dieksekusi ("" bila di luar eksekusi tool)."""
    return _tool_aktif.get()


# --- menyelaraskan bentuk argumen dari model ------------------------------
#
# Model web menyusun JSON-nya sendiri, dan ia kerap memilih bentuk yang PALING
# MASUK AKAL menurut nama parameternya — bukan yang tertulis di skema. Contoh
# nyata yang menjatuhkan giliran pengguna: read_files(paths) berskema string,
# tapi namanya jamak, jadi model mengirim ["a.py", "b.py"]; tool-nya memanggil
# .replace() pada list dan meledak dengan pesan yang tak bisa ditindaklanjuti
# ("'list' object has no attribute 'replace'").
#
# Menyalahkan modelnya tidak menyelesaikan apa pun: mengirim daftar untuk
# parameter bernama "paths" itu tebakan yang wajar. Yang salah adalah tool yang
# hancur oleh bentuk masukan paling alami. Maka penyelarasan dilakukan DI SINI,
# sekali, untuk seluruh tool — bukan ditambal satu per satu tiap kali ketahuan.
#
# Batasnya dijaga: hanya bentuk yang artinya TAK BERUBAH yang diselaraskan.
# Yang meragukan dibiarkan lewat apa adanya supaya tool-nya sendiri yang
# menolak dengan pesan yang lebih paham konteks.
def _gabung_baris(nilai: list) -> str:
    """Gabungkan list jadi string dengan BARIS BARU, bukan koma.

    Baris baru dipilih dengan sengaja: parameter yang menerima daftar path
    (read_files, validate_project) memang memecah pada baris baru maupun koma,
    sedangkan parameter berisi ISI BERKAS hanya benar bila digabung dengan
    baris baru. Koma akan merusak yang kedua — file jadi satu baris panjang
    berkoma, dan kerusakannya senyap."""
    return "\n".join("" if x is None else str(x) for x in nilai)


def _paksa_tipe(nilai: Any, tipe: str) -> Any:
    """Selaraskan `nilai` ke `tipe` skema bila artinya tak berubah."""
    if tipe == "string" and not isinstance(nilai, str):
        if isinstance(nilai, (list, tuple)):
            return _gabung_baris(list(nilai))
        if isinstance(nilai, (int, float, bool)):
            return str(nilai)
        return nilai                      # dict/None -> biar tool yang menolak
    if tipe == "array" and not isinstance(nilai, list):
        if isinstance(nilai, str):
            teks = nilai.strip()
            if teks.startswith("["):      # model menuliskan JSON sebagai teks
                try:
                    hasil = json.loads(teks)
                    return hasil if isinstance(hasil, list) else [hasil]
                except Exception:  # noqa: BLE001 - jatuh ke pemisahan biasa
                    pass
            if not teks:
                return []
            pecah = [p.strip() for p in teks.replace("\n", ",").split(",")]
            return [p for p in pecah if p]
        if isinstance(nilai, (tuple, set)):
            return list(nilai)
        if isinstance(nilai, dict):
            return [nilai]                # satu item ditulis tanpa pembungkus
        return nilai
    if tipe == "integer" and not isinstance(nilai, int):
        try:
            return int(str(nilai).strip())
        except (TypeError, ValueError):
            return nilai                  # bukan angka -> tool yang menjelaskan
    if tipe == "boolean" and not isinstance(nilai, bool):
        if isinstance(nilai, str):
            t = nilai.strip().lower()
            if t in ("true", "ya", "yes", "1"):
                return True
            if t in ("false", "tidak", "no", "0", ""):
                return False
        if isinstance(nilai, (int, float)):
            return bool(nilai)
    return nilai


# Nama argumen yang SERING dipakai model padahal bukan nama resminya. Tiap
# entri harus memenuhi tiga syarat, kalau tidak ia berbahaya:
#   1. artinya benar-benar sama (bukan konsep lain yang kebetulan mirip);
#   2. hanya dipakai bila nama resminya TIDAK ikut dikirim;
#   3. hanya dipakai bila tool-nya memang punya nama resmi itu.
# Yang tak memenuhi syarat sengaja dibiarkan gagal dengan pesan jelas: menebak
# lalu keliru jauh lebih mahal daripada satu bolak-balik untuk membetulkan.
_ALIAS_UMUM = {
    "file": "path", "filename": "path", "filepath": "path", "file_path": "path",
    "berkas": "path", "nama_file": "path",
    "cmd": "command", "perintah": "command",
    "q": "query", "kueri": "query", "keyword": "query",
    "text": "content", "isi": "content", "data": "content",
    "url_": "url", "link": "url",
}
# Khusus per tool, untuk hal yang tak berlaku umum. replace_in_files: `pattern`
# memang mencocokkan PATH RELATIF bila memuat "/" (lihat implementasinya), jadi
# path satu berkas yang dikirim sebagai `path` artinya persis sama.
_ALIAS_TOOL = {
    "replace_in_files": {"path": "pattern", "paths": "pattern"},
}


def _terapkan_alias(nama: str, arguments: dict[str, Any],
                    props: dict) -> dict[str, Any]:
    """Ganti nama argumen yang keliru-tapi-jelas-maksudnya ke nama resminya."""
    peta = {**_ALIAS_UMUM, **_ALIAS_TOOL.get(nama, {})}
    out = dict(arguments)
    for salah, benar in peta.items():
        if salah in out and benar in props and benar not in out:
            out[benar] = out.pop(salah)
    return out


def _selaraskan(tool_obj: "Tool", arguments: dict[str, Any]) -> dict[str, Any]:
    """Salinan `arguments` yang bentuknya sudah sesuai skema tool."""
    props = (tool_obj.schema.get("function", {})
             .get("parameters", {}).get("properties", {}) or {})
    out = {}
    for k, v in arguments.items():
        prop = props.get(k)
        out[k] = _paksa_tipe(v, prop.get("type", "string")) if prop else v
    return out


def execute(name: str, arguments: dict[str, Any]) -> str:
    """Jalankan tool berdasarkan nama; selalu kembalikan string untuk LLM."""
    tool_obj = REGISTRY.get(name)
    if tool_obj is None:
        return f"[error] tool '{name}' tidak ditemukan."
    if not isinstance(arguments, dict):
        return (f"[error] args untuk '{name}' harus objek JSON "
                f"(mis. {{\"path\": \"...\"}}), bukan {type(arguments).__name__}.")
    props = (tool_obj.schema.get("function", {})
             .get("parameters", {}).get("properties", {}) or {})
    arguments = _terapkan_alias(name, arguments, props)
    # Argumen bernama asing TIDAK dibuang diam-diam: kalau model salah menamai
    # parameter berisi konten, membuangnya berarti menulis berkas kosong tanpa
    # ada yang sadar. Lebih baik gagal jujur sambil menyebut nama yang benar.
    asing = [k for k in arguments if k not in props]
    if asing and props:
        return (f"[error] '{name}' tak punya argumen {', '.join(asing)}. "
                f"Yang diterima: {', '.join(props)}.")
    arguments = _selaraskan(tool_obj, arguments)
    tolak = _tolak_tulis_file(name, arguments)
    if tolak:
        return tolak
    token = _tool_aktif.set(name)
    try:
        result = tool_obj.run(**arguments)
        return result if isinstance(result, str) else str(result)
    except Exception as exc:  # noqa: BLE001 - laporkan error apa pun ke LLM
        return f"[error] gagal menjalankan '{name}': {exc}"
    finally:
        _tool_aktif.reset(token)

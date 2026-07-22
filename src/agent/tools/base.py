"""Kerangka tool: dekorator @tool yang otomatis membuat skema fungsi OpenAI
dari type hints + docstring, plus registry global."""
from __future__ import annotations

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
    r"|\b(?:cp|mv)\s+[^\s|&]+\s+[^\s|&]+",
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
    "Gunakan write_file — satu blok per file, berisi isi LENGKAP file itu:\n"
    '  {{"tool": "write_file", "args": {{"path": "src/contoh.js", '
    '"content": "...isi lengkap..."}}}}\n\n'
    "Alasannya: hanya write_file yang menampilkan diff berwarna (hijau = baris "
    "ditambah, merah = dihapus) di terminal pengguna SEBELUM file disentuh. "
    "Perubahan lewat skrip tak terlihat sama sekali, jadi pengguna kehilangan "
    "satu-satunya kesempatan meninjaunya.\n"
    "Isi file panjang tidak masalah — kalau sangat besar, kerjakan satu file "
    "per giliran. JANGAN kembali memakai skrip.\n\n"
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


def execute(name: str, arguments: dict[str, Any]) -> str:
    """Jalankan tool berdasarkan nama; selalu kembalikan string untuk LLM."""
    tool_obj = REGISTRY.get(name)
    if tool_obj is None:
        return f"[error] tool '{name}' tidak ditemukan."
    tolak = _tolak_tulis_file(name, arguments)
    if tolak:
        return tolak
    try:
        result = tool_obj.run(**arguments)
        return result if isinstance(result, str) else str(result)
    except Exception as exc:  # noqa: BLE001 - laporkan error apa pun ke LLM
        return f"[error] gagal menjalankan '{name}': {exc}"

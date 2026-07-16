"""Kerangka tool: dekorator @tool yang otomatis membuat skema fungsi OpenAI
dari type hints + docstring, plus registry global."""
from __future__ import annotations

import inspect
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


def execute(name: str, arguments: dict[str, Any]) -> str:
    """Jalankan tool berdasarkan nama; selalu kembalikan string untuk LLM."""
    tool_obj = REGISTRY.get(name)
    if tool_obj is None:
        return f"[error] tool '{name}' tidak ditemukan."
    try:
        result = tool_obj.run(**arguments)
        return result if isinstance(result, str) else str(result)
    except Exception as exc:  # noqa: BLE001 - laporkan error apa pun ke LLM
        return f"[error] gagal menjalankan '{name}': {exc}"

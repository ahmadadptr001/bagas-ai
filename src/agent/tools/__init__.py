"""Registry tool. Meng-import modul di sini akan mendaftarkan tool-nya
(lewat dekorator @tool) ke REGISTRY."""
from __future__ import annotations

from .base import REGISTRY, Tool, execute, get_schemas, tool

# Import demi efek samping: mendaftarkan tool ke REGISTRY.
from . import web_search  # noqa: F401,E402
from . import files  # noqa: F401,E402
from . import shell  # noqa: F401,E402
from . import memory_tool  # noqa: F401,E402
from . import media  # noqa: F401,E402
from . import scripts_tool  # noqa: F401,E402
from . import interact_tool  # noqa: F401,E402
from . import screen  # noqa: F401,E402

# vision.py sengaja TIDAK diimpor sebagai tool fungsi (dipakai langsung oleh
# antarmuka), jadi tidak didaftarkan di sini.

__all__ = ["REGISTRY", "Tool", "execute", "get_schemas", "tool"]

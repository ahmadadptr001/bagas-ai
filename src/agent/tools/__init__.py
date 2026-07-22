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
from . import search  # noqa: F401,E402
from . import extras  # noqa: F401,E402

# vision.py DIHAPUS bersama model ber-API-key: ia mengirim gambar ke VLM NVIDIA
# lewat endpoint terpisah. Gambar kini DILAMPIRKAN langsung ke percakapan web
# (Agent.run(attachments=...) & take_screenshot), jadi situs AI web sendiri yang
# melihatnya — dalam konteks percakapan yang sama, bukan panggilan sekali-pakai.

__all__ = ["REGISTRY", "Tool", "execute", "get_schemas", "tool"]

"""Metadata varian dari katalog server lokal OpenCode V2."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def model_variants(directory: Path) -> dict[str, tuple[str, ...]]:
    """Baca varian efektif, termasuk konfigurasi project, tanpa menjalankan model.

    `opencode models` menyalakan layanan lokal sebelum fungsi ini dipanggil.
    Kredensial layanan tidak pernah dikirim ke alamat selain loopback.
    Kegagalan metadata tidak boleh menghalangi katalog model biasa.
    """
    try:
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        service = json.loads((state / "opencode/service.json").read_text(encoding="utf-8"))
        base = str(service["url"]).rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1", "localhost")
                or parsed.username or parsed.password or parsed.path not in ("", "/")):
            return {}
        query = urlencode({"location[directory]": str(directory.resolve())})
        headers = {"Accept": "application/json"}
        if service.get("password"):
            credential = base64.b64encode(f"opencode:{service['password']}".encode()).decode()
            headers["Authorization"] = "Basic " + credential
        request = Request(f"{base}/api/model?{query}", headers=headers)
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=3) as response:
            payload = json.load(response)
        result = {}
        for model in payload.get("data", []):
            if model.get("providerID") != "opencode":
                continue
            variants = model.get("variants", [])
            result[str(model["id"])] = tuple(dict.fromkeys(
                v["id"] for v in variants
                if isinstance(v, dict) and isinstance(v.get("id"), str)
                and v["id"] and not v.get("disabled")
            ))
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}

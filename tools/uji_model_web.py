# -*- coding: utf-8 -*-
"""Uji Agent.pasang_model_web — tiga keadaan ganti model web.

Keadaan 1: browser mati            -> connect() + set_web_option(varian)
Keadaan 2: browser hidup, model lain -> tutup_service() + reset state + connect
Keadaan 3: browser hidup, model sama -> open_new_chat(new_tab=True) + konteks

Jalankan: PYTHONIOENCODING=utf-8 python tools/uji_model_web.py
"""
import contextlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@contextlib.contextmanager
def _ctx_kosong():
    yield

from agent import connectors as con_mod
from agent.connectors import browser as browser_mod
from agent.core import Agent

gagal = 0


class FakeConn:
    def __init__(self):
        self.aksi = []
        self.last_chat_id = "chat-999"

    def connect(self, **kw):
        self.aksi.append("connect")

    def open_new_chat(self, new_tab=False, **kw):
        self.aksi.append(("open_new_chat", new_tab))

    def set_web_option(self, label, **kw):
        self.aksi.append(("set_web_option", label))

    def send(self, teks, **kw):
        self.aksi.append(("send", teks[:40]))
        return "ok"


def model_spec():
    return SimpleNamespace(connector="glm", label="GLM (web)", is_web=True)


def fake_agent():
    return SimpleNamespace(
        model_spec=model_spec(),
        _web_varian=None,
        _web_terpasang={},
        _web_ctx_sent=False,
        _web_chars=123,
        _lupakan_chat_web=lambda: None,
    )


def jalankan(tag, kondisi, pesan):
    global gagal
    if kondisi:
        print(f"OK [{tag}]: {pesan}")
    else:
        print(f"GAGAL [{tag}]: {pesan}")
        gagal += 1


# ---------------------------------------------------------------- patch ----
_asli = (con_mod.get_connector, browser_mod.browser_hidup,
         browser_mod.tutup_service)
_conn = FakeConn()
_panggilan = {"tutup": 0}


def _get_connector(svc):
    return _conn


def _hidup(svc):
    return _HIDUP[0]


def _tutup(svc):
    _panggilan["tutup"] += 1


con_mod.get_connector = _get_connector
browser_mod.browser_hidup = _hidup
browser_mod.tutup_service = _tutup
_HIDUP = [False]

try:
    # ================= Keadaan 1: browser belum terbuka =====================
    a = fake_agent()
    _conn.aksi.clear()
    hasil = Agent.pasang_model_web(a, "GLM-5.3-Flash")
    jalankan("k1", "connect" in _conn.aksi, f"connect dipanggil: {_conn.aksi}")
    jalankan("k1", ("set_web_option", "GLM-5.3-Flash") in _conn.aksi,
             "varian diklik")
    jalankan("k1", a._web_terpasang.get("glm") == "GLM-5.3-Flash",
             f"terpasang dicatat: {a._web_terpasang}")
    jalankan("k1", a._web_varian is None, "varian tertunda dikonsumsi")
    jalankan("k1", "siap dengan model GLM-5.3-Flash" in hasil, hasil)

    # ================= Keadaan 2: browser hidup, model BEDA =================
    _HIDUP[0] = True
    a = fake_agent()
    a._web_terpasang["glm"] = "GLM-5.2"
    a._web_ctx_sent = True
    lupa = []
    a._lupakan_chat_web = lambda: lupa.append(1)
    _conn.aksi.clear()
    _panggilan["tutup"] = 0
    hasil = Agent.pasang_model_web(a, "GLM-5.3-Flash")
    jalankan("k2", _panggilan["tutup"] == 1, "tutup_service dipanggil sekali")
    jalankan("k2", len(lupa) == 1, "kaitan chat lama dilupakan")
    jalankan("k2", a._web_ctx_sent is False and a._web_chars == 0,
             "state konteks direset")
    jalankan("k2", ("set_web_option", "GLM-5.3-Flash") in _conn.aksi,
             f"jendela baru + varian: {_conn.aksi}")
    jalankan("k2", a._web_terpasang.get("glm") == "GLM-5.3-Flash",
             "terpasang = varian baru")

    # ================= Keadaan 3: browser hidup, model SAMA =================
    a = fake_agent()
    a._web_terpasang["glm"] = "GLM-5.3-Flash"
    _conn.aksi.clear()
    _panggilan["tutup"] = 0
    _HIDUP[0] = True
    # stub _kirim_konteks_tab: cukup pastikan JALUR inilah yang dipilih
    terpanggil = {}

    def _tab(conn, varian, on_status=None, on_notice=None):
        terpanggil["varian"] = varian
        return "hasil-tab"

    a._kirim_konteks_tab = _tab
    hasil = Agent.pasang_model_web(a, "GLM-5.3-Flash")
    jalankan("k3", _panggilan["tutup"] == 0, "jendela TIDAK ditutup")
    jalankan("k3", ("open_new_chat", True) in _conn.aksi,
             f"open_new_chat new_tab=True: {_conn.aksi}")
    jalankan("k3", terpanggil.get("varian") == "GLM-5.3-Flash"
             and hasil == "hasil-tab", "konteks tab dikirim via _kirim_konteks_tab")

    # ================= _kirim_konteks_tab: isi pengantar read_file ==========
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "konteks-1.txt"
        f.write_text("isi konteks", encoding="utf-8")
        b = fake_agent()
        b.simpan_memory = lambda: [f]
        b._pengantar_memory = lambda nama, kode, lanjutan=False: (
            f"PENGANTAR {nama} {kode}")
        b._konteks_teks = lambda: "TEKS-PENUH"
        b._tanpa_catatan = lambda: _ctx_kosong()
        b._link_web_chat = lambda cid: b.__dict__.setdefault("tautan", []).append(cid)
        rekaman = {}

        def _kirim_konteks(kirim, conn, **kw):
            rekaman.update(kw)
            rekaman["kirim"] = kirim

        b._kirim_konteks = _kirim_konteks
        hasil = Agent._kirim_konteks_tab(b, _conn, "GLM-5.3-Flash")
        peng = rekaman.get("pengantar", "")
        jalankan("tab", peng.endswith(
            "Pakai tool read_file-mu untuk membaca tiap berkasnya."),
            f"pengantar menyebut read_file: {peng!r}")
        jalankan("tab", rekaman.get("berkas") == [f], "berkas konteks dilampirkan")
        jalankan("tab", rekaman.get("new_chat") is False
                 and rekaman.get("open_chat_id") == "",
                 "dikirim ke chat tab baru, bukan chat lama")
        jalankan("tab", b._web_ctx_sent is True, "konteks ditandai terkirim")
        jalankan("tab", b.__dict__.get("tautan") == ["chat-999"],
                 "chat baru dikaitkan")
        jalankan("tab", b._web_terpasang.get("glm") == "GLM-5.3-Flash",
                 "terpasang diperbarui")
        jalankan("tab", "read_file" in hasil, f"pesan hasil: {hasil!r}")

    # ================= Varian None (layanan tanpa varian) ===================
    _HIDUP[0] = False
    a = fake_agent()
    _conn.aksi.clear()
    hasil = Agent.pasang_model_web(a, None)
    jalankan("non", a._web_terpasang.get("glm") == "" and "siap" in hasil,
             f"tanpa varian tetap tercatat: {hasil!r}")

finally:
    con_mod.get_connector, browser_mod.browser_hidup, \
        browser_mod.tutup_service = _asli

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} uji GAGAL")
sys.exit(0 if gagal == 0 else 1)

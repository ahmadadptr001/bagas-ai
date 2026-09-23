"""Verifikasi pasif: model merencanakan, proses nyata membuktikan hasil."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

root = Path(tempfile.mkdtemp(prefix="uji_verifikasi_"))
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import connectors, core, llm, models
from agent.core import Agent, _VERIFIKASI_PASIF


def cek_perbaiki_dan_uji_ulang():
    app = root / "app.py"
    app.write_text("print(1)\n", encoding="utf-8")
    agent = Agent(model="opencode/big-pickle", tool_names=["run_command", "edit_file"])
    calls, prompts, exits = [], [], []
    steps = [
        ("run_command", {"command": "uji aplikasi"}),
        ("edit_file", {"path": "app.py", "old_text": "print(1)", "new_text": "print(2)"}),
        ("run_command", {"command": "uji aplikasi"}),
    ]
    def stream(messages, **kwargs):
        prompts.append([dict(m) for m in messages])
        i = len(prompts) - 1
        if i < len(steps):
            name, args = steps[i]
            return "", [{"id": f"call{i}", "name": name, "arguments": json.dumps(args)}], None
        return "Aplikasi menghasilkan 2; tes lulus.", [], None

    def execute(name, args):
        calls.append(name)
        if name == "edit_file":
            app.write_text(app.read_text().replace(args["old_text"], args["new_text"]), encoding="utf-8")
            return "OK: file diubah"
        assert name == "run_command", name
        # Benar-benar menjalankan aplikasi lalu memeriksa outputnya.
        run = subprocess.run([sys.executable, str(app)], capture_output=True, text=True, timeout=5)
        passed = run.returncode == 0 and run.stdout.strip() == "2"
        exits.append(passed)
        return "OK: output 2" if passed else "[GAGAL] output belum 2"

    with patch.object(core.llm, "stream_completion", side_effect=stream), \
            patch.object(core.tools, "execute", side_effect=execute), \
            patch.object(agent, "_persist"):
        assert "tes lulus" in agent.run("Perbaiki aplikasi agar mencetak 2")
    assert calls == ["run_command", "edit_file", "run_command"], calls
    assert exits == [False, True], exits
    assert sum(m.get("content") == _VERIFIKASI_PASIF for m in prompts[-1]) == 1
    assert "validate_project" not in calls
    print("OK: aplikasi benar-benar diuji, gagal, diperbaiki, lalu diuji ulang; tanpa validator wajib")


def cek_obrolan_dan_batal():
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    with patch.object(core.llm, "stream_completion", return_value=("Halo", [], None)) as stream, \
            patch.object(core.tools, "execute") as execute, patch.object(agent, "_persist"):
        assert agent.run("halo") == "Halo"
        assert stream.call_count == 1
        execute.assert_not_called()
        assert not any(m.get("content") == _VERIFIKASI_PASIF for m in agent.memory.messages)
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    with patch.object(core.llm, "stream_completion", side_effect=llm.Cancelled()) as stream, \
            patch.object(agent, "_persist"):
        try:
            agent.run("perbaiki aplikasi")
        except llm.Cancelled:
            pass
        else:
            raise AssertionError("pembatalan harus diteruskan")
        assert stream.call_count == 1
    print("OK: obrolan tidak dipaksa tes; pembatalan tidak memulai verifikasi")


def cek_web_tanpa_validator():
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    agent.model_spec = next(s for s in models.MODELS.values() if s.connector)
    agent._web_ctx_sent = True
    agent._web_varian = None
    agent._web_chat_id = "uji-verifikasi"
    sent = []
    def send(msg, **kwargs):
        sent.append(msg)
        if len(sent) == 1:
            return '[[TOOL]]{"name":"run_command","arguments":{"command":"python --version"}}[[/TOOL]]'
        return "Pemeriksaan selesai. Python tersedia. Fitur aplikasi belum diuji."
    conn = types.SimpleNamespace(send=send, last_chat_id="uji-verifikasi")
    with patch.object(connectors, "playwright_available", return_value=True), \
            patch.object(connectors, "get_connector", return_value=conn), \
            patch.object(core.tools, "execute", return_value="Python 3.13") as execute, \
            patch.object(agent, "_persist"):
        result = agent.run("cek Python")
    assert "Pemeriksaan selesai" in result, result
    assert len(sent) == 2, sent
    assert _VERIFIKASI_PASIF in sent[1]
    assert [call.args[0] for call in execute.call_args_list] == ["run_command"]
    print("OK: web menerima pengingat bersama hasil langkah, tanpa prompt ulang/validator otomatis")


if __name__ == "__main__":
    cek_perbaiki_dan_uji_ulang()
    cek_obrolan_dan_batal()
    cek_web_tanpa_validator()

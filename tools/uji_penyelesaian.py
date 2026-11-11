"""Regresi tugas yang berhenti setelah membaca atau respons CLI terpotong."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock, patch

root = Path(tempfile.mkdtemp(prefix="uji_selesai_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import llm
from agent.core import Agent


def usage(reason="stop"):
    return llm._UsageCLI({"sessionID": "session-fixture", "part": {"reason": reason}})


def check_followup():
    target = root / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    calls = []
    def stream(_messages, **kwargs):
        calls.append(kwargs.get("session_id"))
        callback = kwargs["on_tool_pending"]
        if len(calls) == 1:
            callback("read", {"status": "completed", "input": {"filePath": str(target)}, "metadata": {}})
            return "Sudah saya perbaiki.", [], usage()
        assert kwargs["session_id"] == "session-fixture"
        if len(calls) == 2:
            target.write_text("value = 2\n", encoding="utf-8")
            callback("write", {"status": "completed", "input": {
                "filePath": str(target), "content": "value = 2\n"}, "metadata": {"exists": True}})
            return "Perubahan selesai.", [], usage()
        assert "Verifikasi pasif" in _messages[-1]["content"]
        compile(target.read_text(encoding="utf-8"), str(target), "exec")
        callback("bash", {"status": "completed", "input": {
            "command": "python -m py_compile app.py"}, "metadata": {}, "output": "exit_code=0"})
        return "Nilai diperbarui. Pemeriksaan sintaks lulus.", [], usage()
    with patch.object(llm, "stream_completion", side_effect=stream):
        final = agent.run("Perbaiki value pada file app.py menjadi 2")
    assert len(calls) == 3 and calls == [None, "session-fixture", "session-fixture"]
    assert final == "Nilai diperbarui. Pemeriksaan sintaks lulus."
    print("OK: baca saja dilanjutkan, perubahan diuji, sesi OpenCode tetap sama")


def check_bounds_and_exceptions():
    for request, response in (("Bikin puisi pendek", "Bulan di langit malam."),
                              ("Perbaiki app.py, cukup jelaskan tanpa mengubah file", "Baris pertama salah."),
                              ("Perbaiki app.py", "Tidak perlu perubahan; hasilnya sudah sesuai.")):
        agent = Agent(model="opencode/big-pickle", tool_names=[])
        with patch.object(llm, "stream_completion", return_value=(response, [], usage())) as stream:
            assert agent.run(request) == response
            assert stream.call_count == 1
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    with patch.object(llm, "stream_completion", return_value=("Sudah diperbaiki.", [], usage())) as stream:
        final = agent.run("Perbaiki app.py")
        assert stream.call_count == 2 and "Belum ada perubahan" in final
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    with patch.object(llm, "stream_completion", side_effect=[
            ("Bagian awal", [], usage("length")), ("Bagian lanjutan selesai.", [], usage())]) as stream:
        assert agent.run("Jelaskan proses ini") == "Bagian lanjutan selesai."
        assert stream.call_count == 2
        assert stream.call_args.kwargs["session_id"] == "session-fixture"
    print("OK: respons terpotong dilanjutkan; puisi/penjelasan/no-op aman; pengingat dibatasi")


def check_transport():
    events = [{"type": "text", "sessionID": "session-fixture", "part": {"id": "answer", "text": "Selesai"}},
              {"type": "step_finish", "sessionID": "session-fixture", "part": {"reason": "stop"}}]
    def process(code):
        proc = MagicMock()
        proc.stdout = io.StringIO("\n".join(json.dumps(ev) for ev in events) + "\n")
        proc.stderr = io.StringIO("")
        proc.returncode = code
        return proc
    with (patch.object(llm.shutil, "which", return_value="opencode"),
          patch.object(llm, "_opencode_variant_args", return_value=["-m", "fixture"])):
        proc = process(0)
        with patch.object(llm.subprocess, "Popen", return_value=proc) as popen:
            answer, _, meta = llm._stream_opencode_cli([
                {"role": "user", "content": "pesan awal"},
                {"role": "user", "content": "lanjut bagian berikutnya"}], session_id="session-fixture")
        assert answer == "Selesai" and meta.session_id == "session-fixture"
        assert meta.finish_reason == "stop"
        assert "--session" in popen.call_args.args[0]
        sent = proc.stdin.write.call_args.args[0]
        assert "pesan awal" not in sent and "lanjut bagian berikutnya" in sent
        events.insert(0, {"type": "error", "error": "temporary upstream error"})
        with patch.object(llm.subprocess, "Popen", return_value=process(0)):
            assert llm._stream_opencode_cli([{"role": "user", "content": "uji"}])[0] == "Selesai"
        events.pop(0)
        with patch.object(llm.subprocess, "Popen", return_value=process(1)):
            try:
                llm._stream_opencode_cli([{"role": "user", "content": "uji"}])
                raise AssertionError("exit nonzero harus dilaporkan meskipun ada teks parsial")
            except llm.IncompleteResponseError:
                pass
    print("OK: resume memakai ID tepat dan hanya instruksi baru; exit nonzero bukan sukses")


check_followup()
check_bounds_and_exceptions()
check_transport()

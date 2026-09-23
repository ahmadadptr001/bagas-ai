"""Regresi handoff model -> tools, tanpa jaringan eksternal."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = Path(tempfile.mkdtemp(prefix="bagasai_handoff_"))
os.environ["BAGASAI_HOME"] = str(root / "config")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import core, llm


def chunk(calls=(), finish=None):
    return NS(usage=None, choices=[NS(finish_reason=finish,
        delta=NS(content=None, reasoning_content=None, tool_calls=calls))])


def tc(index, name=None, args=None):
    return NS(index=index, id=None, function=NS(name=name, arguments=args))


def check_stream():
    class Stream:
        closed = False
        def __iter__(self):
            yield chunk([tc(0, "read_", '{"path":'), tc(1, "read_file", '{"path":"b"}')])
            yield chunk([tc(0, "file", '"a"}')], "tool_calls")
            raise AssertionError("Tidak boleh menunggu socket setelah finish_reason")
        def close(self):
            self.closed = True
    stream = Stream()
    client = MagicMock()
    client.chat.completions.create.return_value = stream
    statuses = []
    with patch.object(llm, "get_client", return_value=client):
        _, calls, _ = llm.stream_completion([], model="test", on_tool_pending=statuses.append)
    assert [c["name"] for c in calls] == ["read_file", "read_file"]
    assert [json.loads(c["arguments"])["path"] for c in calls] == ["a", "b"]
    assert statuses and stream.closed
    assert client.chat.completions.create.call_count == 1
    print("PASS: delta terfragmentasi, multi-tool, finish tanpa menunggu koneksi ditutup")


def check_agent():
    agent = core.Agent(model="nvidia/nemotron-super", tool_names=["read_file"])
    responses = iter([
        ("", [{"id": "same", "name": "read_file", "arguments": '{"path":'}], None),
        ("", [{"id": "same", "name": "read_file", "arguments": '{"path":"a"}'}], None),
        ("", [{"id": "same", "name": "read_file", "arguments": '{"path":"a"}'}], None),
        ("Selesai", [], None),
    ])
    starts, ends = [], []
    with patch.object(llm, "stream_completion", side_effect=lambda *a, **kw: next(responses)), \
            patch.object(core.tools, "execute", return_value="isi berkas") as execute, \
            patch.object(agent, "_persist") as persist:
        assert agent.run("Baca a", on_tool=lambda *a: starts.append(a),
                         on_tool_result=lambda *a: ends.append(a)) == "Selesai"
        assert execute.call_count == 1, "JSON rusak dan cache tidak boleh dieksekusi"
        assert persist.call_count >= 3
    assert len(starts) == len(ends) == 3
    assert "JSON tidak valid" in ends[0][1]
    messages = agent.memory.messages
    ids = [tc["id"] for m in messages for tc in m.get("tool_calls", [])]
    assert len(ids) == len(set(ids)) == 3
    before = [(m["tool_call_id"], m["content"]) for m in messages if m["role"] == "tool"]
    agent.memory.repair_dangling_tools()
    after = [(m["tool_call_id"], m["content"]) for m in agent.memory.messages if m["role"] == "tool"]
    assert before == after
    print("PASS: argumen rusak dikembalikan, tool dieksekusi sekali, hasil cache tampil, ID/history utuh")


def check_text_calls():
    calls = llm._extract_text_tool_calls('<function=read_file>{"path":"a"}</function>')
    assert json.loads(calls[0]["arguments"]) == {"path": "a"}
    calls = llm._extract_text_tool_calls('<tool_call>{"name":"read_file","arguments":"broken"}</tool_call>')
    assert calls[0]["arguments"] == "broken", "Argumen rusak jangan diganti dengan {}"
    assert llm._extract_text_tool_calls('<function=read_file>{"path":"a"}') == []
    print("PASS: fallback XML/JSON mempertahankan argumen, instruksi terpotong tidak dijalankan")


def check_open_socket():
    from openai import OpenAI
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            data = {"id": "test", "object": "chat.completion.chunk", "created": 0,
                    "model": "test", "choices": [{"index": 0, "finish_reason": "tool_calls",
                    "delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]}}]}
            self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
            self.wfile.flush()
            release.wait(5)  # sengaja tanpa [DONE] maupun EOF
            self.close_connection = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = OpenAI(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=0)
    try:
        with patch.object(llm, "get_client", return_value=client):
            start = time.monotonic()
            _, calls, _ = llm.stream_completion([], model="test", cancel_event=threading.Event())
            assert time.monotonic() - start < 2, "Handoff menunggu EOF"
            assert calls[0]["name"] == "read_file"
        print("PASS: HTTP SSE socket tetap terbuka, instruksi diterima dalam <2 detik")
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        client.close()


if __name__ == "__main__":
    check_stream()
    check_agent()
    check_text_calls()
    check_open_socket()

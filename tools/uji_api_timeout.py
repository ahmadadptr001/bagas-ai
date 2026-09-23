"""Regresi streaming lambat dan pembatalan, memakai server HTTP lokal."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openai import OpenAI
from agent import config, llm


class Handler(BaseHTTPRequestHandler):
    requests = 0
    def log_message(self, *args):
        pass
    def do_POST(self):
        type(self).requests += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        def send(text, finish=None):
            data = {"id": "local", "object": "chat.completion.chunk",
                    "created": 0, "model": "local", "choices": [{"index": 0,
                    "delta": {"content": text}, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
            self.wfile.flush()
        try:
            send("awal ")
            time.sleep(0.3)
            send("akhir", "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass  # klien sengaja timeout/batal pada sebagian skenario


def cek_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = OpenAI(api_key="local-test", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    max_retries=0)
    try:
        with patch.object(llm, "get_client", return_value=client), \
                patch.object(config, "TTFT_TIMEOUT", 0.05), \
                patch.object(config, "NVIDIA_READ_TIMEOUT", 1.0):
            before = Handler.requests
            out, _, _ = llm.stream_completion([{"role": "user", "content": "uji"}],
                                              provider="nvidia", model="local",
                                              cancel_event=threading.Event())
            assert out == "awal akhir", out
            assert Handler.requests == before + 1
            assert llm._read_timeout("openrouter") == 0.05
            print("OK: jeda 300 ms melewati timeout umum 50 ms; NVIDIA selesai tanpa retry")

        with patch.object(llm, "get_client", return_value=client), \
                patch.object(config, "NVIDIA_READ_TIMEOUT", 0.05):
            before = Handler.requests
            shown = []
            try:
                llm.stream_completion([{"role": "user", "content": "uji"}],
                                      provider="nvidia", model="local", on_content=shown.append,
                                      cancel_event=threading.Event())
            except llm.ProviderReadTimeout as exc:
                assert "tidak dikirim ulang" in str(exc)
            else:
                raise AssertionError("timeout seharusnya dilaporkan")
            assert shown == ["awal "]
            assert Handler.requests == before + 1
            print("OK: timeout sungguhan dijelaskan; potongan tetap tampil; request hanya sekali")
    finally:
        client.close()
        server.shutdown()
        server.server_close()


def cek_batal():
    for pending_headers in (True, False):
        cancel, entered, release, closed = (threading.Event() for _ in range(4))
        class Stream:
            def __iter__(self):
                entered.set()
                assert release.wait(2)
                yield "token terlambat"
            def close(self):
                closed.set()
                release.set()
        def create():
            if pending_headers:
                entered.set()
                assert release.wait(2)
            return Stream()
        caught = []
        def consume():
            try:
                list(llm._stream_cancellable(create, cancel))
            except llm.Cancelled:
                caught.append(True)
        worker = threading.Thread(target=consume)
        worker.start()
        assert entered.wait(2)
        cancel.set()
        worker.join(0.5)
        assert not worker.is_alive() and caught
        release.set()
        assert closed.wait(2)
    print("OK: batal responsif sebelum header dan saat baca macet; respons terlambat ditutup")


if __name__ == "__main__":
    cek_http()
    cek_batal()

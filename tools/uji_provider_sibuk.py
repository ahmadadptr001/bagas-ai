"""HTTP/SSE overload tidak mengulang prompt atau menghilangkan jawaban parsial."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

import httpx
from openai import OpenAI

os.environ["BAGASAI_PROJECT_ROOT"] = tempfile.mkdtemp(prefix="uji_sibuk_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import llm, models
from agent.core import Agent


def cek_overload(mode):
    requests = []
    def handle(request):
        requests.append(request)
        error = {"error": {"message": "Service temporarily overloaded",
                           "type": "server_error", "code": "overloaded"}}
        if mode == "http":
            return httpx.Response(503, json=error)
        chunk = {"id": "uji", "object": "chat.completion.chunk", "created": 0,
                 "model": "uji", "choices": [{"index": 0, "delta": {
                     "content": "Jawaban parsial."}, "finish_reason": None}]}
        data = "data: " + json.dumps(chunk) + "\n\n"
        data += "data: " + json.dumps(error) + "\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=data)

    client = OpenAI(api_key="local-test", base_url="https://provider.invalid/v1",
                    max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    agent.model_spec = models.ModelSpec(id="uji/sibuk", label="NVIDIA uji",
                                       provider="nvidia", api_model="uji", multimodal=False)
    shown, saved, retries = [], [], []
    try:
        with patch.object(llm, "get_client", return_value=client), \
                patch.object(agent, "_persist", side_effect=lambda: saved.append(
                    [dict(m) for m in agent.memory.messages])):
            try:
                agent.run("halo", on_token=shown.append, cancel_event=threading.Event(),
                          on_retry=lambda *args: retries.append(args))
            except llm.ProviderBusyError as exc:
                assert "NVIDIA" in str(exc) and "tanpa mengirim ulang" in str(exc)
            else:
                raise AssertionError("overload tidak dilaporkan")
        assert len(requests) == 1 and not retries
        assert any(m.get("content") == "halo" for m in saved[-1])
        if mode == "sse":
            assert shown == ["Jawaban parsial."]
            assert sum(m.get("content") == "Jawaban parsial." for m in saved[-1]) == 1
        print(f"OK: overload {mode}, satu request; konteks/parsial tetap tersimpan")
    finally:
        client.close()


def cek_bukan_sibuk():
    class Error(Exception):
        pass
    for status in (400, 401, 403, 404, 422):
        error = Error("overloaded")
        error.status_code = status
        assert not llm._is_provider_busy(error)
    quota = Error("insufficient_quota")
    quota.status_code = 503
    assert not llm._is_provider_busy(quota)
    assert not llm._is_provider_busy(llm.KonteksPenuh("overloaded"))
    print("OK: auth, payload, konteks dan kuota tidak disamarkan menjadi overload")


if __name__ == "__main__":
    cek_overload("http")
    cek_overload("sse")
    cek_bukan_sibuk()

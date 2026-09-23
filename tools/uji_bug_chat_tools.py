"""Regresi chat gagal dan efisiensi tool dasar, tanpa layanan eksternal."""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(tempfile.mkdtemp(prefix="uji_chat_tools_"))
os.environ["BAGASAI_PROJECT_ROOT"] = str(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import connectors, llm, models
from agent.core import Agent
from agent.interfaces.textual_app import BagasAIApp
from agent.tools import files, search


def cek_parsial():
    for error in (RuntimeError("putus"), llm.Cancelled(), KeyboardInterrupt()):
        agent = Agent(model="opencode/big-pickle", tool_names=[])
        shown = []
        snapshots = []

        def stream(*args, **kw):
            kw["on_content"]("Jawaban ")
            kw["on_content"]("parsial")
            raise error

        with patch("agent.core.llm.stream_completion", side_effect=stream), \
                patch.object(agent, "_persist", side_effect=lambda: snapshots.append(
                    [dict(m) for m in agent.memory.messages])):
            try:
                agent.run("jelaskan", on_token=shown.append)
            except BaseException as exc:
                assert exc is error
            else:
                raise AssertionError("error tertelan")
        assert "".join(shown) == "Jawaban parsial"
        saved = [m for m in snapshots[-1] if m.get("content") == "Jawaban parsial"]
        assert len(saved) == 1 and saved[0]["role"] == "assistant"

    # Sukses tetap hanya mencatat jawaban sekali.
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    def sukses(*args, **kw):
        kw["on_content"]("Jawaban utuh")
        return "Jawaban utuh", [], None
    with patch("agent.core.llm.stream_completion", side_effect=sukses), \
            patch.object(agent, "_persist"):
        assert agent.run("halo") == "Jawaban utuh"
    assert sum(m.get("content") == "Jawaban utuh" for m in agent.memory.messages) == 1
    print("OK: parsial tersimpan sebelum error/batal; sukses tidak diduplikasi")


def cek_chat_rusak():
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    agent.model_spec = next(s for s in models.MODELS.values() if s.connector)
    agent._web_ctx_sent = True
    agent._web_varian = None
    agent._web_chat_id = "chat-uji"
    cancel = threading.Event()
    conn = types.SimpleNamespace(send=MagicMock(
        side_effect=connectors.WebChatRusakError("invalid parent_id")))
    with patch.object(connectors, "playwright_available", return_value=True), \
            patch.object(connectors, "get_connector", return_value=conn), \
            patch.object(agent, "_persist"), \
            patch.object(agent, "_lupakan_chat_web") as reset:
        result = agent.run("halo", cancel_event=cancel)
    assert conn.send.call_count == 1
    assert conn.send.call_args.kwargs["cancel_event"] is cancel
    reset.assert_not_called()
    assert agent._web_chat_id == "chat-uji"
    assert "tidak dikirim ulang otomatis" in result
    print("OK: chat rusak berhenti setelah satu pengiriman; chat lama tetap terkait")


async def cek_antrean_error():
    fake = MagicMock()
    fake.model_spec = MagicMock(label="uji", is_web=False)
    started, fail = threading.Event(), threading.Event()
    def run(text, **kwargs):
        if text == "pertama":
            started.set()
            assert fail.wait(5)
            kwargs["on_token"]("potongan lama")
            raise RuntimeError("putus")
        return "jawaban berikutnya"
    fake.run.side_effect = run
    app = BagasAIApp(agent=fake)
    with patch("agent.suara.ucap_panjang"), patch("agent.suara.getar"):
        async with app.run_test(size=(100, 40)) as pilot:
            app._start_turn("pertama")
            for _ in range(100):
                if started.is_set():
                    break
                await pilot.pause(0.02)
            assert started.is_set()
            with app._antre_lock:
                app._prompt_queue.append("kedua")
            fail.set()
            for _ in range(100):
                await pilot.pause(0.02)
                if fake.run.call_count == 2 and not app.is_turn_active:
                    break
            assert [c.args[0] for c in fake.run.call_args_list] == ["pertama", "kedua"]
            assert not app._prompt_queue and not app.is_turn_active
            app._turn_error(RuntimeError("error basi"), app._turn_id - 1)
            assert fake.run.call_count == 2
    print("OK: antrean maju sesudah error; error giliran basi diabaikan")


def cek_tools():
    sample = ROOT / "sample.txt"
    sample.write_text("alpha beta gamma\n" * 50, encoding="utf-8")
    (ROOT / "folder").mkdir()
    visited = []
    def walk(root):
        for i in range(1000):
            visited.append(i)
            yield root / f"file{i}.txt"
    with patch.object(search, "_telusuri", side_effect=walk):
        assert "3 berkas" in search.glob_files("*.txt", max_results=3)
    assert len(visited) == 3

    class CountText(str):
        splits = 0
        def split(self, *args, **kw):
            self.splits += 1
            return super().split(*args, **kw)
    text = CountText(sample.read_text(encoding="utf-8"))
    with patch.object(search, "_telusuri", return_value=iter([sample])), \
            patch.object(search, "_isi", return_value=text) as read, \
            patch.object(search, "_susun", side_effect=lambda found, *args: str(found)):
        result = search.search_multi_text(["alpha", "beta", "gamma", "alpha"],
                                          max_results=2)
    assert text.splits == 1 and read.call_count == 1
    assert result.count("sample.txt") == 3
    found, cut = search._pindai(re.compile("alpha"), lambda p: True, 2)
    assert sum(len(matches) for _, matches, _ in found) == 2 and cut
    for func, arg in ((search.glob_files, "*.txt"), (search.search_text, "alpha"),
                      (search.search_multi_text, ["alpha"])):
        assert "[error]" in func(arg, max_results=0)
    listing = files.list_dir()
    assert "[dir ] folder (-)" in listing
    assert f"[file] sample.txt ({sample.stat().st_size})" in listing
    assert "sample.txt" in search.glob_files("sample.txt")
    assert "sample.txt" in search.glob_files("sampl.txt")  # saran salah ketik
    print("OK: glob berhenti di batas; multi-query satu baca/split; batas hasil tepat; list_dir utuh")


if __name__ == "__main__":
    cek_parsial()
    cek_chat_rusak()
    asyncio.run(cek_antrean_error())
    cek_tools()

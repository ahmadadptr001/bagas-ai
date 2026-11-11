"""Regresi effort: katalog -> pilihan UI -> Agent -> argumen CLI, tanpa inferensi."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

root = Path(tempfile.mkdtemp(prefix="uji_effort_"))
os.environ["BAGASAI_HOME"] = str(root / "home")
os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import config, llm, models, prefs
from agent.core import Agent
from agent.interfaces.textual_app import BagasAIApp


def spec():
    return models.ModelSpec(
        id="opencode/fixture", label="Model uji (CLI)", provider="opencode",
        api_model="opencode/fixture", effort_cli=True,
        effort_levels=("default", "low", "high", "max"), effort_default="default",
    )


def check_transport():
    agent = Agent(model="opencode/big-pickle", tool_names=[])
    agent.model_spec = spec()
    assert agent.set_effort("mendalam") == "high"
    assert prefs.get_effort() == "high"
    agent._init_effort()
    assert agent.effort == "high"
    with patch.object(llm, "stream_completion", return_value=("selesai", [], None)) as stream:
        assert agent.run("uji") == "selesai"
        assert stream.call_args.kwargs["variant"] == "high"
        assert stream.call_args.kwargs["extra_body"] is None
    try:
        agent.set_effort("medium")
        raise AssertionError("effort yang tak tersedia harus ditolak")
    except ValueError:
        pass
    assert agent.model_spec.extra_body_for("high") is None
    assert agent.model_spec.cli_variant_for("default") is None

    # Tangkap argumen proses yang benar-benar akan dipakai oleh jalur streaming.
    for help_text, expected in (("provider/model#variant", ["-m", "opencode/fixture#high"]),
                                ("--variant", ["-m", "opencode/fixture", "--variant", "high"])):
        process = MagicMock()
        process.stdout = io.StringIO(json.dumps({"type": "text", "part": {"text": "hasil"}}) + "\n")
        process.stderr = io.StringIO("")
        process.returncode = 0
        with (patch.object(config, "opencode_cli_tersedia", return_value=True),
              patch.object(llm.shutil, "which", return_value="opencode"),
              patch.object(llm.subprocess, "run", return_value=SimpleNamespace(stdout=help_text, returncode=0)),
              patch.object(llm.subprocess, "Popen", return_value=process) as popen):
            result = llm.stream_completion([{"role": "user", "content": "uji"}],
                model="opencode/fixture", provider="opencode", variant="high")
            assert result[0] == "hasil"
            args = popen.call_args.args[0]
            assert args[2:2 + len(expected)] == expected, args
            assert "--thinking" in args
    with patch.object(config, "opencode_cli_tersedia", return_value=False):
        try:
            llm.stream_completion([], provider="opencode", variant="high")
            raise AssertionError("HTTP tidak boleh mengabaikan varian CLI")
        except llm.ProviderBusyError:
            pass
    print("OK: effort dipulihkan, divalidasi, diteruskan Agent dan CLI V1/V2; body HTTP bersih")


async def check_ui():
    agent = MagicMock()
    agent.model_spec = spec()
    agent.effort = "default"
    agent.memory.messages = []
    agent.set_effort.side_effect = lambda name: Agent.set_effort(agent, name)
    app = BagasAIApp(agent=agent)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        button = app.query_one("#input-effort")
        assert not button.disabled and "Bawaan" in str(button.label)
        await pilot.click("#input-effort")
        await pilot.pause()
        assert app._prompt_inline["nilai"] == ["default", "low", "high", "max"]
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        assert agent.effort == "high" and "high" in str(button.label)
        agent.model_spec = models.ModelSpec(id="opencode/plain", label="Model biasa", provider="opencode")
        app._refresh_status()
        assert button.disabled
        app._cmd_effort("/effort")
        assert app._prompt_inline is None
    print("OK: tombol effort memilih nilai asli, indikator berubah, model tanpa varian dinonaktifkan")


check_transport()
asyncio.run(check_ui())

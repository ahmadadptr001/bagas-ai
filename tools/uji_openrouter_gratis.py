"""Regresi katalog gratis, validasi setup, dan batas biaya request."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import llm, models, setup_wizard


def main():
    for _, _, spec in models.catalog():
        assert spec.aktif
        if spec.provider == "openrouter":
            assert spec.api_model.endswith(":free")
    assert models.cari("oxalpha").ditunda
    assert models.spec_for_id("openrouter/ox-alpha").aktif
    assert any(s.is_web for _, _, s in models.catalog(include_postponed=True))

    with patch.object(setup_wizard.requests, "get") as get:
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"data": {"is_free_tier": True}}
        assert setup_wizard.validate_openrouter_key("test-only")[0]
        assert get.call_args.args[0].endswith("/key")
        for status in (401, 403, 429, 503):
            get.return_value.status_code = status
            assert not setup_wizard.validate_openrouter_key("test-only")[0]

    client = MagicMock()
    client.chat.completions.create.return_value = iter([
        NS(usage=None, choices=[NS(delta=NS(content="OK", tool_calls=None,
            reasoning_content=None), finish_reason="stop")])])
    original = {"reasoning": {"enabled": True}, "provider": {"sort": "latency"}}
    with patch.object(llm, "get_client", return_value=client):
        llm.stream_completion([{"role": "user", "content": "test"}],
                              provider="openrouter", model="example/test:free",
                              extra_body=original)
    sent = client.chat.completions.create.call_args.kwargs["extra_body"]
    assert sent["provider"]["max_price"] == {
        "prompt": 0, "completion": 0, "request": 0, "image": 0}
    assert sent["provider"]["sort"] == "latency"
    assert sent["reasoning"] == original["reasoning"]
    assert "max_price" not in original["provider"]
    assert client.chat.completions.create.call_count == 1
    print("OK: katalog, migrasi, validasi key tanpa inference, batas biaya nol")


if __name__ == "__main__":
    main()

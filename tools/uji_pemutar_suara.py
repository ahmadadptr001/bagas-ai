"""Pemutar tidak boleh menyatakan sukses sebelum playback dimulai."""
import io
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent import suara

for status, expected in (("MULAI\nOK\n", True), ("OK\n", False), ("GAGAL\n", False)):
    speaker = suara.Pengucap()
    process = MagicMock()
    process.poll.return_value = None
    process.stdin = io.StringIO()
    process.stdout = io.StringIO(status)
    speaker._proc = process
    with patch.object(suara.sys, "platform", "win32"):
        assert speaker._windows_kirim("P", "fixture.wav") is expected
print("OK: pemutar perlu konfirmasi MULAI; gagal memuat audio tidak dianggap sukses")

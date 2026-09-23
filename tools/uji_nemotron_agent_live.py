"""Uji opt-in Nemotron Super: file -> perbaikan -> proses Python sungguhan.

Jalankan python tools/uji_nemotron_agent_live.py --live (memakai kuota NVIDIA).
Proyek dan konfigurasi sesi terisolasi di direktori sementara.
"""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time


def main():
    if "--live" not in sys.argv:
        raise SystemExit("Tambahkan --live untuk memakai API NVIDIA.")
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "src"))
    from dotenv import load_dotenv
    load_dotenv(repo / ".env")
    load_dotenv(Path.home() / ".bagasai" / ".env")
    root = Path(tempfile.mkdtemp(prefix="bagasai_super_live_"))
    os.environ["BAGASAI_HOME"] = str(root / "config")
    os.environ["BAGASAI_PROJECT_ROOT"] = str(root)
    os.chdir(root)
    (root / "app.py").write_text("def tambah(a, b):\n    return a - b\n", encoding="utf-8")
    from agent.core import Agent
    agent = Agent(model="nvidia/nemotron-super", tool_names=[
        "read_file", "write_file", "edit_file", "run_python", "run_command"],
        max_iterations=10)
    cancel = threading.Event()
    timer = threading.Timer(180, cancel.set)
    timer.daemon = True
    started = time.monotonic()
    events = []
    def event(kind, text):
        events.append((kind, text))
        print(f"{time.monotonic()-started:.1f}s {kind}: {text}", flush=True)
    timer.start()
    try:
        answer = agent.run(
            "Baca app.py, perbaiki fungsi tambah agar menjumlahkan dua angka. "
            "Jalankan tes Python sungguhan untuk tambah(2,3)==5 dan tambah(-2,2)==0. "
            "Setelah tes lulus tulis hasil.txt berisi LULUS. Kerjakan sekarang dengan tools, "
            "jangan sekadar menjelaskan rencana. Jangan gunakan jaringan atau instal paket.",
            cancel_event=cancel,
            on_status=lambda s: event("status", s),
            on_tool=lambda name, args: event("tool", name),
            on_tool_result=lambda name, result: event("result", name + ": " + result[:180]),
        )
        event("final", answer[:500])
        namespace = {}
        exec((root / "app.py").read_text(encoding="utf-8"), namespace)
        assert namespace["tambah"](2, 3) == 5
        assert namespace["tambah"](-2, 2) == 0
        assert (root / "hasil.txt").read_text(encoding="utf-8").strip() == "LULUS"
        assert any(k == "tool" and n in ("run_python", "run_command") for k, n in events)
        assert any(k == "result" and "exit_code=0" in n for k, n in events)
        print("PASS: Nemotron Super membaca, mengubah, menguji, dan menyelesaikan tugas.", flush=True)
    finally:
        timer.cancel()
        print("Artefak:", root, flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Aktivasi bagasAI (sekali jalan).

    python run.py

Memeriksa konfigurasi, menandai bagasAI AKTIF, menampilkan dashboard, lalu
selesai. Untuk mengobrol, ketik `bagasai` di terminal mana pun.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rich.console import Console, Group  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.text import Text  # noqa: E402

from agent import config, longmem, models, scripts  # noqa: E402

console = Console()


def main() -> None:
    console.clear()
    ok = config.has_api_key()

    header = Text("bagasAI", style="bold magenta")
    header.append("  —  aktivasi", style="dim")

    status = Text()
    if ok:
        status.append("● AKTIF\n", style="bold green")
    else:
        status.append("● BELUM SIAP (API key kosong)\n", style="bold red")

    info = Text()
    info.append("Model default : ", style="bold")
    info.append(f"{models.spec_for_id(config.CHAT_MODEL).label}\n", style="cyan")
    info.append("Endpoint      : ", style="bold")
    info.append(f"{config.NVIDIA_BASE_URL}\n", style="dim")
    info.append("Config        : ", style="bold")
    info.append(f"{config.CONFIG_HOME}\n", style="dim")
    info.append("Project root  : ", style="bold")
    info.append(f"{config.PROJECT_ROOT}\n", style="green")
    info.append("Memory        : ", style="bold")
    info.append(f"{len(longmem.all_facts())} fakta\n", style="yellow")
    info.append("Script memory : ", style="bold")
    info.append(f"{len(scripts.index_list())} skrip", style="blue")

    tip = Text()
    if ok:
        tip.append("Ketik ", style="dim")
        tip.append("bagasai", style="bold cyan")
        tip.append(" untuk mulai chat, atau ", style="dim")
        tip.append("bagasai --resume", style="bold cyan")
        tip.append(" untuk melanjutkan.", style="dim")
    else:
        tip.append("Jalankan ", style="dim")
        tip.append("bagasai setup", style="bold cyan")
        tip.append(" lalu isi NVIDIA_API_KEY.", style="dim")

    console.print(
        Panel(
            Group(header, Text(), status, info, Text(), tip),
            border_style="green" if ok else "red",
            padding=(1, 2),
        )
    )

    # Tandai aktif.
    try:
        config.ACTIVE_FILE.write_text(
            json.dumps(
                {
                    "active": ok,
                    "activated_at": time.time(),
                    "model": config.CHAT_MODEL,
                    "project_root": str(config.PROJECT_ROOT),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

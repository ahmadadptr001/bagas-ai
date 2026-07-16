"""Paket agent. Ekspor kelas utama agar bisa dipakai sebagai library:

    from agent import Agent
    agent = Agent()
    print(agent.run("Halo!"))
"""
from __future__ import annotations

from .core import Agent

__all__ = ["Agent"]

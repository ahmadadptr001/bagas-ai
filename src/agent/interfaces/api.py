"""Antarmuka API (FastAPI): POST /chat untuk integrasi aplikasi lain."""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from ..core import Agent

app = FastAPI(title="bagas-ai", version="1.0.0")

# Satu Agent per session_id (in-memory; hilang saat server restart).
_sessions: dict[str, Agent] = {}


def _get_agent(session_id: str) -> Agent:
    if session_id not in _sessions:
        _sessions[session_id] = Agent()
    return _sessions[session_id]


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    reply: str
    session_id: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    agent = _get_agent(req.session_id)
    reply = agent.run(req.message)
    return ChatResponse(reply=reply, session_id=req.session_id)


@app.post("/reset")
def reset(session_id: str = "default") -> dict:
    if session_id in _sessions:
        _sessions[session_id].reset()
    return {"status": "reset", "session_id": session_id}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

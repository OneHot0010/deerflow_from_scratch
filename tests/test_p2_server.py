"""P2 tests: the web/SSE server (server.py).

Uses FastAPI's in-process TestClient (ASGI transport) — no socket is bound, so
this never starts a real listening server. We patch ``LeadAgent.run_stream`` to
emit a scripted event sequence, then assert:

- GET /health returns the liveness payload.
- POST /chat collapses the stream to the final content (and surfaces errors).
- POST /chat/stream emits well-formed SSE frames whose event names match the
  agent events, terminated by a `done` frame.
- request validation rejects an empty question.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import server
from agents.lead_agent import LeadAgent


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def patch_stream(monkeypatch):
    """Make every LeadAgent.run_stream emit a fixed event list."""

    def _install(events):
        def fake_run_stream(self, question, history=None):
            self.messages = []  # server persists this after a clean run
            for e in events:
                yield dict(e)  # copy: server mutates via .pop('type')

        monkeypatch.setattr(LeadAgent, "run_stream", fake_run_stream)

    return _install


def _parse_sse(text: str):
    """Parse raw SSE text into a list of (event, data_dict) tuples."""
    frames = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
            frames.append((event, json.loads(data) if data else {}))
    return frames


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "phase": "P4"}


def test_chat_returns_final_content(client, patch_stream):
    patch_stream(
        [
            {"type": "message_chunk", "delta": "par"},
            {"type": "message_chunk", "delta": "tial"},
            {"type": "final", "content": "partial answer"},
        ]
    )
    r = client.post("/chat", json={"question": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "partial answer"
    assert "thread_id" in body


def test_chat_surfaces_error(client, patch_stream):
    patch_stream([{"type": "error", "message": "boom"}])
    r = client.post("/chat", json={"question": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] == "boom"
    assert "thread_id" in body


def test_chat_stream_sse_frames_and_done(client, patch_stream):
    patch_stream(
        [
            {"type": "tool_start", "name": "bash", "arguments": "{}"},
            {"type": "tool_end", "name": "bash", "result": "ok"},
            {"type": "message_chunk", "delta": "hi"},
            {"type": "final", "content": "hi"},
        ]
    )
    r = client.post("/chat/stream", json={"question": "go"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(r.text)
    names = [ev for ev, _ in frames]
    assert names[0] == "thread_id"
    assert names[1:] == ["tool_start", "tool_end", "message_chunk", "final", "done"]
    # data payloads must not carry the redundant 'type' key.
    assert all("type" not in data for _, data in frames)
    tool_start_data = frames[1][1]
    assert tool_start_data == {"name": "bash", "arguments": "{}"}


def test_chat_stream_error_frame(client, patch_stream):
    patch_stream([{"type": "error", "message": "bad"}])
    r = client.post("/chat/stream", json={"question": "go"})
    frames = _parse_sse(r.text)
    assert frames[0][0] == "thread_id"
    assert frames[1][0] == "error"
    assert frames[1][1] == {"message": "bad"}
    assert frames[-1][0] == "done"


def test_empty_question_rejected(client):
    r = client.post("/chat", json={"question": ""})
    assert r.status_code == 422  # pydantic min_length validation


def test_missing_question_rejected(client):
    r = client.post("/chat", json={})
    assert r.status_code == 422


def test_max_steps_override_forwarded(client, monkeypatch):
    captured = {}

    real_init = LeadAgent.__init__

    def spy_init(self, *a, **k):
        captured["max_steps"] = k.get("max_steps")
        real_init(self, *a, **k)

    def fake_run_stream(self, question, history=None):
        self.messages = []
        yield {"type": "final", "content": "x"}

    monkeypatch.setattr(LeadAgent, "__init__", spy_init)
    monkeypatch.setattr(LeadAgent, "run_stream", fake_run_stream)

    client.post("/chat", json={"question": "hi", "max_steps": 5})
    assert captured["max_steps"] == 5

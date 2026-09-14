"""P3 tests: thread persistence over the web layer (server.py).

FastAPI's in-process TestClient — no socket bound. The autouse
``_isolated_thread_store`` fixture (see conftest) swaps in a fresh in-memory
store per test, so nothing touches the filesystem.

We assert:
- /threads create / list / get / delete, incl. 404s and 409 on duplicate id.
- /health reports P3.
- /chat with a thread_id persists the updated conversation and resumes it on a
  second call (the prior history is fed to the agent).
- /chat without a thread_id auto-creates one and returns it.
- /chat/stream emits a leading thread_id frame and saves history on clean finish.
- a failed turn (error event) does not persist anything.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import server
from agents.lead_agent import LeadAgent
from store import get_store


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def patch_stream(monkeypatch):
    """Make LeadAgent.run_stream emit fixed events and record the history it saw.

    The fake mirrors the real contract: it sets ``self.messages`` to the
    conversation the server will persist (seeded history + new turns).
    """
    seen = {}

    def _install(events, produced_messages=None):
        def fake_run_stream(self, question, history=None):
            seen["question"] = question
            seen["history"] = history
            base = list(history or [])
            base.append({"role": "user", "content": question})
            if produced_messages is not None:
                self.messages = list(produced_messages)
            else:
                # default: echo a trivial assistant turn
                final = next(
                    (e.get("content", "") for e in events if e.get("type") == "final"),
                    "",
                )
                base.append({"role": "assistant", "content": final})
                self.messages = base
            for e in events:
                yield dict(e)

        monkeypatch.setattr(LeadAgent, "run_stream", fake_run_stream)
        return seen

    return _install


def _parse_sse(text: str):
    frames = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
            frames.append((event, json.loads(data) if data else {}))
    return frames


# --- thread management -------------------------------------------------------
def test_health_reports_p3(client):
    assert client.get("/health").json() == {"status": "ok", "phase": "P4"}


def test_create_list_get_delete_thread(client):
    r = client.post("/threads", json={"title": "my chat"})
    assert r.status_code == 201
    tid = r.json()["id"]
    assert r.json()["title"] == "my chat"

    listed = client.get("/threads").json()["threads"]
    assert any(t["id"] == tid for t in listed)

    got = client.get(f"/threads/{tid}")
    assert got.status_code == 200
    assert got.json()["messages"] == []

    d = client.delete(f"/threads/{tid}")
    assert d.status_code == 200 and d.json() == {"deleted": tid}
    assert client.get(f"/threads/{tid}").status_code == 404
    assert client.delete(f"/threads/{tid}").status_code == 404


def test_create_thread_no_body(client):
    r = client.post("/threads")
    assert r.status_code == 201
    assert r.json()["message_count"] == 0


def test_create_duplicate_id_conflicts(client):
    client.post("/threads", json={"thread_id": "dup"})
    r = client.post("/threads", json={"thread_id": "dup"})
    assert r.status_code == 409


# --- chat persistence --------------------------------------------------------
def test_chat_autocreates_thread_and_persists(client, patch_stream):
    patch_stream([{"type": "final", "content": "hi there"}])
    r = client.post("/chat", json={"question": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "hi there"
    tid = body["thread_id"]
    assert tid

    # The conversation was saved under the returned thread id.
    stored = get_store().get_thread(tid)
    assert stored is not None
    assert stored["message_count"] >= 2
    assert stored["messages"][-1] == {"role": "assistant", "content": "hi there"}


def test_chat_resumes_prior_history(client, patch_stream):
    # Seed a thread with an existing exchange.
    get_store().save_messages(
        "resume-me",
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "my name is Sam"},
            {"role": "assistant", "content": "nice to meet you Sam"},
        ],
    )
    seen = patch_stream([{"type": "final", "content": "your name is Sam"}])

    r = client.post("/chat", json={"question": "what is my name?", "thread_id": "resume-me"})
    assert r.status_code == 200
    assert r.json()["thread_id"] == "resume-me"

    # The agent was handed the prior history to resume from.
    assert seen["history"][1]["content"] == "my name is Sam"
    # And the thread grew.
    assert get_store().get_thread("resume-me")["message_count"] >= 4


def test_chat_error_does_not_persist(client, patch_stream):
    patch_stream([{"type": "error", "message": "boom"}])
    r = client.post("/chat", json={"question": "x", "thread_id": "err-thread"})
    assert r.status_code == 200
    assert r.json()["error"] == "boom"
    assert r.json()["thread_id"] == "err-thread"
    # Nothing durable recorded on a failed turn.
    assert get_store().get_thread("err-thread") is None


def test_chat_stream_leads_with_thread_id_and_saves(client, patch_stream):
    patch_stream(
        [
            {"type": "message_chunk", "delta": "hi"},
            {"type": "final", "content": "hi"},
        ]
    )
    r = client.post("/chat/stream", json={"question": "go", "thread_id": "st1"})
    frames = _parse_sse(r.text)
    names = [ev for ev, _ in frames]
    assert names[0] == "thread_id"
    assert frames[0][1] == {"thread_id": "st1"}
    assert names[-1] == "done"
    # Clean finish -> history persisted.
    assert get_store().get_thread("st1") is not None


def test_chat_stream_error_does_not_save(client, patch_stream):
    patch_stream([{"type": "error", "message": "bad"}])
    r = client.post("/chat/stream", json={"question": "go", "thread_id": "st-err"})
    frames = _parse_sse(r.text)
    assert frames[0][0] == "thread_id"
    assert any(ev == "error" for ev, _ in frames)
    assert frames[-1][0] == "done"
    assert get_store().get_thread("st-err") is None

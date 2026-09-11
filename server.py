"""mini-deerflow web server (P3: 会话持久化 / thread persistence).

Exposes the tool-calling lead agent over HTTP, mirroring DeerFlow's
`POST /stream` + `text/event-stream` contract and its *thread* model in the
simplest possible form.

What P3 adds on top of P2's stateless server:
- A SQLite-backed thread store (see ``store.py``). Each conversation is a
  ``thread`` with an id, a title, timestamps, and its full message history.
- ``/chat`` and ``/chat/stream`` accept an optional ``thread_id``: prior history
  is loaded and fed to the agent, and the updated conversation is saved back.
  Omitting ``thread_id`` keeps the old stateless behaviour but auto-creates a
  fresh thread so the client can keep talking.
- Thread management endpoints: create / list / get / delete.

Endpoints:
    GET    /health              -> liveness probe {"status": "ok", "phase": "P3"}
    POST   /chat                -> blocking JSON reply {"content", "thread_id"}
    POST   /chat/stream         -> Server-Sent Events; one `event:`/`data:` per event
    POST   /threads             -> create a thread -> metadata
    GET    /threads             -> list thread metadata (newest first)
    GET    /threads/{thread_id} -> thread metadata + full message history
    DELETE /threads/{thread_id} -> delete a thread

SSE event names map 1:1 onto the agent's event `type`, plus a leading
``thread_id`` frame so the client learns which thread it is talking to:
    thread_id | message_chunk | tool_start | tool_end | max_steps | final | error
A trailing ``done`` frame signals normal completion.

Run it (outside the sandbox):
    uvicorn server:app --host 0.0.0.0 --port 8000
    # or: python server.py
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.lead_agent import LeadAgent
from store import get_store

app = FastAPI(title="mini-deerflow", version="0.3.0 (P3)")


# --- request / response models ----------------------------------------------
class ChatRequest(BaseModel):
    """Request body for both /chat and /chat/stream."""

    question: str = Field(..., min_length=1, description="The user's task / question.")
    thread_id: str | None = Field(
        default=None,
        description="Resume this thread's history. Omit to start a new thread.",
    )
    max_steps: int | None = Field(
        default=None, ge=1, le=50, description="Optional ReAct step cap override."
    )


class CreateThreadRequest(BaseModel):
    """Optional body for POST /threads."""

    title: str = Field(default="", description="Human-friendly thread title.")
    thread_id: str | None = Field(
        default=None, description="Client-chosen id (must be unique). Auto if omitted."
    )


# --- helpers -----------------------------------------------------------------
def _build_agent(req: ChatRequest) -> LeadAgent:
    if req.max_steps is not None:
        return LeadAgent(max_steps=req.max_steps)
    return LeadAgent()


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format one Server-Sent Event frame: an `event:` line + a JSON `data:` line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resolve_thread(thread_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Return (thread_id, prior_history).

    An existing thread's history is loaded; a missing/None id becomes a brand
    new thread id with empty history. Persistence of the *updated* history
    happens after the agent runs.
    """
    store = get_store()
    if thread_id:
        thread = store.get_thread(thread_id)
        if thread is not None:
            return thread_id, thread["messages"]
        # Unknown id: honour it as a client-chosen new thread id.
        return thread_id, []
    return uuid.uuid4().hex, []


# --- endpoints ---------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "phase": "P3"}


@app.post("/chat")
def chat(req: ChatRequest) -> dict[str, str]:
    """Blocking variant: run the agent to completion and return the final text.

    Loads any prior thread history, runs the ReAct loop, and persists the
    updated conversation back to the thread. Returns the final content plus the
    ``thread_id`` the client should reuse to continue the conversation.
    """
    agent = _build_agent(req)
    thread_id, history = _resolve_thread(req.thread_id)

    content = ""
    error: str | None = None
    for ev in agent.run_stream(req.question, history=history):
        if ev["type"] == "final":
            content = ev.get("content", "")
        elif ev["type"] == "error":
            error = ev.get("message", "unknown error")

    if error is not None:
        # Nothing durable to record on a failed turn.
        return {"error": error, "thread_id": thread_id}

    get_store().save_messages(thread_id, agent.messages)
    return {"content": content, "thread_id": thread_id}


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Stream the agent's work as Server-Sent Events (token-level).

    Emits a leading ``thread_id`` frame, then one SSE frame per agent event, and
    a trailing ``done`` frame. On a clean (non-error) finish the updated
    conversation is saved back to the thread store.
    """
    agent = _build_agent(req)
    thread_id, history = _resolve_thread(req.thread_id)

    def event_source() -> Iterator[str]:
        yield _sse("thread_id", {"thread_id": thread_id})
        errored = False
        for ev in agent.run_stream(req.question, history=history):
            kind = ev.pop("type")
            if kind == "error":
                errored = True
            yield _sse(kind, ev)
        if not errored:
            get_store().save_messages(thread_id, agent.messages)
        yield _sse("done", {})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering so tokens flush live
        },
    )


@app.post("/threads", status_code=201)
def create_thread(req: CreateThreadRequest | None = None) -> dict[str, Any]:
    """Create a new (empty) thread and return its metadata."""
    req = req or CreateThreadRequest()
    try:
        return get_store().create_thread(thread_id=req.thread_id, title=req.title)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/threads")
def list_threads() -> dict[str, Any]:
    """List thread metadata (no message bodies), newest activity first."""
    return {"threads": get_store().list_threads()}


@app.get("/threads/{thread_id}")
def get_thread(thread_id: str) -> dict[str, Any]:
    """Return a thread's metadata plus its full message history."""
    thread = get_store().get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
    return thread


@app.delete("/threads/{thread_id}")
def delete_thread(thread_id: str) -> dict[str, Any]:
    """Delete a thread. 404 if it does not exist."""
    if not get_store().delete_thread(thread_id):
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
    return {"deleted": thread_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

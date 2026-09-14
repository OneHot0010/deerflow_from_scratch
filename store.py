"""Thread/message persistence — P3 (会话持久化).

DeerFlow keeps every conversation as a *thread* (``thread_id``) whose state is
checkpointed so a client can resume it, list past threads, or branch from one.
This module is the minimal, zero-new-dependency counterpart: a tiny store built
on Python's stdlib ``sqlite3`` (no SQLAlchemy, no LangGraph checkpointer).

Design, in the spirit of the earlier phases:
- One ``Tool``-sized abstraction: a ``ThreadStore`` class owning a single SQLite
  connection. Nothing else in the codebase needs to know it is SQLite.
- Messages are persisted as one JSON blob per thread (the OpenAI-style message
  list the agent already passes around). A thread is small; a blob keeps the
  schema trivial and the round-trip loss-free — tool_calls and all.
- ``:memory:`` is supported for tests (a single shared connection keeps the
  in-memory DB alive for the store's lifetime).
- Thread-safe enough for FastAPI's threadpool: ``check_same_thread=False`` plus
  a coarse lock around writes.

Schema (one table)::

    threads(
        id          TEXT PRIMARY KEY,   -- uuid4 hex unless caller supplies one
        title       TEXT,               -- derived from the first user message
        created_at  TEXT,               -- ISO-8601 UTC
        updated_at  TEXT,               -- ISO-8601 UTC, bumped on every save
        messages    TEXT                -- JSON array of message dicts
    )

Later phases (P4 middleware, P10 layering) can swap the backend without touching
callers, exactly like ``get_available_tools()`` stayed stable across P1→P8.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import config


def _now_iso() -> str:
    """UTC timestamp, ISO-8601 with a trailing 'Z' — stable and sortable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _derive_title(messages: list[dict[str, Any]], fallback: str = "New thread") -> str:
    """Use the first user message as the thread title (trimmed), like most chat UIs."""
    for m in messages:
        if m.get("role") == "user":
            text = (m.get("content") or "").strip().replace("\n", " ")
            if text:
                return text[:60]
    return fallback


class ThreadStore:
    """SQLite-backed store for conversation threads and their message history.

    Args:
        db_path: SQLite file path, or ``":memory:"`` for a transient DB. Defaults
            to ``config.THREAD_DB_PATH``.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or config.THREAD_DB_PATH
        # check_same_thread=False: FastAPI may touch the store from worker
        # threads. We serialize writes ourselves with the lock below.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id         TEXT PRIMARY KEY,
                    title      TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    messages   TEXT NOT NULL DEFAULT '[]'
                )
                """
            )

    # -- create / read -------------------------------------------------------
    def create_thread(
        self, thread_id: str | None = None, title: str = ""
    ) -> dict[str, Any]:
        """Create a new (empty) thread and return its metadata.

        A ``thread_id`` may be supplied (idempotent client-chosen id); otherwise
        a uuid4 hex is generated. Raises ``ValueError`` if the id already exists.
        """
        tid = thread_id or uuid.uuid4().hex
        now = _now_iso()
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO threads (id, title, created_at, updated_at, messages)"
                    " VALUES (?, ?, ?, ?, '[]')",
                    (tid, title, now, now),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"thread already exists: {tid}") from e
        return {
            "id": tid,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        }

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Return thread metadata + full message list, or None if absent."""
        row = self._conn.execute(
            "SELECT id, title, created_at, updated_at, messages FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        messages = json.loads(row["messages"])
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": messages,
            "message_count": len(messages),
        }

    def list_threads(self) -> list[dict[str, Any]]:
        """Return thread metadata (no message bodies), newest activity first."""
        rows = self._conn.execute(
            "SELECT id, title, created_at, updated_at, messages FROM threads"
            " ORDER BY updated_at DESC"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "message_count": len(json.loads(row["messages"])),
                }
            )
        return out

    def get_messages(self, thread_id: str) -> list[dict[str, Any]]:
        """Return just the message list for a thread ([] if it has none/absent)."""
        row = self._conn.execute(
            "SELECT messages FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        return json.loads(row["messages"]) if row else []

    # -- write ---------------------------------------------------------------
    def save_messages(
        self, thread_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Overwrite a thread's message history and bump ``updated_at``.

        Auto-creates the thread if it does not exist yet (so callers can persist
        a brand-new conversation in one call). Derives a title from the first
        user message when the thread has none.
        """
        blob = json.dumps(messages, ensure_ascii=False)
        now = _now_iso()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT title FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO threads (id, title, created_at, updated_at, messages)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (thread_id, _derive_title(messages), now, now, blob),
                )
            else:
                title = existing["title"] or _derive_title(messages)
                self._conn.execute(
                    "UPDATE threads SET messages = ?, updated_at = ?, title = ? WHERE id = ?",
                    (blob, now, title, thread_id),
                )
        return {
            "id": thread_id,
            "updated_at": now,
            "message_count": len(messages),
        }

    def set_title(self, thread_id: str, title: str) -> bool:
        """Set a thread's title explicitly (used by TitleMiddleware, P4).

        Overwrites whatever title the thread currently has. Returns True if a
        row was updated. Unlike ``save_messages``' fallback derivation, this is
        the authoritative title the middleware computed for the conversation.
        """
        title = (title or "").strip()
        if not title:
            return False
        now = _now_iso()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, thread_id),
            )
        return cur.rowcount > 0

    def delete_thread(self, thread_id: str) -> bool:
        """Delete a thread. Returns True if a row was removed."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM threads WHERE id = ?", (thread_id,)
            )
        return cur.rowcount > 0

    def close(self) -> None:
        """Close the underlying connection (mostly for tests)."""
        self._conn.close()


# A process-wide default store, created lazily so importing this module never
# touches the filesystem (and tests can inject their own instance instead).
_default_store: ThreadStore | None = None


def get_store() -> ThreadStore:
    """Return the process-wide default ThreadStore, creating it on first use."""
    global _default_store
    if _default_store is None:
        _default_store = ThreadStore()
    return _default_store


def set_store(store: ThreadStore | None) -> None:
    """Override (or reset) the process-wide default store. Used by tests."""
    global _default_store
    _default_store = store

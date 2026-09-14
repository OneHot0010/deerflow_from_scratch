"""P3 tests: the SQLite thread store (store.py).

All offline and side-effect free: every store here is ``:memory:``. We assert
the CRUD surface the web layer relies on — create / get / list / save / delete —
plus title derivation, lossless message round-trip (tool_calls included), and
the process-wide default-store accessors.
"""
from __future__ import annotations

import pytest

from store import ThreadStore, get_store, set_store


@pytest.fixture
def s():
    st = ThreadStore(":memory:")
    yield st
    st.close()


def test_create_then_get_roundtrip(s):
    meta = s.create_thread(title="hello")
    assert meta["title"] == "hello"
    assert meta["message_count"] == 0

    got = s.get_thread(meta["id"])
    assert got is not None
    assert got["id"] == meta["id"]
    assert got["messages"] == []
    assert got["created_at"] and got["updated_at"]


def test_get_missing_thread_returns_none(s):
    assert s.get_thread("does-not-exist") is None


def test_create_with_explicit_id_and_duplicate_rejected(s):
    s.create_thread(thread_id="fixed-id")
    assert s.get_thread("fixed-id") is not None
    with pytest.raises(ValueError):
        s.create_thread(thread_id="fixed-id")


def test_save_messages_autocreates_and_derives_title(s):
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
    ]
    res = s.save_messages("t1", msgs)
    assert res["message_count"] == 3

    got = s.get_thread("t1")
    assert got["title"] == "What is 2+2?"  # derived from first user message
    assert got["messages"] == msgs  # lossless round-trip


def test_save_messages_preserves_tool_calls(s):
    msgs = [
        {"role": "user", "content": "run"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file.txt"},
    ]
    s.save_messages("t2", msgs)
    got = s.get_thread("t2")
    assert got["messages"][1]["tool_calls"][0]["function"]["name"] == "bash"
    assert got["messages"][2]["tool_call_id"] == "c1"


def test_save_messages_updates_existing_and_bumps_timestamp(s):
    s.save_messages("t3", [{"role": "user", "content": "first"}])
    first = s.get_thread("t3")["updated_at"]
    s.save_messages(
        "t3",
        [{"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"}],
    )
    got = s.get_thread("t3")
    assert got["message_count"] == 2
    assert got["updated_at"] >= first
    # A once-derived title is kept even as history grows.
    assert got["title"] == "first"


def test_get_messages_shortcut(s):
    assert s.get_messages("nope") == []
    s.save_messages("t4", [{"role": "user", "content": "hey"}])
    assert s.get_messages("t4") == [{"role": "user", "content": "hey"}]


def test_list_threads_orders_by_recent_activity(s):
    s.save_messages("a", [{"role": "user", "content": "a"}])
    s.save_messages("b", [{"role": "user", "content": "b"}])
    s.save_messages("a", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "x"}])

    listed = s.list_threads()
    ids = [t["id"] for t in listed]
    assert set(ids) == {"a", "b"}
    # 'a' was touched most recently -> should sort first.
    assert ids[0] == "a"
    # list must not carry message bodies, only counts.
    assert all("messages" not in t for t in listed)
    assert all("message_count" in t for t in listed)


def test_delete_thread(s):
    s.create_thread(thread_id="gone")
    assert s.delete_thread("gone") is True
    assert s.get_thread("gone") is None
    assert s.delete_thread("gone") is False  # idempotent-ish: already absent


def test_default_store_accessors_are_overridable(monkeypatch):
    custom = ThreadStore(":memory:")
    set_store(custom)
    assert get_store() is custom

    # After reset, a fresh default is lazily built. Point the default path at an
    # in-memory DB so the test never writes a real threads.db file.
    import config

    monkeypatch.setattr(config, "THREAD_DB_PATH", ":memory:")
    set_store(None)
    rebuilt = get_store()
    assert isinstance(rebuilt, ThreadStore)
    set_store(None)  # don't leak into other tests

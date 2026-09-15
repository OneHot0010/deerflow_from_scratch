"""P5 (沙箱化执行) offline tests.

Cover the roadmap's P5 acceptance — "工具执行在沙箱内,虚拟路径正确映射,防目录穿越":

- Provider lifecycle: acquire / get / release / reset, per-thread isolation.
- Virtual-path mapping: /workspace/... resolves under the host root; output and
  listings are virtualised (host paths never leak).
- Directory-traversal defense: ``../`` escapes, absolute escapes, and symlinks
  pointing outside a mapping all raise SandboxPathError before any I/O.
- Read-only mappings reject writes with EROFS.
- Tool routing: ``get_available_tools(sandbox=...)`` returns sandbox-bound tools
  with identical names/schemas, and their error text degrades gracefully.
- The provider singleton (get/set_sandbox_provider) is injectable for tests.

Everything is rooted at pytest's ``tmp_path`` so the suite stays offline and
creates no files outside the test's temporary directory.
"""
from __future__ import annotations

import errno
import os

import pytest

from sandbox import (
    LocalSandbox,
    LocalSandboxProvider,
    PathMapping,
    SandboxPathError,
    get_sandbox_provider,
    set_sandbox_provider,
)
from tools import get_available_tools, tools_by_name


# --- helpers ----------------------------------------------------------------
def _provider(tmp_path, **kw) -> LocalSandboxProvider:
    return LocalSandboxProvider(base_dir=str(tmp_path / "sandboxes"), **kw)


def _sandbox(provider, thread_id=None):
    return provider.get(provider.acquire(thread_id))


# --- provider lifecycle -----------------------------------------------------
def test_acquire_generic_and_thread_ids(tmp_path):
    p = _provider(tmp_path)
    assert p.acquire(None) == "local"
    assert p.acquire("abc") == "thread-abc"


def test_acquire_is_idempotent_and_get_returns_same_object(tmp_path):
    p = _provider(tmp_path)
    sid1 = p.acquire("t1")
    sid2 = p.acquire("t1")
    assert sid1 == sid2
    assert p.get(sid1) is p.get(sid2)


def test_per_thread_isolation(tmp_path):
    p = _provider(tmp_path)
    a = _sandbox(p, "one")
    b = _sandbox(p, "two")
    a.write_file("/workspace/x.txt", "A")
    b.write_file("/workspace/x.txt", "B")
    assert a.read_file("/workspace/x.txt") == "A"
    assert b.read_file("/workspace/x.txt") == "B"


def test_get_unknown_returns_none(tmp_path):
    assert _provider(tmp_path).get("nope") is None


def test_release_and_reset(tmp_path):
    p = _provider(tmp_path)
    sid = p.acquire("gone")
    assert p.get(sid) is not None
    p.release(sid)
    assert p.get(sid) is None
    p.acquire("a")
    p.acquire("b")
    p.reset()
    assert p.get("thread-a") is None and p.get("thread-b") is None


# --- virtual-path mapping ---------------------------------------------------
def test_write_read_roundtrip_via_virtual_path(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    sb.write_file("/workspace/notes.txt", "hello")
    assert sb.read_file("/workspace/notes.txt") == "hello"


def test_relative_path_resolves_against_primary_root(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    sb.write_file("rel.txt", "r")
    assert sb.read_file("/workspace/rel.txt") == "r"


def test_file_lands_on_host_under_mapped_root(tmp_path):
    p = _provider(tmp_path)
    sid = p.acquire(None)
    sb = p.get(sid)
    sb.write_file("/workspace/deep/a.txt", "x")
    host = os.path.join(str(tmp_path / "sandboxes"), sid, "workspace", "deep", "a.txt")
    assert os.path.isfile(host)
    with open(host) as f:
        assert f.read() == "x"


def test_append_mode(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    sb.write_file("/workspace/log.txt", "a")
    sb.write_file("/workspace/log.txt", "b", append=True)
    assert sb.read_file("/workspace/log.txt") == "ab"


def test_list_dir_returns_virtual_paths(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    sb.write_file("/workspace/a.txt", "1")
    sb.write_file("/workspace/sub/b.txt", "2")
    entries = sb.list_dir("/workspace")
    assert "/workspace/a.txt" in entries
    assert "/workspace/sub/" in entries
    # No host path ever leaks into a listing.
    assert not any(str(tmp_path) in e for e in entries)


def test_execute_command_runs_in_sandbox_and_rewrites_paths(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    sb.write_file("/workspace/a.txt", "sandboxed!")
    out = sb.execute_command("cat /workspace/a.txt")
    assert "sandboxed!" in out
    assert "exit_code: 0" in out


def test_command_output_is_virtualized(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    out = sb.execute_command("pwd")
    assert "/workspace" in out
    assert str(tmp_path) not in out


# --- directory-traversal defense --------------------------------------------
def test_dotdot_escape_blocked(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    with pytest.raises(SandboxPathError):
        sb.read_file("/workspace/../../etc/passwd")


def test_absolute_escape_blocked(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    with pytest.raises(SandboxPathError):
        sb.read_file("/etc/passwd")


def test_nul_byte_blocked(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    with pytest.raises(SandboxPathError):
        sb.read_file("/workspace/a\x00b")


def test_symlink_escape_blocked(tmp_path):
    p = _provider(tmp_path)
    sid = p.acquire(None)
    sb = p.get(sid)
    # A secret outside every mapping.
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret")
    workspace = os.path.join(str(tmp_path / "sandboxes"), sid, "workspace")
    os.makedirs(workspace, exist_ok=True)
    link = os.path.join(workspace, "escape")
    os.symlink(str(secret), link)
    with pytest.raises(SandboxPathError):
        sb.read_file("/workspace/escape")


def test_write_traversal_blocked(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    with pytest.raises(SandboxPathError):
        sb.write_file("/workspace/../evil.txt", "x")


# --- read-only mappings -----------------------------------------------------
def test_read_only_mapping_rejects_write(tmp_path):
    ro_host = tmp_path / "libs"
    ro_host.mkdir()
    (ro_host / "readme.txt").write_text("docs")
    sb = LocalSandbox(
        "ro",
        [
            PathMapping("/workspace", str(tmp_path / "ws")),
            PathMapping("/libs", str(ro_host), read_only=True),
        ],
    )
    # Reading the read-only tree works.
    assert sb.read_file("/libs/readme.txt") == "docs"
    # Writing to it is rejected with EROFS.
    with pytest.raises(OSError) as ei:
        sb.write_file("/libs/x.txt", "nope")
    assert ei.value.errno == errno.EROFS


def test_extra_mappings_flow_through_provider(tmp_path):
    ro_host = tmp_path / "shared"
    ro_host.mkdir()
    (ro_host / "note.txt").write_text("shared-note")
    p = _provider(
        tmp_path,
        extra_mappings=[PathMapping("/shared", str(ro_host), read_only=True)],
    )
    sb = _sandbox(p, "t")
    assert sb.read_file("/shared/note.txt") == "shared-note"


# --- tool routing -----------------------------------------------------------
def test_get_available_tools_without_sandbox_are_host_builtins(tmp_path):
    tools = get_available_tools()
    names = {t.name for t in tools}
    assert {"bash", "read_file", "write_file"} <= names


def test_get_available_tools_with_sandbox_keep_names_and_schemas(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    host = tools_by_name(get_available_tools())
    sandboxed = tools_by_name(get_available_tools(sandbox=sb))
    # Identical tool *set* so the LLM sees the same surface either way.
    assert set(host) == set(sandboxed)
    # Identical schema *structure* (param names, types, required list). Only the
    # human-facing descriptions differ (virtual vs host paths), which is fine.
    def _shape(schema: dict) -> dict:
        props = {
            k: v.get("type") for k, v in schema.get("properties", {}).items()
        }
        return {"required": schema.get("required"), "props": props}

    for name in ("bash", "read_file", "write_file"):
        assert _shape(host[name].parameters) == _shape(sandboxed[name].parameters)


def test_sandbox_tools_execute_against_sandbox(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    tools = tools_by_name(get_available_tools(sandbox=sb))
    tools["write_file"].run('{"path": "/workspace/t.txt", "content": "hi"}')
    out = tools["read_file"].run('{"path": "/workspace/t.txt"}')
    assert "hi" in out
    assert sb.read_file("/workspace/t.txt") == "hi"


def test_sandbox_tool_blocks_traversal_as_text(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    tools = tools_by_name(get_available_tools(sandbox=sb))
    out = tools["read_file"].run('{"path": "/etc/passwd"}')
    assert "blocked" in out.lower()


def test_sandbox_write_tool_invalid_mode_text(tmp_path):
    sb = _sandbox(_provider(tmp_path))
    tools = tools_by_name(get_available_tools(sandbox=sb))
    out = tools["write_file"].run('{"path": "/workspace/x", "content": "y", "mode": "bogus"}')
    assert "invalid mode" in out.lower()


# --- provider singleton -----------------------------------------------------
def test_set_and_get_sandbox_provider(tmp_path):
    custom = _provider(tmp_path)
    set_sandbox_provider(custom)
    try:
        assert get_sandbox_provider() is custom
    finally:
        set_sandbox_provider(None)

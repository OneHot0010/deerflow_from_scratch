"""P1 tests: the tool layer (tools/base.py, tools/builtins.py, tools/__init__.py).

Two concerns:
1. The Tool abstraction — schema rendering, robust argument parsing, and error
   capture so a bad call degrades into text instead of an exception.
2. The three builtins — bash / read_file / write_file — including truncation,
   append vs overwrite, and the friendly error strings for missing files etc.

All filesystem work happens under pytest's tmp_path; bash tests use trivial,
portable commands.
"""
from __future__ import annotations

import pytest

from tools import BUILTIN_TOOLS, get_available_tools, tools_by_name
from tools.base import Tool, _stringify, tool
from tools import builtins as B


# --- Tool abstraction -------------------------------------------------------
def _echo_tool() -> Tool:
    @tool("echo", "echo back", {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]})
    def _echo(x: str) -> str:
        return f"got:{x}"

    return _echo


def test_to_openai_schema_shape():
    t = _echo_tool()
    schema = t.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["description"] == "echo back"
    assert schema["function"]["parameters"]["required"] == ["x"]


def test_run_parses_json_string_args():
    t = _echo_tool()
    assert t.run('{"x": "hi"}') == "got:hi"


def test_run_accepts_dict_args():
    t = _echo_tool()
    assert t.run({"x": "hi"}) == "got:hi"


def test_run_empty_args_calls_with_no_kwargs():
    @tool("noarg", "no args", {"type": "object", "properties": {}})
    def _noarg() -> str:
        return "ok"

    assert _noarg.run("") == "ok"
    assert _noarg.run(None) == "ok"


def test_run_invalid_json_returns_error_text():
    t = _echo_tool()
    out = t.run("{not json")
    assert out.startswith("[tool-error]") and "parse" in out


def test_run_non_object_json_returns_error():
    t = _echo_tool()
    out = t.run("[1, 2, 3]")
    assert out.startswith("[tool-error]") and "JSON object" in out


def test_run_bad_kwargs_returns_error():
    t = _echo_tool()  # missing required 'x'
    out = t.run("{}")
    assert out.startswith("[tool-error]") and "bad arguments" in out


def test_run_tool_body_exception_is_caught():
    @tool("boom", "raises", {"type": "object", "properties": {}})
    def _boom() -> str:
        raise ValueError("kaboom")

    out = _boom.run("{}")
    assert out.startswith("[tool-error]") and "kaboom" in out


def test_stringify_coerces_non_str():
    assert _stringify("x") == "x"
    assert _stringify({"a": 1}) == '{"a": 1}'
    assert _stringify(123) == "123"


# --- registry ---------------------------------------------------------------
def test_get_available_tools_returns_all_by_default():
    names = {t.name for t in get_available_tools()}
    assert names == {"bash", "read_file", "write_file"}


def test_get_available_tools_allow_list_filters():
    got = get_available_tools(["bash", "nonexistent"])
    assert [t.name for t in got] == ["bash"]


def test_tools_by_name_indexes():
    idx = tools_by_name()
    assert set(idx) == {"bash", "read_file", "write_file"}
    assert idx["bash"].name == "bash"


def test_builtin_tools_are_tool_instances():
    assert all(isinstance(t, Tool) for t in BUILTIN_TOOLS)


# --- bash -------------------------------------------------------------------
def test_bash_captures_stdout_and_exit_code():
    out = B.bash.func(command="echo hello")
    assert "exit_code: 0" in out
    assert "hello" in out


def test_bash_reports_nonzero_exit():
    out = B.bash.func(command="sh -c 'exit 3'")
    assert "exit_code: 3" in out


def test_bash_captures_stderr():
    out = B.bash.func(command="sh -c 'echo oops 1>&2'")
    assert "stderr:" in out and "oops" in out


def test_bash_timeout_message():
    out = B.bash.func(command="sleep 2", timeout=1)
    assert "timed out" in out


# --- read_file --------------------------------------------------------------
def test_read_file_returns_contents(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("line1\nline2\n", encoding="utf-8")
    assert B.read_file.func(path=str(p)) == "line1\nline2\n"


def test_read_file_max_lines_clips(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("\n".join(str(i) for i in range(10)), encoding="utf-8")
    out = B.read_file.func(path=str(p), max_lines=3)
    assert "0\n1\n2" in out
    assert "more lines" in out


def test_read_file_missing():
    out = B.read_file.func(path="/no/such/file_xyz.txt")
    assert out.startswith("[read_file] no such file")


def test_read_file_directory(tmp_path):
    out = B.read_file.func(path=str(tmp_path))
    assert "is a directory" in out


def test_read_file_truncates_huge(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("x" * (B._MAX_OUTPUT_CHARS + 500), encoding="utf-8")
    out = B.read_file.func(path=str(p))
    assert "truncated" in out


# --- write_file -------------------------------------------------------------
def test_write_file_overwrite(tmp_path):
    p = tmp_path / "sub" / "out.txt"
    msg = B.write_file.func(path=str(p), content="hi")
    assert "wrote 2 chars" in msg
    assert p.read_text(encoding="utf-8") == "hi"


def test_write_file_append(tmp_path):
    p = tmp_path / "out.txt"
    B.write_file.func(path=str(p), content="a")
    B.write_file.func(path=str(p), content="b", mode="append")
    assert p.read_text(encoding="utf-8") == "ab"


def test_write_file_invalid_mode(tmp_path):
    out = B.write_file.func(path=str(tmp_path / "x.txt"), content="y", mode="nope")
    assert "invalid mode" in out


def test_write_file_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "f.txt"
    B.write_file.func(path=str(p), content="z")
    assert p.exists()

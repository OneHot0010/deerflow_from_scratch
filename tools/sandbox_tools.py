"""Sandbox-backed built-in tools — P5 (沙箱化执行).

The P1 builtins (``tools/builtins.py``) run ``bash`` / ``read_file`` /
``write_file`` directly on the host with raw host paths. P5 introduces the same
three tools, but bound to a :class:`sandbox.Sandbox`: every command and file
operation runs *inside* the sandbox and is addressed by **virtual paths**
(``/workspace/...``). Path traversal is hard-blocked by the sandbox, so the
model can no longer touch anything outside its mapped roots.

The tool *names and schemas stay identical* to the P1 builtins, so nothing at
the call site (the LLM's tool selection, the ReAct loop, the SSE protocol)
changes — only the execution substrate. This mirrors how DeerFlow keeps a
stable ``get_available_tools()`` surface while swapping the sandbox behind it.

Because a :class:`~sandbox.Sandbox` is a *live per-thread object*, these tools
are built by a factory (:func:`make_sandbox_tools`) that closes over one
sandbox instance, rather than being module-level singletons like the host
builtins. The tool layer's :func:`tools.get_available_tools` accepts a
``sandbox=`` argument and returns these when one is supplied.
"""
from __future__ import annotations

from sandbox.base import Sandbox, SandboxPathError
from tools.base import Tool, tool

# Reuse the same argument schemas as the P1 builtins so the model sees an
# identical tool surface whether or not the sandbox is active.
_BASH_PARAMS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The shell command to execute, e.g. 'ls -la'.",
        },
        "timeout": {
            "type": "integer",
            "description": "Max seconds to wait before killing the command (default 60).",
        },
    },
    "required": ["command"],
}

_READ_PARAMS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Virtual path to the file, e.g. '/workspace/notes.txt'.",
        },
        "max_lines": {
            "type": "integer",
            "description": "If set, return at most this many lines from the top.",
        },
    },
    "required": ["path"],
}

_WRITE_PARAMS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Virtual destination path, e.g. '/workspace/out.txt'.",
        },
        "content": {"type": "string", "description": "The text content to write."},
        "mode": {
            "type": "string",
            "enum": ["overwrite", "append"],
            "description": "'overwrite' replaces the file; 'append' adds to it. Default 'overwrite'.",
        },
    },
    "required": ["path", "content"],
}


def make_sandbox_tools(sandbox: Sandbox) -> list[Tool]:
    """Build ``bash`` / ``read_file`` / ``write_file`` bound to ``sandbox``.

    Each returned :class:`~tools.base.Tool` closes over the given sandbox and
    speaks virtual paths. Errors — including a blocked traversal
    (:class:`~sandbox.SandboxPathError`) — degrade into plain text so the ReAct
    loop can read and recover from them, exactly like the host builtins.
    """

    @tool(
        name="bash",
        description=(
            "Run a shell command inside the sandbox and return its stdout, "
            "stderr and exit code. The working directory is the sandbox root "
            "(virtual path '/workspace'). Use virtual paths like "
            "'/workspace/file.txt' to reference files."
        ),
        parameters=_BASH_PARAMS,
    )
    def bash(command: str, timeout: int = 60) -> str:
        return sandbox.execute_command(command, timeout=timeout)

    @tool(
        name="read_file",
        description=(
            "Read a UTF-8 text file from inside the sandbox by its virtual "
            "path (e.g. '/workspace/notes.txt'). Optionally limit how many "
            "lines are returned."
        ),
        parameters=_READ_PARAMS,
    )
    def read_file(path: str, max_lines: int | None = None) -> str:
        try:
            return sandbox.read_file(path, max_lines=max_lines)
        except SandboxPathError as e:
            return f"[read_file] blocked: {e}"
        except FileNotFoundError:
            return f"[read_file] no such file: {path}"
        except IsADirectoryError:
            return f"[read_file] path is a directory, not a file: {path}"
        except UnicodeDecodeError:
            return f"[read_file] not a UTF-8 text file: {path}"
        except OSError as e:
            return f"[read_file] could not read {path}: {e}"

    @tool(
        name="write_file",
        description=(
            "Write text to a file inside the sandbox by its virtual path. "
            "Creates parent directories as needed. Use mode='overwrite' "
            "(default) to replace the file, or mode='append' to add to the end."
        ),
        parameters=_WRITE_PARAMS,
    )
    def write_file(path: str, content: str, mode: str = "overwrite") -> str:
        if mode not in ("overwrite", "append"):
            return f"[write_file] invalid mode '{mode}'; use 'overwrite' or 'append'"
        try:
            sandbox.write_file(path, content, append=(mode == "append"))
        except SandboxPathError as e:
            return f"[write_file] blocked: {e}"
        except OSError as e:
            return f"[write_file] could not write {path}: {e}"
        return f"[write_file] wrote {len(content)} chars to {path} (mode={mode})"

    return [bash, read_file, write_file]


__all__ = ["make_sandbox_tools"]

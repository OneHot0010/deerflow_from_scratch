"""Built-in tools for mini-deerflow (P1).

Three primitive tools that let the lead agent actually *do* things, mirroring
DeerFlow's builtin surface at its simplest:

- ``bash``       — run a shell command and capture stdout/stderr/exit code.
- ``read_file``  — read (part of) a UTF-8 text file.
- ``write_file`` — create / overwrite / append a text file.

Everything runs on the local host (no sandbox yet — that is P5). Each tool
guards itself and returns a plain string so failures degrade into text the
model can read and react to, rather than exceptions that break the loop.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tools.base import Tool, tool

# Cap how much text we ever hand back to the model, to protect the context
# window from a runaway command or a huge file.
_MAX_OUTPUT_CHARS = 20_000


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    return f"{head}\n...[truncated {len(text) - limit} chars]"


# --- bash -------------------------------------------------------------------
@tool(
    name="bash",
    description=(
        "Run a shell command on the local machine and return its stdout, "
        "stderr and exit code. Use for listing directories, running scripts, "
        "installing packages, or any task better done via the shell."
    ),
    parameters={
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
    },
)
def bash(command: str, timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[bash] command timed out after {timeout}s: {command}"

    parts = [f"exit_code: {proc.returncode}"]
    if proc.stdout:
        parts.append("stdout:\n" + _truncate(proc.stdout))
    if proc.stderr:
        parts.append("stderr:\n" + _truncate(proc.stderr))
    if not proc.stdout and not proc.stderr:
        parts.append("(no output)")
    return "\n".join(parts)


# --- read_file --------------------------------------------------------------
@tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file from the local filesystem and return its "
        "contents. Optionally limit how many lines are returned."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (absolute or relative).",
            },
            "max_lines": {
                "type": "integer",
                "description": "If set, return at most this many lines from the top.",
            },
        },
        "required": ["path"],
    },
)
def read_file(path: str, max_lines: int | None = None) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"[read_file] no such file: {path}"
    if p.is_dir():
        return f"[read_file] path is a directory, not a file: {path}"
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"[read_file] not a UTF-8 text file: {path}"
    except OSError as e:
        return f"[read_file] could not read {path}: {e}"

    if max_lines is not None and max_lines > 0:
        lines = text.splitlines()
        clipped = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            clipped += f"\n...[{len(lines) - max_lines} more lines]"
        return _truncate(clipped)
    return _truncate(text)


# --- write_file -------------------------------------------------------------
@tool(
    name="write_file",
    description=(
        "Write text to a file on the local filesystem. Creates parent "
        "directories as needed. Use mode='overwrite' (default) to replace the "
        "file, or mode='append' to add to the end."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Destination file path (absolute or relative).",
            },
            "content": {
                "type": "string",
                "description": "The text content to write.",
            },
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "description": "'overwrite' replaces the file; 'append' adds to it. Default 'overwrite'.",
            },
        },
        "required": ["path", "content"],
    },
)
def write_file(path: str, content: str, mode: str = "overwrite") -> str:
    if mode not in ("overwrite", "append"):
        return f"[write_file] invalid mode '{mode}'; use 'overwrite' or 'append'"
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"[write_file] could not write {path}: {e}"
    return f"[write_file] wrote {len(content)} chars to {p} (mode={mode})"


# Exported list — the registry in tools/__init__.py assembles from these.
BUILTIN_TOOLS: list[Tool] = [bash, read_file, write_file]

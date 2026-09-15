"""Tools package for mini-deerflow.

P1 introduces tool calling. This package mirrors the intent of DeerFlow's
`tools/` module and its `get_available_tools()` entry point in the simplest
possible form: a flat registry of built-in tools that the lead agent can hand
to the LLM.

P5 (沙箱化执行) adds an optional `sandbox=` argument: when a `Sandbox` is
supplied, `get_available_tools()` returns bash / read_file / write_file bound to
that sandbox (virtual paths, traversal-guarded) instead of the host builtins.
The tool *names and schemas are identical either way*, so the call site —
`get_available_tools()` — stays stable across the P1→P5 upgrade.

Later phases grow this further (P7 MCP tools, P8 skills), but the entry point
does not change.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from tools.base import Tool
from tools.builtins import BUILTIN_TOOLS

if TYPE_CHECKING:
    from sandbox.base import Sandbox


def get_available_tools(
    names: list[str] | None = None,
    sandbox: "Sandbox | None" = None,
) -> list[Tool]:
    """Return the tools available to an agent.

    Args:
        names: Optional allow-list of tool names to include. When None (the
            default) all tools are returned. Unknown names are ignored.
        sandbox: Optional :class:`~sandbox.Sandbox`. When supplied (P5), the
            bash / read_file / write_file tools execute *inside* the sandbox and
            speak virtual paths, instead of running on the host directly. When
            None, the classic host builtins are used (P1-P4 behaviour).
    """
    if sandbox is None:
        pool = list(BUILTIN_TOOLS)
    else:
        # Import lazily so the tools package has no hard dependency on the
        # sandbox package when sandboxing is not used.
        from tools.sandbox_tools import make_sandbox_tools

        pool = make_sandbox_tools(sandbox)

    if names is None:
        return pool
    wanted = set(names)
    return [t for t in pool if t.name in wanted]


def tools_by_name(tools: list[Tool] | None = None) -> dict[str, Tool]:
    """Index a list of tools by name for fast dispatch during the ReAct loop."""
    tools = get_available_tools() if tools is None else tools
    return {t.name: t for t in tools}


__all__ = ["Tool", "get_available_tools", "tools_by_name", "BUILTIN_TOOLS"]

"""Tools package for mini-deerflow.

P1 introduces tool calling. This package mirrors the intent of DeerFlow's
`tools/` module and its `get_available_tools()` entry point in the simplest
possible form: a flat registry of built-in tools that the lead agent can hand
to the LLM.

Later phases grow this (P5 sandbox execution, P7 MCP tools, P8 skills), but the
call site — `get_available_tools()` — stays stable.
"""
from __future__ import annotations

from tools.base import Tool
from tools.builtins import BUILTIN_TOOLS


def get_available_tools(names: list[str] | None = None) -> list[Tool]:
    """Return the tools available to an agent.

    Args:
        names: Optional allow-list of tool names to include. When None (the
            default) all built-in tools are returned. Unknown names are ignored.
    """
    if names is None:
        return list(BUILTIN_TOOLS)
    wanted = set(names)
    return [t for t in BUILTIN_TOOLS if t.name in wanted]


def tools_by_name(tools: list[Tool] | None = None) -> dict[str, Tool]:
    """Index a list of tools by name for fast dispatch during the ReAct loop."""
    tools = get_available_tools() if tools is None else tools
    return {t.name: t for t in tools}


__all__ = ["Tool", "get_available_tools", "tools_by_name", "BUILTIN_TOOLS"]

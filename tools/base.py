"""Tool abstraction for mini-deerflow (P1: 工具调用).

Corresponds to the earliest form of DeerFlow's `tools/` layer: a light wrapper
that turns a plain Python callable into something the LLM can (a) discover via a
JSON schema and (b) invoke by name with JSON arguments.

Design goals for this phase:
- No LangGraph / langchain dependency — we speak the OpenAI-style
  function-calling protocol that the Volcengine Ark SDK exposes directly.
- One `Tool` object = one callable + its schema + safe invocation.
- A tiny registry (`get_available_tools`) mirrors DeerFlow's
  `get_available_tools()` entry point so later phases can grow it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    """A single callable tool exposed to the LLM.

    Attributes:
        name:        Unique tool name the model uses to call it.
        description: What the tool does / when to use it (the model reads this).
        parameters:  JSON Schema (draft-07 style object) describing the args.
        func:        The Python callable implementing the tool. It receives
                     keyword arguments matching `parameters` and returns a value
                     that is coerced to a string for the model.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any] = field(repr=False)

    def to_openai_schema(self) -> dict[str, Any]:
        """Render this tool as an OpenAI/Ark `tools` array entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, arguments: str | dict[str, Any] | None) -> str:
        """Execute the tool with model-provided arguments.

        `arguments` is whatever the model returned in `tool_call.function.arguments`
        — usually a JSON string. We parse, invoke, and always return a string so
        the result can be fed back as a `tool` message. Errors are captured and
        returned as text so the ReAct loop can recover instead of crashing.
        """
        try:
            if arguments is None or arguments == "":
                kwargs: dict[str, Any] = {}
            elif isinstance(arguments, dict):
                kwargs = arguments
            else:
                kwargs = json.loads(arguments)
        except (json.JSONDecodeError, TypeError) as e:
            return f"[tool-error] could not parse arguments for '{self.name}': {e}"

        if not isinstance(kwargs, dict):
            return f"[tool-error] arguments for '{self.name}' must be a JSON object"

        try:
            result = self.func(**kwargs)
        except TypeError as e:
            # Wrong / missing kwargs — surface it so the model can retry.
            return f"[tool-error] bad arguments for '{self.name}': {e}"
        except Exception as e:  # tool bodies also guard themselves, this is a backstop
            return f"[tool-error] '{self.name}' failed: {type(e).__name__}: {e}"

        return _stringify(result)


def _stringify(result: Any) -> str:
    """Coerce any tool return value into a string for the model."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def tool(name: str, description: str, parameters: dict[str, Any]) -> Callable[[Callable[..., Any]], Tool]:
    """Decorator sugar: turn a function into a `Tool`.

    Usage:
        @tool("read_file", "Read a text file", {...})
        def read_file(path: str) -> str: ...
    """

    def _wrap(fn: Callable[..., Any]) -> Tool:
        return Tool(name=name, description=description, parameters=parameters, func=fn)

    return _wrap

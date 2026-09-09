"""mini-deerflow CLI entry (P1: 工具调用 Agent).

The lead agent can now use tools (bash / read_file / write_file) and runs a
multi-turn ReAct loop under the hood. This CLI wires up an event hook so you can
watch the agent's tool activity as it works.

Usage:
    # single-shot
    python main.py "在当前目录建一个 hello.txt,写入 'hi',再读回来"

    # interactive REPL (each line is an independent task)
    python main.py

    # hide the tool-activity trace
    python main.py --quiet "列出当前目录的文件"
"""
from __future__ import annotations

import sys

from agents.lead_agent import LeadAgent


def _make_tracer(enabled: bool):
    """Build an on_event hook that prints tool activity to stderr."""

    def _trace(kind: str, payload: dict) -> None:
        if not enabled:
            return
        if kind == "tool_start":
            args = payload.get("arguments") or ""
            print(f"  · calling {payload['name']}({args})", file=sys.stderr)
        elif kind == "tool_end":
            result = str(payload.get("result", ""))
            preview = result if len(result) <= 200 else result[:200] + " …"
            print(f"    -> {preview}", file=sys.stderr)
        elif kind == "max_steps_reached":
            print(f"  · reached max tool steps ({payload['max_steps']})", file=sys.stderr)

    return _trace


def _answer(agent: LeadAgent, question: str) -> None:
    try:
        reply = agent.run(question)
    except Exception as e:  # keep the CLI from crashing on API/config errors
        print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
        return
    print(reply)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    verbose = True
    if argv and argv[0] in ("--quiet", "-q"):
        verbose = False
        argv = argv[1:]

    agent = LeadAgent(on_event=_make_tracer(verbose))

    # Single-shot mode: everything after flags is the task.
    if argv:
        _answer(agent, " ".join(argv))
        return 0

    # Interactive mode.
    print("mini-deerflow P1 · tool-calling agent. Type 'exit' or Ctrl-D to quit.")
    while True:
        try:
            question = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        _answer(agent, question)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""mini-deerflow CLI entry (P0: 最小可运行 Agent).

Demo target from the roadmap: a command-line single-turn conversation —
input a question, the LLM answers, the result is printed.

Usage:
    # single-shot
    python main.py "介绍一下你自己"

    # interactive REPL (each line is an independent single turn)
    python main.py
"""
from __future__ import annotations

import sys

from agents.lead_agent import LeadAgent


def _answer(agent: LeadAgent, question: str) -> None:
    try:
        reply = agent.run(question)
    except Exception as e:  # keep the CLI from crashing on API/config errors
        print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)
        return
    print(reply)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    agent = LeadAgent()

    # Single-shot mode: everything after the program name is the question.
    if argv:
        _answer(agent, " ".join(argv))
        return 0

    # Interactive mode.
    print("mini-deerflow P0 · single-turn chat. Type 'exit' or Ctrl-D to quit.")
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
        print("bot> ", end="", flush=True)
        _answer(agent, question)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

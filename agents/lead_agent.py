"""Lead agent — simplest form (P0).

Corresponds to DeerFlow's `agents/lead_agent` at its most minimal:
a single-turn "ask a question -> LLM answers" flow. No tools, no memory, no
loop yet. Those arrive in later roadmap phases (P1+).
"""
from __future__ import annotations

import llm

SYSTEM_PROMPT = "You are a helpful assistant."


class LeadAgent:
    """Minimal single-turn agent.

    Holds only a system prompt and delegates to the LLM client. Kept as a class
    so later phases can attach tools / state without changing the call site.
    """

    def __init__(self, system_prompt: str = SYSTEM_PROMPT) -> None:
        self.system_prompt = system_prompt

    def run(self, question: str) -> str:
        """Take one user question, return the model's answer (single turn)."""
        if not question or not question.strip():
            raise ValueError("question must be a non-empty string")
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        return llm.chat_completion(messages)

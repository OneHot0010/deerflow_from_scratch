"""Configuration for mini-deerflow.

P0 milestone: keep it minimal. All runtime config comes from environment
variables (optionally loaded from a local `.env` file), so no secret is ever
hard-coded in source. Mirrors the intent of DeerFlow's `config/` layer in the
simplest possible form.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (no external dependency).

    Reads KEY=VALUE lines from ./.env into os.environ without overriding
    variables that are already set in the real environment.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# --- Volcengine Ark (Doubao) settings ---------------------------------------
# The reference files call the Ark SDK directly with an API key. We keep the
# same SDK but source the key from the environment instead of hard-coding it.
ARK_API_KEY: str = os.getenv("ARK_API_KEY", "")

# Chat model (see call_llm.py). Overridable via env.
CHAT_MODEL: str = os.getenv("CHAT_MODEL", "doubao-seed-1-6-250615")

# Multimodal embedding model (see embedding_model.py). Overridable via env.
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "doubao-embedding-vision-251215")


def require_api_key() -> str:
    """Return the Ark API key or raise a clear, actionable error."""
    if not ARK_API_KEY or ARK_API_KEY.startswith("your-"):
        raise RuntimeError(
            "Missing ARK_API_KEY. Copy .env.example to .env and fill in your "
            "Volcengine Ark API key, or export ARK_API_KEY in your shell."
        )
    return ARK_API_KEY

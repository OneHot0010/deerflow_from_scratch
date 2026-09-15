"""Sandbox package for mini-deerflow — P5 (沙箱化执行).

Corresponds to DeerFlow's ``sandbox/`` module: the layer that runs the agent's
commands and file operations inside an isolated environment addressed by
**virtual paths**, instead of directly on the host with raw host paths.

Public surface (kept stable so later phases can grow it — P13's Docker
``AioSandbox`` slots in behind the same interface):

- ``Sandbox`` / ``SandboxProvider`` — the abstract contract (``base.py``).
- ``PathMapping`` — one ``virtual_path -> host_path`` binding.
- ``SandboxError`` / ``SandboxPathError`` — typed failures.
- ``LocalSandbox`` / ``LocalSandboxProvider`` — the P5 development backend.
- ``get_sandbox_provider()`` — the process-wide provider singleton, mirroring
  ``tools.get_available_tools()`` and ``store.get_store()`` in spirit: a single
  entry point the tool layer and the web/CLI call sites share.

The singleton is created lazily from config on first use (``config.SANDBOX_DIR``
for where real files live). ``set_sandbox_provider()`` lets tests inject a
provider rooted at a ``tmp_path`` so the suite stays offline and side-effect
free, exactly like ``store.set_store()``.
"""
from __future__ import annotations

import threading

import config
from sandbox.base import (
    PathMapping,
    Sandbox,
    SandboxError,
    SandboxPathError,
    SandboxProvider,
)
from sandbox.local import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_VIRTUAL_ROOT,
    LocalSandbox,
    LocalSandboxProvider,
)

_provider: SandboxProvider | None = None
_lock = threading.Lock()


def get_sandbox_provider() -> SandboxProvider:
    """Return the process-wide sandbox provider, creating it on first use.

    Lazily builds a :class:`LocalSandboxProvider` rooted at ``config.SANDBOX_DIR``
    (the roadmap's "LocalSandboxProvider(开发)"). The instance is cached so every
    caller shares one provider — and therefore one set of per-thread sandboxes.
    """
    global _provider
    with _lock:
        if _provider is None:
            _provider = LocalSandboxProvider(base_dir=config.SANDBOX_DIR)
        return _provider


def set_sandbox_provider(provider: SandboxProvider | None) -> None:
    """Install a custom provider (or clear it with None).

    Used by tests to inject a provider rooted at a temporary directory so no
    real files are created outside the test's ``tmp_path``. Passing None forces
    the next :func:`get_sandbox_provider` call to rebuild from config.
    """
    global _provider
    with _lock:
        _provider = provider


__all__ = [
    "Sandbox",
    "SandboxProvider",
    "PathMapping",
    "SandboxError",
    "SandboxPathError",
    "LocalSandbox",
    "LocalSandboxProvider",
    "DEFAULT_VIRTUAL_ROOT",
    "DEFAULT_COMMAND_TIMEOUT",
    "get_sandbox_provider",
    "set_sandbox_provider",
]

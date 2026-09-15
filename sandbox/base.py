"""Sandbox abstractions — P5 (沙箱化执行).

Mirrors the two-layer contract at the heart of DeerFlow's ``sandbox/`` module
(``deerflow/sandbox/sandbox.py`` + ``sandbox_provider.py``), reduced to the
smallest useful form in keeping with the earlier phases:

- ``Sandbox``          — an *environment* in which commands run and files are
  read/written. Everything the environment exposes speaks **virtual paths**
  (e.g. ``/workspace/notes.txt``); the implementation maps those onto real host
  locations and refuses anything that escapes them.
- ``SandboxProvider``  — a *factory + lifecycle manager* for sandboxes:
  ``acquire()`` hands back a sandbox id, ``get()`` retrieves the live object,
  ``release()`` tears it down. This is the ``SandboxProvider`` abstract
  interface the roadmap's P5 acceptance calls for.
- ``PathMapping``      — one ``virtual_path -> host_path`` binding (optionally
  read-only), the unit the local provider uses to build a sandbox's view.
- ``SandboxError`` / ``SandboxPathError`` — typed failures so callers (and the
  tool layer) can tell "you asked for something outside the sandbox" apart from
  ordinary I/O errors.

Design constraints carried over from P0-P4:

- **Zero new dependencies.** Pure stdlib (``os`` / ``pathlib`` / ``subprocess``).
  Docker is the *aspirational* backend named in the roadmap's "关键技术" column;
  the abstraction is shaped so a ``DockerSandboxProvider`` can slot in later
  without touching call sites, but the only concrete provider we ship in P5 is
  the local one (roadmap: "LocalSandboxProvider(开发)").
- **Virtual paths are the contract.** Callers never see host paths. The
  provider decides where ``/workspace`` physically lives; the sandbox translates
  every path argument through its mappings and validates containment, so a
  ``../../etc/passwd`` can never resolve outside a mapped root (roadmap:
  "虚拟路径正确映射" + "防目录穿越").

The abstract methods intentionally track the subset of DeerFlow's ``Sandbox``
API the mini-deerflow tools actually need — ``execute_command`` / ``read_file``
/ ``write_file`` / ``list_dir`` — leaving richer operations (glob / grep /
binary download) to later phases.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class SandboxError(Exception):
    """Base class for sandbox failures the tool layer can render as text."""


class SandboxPathError(SandboxError):
    """A virtual path was malformed or escaped every mapped root.

    Raised by :meth:`Sandbox.read_file` / ``write_file`` / ``list_dir`` /
    ``execute_command`` (indirectly) when a path cannot be resolved to a
    location *inside* one of the sandbox's mappings — the core "防目录穿越"
    guarantee. Distinct from ``OSError`` so a caller can tell a policy violation
    apart from a plain missing-file error.
    """


@dataclass(frozen=True)
class PathMapping:
    """One ``virtual_path -> host_path`` binding for a sandbox.

    Attributes:
        virtual_path: The path the *agent* sees, always POSIX-style and
            absolute (e.g. ``/workspace``). This is the prefix the sandbox
            recognises in tool arguments.
        host_path:    The real directory on the host this virtual root maps to.
            All reads/writes/commands under ``virtual_path`` are confined here.
        read_only:    When True, ``write_file`` under this mapping is rejected
            (used later for mounting read-only skill/library trees).

    The mapping is frozen so it can be cached and safely shared across threads.
    """

    virtual_path: str
    host_path: str
    read_only: bool = False


class Sandbox(ABC):
    """An isolated execution environment addressed by virtual paths.

    A sandbox owns a set of :class:`PathMapping` and exposes a small filesystem
    + shell surface. Every path argument to its methods is a *virtual* path; the
    implementation resolves it through the mappings and guarantees the result
    stays inside a mapped root (raising :class:`SandboxPathError` otherwise).

    ``id`` uniquely names this environment within its provider so it can be
    retrieved via :meth:`SandboxProvider.get` and torn down via ``release``.
    """

    def __init__(self, id: str) -> None:
        self._id = id

    @property
    def id(self) -> str:
        """The provider-assigned identifier for this sandbox."""
        return self._id

    @abstractmethod
    def execute_command(self, command: str, timeout: float | None = None) -> str:
        """Run a shell command *inside* the sandbox and return its output.

        The command executes with the sandbox's primary virtual root as its
        working directory, so relative paths land in the sandboxed area. The
        returned string bundles exit code / stdout / stderr in a stable,
        model-readable shape (the tool layer feeds it straight back to the LLM).

        Args:
            command: The shell command to run.
            timeout: Max wall-clock seconds before the command is killed. When
                None the implementation applies its own default.
        """

    @abstractmethod
    def read_file(self, path: str, max_lines: int | None = None) -> str:
        """Read a UTF-8 text file at a *virtual* path inside the sandbox.

        Args:
            path: Virtual path of the file (e.g. ``/workspace/a.txt``).
            max_lines: If set, return at most this many lines from the top.

        Raises:
            SandboxPathError: If ``path`` escapes every mapped root.
            OSError: If the file is missing / unreadable.
        """

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """Write UTF-8 text to a *virtual* path inside the sandbox.

        Parent directories are created as needed. Writes under a read-only
        mapping are rejected.

        Args:
            path: Virtual destination path.
            content: Text to write.
            append: Append instead of overwrite when True.

        Raises:
            SandboxPathError: If ``path`` escapes every mapped root.
            OSError: On a read-only mapping (``errno.EROFS``) or I/O failure.
        """

    @abstractmethod
    def list_dir(self, path: str) -> list[str]:
        """List entries under a *virtual* directory path.

        Directory entries are suffixed with ``/`` and results are returned as
        virtual paths (never leaking host locations).

        Args:
            path: Virtual directory path (e.g. ``/workspace``).

        Raises:
            SandboxPathError: If ``path`` escapes every mapped root.
            OSError: If the directory is missing / unreadable.
        """


class SandboxProvider(ABC):
    """Factory + lifecycle manager for :class:`Sandbox` instances.

    The provider is the seam the roadmap's P5 abstraction turns on: application
    code always talks to this interface, so swapping ``LocalSandboxProvider``
    for a future ``DockerSandboxProvider`` (the roadmap's Docker SDK backend)
    never touches a call site.

    Lifecycle:
        ``acquire(thread_id)`` -> sandbox id (creates/reuses an environment)
        ``get(sandbox_id)``    -> the live ``Sandbox`` (or None if unknown)
        ``release(sandbox_id)``-> tear the environment down

    ``thread_id`` lets a provider scope a sandbox to a P3 conversation thread
    (so two threads get isolated working areas); passing None yields a generic,
    process-wide sandbox for callers with no thread context (the CLI, tests).
    """

    @abstractmethod
    def acquire(self, thread_id: str | None = None) -> str:
        """Create or reuse a sandbox environment and return its id."""

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """Return the live sandbox for ``sandbox_id`` (or None if unknown)."""

    @abstractmethod
    def release(self, sandbox_id: str) -> None:
        """Destroy the sandbox environment named by ``sandbox_id``."""

    def reset(self) -> None:
        """Drop any cached state (optional; default no-op).

        Overridden by providers that keep sandboxes alive across ``acquire``
        calls so tests / config changes can start from a clean slate.
        """


__all__ = [
    "Sandbox",
    "SandboxProvider",
    "PathMapping",
    "SandboxError",
    "SandboxPathError",
]

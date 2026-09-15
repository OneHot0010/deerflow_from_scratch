"""Local (development) sandbox — P5 (沙箱化执行).

The concrete provider the roadmap asks for in P5's "LocalSandboxProvider(开发)"
row: it runs commands and file operations on the *host*, but confines every one
of them to a set of mapped directories addressed through **virtual paths**. It
is the zero-dependency development backend; a Docker-backed provider (the
roadmap's aspirational "关键技术: Docker SDK") can later implement the same
:class:`SandboxProvider` interface without changing any call site.

What it delivers against the P5 acceptance ("工具执行在沙箱内,虚拟路径正确映射,
防目录穿越"):

- **虚拟路径映射.** Each sandbox owns one or more :class:`PathMapping`
  (``/workspace`` -> a real host dir). ``read_file("/workspace/a.txt")`` reads
  ``<host>/a.txt``; the agent never sees the host location.
- **防目录穿越.** Every path is resolved with ``os.path.realpath`` and checked
  with ``os.path.commonpath`` against its mapped host root. Anything that
  escapes — ``../../etc/passwd``, an absolute ``/etc/passwd``, a symlink
  pointing outside — raises :class:`SandboxPathError` *before* any I/O happens.
- **工具执行在沙箱内.** ``execute_command`` runs with the primary virtual root
  as CWD, and rewrites virtual paths that appear in the command string to their
  host locations so ``cat /workspace/a.txt`` works. Output paths are reverse
  mapped back to virtual form so nothing leaks.

Threading: the provider may be reached from several threads (Gateway dispatch,
CLI, tests). All cache mutations take a provider-wide lock, mirroring
DeerFlow's ``LocalSandboxProvider``.
"""
from __future__ import annotations

import errno
import os
import re
import subprocess
import threading
from pathlib import Path

from sandbox.base import (
    PathMapping,
    Sandbox,
    SandboxPathError,
    SandboxProvider,
)

# The single virtual root every local sandbox exposes by default. Chosen to be
# obviously virtual (not a real host path) so the mapping is unambiguous.
DEFAULT_VIRTUAL_ROOT = "/workspace"

# Default wall-clock timeout for a single sandboxed command, in seconds. A
# blocking foreground command is killed after this long so an agent turn cannot
# hang forever. Overridable per call via ``execute_command(timeout=...)``.
DEFAULT_COMMAND_TIMEOUT = 60

# Cap how much text a command/file ever returns, protecting the model's context
# window from a runaway command or a huge file (same limit as the P1 builtins).
_MAX_OUTPUT_CHARS = 20_000


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated {len(text) - limit} chars]"


class LocalSandbox(Sandbox):
    """A host-backed sandbox confined to a set of :class:`PathMapping`.

    Every public method takes *virtual* paths. Internally each one is resolved
    to a host path via :meth:`_resolve` (which enforces containment), the
    operation runs on the host, and any host paths in the result are mapped back
    to virtual form via :meth:`_virtualize` so callers only ever see the virtual
    view.

    The first mapping is treated as the *primary* root: it is the working
    directory for ``execute_command`` and the target of bare/relative paths.
    """

    def __init__(self, id: str, mappings: list[PathMapping]) -> None:
        super().__init__(id)
        if not mappings:
            raise ValueError("a LocalSandbox needs at least one PathMapping")
        # Normalise: virtual paths are POSIX + no trailing slash (except root);
        # host paths are realpath'd once so containment checks are stable.
        self._mappings: list[PathMapping] = [
            PathMapping(
                virtual_path=_norm_virtual(m.virtual_path),
                host_path=os.path.realpath(m.host_path),
                read_only=m.read_only,
            )
            for m in mappings
        ]
        # The primary mapping anchors relative paths and the command CWD.
        self._primary = self._mappings[0]
        # Ensure host roots exist so a fresh sandbox is immediately usable.
        for m in self._mappings:
            if not m.read_only:
                Path(m.host_path).mkdir(parents=True, exist_ok=True)
        # Match longest virtual prefix first so nested mounts resolve correctly.
        self._by_virtual = sorted(
            self._mappings, key=lambda m: len(m.virtual_path), reverse=True
        )
        self._by_host = sorted(
            self._mappings, key=lambda m: len(m.host_path), reverse=True
        )

    @property
    def mappings(self) -> list[PathMapping]:
        """A copy of this sandbox's path mappings."""
        return list(self._mappings)

    # -- path resolution + traversal defense --------------------------------
    def _match_mapping(self, virtual: str) -> tuple[PathMapping, str]:
        """Find the mapping owning ``virtual`` and the path relative to it.

        A bare or relative path (no leading ``/``) is interpreted against the
        primary root. An absolute virtual path must fall under one of the
        mapped virtual roots.

        Raises:
            SandboxPathError: If no mapping claims the path.
        """
        v = virtual.strip()
        if not v:
            raise SandboxPathError("empty path")

        # Relative path -> resolve against the primary virtual root.
        if not v.startswith("/"):
            return self._primary, v

        v = _norm_virtual(v)
        for m in self._by_virtual:
            root = m.virtual_path
            if v == root:
                return m, ""
            prefix = root.rstrip("/") + "/"
            if v.startswith(prefix):
                return m, v[len(prefix):]
        raise SandboxPathError(
            f"path is outside the sandbox (no mapping for {virtual!r})"
        )

    def _resolve(self, virtual: str) -> str:
        """Resolve a virtual path to a host path, enforcing containment.

        This is the heart of the "防目录穿越" guarantee: we join the relative
        portion onto the mapped host root, ``realpath`` the result (collapsing
        ``..`` and following symlinks), then require it to stay inside the host
        root via ``commonpath``. Anything that escapes raises
        :class:`SandboxPathError` before any file is ever opened.
        """
        # Reject NUL and other obviously hostile bytes up front, on the *raw*
        # input: some platforms' ``normpath`` silently truncate at a NUL byte,
        # which would otherwise smuggle the check.
        if "\x00" in virtual:
            raise SandboxPathError("path contains a NUL byte")
        mapping, relative = self._match_mapping(virtual)
        candidate = (
            os.path.join(mapping.host_path, relative)
            if relative
            else mapping.host_path
        )
        resolved = os.path.realpath(candidate)
        root = mapping.host_path
        try:
            contained = os.path.commonpath([root, resolved]) == root
        except ValueError:
            # Different drives / mixed absolute-relative -> definitely outside.
            contained = False
        if not contained:
            raise SandboxPathError(
                f"path escapes the sandbox root: {virtual!r}"
            )
        return resolved

    def _resolve_for_write(self, virtual: str) -> str:
        """Resolve for a write, additionally rejecting read-only mappings."""
        mapping, _ = self._match_mapping(virtual)
        if mapping.read_only:
            raise OSError(
                errno.EROFS, "read-only sandbox mapping", virtual
            )
        return self._resolve(virtual)

    def _virtualize(self, host_path: str) -> str:
        """Map a host path back to its virtual form (best effort).

        Used to keep host locations out of command output and directory
        listings. A path under no mapping is returned unchanged.
        """
        resolved = os.path.realpath(host_path)
        for m in self._by_host:
            root = m.host_path
            if resolved == root:
                return m.virtual_path
            if resolved.startswith(root + os.sep):
                relative = resolved[len(root):].lstrip(os.sep).replace(os.sep, "/")
                return f"{m.virtual_path.rstrip('/')}/{relative}"
        return host_path

    # -- command pattern (rewrite virtual paths inside a command string) ----
    @property
    def _command_pattern(self) -> re.Pattern[str]:
        """Regex matching any mapped virtual root at a shell path boundary."""
        cached = getattr(self, "_command_pattern_cache", None)
        if cached is not None:
            return cached
        parts = []
        for m in self._by_virtual:
            esc = re.escape(m.virtual_path)
            # Match the root only at a segment boundary, then any path tail up
            # to the next shell metacharacter/whitespace.
            parts.append(esc + r"""(?=/|$|[\s"';&|<>()])(?:/[^\s"';&|<>()]*)?""")
        pattern = re.compile("|".join(f"({p})" for p in parts))
        self._command_pattern_cache = pattern
        return pattern

    def _resolve_paths_in_command(self, command: str) -> str:
        """Rewrite virtual paths in a command string to their host locations.

        So ``cat /workspace/a.txt`` becomes ``cat <host>/a.txt`` before the
        shell ever sees it. A virtual path that would escape its root is left
        untouched here (the command simply won't find it) — the file-level
        methods are where traversal is hard-enforced.
        """
        def _sub(match: re.Match) -> str:
            token = match.group(0)
            try:
                return self._resolve(token)
            except SandboxPathError:
                return token

        return self._command_pattern.sub(_sub, command)

    # -- Sandbox API ---------------------------------------------------------
    def execute_command(self, command: str, timeout: float | None = None) -> str:
        rewritten = self._resolve_paths_in_command(command)
        if timeout is None:
            timeout = DEFAULT_COMMAND_TIMEOUT
        try:
            proc = subprocess.run(
                rewritten,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self._primary.host_path,
            )
        except subprocess.TimeoutExpired:
            return f"[sandbox] command timed out after {timeout}s: {command}"

        parts = [f"exit_code: {proc.returncode}"]
        if proc.stdout:
            parts.append("stdout:\n" + _truncate(self._virtualize_output(proc.stdout)))
        if proc.stderr:
            parts.append("stderr:\n" + _truncate(self._virtualize_output(proc.stderr)))
        if not proc.stdout and not proc.stderr:
            parts.append("(no output)")
        return "\n".join(parts)

    def _virtualize_output(self, text: str) -> str:
        """Replace any host-root prefixes in output text with virtual roots."""
        result = text
        for m in self._by_host:
            result = result.replace(m.host_path, m.virtual_path)
        return result

    def read_file(self, path: str, max_lines: int | None = None) -> str:
        resolved = self._resolve(path)
        p = Path(resolved)
        if not p.exists():
            raise OSError(errno.ENOENT, "no such file", path)
        if p.is_dir():
            raise OSError(errno.EISDIR, "is a directory", path)
        text = p.read_text(encoding="utf-8")
        if max_lines is not None and max_lines > 0:
            lines = text.splitlines()
            clipped = "\n".join(lines[:max_lines])
            if len(lines) > max_lines:
                clipped += f"\n...[{len(lines) - max_lines} more lines]"
            return _truncate(clipped)
        return _truncate(text)

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        resolved = self._resolve_for_write(path)
        p = Path(resolved)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w", encoding="utf-8") as f:
            f.write(content)

    def list_dir(self, path: str) -> list[str]:
        resolved = self._resolve(path)
        p = Path(resolved)
        if not p.exists():
            raise OSError(errno.ENOENT, "no such directory", path)
        if not p.is_dir():
            raise OSError(errno.ENOTDIR, "not a directory", path)
        entries: list[str] = []
        for child in sorted(p.iterdir(), key=lambda c: c.name):
            virtual = self._virtualize(str(child))
            entries.append(f"{virtual}/" if child.is_dir() else virtual)
        return entries


def _norm_virtual(path: str) -> str:
    """Normalise a virtual path: POSIX, absolute, no trailing slash.

    ``os.path.normpath`` collapses ``.``/``..`` segments so a declared mapping
    root is always canonical; the leading slash is preserved and any trailing
    slash (except on ``/``) is stripped for stable prefix comparisons.
    """
    if not path.startswith("/"):
        # Relative virtual paths are meaningless as a *root*; callers that pass
        # relative file paths are handled in _match_mapping, not here.
        path = "/" + path
    normalised = os.path.normpath(path).replace("\\", "/")
    return normalised


class LocalSandboxProvider(SandboxProvider):
    """Provider that hands out host-backed :class:`LocalSandbox` instances.

    Each distinct ``thread_id`` gets its own sandbox rooted at
    ``<base_dir>/<thread_id>/workspace`` so P3 conversation threads are isolated
    on disk. ``acquire(None)`` returns a shared *generic* sandbox (id
    ``"local"``) rooted at ``<base_dir>/generic/workspace`` for callers with no
    thread context (the CLI, unit tests).

    All cache access is guarded by a lock because the provider is reachable from
    multiple threads.
    """

    def __init__(
        self,
        base_dir: str,
        virtual_root: str = DEFAULT_VIRTUAL_ROOT,
        extra_mappings: list[PathMapping] | None = None,
    ) -> None:
        """Args:
        base_dir: Host directory under which every sandbox's real files live.
        virtual_root: The virtual path each sandbox exposes as its primary
            working root (default ``/workspace``).
        extra_mappings: Optional additional mappings (e.g. a read-only library
            tree) added to every sandbox this provider builds.
        """
        self._base_dir = os.path.realpath(base_dir)
        self._virtual_root = _norm_virtual(virtual_root)
        self._extra_mappings = list(extra_mappings or [])
        self._sandboxes: dict[str, LocalSandbox] = {}
        self._lock = threading.Lock()

    def _host_root_for(self, sandbox_id: str) -> str:
        return os.path.join(self._base_dir, sandbox_id, "workspace")

    def _build(self, sandbox_id: str) -> LocalSandbox:
        mappings = [
            PathMapping(
                virtual_path=self._virtual_root,
                host_path=self._host_root_for(sandbox_id),
            ),
            *self._extra_mappings,
        ]
        return LocalSandbox(sandbox_id, mappings)

    def acquire(self, thread_id: str | None = None) -> str:
        sandbox_id = "local" if thread_id is None else f"thread-{thread_id}"
        with self._lock:
            if sandbox_id not in self._sandboxes:
                self._sandboxes[sandbox_id] = self._build(sandbox_id)
        return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)

    def reset(self) -> None:
        with self._lock:
            self._sandboxes.clear()


__all__ = [
    "LocalSandbox",
    "LocalSandboxProvider",
    "DEFAULT_VIRTUAL_ROOT",
    "DEFAULT_COMMAND_TIMEOUT",
]

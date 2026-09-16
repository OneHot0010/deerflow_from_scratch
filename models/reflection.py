"""``resolve_class("pkg.module:Class")`` — the tiny reflection helper (P6).

DeerFlow's ``reflection/`` layer lets ``config.yaml`` declare model classes by
string so the factory does not carry a hard-coded provider table. This is the
minimal counterpart: a single function that imports the module and returns the
named attribute, with clear errors and a small cache so a config with dozens of
model entries does not re-import the same module dozens of times.

The path format is ``pkg.mod:Class`` (colon-delimited, mirroring
``setuptools.entry_point``). We also tolerate ``pkg.mod.Class`` (dotted) so
users who forget the colon get a graceful resolution instead of an obscure
``ImportError``.
"""
from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any


class ClassResolutionError(RuntimeError):
    """Raised when ``resolve_class`` cannot find the target attribute.

    The message points at both the config key that produced the string and the
    resolution step that failed, so a misspelled provider is easy to fix.
    """


@lru_cache(maxsize=64)
def resolve_class(path: str) -> Any:
    """Import and return the class named by ``path``.

    Supported formats:

    - ``pkg.module:Attr``  — canonical, colon-delimited.
    - ``pkg.module.Attr``  — dotted; we split on the last dot as a fallback.

    Raises :class:`ClassResolutionError` with an actionable message on any
    failure (missing module, missing attribute, malformed path).
    """
    if not path or not isinstance(path, str):
        raise ClassResolutionError(
            f"resolve_class: path must be a non-empty string, got {path!r}"
        )

    module_name, sep, attr = path.partition(":")
    if not sep:
        # Fallback: split on the last dot ("pkg.mod.Cls" -> "pkg.mod", "Cls").
        if "." not in path:
            raise ClassResolutionError(
                f"resolve_class: {path!r} is not a valid class path "
                "(expected 'pkg.mod:Class' or 'pkg.mod.Class')"
            )
        module_name, _, attr = path.rpartition(".")

    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ClassResolutionError(
            f"resolve_class: cannot import module {module_name!r}: {e}"
        ) from e

    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise ClassResolutionError(
            f"resolve_class: module {module_name!r} has no attribute {attr!r}"
        ) from e


def clear_cache() -> None:
    """Reset the resolution cache (used by tests that reload modules)."""
    resolve_class.cache_clear()

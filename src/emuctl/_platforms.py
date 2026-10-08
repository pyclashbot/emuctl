"""§3 — Platform truth: Platform enum and current_platform() (the single mapping point)."""

from __future__ import annotations

import enum
import sys

__all__ = ["Platform", "current_platform"]


class Platform(enum.StrEnum):
    """Host OS truth. Pinned members; the sole platform vocabulary in the package.

    The §3 spec table consumes this (adapters never declare platforms and never
    branch on ``sys.platform`` themselves).
    """

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


def current_platform() -> Platform:
    """Map the host to a :class:`Platform` via ``sys.platform``.

    The single mapping point: ``darwin`` → MACOS, ``win32``/``cygwin`` →
    WINDOWS, everything else → LINUX. Pure and deterministic per host.
    """
    if sys.platform == "darwin":
        return Platform.MACOS
    if sys.platform in ("win32", "cygwin"):
        return Platform.WINDOWS
    return Platform.LINUX

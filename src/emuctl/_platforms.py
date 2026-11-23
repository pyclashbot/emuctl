from __future__ import annotations

import enum
import sys

__all__ = ["Platform", "current_platform"]


class Platform(enum.StrEnum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


def current_platform() -> Platform:
    if sys.platform == "darwin":
        return Platform.MACOS
    if sys.platform in ("win32", "cygwin"):
        return Platform.WINDOWS
    return Platform.LINUX

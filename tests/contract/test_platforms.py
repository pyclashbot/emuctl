import sys
from pathlib import Path

import emuctl._identification as identification
import emuctl._platforms as platforms
from emuctl._platforms import Platform, current_platform

SRC = Path(__file__).resolve().parents[2] / "src" / "emuctl"


def test_platform_members_pinned() -> None:
    assert set(Platform) == {Platform.WINDOWS, Platform.MACOS, Platform.LINUX}
    assert Platform.WINDOWS.value == "windows"
    assert Platform.MACOS.value == "macos"
    assert Platform.LINUX.value == "linux"
    assert isinstance(Platform.LINUX, str)


def test_current_platform_deterministic_and_correct() -> None:
    first = current_platform()
    assert isinstance(first, Platform)
    assert current_platform() == first
    if sys.platform == "darwin":
        assert first is Platform.MACOS
    elif sys.platform in ("win32", "cygwin"):
        assert first is Platform.WINDOWS
    else:
        assert first is Platform.LINUX


def test_platform_is_defined_once_and_imported_by_identification() -> None:
    definers = [
        path for path in sorted((SRC).rglob("*.py")) if "class Platform" in path.read_text(encoding="utf-8")
    ]
    assert definers == [SRC / "_platforms.py"]
    mappers = [
        path for path in sorted(SRC.rglob("*.py")) if "def current_platform" in path.read_text(encoding="utf-8")
    ]
    assert mappers == [SRC / "_platforms.py"]
    assert identification.Platform is Platform
    assert identification.current_platform is current_platform
    assert platforms.__all__ == ["Platform", "current_platform"]

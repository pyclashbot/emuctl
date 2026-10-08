"""§6 — BlueStacks family-private: per-platform install-dir / bundled-binary
path discovery.

Vendor evidence (py-clash-bot, ``pyclashbot/emulators/bluestacks.py``):

- Windows: the ``SOFTWARE\\BlueStacks_nxt`` HKLM key carries ``InstallDir``
  (the folder holding ``HD-Player.exe`` / ``HD-Adb.exe``) and ``DataDir``
  (e.g. ``…\\BlueStacks_nxt\\Engine``); ``bluestacks.conf`` sits BESIDE the
  DataDir's parent-ending directory (``dirname(DataDir)/bluestacks.conf``)
  and ``MimMetaData.json`` under ``DataDir/UserData`` (bluestacks.py:127-143,
  :146-159).
- macOS: ``/Applications/BlueStacks.app/Contents/MacOS`` holds the player
  binary ``BlueStacks`` and the bundled ``hd-adb``; the config store is
  ``/Users/Shared/Library/Application Support/BlueStacks`` with
  ``Engine/UserData/MimMetaData.json`` (bluestacks.py:68-73, :104-114,
  :127-134).

Interpretation decisions (CONTRACT wins; these fill its silences):

- ``BlueStacksConfig.install_dir`` / ``conf_path`` are the caller's explicit
  knobs (§4 rule 1 — explicit paths win) and are the ONLY tested seam here:
  when set, no registry/app-bundle probe runs. The remaining defaults are
  resolved per HOST platform (the §3 table is the sole gating truth, but the
  PATH SHAPE of a candidate is per-platform, resolved honestly as ``None``
  when the platform's vendor store is unavailable — e.g. ``winreg`` absent
  on non-Windows hosts is an honest no-candidate, never a raise).
- Existence checks are plain ``Path`` ``stat`` calls — bounded local I/O,
  never subprocess probes.
- This module never executes argv and never parses vendor output — pure
  filesystem/registry reads.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Final

from emuctl._platforms import Platform
from emuctl._platforms import current_platform as _core_platform
from emuctl.config.model import BlueStacksConfig

__all__ = [
    "conf_path",
    "install_root",
    "mim_meta_path",
    "registry_value",
    "tool_path",
]

#: Windows registry key + values (py-clash-bot bluestacks.py:146-159).
_WIN_REG_KEY: Final[str] = r"SOFTWARE\BlueStacks_nxt"

#: macOS app-bundle paths (bluestacks.py:107-114, :127-134).
_MACOS_TOOL_DIR: Final[Path] = Path("/Applications/BlueStacks.app/Contents/MacOS")
_MACOS_CONF_BASE: Final[Path] = Path("/Users/Shared/Library/Application Support/BlueStacks")

#: Per-platform executable/table names (bluestacks.py:68-73).
_PLAYER_NAME: Final[dict[str, str]] = {"windows": "HD-Player.exe", "macos": "BlueStacks"}
_HD_ADB_NAME: Final[dict[str, str]] = {"windows": "HD-Adb.exe", "macos": "hd-adb"}

_CONF_NAME: Final[str] = "bluestacks.conf"
_MIM_META_REL: Final[tuple[str, ...]] = ("UserData", "MimMetaData.json")
_MACOS_MIM_META_REL: Final[tuple[str, ...]] = ("Engine", "UserData", "MimMetaData.json")


def _platform() -> Platform:
    """Patched seam for tests (the §3 single mapping point, one indirection)."""
    return _core_platform()


def registry_value(name: str) -> str | None:
    """Read one ``SOFTWARE\\BlueStacks_nxt`` HKLM value, or None.

    ``winreg`` is Windows-only stdlib — importing it is platform-guarded and
    every registry failure maps to an honest no-candidate (mirrors
    py-clash-bot bluestacks.py:145-159, without its broad exception swallowing
    beyond the OSError surface winreg actually raises). Never a raise.
    """
    try:
        winreg: Any = importlib.import_module("winreg")  # noqa: PLC0415 — Windows-only stdlib
    except ImportError:
        return None
    try:
        root = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
    except OSError:
        return None
    try:
        with winreg.OpenKey(root, _WIN_REG_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if isinstance(value, str) and value.strip():
        return value
    return None


def _win_data_dir() -> str | None:
    value = registry_value("DataDir")
    return value.strip() if value else None


def install_root(cfg: BlueStacksConfig, *, platform: Platform | None = None) -> Path | None:
    """The directory holding the player binary + bundled HD-Adb.

    Explicit ``bluestacks.install_dir`` wins (the caller owns the shape —
    the directory that contains HD-Player.exe on Windows / the ``BlueStacks``
    binary on macOS, i.e. ``…/Contents/MacOS``). Otherwise: Windows probes
    the registry ``InstallDir`` (verified to hold ``HD-Player.exe``); macOS
    pins ``/Applications/BlueStacks.app/Contents/MacOS``. None = honestly no
    candidate on this host.
    """
    host = platform if platform is not None else _platform()
    if cfg.install_dir:
        return Path(cfg.install_dir)
    if host is Platform.WINDOWS:
        location = registry_value("InstallDir")
        if not location:
            return None
        root = Path(location.strip())
        if (root / _PLAYER_NAME["windows"]).is_file():
            return root
        return None
    if host is Platform.MACOS:
        return _MACOS_TOOL_DIR if _MACOS_TOOL_DIR.is_dir() else None
    return None


def tool_path(cfg: BlueStacksConfig, name: str, *, platform: Platform | None = None) -> Path | None:
    """Bundled tool path (player or HD-Adb) or None when it does not exist."""
    root = install_root(cfg, platform=platform)
    if root is None:
        return None
    candidate = root / name
    return candidate if candidate.is_file() else None


def conf_path(cfg: BlueStacksConfig, *, platform: Platform | None = None) -> Path | None:
    """The ``bluestacks.conf`` path for the host platform.

    Explicit ``bluestacks.conf_path`` wins. Defaults mirror the vendor store
    shapes above; existence is NOT required here (callers decide what a
    missing file means for their op — enumerate = no rows, configure =
    unreadable). No host platform match (e.g. LINUX) → None.
    """
    host = platform if platform is not None else _platform()
    if cfg.conf_path:
        return Path(cfg.conf_path)
    if host is Platform.WINDOWS:
        data_dir = _win_data_dir()
        if not data_dir:
            return None
        return Path(data_dir.rstrip("\\/")).parent / _CONF_NAME
    if host is Platform.MACOS:
        return _MACOS_CONF_BASE / _CONF_NAME
    return None


def mim_meta_path(cfg: BlueStacksConfig, *, platform: Platform | None = None) -> Path | None:
    """The Multi-Instance Manager metadata path (see module docstring).

    Derived from DataDir on Windows and the shared app-support base on
    macOS; existence is the caller's decision (a cold install has none —
    the family synthesizes metadata in memory, :mod:`._meta`).
    """
    host = platform if platform is not None else _platform()
    pinned = cfg.conf_path
    if pinned is not None:
        # An explicit conf_path defines the store layout by derivation: the
        # conf's parent is the BlueStacks store dir, and MIM metadata lives
        # under Engine/UserData within it on BOTH platforms (Windows
        # dirname(DataDir)=blueStacks_nxt holds Engine/UserData/MimMetaData
        # because DataDir itself IS the Engine dir; macOS equivalent).
        return Path(pinned).parent / "Engine" / "UserData" / "MimMetaData.json"
    if host is Platform.WINDOWS:
        data_dir = _win_data_dir()
        if not data_dir:
            return None
        return Path(data_dir.rstrip("\\/")).joinpath(*_MIM_META_REL)
    if host is Platform.MACOS:
        return _MACOS_CONF_BASE.joinpath(*_MACOS_MIM_META_REL)
    return None


#: Bundled-lived tool names per host (single site; used by both family
#: adapters via ``tool_path`` above).
PLAYER_NAME: Final[dict[str, str]] = dict(_PLAYER_NAME)
HD_ADB_NAME: Final[dict[str, str]] = dict(_HD_ADB_NAME)

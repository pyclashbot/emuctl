"""§4 — Config models: frozen slots dataclasses, specs, and the capability gate."""

from __future__ import annotations

import enum
import logging
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Final

from emuctl._identification import BackendId
from emuctl.errors import ConfigValidationError, FeatureNotSupportedError

__all__ = [
    "RendererMode",
    "DisplayConfig",
    "ResourceConfig",
    "AdbConfig",
    "MemuSettings",
    "MemuConfig",
    "BlueStacksSettings",
    "BlueStacksConfig",
    "LogConfig",
    "EmuctlConfig",
    "CreateSpec",
    "ConfigSpec",
    "CONFIGURE_FLAG",
    "resolve_renderer",
    "ensure_config_supported",
]

#: An invalid directly-constructed instance reports this layer; resolve_config
#: remaps structural failures to the winning file/env layer by leaf path.
_DIRECT_LAYER: Final[str] = "direct"

#: The capability flag value the config gate checks (CapabilityFlag.CONFIGURE).
CONFIGURE_FLAG: Final[str] = "configure"

#: Canonical overlay type: canonical dotted leaves → strict typed primitives.
ConfigSpec = Mapping[str, str | int | float | bool | None]


class RendererMode(enum.Enum):
    """Canonical renderer vocabulary (per semantic, per backend)."""

    AUTO = "auto"
    OPENGL = "opengl"
    DIRECTX = "directx"
    VULKAN = "vulkan"


def _fail(
    path: str, value: str | int | float | bool | None, expected: str
) -> ConfigValidationError:
    return ConfigValidationError(layer=_DIRECT_LAYER, path=path, value=value, expected=expected)


def _check_int(
    path: str,
    value: int | None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    """Range/type check for an optional int leaf (bools are never ints here)."""
    if value is None:
        return
    if minimum is not None and maximum is not None:
        expected = f"an int in {minimum}..{maximum}"
    elif minimum is not None:
        expected = f"an int >= {minimum}"
    elif maximum is not None:
        expected = f"an int <= {maximum}"
    else:
        expected = "an int"
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, value, expected)
    if minimum is not None and value < minimum:
        raise _fail(path, value, expected)
    if maximum is not None and value > maximum:
        raise _fail(path, value, expected)


def _check_bool(path: str, value: bool | None) -> None:
    if value is None:
        return
    if not isinstance(value, bool):
        raise _fail(path, value, "a bool")


def _check_str(path: str, value: str | None, *, strip: bool = False) -> str | None:
    """Non-empty check for an optional str leaf; returns the stored spelling."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, value, "a non-empty string")
    return value.strip() if strip else value


def _coerce_renderer(value: RendererMode | str | None, path: str) -> RendererMode | None:
    if value is None or isinstance(value, RendererMode):
        return value
    if isinstance(value, str):
        try:
            return RendererMode(value.strip().lower())
        except ValueError:
            pass
    raise _fail(path, value, "one of auto, opengl, directx, vulkan")


@dataclass(frozen=True, slots=True)
class DisplayConfig:
    """Target framebuffer geometry. All None = leave as-is (INV-C1: data only)."""

    width: int | None = None
    height: int | None = None
    dpi: int | None = None
    fps: int | None = None
    renderer: RendererMode | str | None = None

    def __post_init__(self) -> None:
        _check_int("display.width", self.width, minimum=1)
        _check_int("display.height", self.height, minimum=1)
        _check_int("display.dpi", self.dpi, minimum=1)
        _check_int("display.fps", self.fps, minimum=1)
        object.__setattr__(self, "renderer", _coerce_renderer(self.renderer, "display.renderer"))


@dataclass(frozen=True, slots=True)
class ResourceConfig:
    """CPU/memory targets. None = the vendor's current value."""

    cpus: int | None = None
    memory_mb: int | None = None
    cpu_cap_pct: int | None = None

    def __post_init__(self) -> None:
        _check_int("resources.cpus", self.cpus, minimum=1)
        _check_int("resources.memory_mb", self.memory_mb, minimum=1)
        _check_int("resources.cpu_cap_pct", self.cpu_cap_pct, minimum=1, maximum=100)


@dataclass(frozen=True, slots=True)
class AdbConfig:
    """System ADB server selector. None path → §3 discovery at selection."""

    path: str | None = None
    host: str = "127.0.0.1"
    port: int = 5037

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _check_str("adb.path", self.path))
        host = _check_str("adb.host", self.host, strip=True)
        if not host:
            raise _fail("adb.host", self.host, "a non-empty string")
        object.__setattr__(self, "host", host)
        if self.port is None:
            raise _fail("adb.port", None, "an int in 1..65535")
        _check_int("adb.port", self.port, minimum=1, maximum=65535)


@dataclass(frozen=True, slots=True)
class MemuSettings:
    """MEmu tweakable settings (applied at configure-time)."""

    hide_toolbar: bool | None = None
    turbo: bool | None = None
    audio: bool | None = None
    window_mode: str | None = None
    scale_percent: int | None = None

    def __post_init__(self) -> None:
        _check_bool("memu.settings.hide_toolbar", self.hide_toolbar)
        _check_bool("memu.settings.turbo", self.turbo)
        _check_bool("memu.settings.audio", self.audio)
        object.__setattr__(
            self, "window_mode", _check_str("memu.settings.window_mode", self.window_mode)
        )
        _check_int("memu.settings.scale_percent", self.scale_percent, minimum=1, maximum=100)


@dataclass(frozen=True, slots=True)
class MemuConfig:
    """MEmu backend selection (paths/names) + settings."""

    memuc_path: str | None = None
    vm_name: str | None = None
    settings: MemuSettings = field(default_factory=MemuSettings)

    def __post_init__(self) -> None:
        object.__setattr__(self, "memuc_path", _check_str("memu.memuc_path", self.memuc_path))
        object.__setattr__(self, "vm_name", _check_str("memu.vm_name", self.vm_name, strip=True))
        if not isinstance(self.settings, MemuSettings):
            raise _fail("memu.settings", type(self.settings).__name__, "a MemuSettings")


@dataclass(frozen=True, slots=True)
class BlueStacksSettings:
    """BlueStacks tweakable settings (applied at configure-time)."""

    vsync: bool | None = None
    high_fps: bool | None = None

    def __post_init__(self) -> None:
        _check_bool("bluestacks.settings.vsync", self.vsync)
        _check_bool("bluestacks.settings.high_fps", self.high_fps)


@dataclass(frozen=True, slots=True)
class BlueStacksConfig:
    """BlueStacks backend selection (ports/paths/names) + settings."""

    adb_server_port: int = 5041
    install_dir: str | None = None
    conf_path: str | None = None
    instance_name: str | None = None
    settings: BlueStacksSettings = field(default_factory=BlueStacksSettings)

    def __post_init__(self) -> None:
        if self.adb_server_port is None:
            raise _fail("bluestacks.adb_server_port", None, "an int in 1..65535")
        _check_int("bluestacks.adb_server_port", self.adb_server_port, minimum=1, maximum=65535)
        object.__setattr__(
            self, "install_dir", _check_str("bluestacks.install_dir", self.install_dir)
        )
        object.__setattr__(self, "conf_path", _check_str("bluestacks.conf_path", self.conf_path))
        object.__setattr__(
            self,
            "instance_name",
            _check_str("bluestacks.instance_name", self.instance_name, strip=True),
        )
        if not isinstance(self.settings, BlueStacksSettings):
            raise _fail("bluestacks.settings", type(self.settings).__name__, "a BlueStacksSettings")


@dataclass(frozen=True, slots=True)
class LogConfig:
    """Logging surface. Consumed ONLY by the CLI; the library never configures handlers."""

    level: str = "warning"
    file: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.level, str)
            or self.level.upper() not in logging.getLevelNamesMapping()
        ):
            raise _fail("log.level", self.level, "a stdlib logging level name")
        object.__setattr__(self, "file", _check_str("log.file", self.file))


@dataclass(frozen=True, slots=True)
class EmuctlConfig:
    """ROOT contract object. Programmatic consumers build this directly (files are sugar)."""

    adb: AdbConfig = field(default_factory=AdbConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    log: LogConfig = field(default_factory=LogConfig)
    memu: MemuConfig | None = None
    bluestacks: BlueStacksConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.adb, AdbConfig):
            raise _fail("adb", type(self.adb).__name__, "an AdbConfig")
        if not isinstance(self.display, DisplayConfig):
            raise _fail("display", type(self.display).__name__, "a DisplayConfig")
        if not isinstance(self.resources, ResourceConfig):
            raise _fail("resources", type(self.resources).__name__, "a ResourceConfig")
        if not isinstance(self.log, LogConfig):
            raise _fail("log", type(self.log).__name__, "a LogConfig")
        if self.memu is not None and not isinstance(self.memu, MemuConfig):
            raise _fail("memu", type(self.memu).__name__, "a MemuConfig or None")
        if self.bluestacks is not None and not isinstance(self.bluestacks, BlueStacksConfig):
            raise _fail("bluestacks", type(self.bluestacks).__name__, "a BlueStacksConfig or None")


@dataclass(frozen=True, slots=True)
class CreateSpec:
    """Provisioning request. ``name`` is REQUIRED at v0; None version ⇒ default image."""

    name: str
    android_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _check_str("spec.name", self.name, strip=True) or "")
        if not self.name:
            raise _fail("spec.name", self.name, "a non-empty string")
        object.__setattr__(
            self,
            "android_version",
            _check_str("spec.android_version", self.android_version, strip=True),
        )


def resolve_renderer(mode: RendererMode, backend: BackendId) -> RendererMode | None:
    """Resolve ``AUTO`` to the §4-pinned vendor default for ``backend``.

    MEmu → DIRECTX; BlueStacks (Windows) → DIRECTX; BlueStacks Air (macOS) →
    VULKAN; anything else (ADB has no renderer semantics) → None. A concrete
    mode passes through untouched. Pure.
    """
    if mode is not RendererMode.AUTO:
        return mode
    if backend is BackendId.MEMU:
        return RendererMode.DIRECTX
    if backend is BackendId.BLUESTACKS:
        return RendererMode.DIRECTX
    if backend is BackendId.BLUESTACKS_AIR:
        return RendererMode.VULKAN
    return None


def ensure_config_supported(cfg: EmuctlConfig, features: AbstractSet[object]) -> None:
    """Construction-time config ↔ capability gate.

    ``[display]`` / ``[resources]`` / ``[memu.settings]`` /
    ``[bluestacks.settings]`` each require the adapter to declare CONFIGURE;
    anything set there without the flag is a typed refusal
    (``FeatureNotSupportedError``) — never a silent ignore. Selection/path
    knobs (adb host/port, vm names, binary paths) are not gated. Backend
    ``features`` is the adapter's declared set; members are read by value so
    both enum and plain-string sets work. Pure.
    """
    names = {f.value if isinstance(f, enum.Enum) else f for f in features}
    if CONFIGURE_FLAG in names:
        return
    offenders: list[str] = []
    if cfg.display != DisplayConfig():
        offenders.append("[display]")
    if cfg.resources != ResourceConfig():
        offenders.append("[resources]")
    if cfg.memu is not None and cfg.memu.settings != MemuSettings():
        offenders.append("[memu.settings]")
    if cfg.bluestacks is not None and cfg.bluestacks.settings != BlueStacksSettings():
        offenders.append("[bluestacks.settings]")
    if offenders:
        joined = " ".join(offenders)
        raise FeatureNotSupportedError(
            feature=CONFIGURE_FLAG,
            detail=(
                f"config requests {joined} but the selected backend does not declare CONFIGURE"
            ),
        )

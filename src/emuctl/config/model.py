from __future__ import annotations

import enum
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Final

from emuctl.errors import ConfigValidationError, FeatureNotSupportedError

__all__ = [
    "DisplayConfig",
    "AdbConfig",
    "EmuctlConfig",
    "CreateSpec",
    "ConfigSpec",
    "CONFIGURE_FLAG",
    "ensure_config_supported",
]

_DIRECT_LAYER: Final[str] = "direct"

CONFIGURE_FLAG: Final[str] = "configure"

ConfigSpec = Mapping[str, str | int | float | bool | None]


def _fail(path: str, value: str | int | float | bool | None, expected: str) -> ConfigValidationError:
    return ConfigValidationError(layer=_DIRECT_LAYER, path=path, value=value, expected=expected)


def _check_int(
    path: str,
    value: int | None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
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
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, value, "a non-empty string")
    return value.strip() if strip else value


@dataclass(frozen=True, slots=True)
class DisplayConfig:
    width: int | None = None
    height: int | None = None
    dpi: int | None = None

    def __post_init__(self) -> None:
        _check_int("display.width", self.width, minimum=1)
        _check_int("display.height", self.height, minimum=1)
        _check_int("display.dpi", self.dpi, minimum=1)


@dataclass(frozen=True, slots=True)
class AdbConfig:
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
class CreateSpec:
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


@dataclass(frozen=True, slots=True)
class EmuctlConfig:
    adb: AdbConfig = field(default_factory=AdbConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.adb, AdbConfig):
            raise _fail("adb", type(self.adb).__name__, "an AdbConfig")
        if not isinstance(self.display, DisplayConfig):
            raise _fail("display", type(self.display).__name__, "a DisplayConfig")


def ensure_config_supported(cfg: EmuctlConfig, features: AbstractSet[object]) -> None:
    names = {f.value if isinstance(f, enum.Enum) else f for f in features}
    if CONFIGURE_FLAG in names:
        return
    offenders: list[str] = []
    if cfg.display != DisplayConfig():
        offenders.append("[display]")
    if offenders:
        joined = " ".join(offenders)
        raise FeatureNotSupportedError(
            feature=CONFIGURE_FLAG,
            detail=(f"config requests {joined} but the selected backend does not declare CONFIGURE"),
        )

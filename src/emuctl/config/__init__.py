from __future__ import annotations

import difflib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from emuctl.config.model import (
    AdbConfig,
    BlueStacksConfig,
    BlueStacksSettings,
    ConfigSpec,
    CreateSpec,
    DisplayConfig,
    EmuctlConfig,
    RendererMode,
    ResourceConfig,
)
from emuctl.errors import ConfigError, ConfigValidationError, InvalidArgumentError

__all__ = [
    "RendererMode",
    "DisplayConfig",
    "ResourceConfig",
    "AdbConfig",
    "BlueStacksSettings",
    "BlueStacksConfig",
    "EmuctlConfig",
    "ConfigSpec",
    "CreateSpec",
    "ConfigWarning",
    "ResolvedConfig",
    "SCHEMA_VERSION",
    "resolve_config",
]

SCHEMA_VERSION: Final[int] = 1

_LeafKind = Literal["int", "bool", "str", "renderer", "level"]

_LEAVES: Final[dict[str, _LeafKind]] = {
    "display.width": "int",
    "display.height": "int",
    "display.dpi": "int",
    "display.fps": "int",
    "display.renderer": "renderer",
    "resources.cpus": "int",
    "resources.memory_mb": "int",
    "resources.cpu_cap_pct": "int",
    "adb.path": "str",
    "adb.host": "str",
    "adb.port": "int",
    "bluestacks.adb_server_port": "int",
    "bluestacks.install_dir": "str",
    "bluestacks.conf_path": "str",
    "bluestacks.instance_name": "str",
    "bluestacks.settings.vsync": "bool",
    "bluestacks.settings.high_fps": "bool",
}

_SECTIONS: Final[frozenset[str]] = frozenset({"display", "resources", "adb", "bluestacks"})

_ENV_PREFIX: Final[str] = "EMUCTL_"
_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"true", "1"})
_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"false", "0"})


def _shown(value: object) -> str | int | float | bool | None:
    if isinstance(value, RendererMode):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


@dataclass(frozen=True, slots=True)
class ConfigWarning:
    layer: str
    path: str
    nearest: str | None = None

    def __str__(self) -> str:
        text = f"unknown config key {self.path!r} in layer {self.layer!r} (ignored)"
        if self.nearest is not None:
            text += f"; did you mean {self.nearest!r}?"
        return text


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    config: EmuctlConfig
    warnings: tuple[ConfigWarning, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.config, EmuctlConfig):
            raise InvalidArgumentError(parameter="config", constraint="must be an EmuctlConfig")
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if not all(isinstance(w, ConfigWarning) for w in self.warnings):
            raise InvalidArgumentError(parameter="warnings", constraint="must all be ConfigWarning")


def _nearest_leaf(path: str) -> str | None:
    matches = difflib.get_close_matches(path, sorted(_LEAVES), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _coerce_leaf(leaf: str, value: object, *, layer: str) -> str | int | bool | RendererMode | None:
    kind = _LEAVES[leaf]

    def fail(expected: str) -> ConfigValidationError:
        return ConfigValidationError(layer=layer, path=leaf, value=_shown(value), expected=expected)

    if value is None:
        return None
    if kind == "renderer":
        if isinstance(value, RendererMode):
            return value
        if isinstance(value, str):
            try:
                return RendererMode(value.strip().lower())
            except ValueError:
                raise fail("one of auto, opengl, directx, vulkan") from None
        raise fail("one of auto, opengl, directx, vulkan")
    if kind == "level":
        if isinstance(value, str) and value.strip():
            return value
        raise fail("a stdlib logging level name")
    if kind == "str":
        if isinstance(value, str) and value.strip():
            return value
        raise fail("a non-empty string")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            token = value.strip().lower()
            if token in _TRUE_TOKENS:
                return True
            if token in _FALSE_TOKENS:
                return False
        raise fail("a bool (true/false)")
    if isinstance(value, bool):
        raise fail("an int")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            raise fail("an int") from None
    raise fail("an int")


class _Collector:
    def __init__(self) -> None:
        self.leaves: dict[str, str | int | bool | RendererMode | None] = {}
        self.layers: dict[str, str] = {}
        self.warnings: list[ConfigWarning] = []

    def set_leaf(self, leaf: str, value: object, layer: str) -> None:
        coerced = _coerce_leaf(leaf, value, layer=layer)
        if coerced is None:
            self.leaves.pop(leaf, None)
            self.layers.pop(leaf, None)
        else:
            self.leaves[leaf] = coerced
            self.layers[leaf] = layer

    def warn(self, layer: str, path: str) -> None:
        self.warnings.append(ConfigWarning(layer=layer, path=path, nearest=_nearest_leaf(path)))

    def apply_flags(self, flags: Mapping[str, object], layer: str) -> None:
        for key in sorted(flags):
            raw = flags[key]
            if key in _LEAVES:
                self.set_leaf(key, raw, layer)
            else:
                self.warn(layer, key)


def _apply_env(env: Mapping[str, str], out: _Collector) -> None:
    by_flat = {leaf.replace(".", "_"): leaf for leaf in _LEAVES}
    for name in sorted(env):
        if not name.startswith(_ENV_PREFIX):
            continue
        remainder = name[len(_ENV_PREFIX) :].lower()
        leaf = by_flat.get(remainder)
        if leaf is None:
            flat_candidates = {flat: flat for flat in by_flat}
            guess = difflib.get_close_matches(remainder, sorted(flat_candidates), n=1, cutoff=0.6)
            nearest = by_flat[guess[0]] if guess else None
            out.warnings.append(ConfigWarning(layer="env", path=name, nearest=nearest))
            continue
        out.set_leaf(leaf, env[name], "env")


def _check_schema_version(value: object, *, layer: str, path: str) -> None:
    if isinstance(value, bool):
        raise ConfigValidationError(layer=layer, path="schema_version", value=value, expected="schema major 1")
    major: int | None = None
    if isinstance(value, int):
        major = value
    elif isinstance(value, float):
        major = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            major = int(text)
        except ValueError:
            try:
                major = int(float(text))
            except ValueError:
                major = None
    if major is None:
        raise ConfigValidationError(
            layer=layer, path="schema_version", value=_shown(value), expected="schema major 1"
        )
    if major != SCHEMA_VERSION:
        raise ConfigError(
            layer=layer,
            path=path,
            reason="bad_version",
            value=value if isinstance(value, (str, int, float, bool)) or value is None else None,
            detail=f"unsupported schema major {major}; installed schema is {SCHEMA_VERSION}",
        )


def _walk_mapping(out: _Collector, data: Mapping[str, object], *, layer: str, prefix: str) -> None:
    for key in sorted(data):
        value = data[key]
        path = f"{prefix}.{key}" if prefix else str(key)
        if prefix == "" and key == "schema_version":
            _check_schema_version(value, layer=layer, path=path)
            continue
        if prefix == "" and key in _SECTIONS and not isinstance(value, Mapping):
            raise ConfigValidationError(layer=layer, path=path, value=_shown(value), expected="a table")
        if prefix == "bluestacks" and key == "settings" and not isinstance(value, Mapping):
            raise ConfigValidationError(layer=layer, path=path, value=_shown(value), expected="a table")
        if isinstance(value, Mapping):
            _walk_mapping(out, value, layer=layer, prefix=path)
        elif path in _LEAVES:
            out.set_leaf(path, value, layer)
        else:
            out.warn(layer, path)


def _read_toml_file(path: Path, *, layer: str) -> Mapping[str, object]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(layer=layer, path=str(path), reason="unreadable", value=None) from None
    except OSError as exc:
        raise ConfigError(layer=layer, path=str(path), reason="unreadable", value=str(exc)) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(layer=layer, path=str(path), reason="unreadable", value=str(exc)) from None


def _apply_project_file(path: Path, data: Mapping[str, object], out: _Collector) -> None:
    if path.name == "pyproject.toml":
        tool = data.get("tool")
        if not isinstance(tool, Mapping):
            return
        scoped = tool.get("emuctl")
        if not isinstance(scoped, Mapping):
            return
        data = scoped
    _walk_mapping(out, data, layer="project", prefix="")


def _find_project_file(cwd: Path) -> tuple[Path, Mapping[str, object]] | None:
    for directory in (cwd, *cwd.parents):
        candidate = directory / "emuctl.toml"
        if candidate.is_file():
            data = _read_toml_file(candidate, layer="project")
            return (candidate, data)
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            data = _read_toml_file(pyproject, layer="project")
            tool = data.get("tool")
            if isinstance(tool, Mapping) and isinstance(tool.get("emuctl"), Mapping):
                return (pyproject, data)
    return None


def _user_config_path(env: Mapping[str, str]) -> Path | None:
    appdata = env.get("APPDATA")
    if appdata:
        return Path(appdata) / "emuctl" / "emuctl.toml"
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "emuctl" / "emuctl.toml"
    home = env.get("HOME")
    if home:
        return Path(home) / ".config" / "emuctl" / "emuctl.toml"
    return None


def _build_config(out: _Collector) -> EmuctlConfig:
    leaves = out.leaves
    try:
        display = DisplayConfig(
            width=cast("int | None", leaves.get("display.width")),
            height=cast("int | None", leaves.get("display.height")),
            dpi=cast("int | None", leaves.get("display.dpi")),
            fps=cast("int | None", leaves.get("display.fps")),
            renderer=cast("RendererMode | str | None", leaves.get("display.renderer")),
        )
        resources = ResourceConfig(
            cpus=cast("int | None", leaves.get("resources.cpus")),
            memory_mb=cast("int | None", leaves.get("resources.memory_mb")),
            cpu_cap_pct=cast("int | None", leaves.get("resources.cpu_cap_pct")),
        )
        adb = AdbConfig(
            path=cast("str | None", leaves.get("adb.path")),
            host=cast(str, leaves.get("adb.host", "127.0.0.1")),
            port=cast(int, leaves.get("adb.port", 5037)),
        )
        bluestacks: BlueStacksConfig | None = None
        if any(key.startswith("bluestacks.") for key in leaves):
            bluestacks = BlueStacksConfig(
                adb_server_port=cast(int, leaves.get("bluestacks.adb_server_port", 5041)),
                install_dir=cast("str | None", leaves.get("bluestacks.install_dir")),
                conf_path=cast("str | None", leaves.get("bluestacks.conf_path")),
                instance_name=cast("str | None", leaves.get("bluestacks.instance_name")),
                settings=BlueStacksSettings(
                    vsync=cast("bool | None", leaves.get("bluestacks.settings.vsync")),
                    high_fps=cast("bool | None", leaves.get("bluestacks.settings.high_fps")),
                ),
            )
        return EmuctlConfig(adb=adb, display=display, resources=resources, bluestacks=bluestacks)
    except ConfigValidationError as exc:
        exc.layer = out.layers.get(exc.path, "config")
        raise


def resolve_config(
    flags: ConfigSpec | Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    *,
    config_file: str | os.PathLike[str] | None = None,
    no_config: bool = False,
) -> ResolvedConfig:
    if config_file is not None and no_config:
        raise InvalidArgumentError(
            parameter="--config-file",
            constraint="cannot be combined with --no-config",
        )
    environ: Mapping[str, str] = os.environ if env is None else env
    base = Path.cwd() if cwd is None else Path(cwd)
    if not base.is_absolute():
        base = Path.cwd() / base

    out = _Collector()
    if not no_config:
        if config_file is not None:
            data = _read_toml_file(Path(config_file), layer="config-file")
            _walk_mapping(out, data, layer="config-file", prefix="")
        else:
            user_path = _user_config_path(environ)
            if user_path is not None and user_path.is_file():
                _walk_mapping(out, _read_toml_file(user_path, layer="user"), layer="user", prefix="")
            found = _find_project_file(base)
            if found is not None:
                _apply_project_file(found[0], found[1], out)
    _apply_env(environ, out)
    out.apply_flags(dict(flags) if flags else {}, "flags")
    return ResolvedConfig(config=_build_config(out), warnings=tuple(out.warnings))

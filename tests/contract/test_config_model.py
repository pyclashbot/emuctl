"""§7 conformance (config models, §4): validation invariants + capability gate."""

import dataclasses
import typing
from typing import Any

import pytest

from emuctl.config.model import (
    AdbConfig,
    ConfigSpec,
    CreateSpec,
    DisplayConfig,
    EmuctlConfig,
    LogConfig,
    RendererMode,
    ResourceConfig,
    ensure_config_supported,
)
from emuctl.errors import ConfigValidationError, FeatureNotSupportedError


def test_renderer_modes_pinned() -> None:
    assert {m.value for m in RendererMode} == {"auto", "opengl", "directx", "vulkan"}


def test_defaults_are_leave_as_is() -> None:
    cfg = EmuctlConfig()
    assert cfg.adb.port == 5037
    assert cfg.adb.host == "127.0.0.1"
    assert cfg.display == DisplayConfig()
    assert cfg.resources == ResourceConfig()
    assert cfg.log == LogConfig(level="warning")


@pytest.mark.parametrize(
    "make",
    [
        lambda: DisplayConfig(width=0),
        lambda: DisplayConfig(width=-800),
        lambda: DisplayConfig(height=0),
        lambda: DisplayConfig(dpi=-1),
        lambda: DisplayConfig(fps=0),
        lambda: DisplayConfig(width=True),
        lambda: DisplayConfig(width="800"),  # ty: ignore[invalid-argument-type]
        lambda: DisplayConfig(width=800.0),  # ty: ignore[invalid-argument-type]
        lambda: ResourceConfig(cpus=0),
        lambda: ResourceConfig(cpus=-2),
        lambda: ResourceConfig(cpus=True),
        lambda: ResourceConfig(memory_mb=0),
        lambda: ResourceConfig(memory_mb=-512),
        lambda: ResourceConfig(cpu_cap_pct=0),
        lambda: ResourceConfig(cpu_cap_pct=101),
        lambda: AdbConfig(port=0),
        lambda: AdbConfig(port=65536),
        lambda: AdbConfig(port=-1),
        lambda: AdbConfig(port=True),
        lambda: AdbConfig(host=""),
        lambda: AdbConfig(host="   "),
        lambda: DisplayConfig(renderer="bogus"),
        lambda: DisplayConfig(renderer=5),  # ty: ignore[invalid-argument-type]
        lambda: LogConfig(level="verbose"),
        lambda: LogConfig(level=""),
        lambda: LogConfig(level=10),  # ty: ignore[invalid-argument-type]
        lambda: CreateSpec(name=""),
        lambda: CreateSpec(name="   "),
    ],
)
def test_invalid_values_rejected(make: Any) -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        make()
    assert excinfo.value.error_code == "config_validation"
    assert excinfo.value.layer == "direct"


def test_error_payload_carries_dotted_path() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        DisplayConfig(width=-1)
    assert excinfo.value.path == "display.width"
    assert excinfo.value.value == -1


def test_renderer_string_coerced_case_insensitively() -> None:
    assert DisplayConfig(renderer="VULKAN").renderer is RendererMode.VULKAN
    assert DisplayConfig(renderer="auto").renderer is RendererMode.AUTO
    assert DisplayConfig(renderer=RendererMode.DIRECTX).renderer is RendererMode.DIRECTX


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical", "WARNING"])
def test_stdlib_level_names_accepted(level: str) -> None:
    assert LogConfig(level=level).level == level


def test_create_spec_name_required_version_optional() -> None:
    assert CreateSpec(name="pyclashbot-136").android_version is None
    assert CreateSpec(name="  padded  ").name == "padded"
    assert CreateSpec(name="x", android_version="136").android_version == "136"


def test_models_are_frozen_slotted() -> None:
    cfg = EmuctlConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.log = LogConfig()  # ty: ignore[invalid-assignment]
    for cls in (
        DisplayConfig,
        ResourceConfig,
        AdbConfig,
        LogConfig,
        EmuctlConfig,
        CreateSpec,
    ):
        assert "__slots__" in cls.__dict__, cls
        params = cls.__dict__.get("__dataclass_params__")
        assert params is not None and params.frozen, cls


def test_config_spec_alias_shape() -> None:
    import collections.abc

    assert typing.get_origin(ConfigSpec) is collections.abc.Mapping
    args = typing.get_args(ConfigSpec)
    assert args[0] is str


def test_gate_passes_with_configure_or_empty_config() -> None:
    assert ensure_config_supported(EmuctlConfig(), frozenset()) is None
    full = EmuctlConfig(
        display=DisplayConfig(width=800),
        resources=ResourceConfig(cpus=2),
    )
    assert ensure_config_supported(full, frozenset({"configure"})) is None
    # Selection/path knobs alone never trip the gate.
    assert ensure_config_supported(EmuctlConfig(adb=AdbConfig(port=5038)), frozenset()) is None


@pytest.mark.parametrize(
    "cfg",
    [
        EmuctlConfig(display=DisplayConfig(width=800)),
        EmuctlConfig(resources=ResourceConfig(cpus=2)),
    ],
)
def test_gate_refuses_without_configure(cfg: EmuctlConfig) -> None:
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        ensure_config_supported(cfg, frozenset())
    assert excinfo.value.feature == "configure"
    assert "CONFIGURE" in (excinfo.value.detail or "")
    assert excinfo.value.error_code == "feature_not_supported"


def test_plain_adb_display_passes_once_configure_is_declared() -> None:
    cfg = EmuctlConfig(display=DisplayConfig(width=800))
    assert ensure_config_supported(cfg, frozenset({"configure"})) is None

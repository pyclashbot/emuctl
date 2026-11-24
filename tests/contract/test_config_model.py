"""Config model: display/adb validation and capability gate."""

import dataclasses
from typing import Any

import pytest

from emuctl._identification import BackendId
from emuctl.config.model import (
    AdbConfig,
    DisplayConfig,
    EmuctlConfig,
    MemuConfig,
    MemuSettings,
    RendererMode,
    ResourceConfig,
    ensure_config_supported,
    resolve_renderer,
)
from emuctl.errors import ConfigValidationError, FeatureNotSupportedError


def test_defaults_and_frozen_slots() -> None:
    cfg = EmuctlConfig()
    assert cfg.adb.port == 5037
    assert cfg.display == DisplayConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.adb = AdbConfig()  # ty: ignore[invalid-assignment]
    assert "__slots__" in DisplayConfig.__dict__


@pytest.mark.parametrize(
    "make",
    [
        lambda: DisplayConfig(width=0),
        lambda: DisplayConfig(height=-1),
        lambda: DisplayConfig(dpi=True),
        lambda: DisplayConfig(width="800"),  # ty: ignore[invalid-argument-type]
        lambda: DisplayConfig(fps=0),
        lambda: DisplayConfig(renderer="meteor"),
        lambda: ResourceConfig(cpus=0),
        lambda: ResourceConfig(memory_mb=-1),
        lambda: ResourceConfig(cpu_cap_pct=101),
        lambda: MemuSettings(scale_percent=0),
        lambda: MemuSettings(scale_percent=101),
        lambda: MemuSettings(hide_toolbar="yes"),  # ty: ignore[invalid-argument-type]
        lambda: AdbConfig(port=0),
        lambda: AdbConfig(port=65536),
        lambda: AdbConfig(host="   "),
    ],
)
def test_invalid_values_rejected(make: Any) -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        make()
    assert excinfo.value.error_code == "config_validation"
    assert excinfo.value.layer == "direct"


def test_error_payload_carries_path() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        DisplayConfig(width=-1)
    assert excinfo.value.path == "display.width"
    assert excinfo.value.value == -1


def test_gate_requires_configure_for_display() -> None:
    cfg = EmuctlConfig(display=DisplayConfig(width=800))
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        ensure_config_supported(cfg, frozenset())
    assert excinfo.value.feature == "configure"
    assert ensure_config_supported(cfg, frozenset({"configure"})) is None
    assert ensure_config_supported(EmuctlConfig(), frozenset()) is None


@pytest.mark.parametrize(
    ("cfg", "section"),
    [
        (EmuctlConfig(resources=ResourceConfig(cpus=2)), "[resources]"),
        (
            EmuctlConfig(memu=MemuConfig(settings=MemuSettings(hide_toolbar=True))),
            "[memu.settings]",
        ),
    ],
)
def test_gate_requires_configure_for_resources_and_memu_settings(cfg: EmuctlConfig, section: str) -> None:
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        ensure_config_supported(cfg, frozenset())
    assert section in (excinfo.value.detail or "")
    assert ensure_config_supported(cfg, frozenset({"configure"})) is None


def test_renderer_auto_resolves_to_memu_directx() -> None:
    assert resolve_renderer(RendererMode.AUTO, BackendId.MEMU) is RendererMode.DIRECTX
    assert resolve_renderer(RendererMode.OPENGL, BackendId.MEMU) is RendererMode.OPENGL
    assert resolve_renderer(RendererMode.AUTO, BackendId.ADB) is None


def test_display_renderer_accepts_strings_case_insensitively() -> None:
    assert DisplayConfig(renderer="VULKAN").renderer is RendererMode.VULKAN
    assert DisplayConfig(renderer=" directx ").renderer is RendererMode.DIRECTX

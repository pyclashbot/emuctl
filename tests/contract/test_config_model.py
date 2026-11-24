"""Config model: display/resources/adb/bluestacks validation and the capability gate."""

import dataclasses
from typing import Any

import pytest

from emuctl._identification import BackendId
from emuctl.config.model import (
    AdbConfig,
    BlueStacksConfig,
    BlueStacksSettings,
    DisplayConfig,
    EmuctlConfig,
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
    assert cfg.bluestacks is None
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
        lambda: BlueStacksConfig(adb_server_port=0),
        lambda: BlueStacksConfig(adb_server_port=65536),
        lambda: BlueStacksSettings(vsync="yes"),  # ty: ignore[invalid-argument-type]
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
            EmuctlConfig(bluestacks=BlueStacksConfig(settings=BlueStacksSettings(vsync=True))),
            "[bluestacks.settings]",
        ),
    ],
)
def test_gate_requires_configure_for_resources_and_bluestacks_settings(cfg: EmuctlConfig, section: str) -> None:
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        ensure_config_supported(cfg, frozenset())
    assert section in (excinfo.value.detail or "")
    assert ensure_config_supported(cfg, frozenset({"configure"})) is None


def test_renderer_auto_resolves_to_bluestacks_directx() -> None:
    assert resolve_renderer(RendererMode.AUTO, BackendId.BLUESTACKS) is RendererMode.DIRECTX
    assert resolve_renderer(RendererMode.OPENGL, BackendId.BLUESTACKS) is RendererMode.OPENGL
    assert resolve_renderer(RendererMode.AUTO, BackendId.ADB) is None


def test_display_renderer_accepts_strings_case_insensitively() -> None:
    assert DisplayConfig(renderer="VULKAN").renderer is RendererMode.VULKAN
    assert DisplayConfig(renderer=" directx ").renderer is RendererMode.DIRECTX

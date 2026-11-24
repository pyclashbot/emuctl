"""Config model: display/adb validation and capability gate."""

import dataclasses
from typing import Any

import pytest

from emuctl.config.model import (
    AdbConfig,
    DisplayConfig,
    EmuctlConfig,
    GooglePlayConfig,
    ensure_config_supported,
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
        lambda: GooglePlayConfig(device_serial="not a serial"),
        lambda: GooglePlayConfig(device_serial=""),
        lambda: GooglePlayConfig(install_dir="   "),
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


def test_google_play_defaults() -> None:
    cfg = EmuctlConfig()
    assert cfg.google_play is None
    assert GooglePlayConfig().device_serial == "localhost:6520"

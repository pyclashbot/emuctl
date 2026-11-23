import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl.config.model import ConfigSpec, DisplayConfig
from emuctl.errors import (
    ControllerClosedError,
    FeatureNotSupportedError,
    InvalidArgumentError,
)
from emuctl.port import (
    MECHANICS,
    PROBE_REASONS,
    AsyncEmulatorController,
    CapabilityFlag,
    DisplayState,
)
from emuctl.timing import CONFIG_TIMEOUT, SNAPSHOT_TIMEOUT, resolve_timeout

REF = InstanceRef(backend=BackendId.ADB, key="emulator-5554")


class Fake(AsyncEmulatorController):
    FEATURES = frozenset({CapabilityFlag.CONFIGURE, CapabilityFlag.SCREENSHOT})

    def __init__(self) -> None:
        super().__init__(REF)
        self.calls: list[tuple[str, float | None]] = []

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> Any:
        return b""

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:
        self.calls.append(("_configure", timeout))
        return DisplayState(1, 2, 3)

    async def _screenshot(self, timeout: float | None) -> bytes:
        self.calls.append(("_screenshot", timeout))
        return b"png"


def test_flags_and_mechanics_pinned() -> None:
    assert {flag.value for flag in CapabilityFlag} == {
        "boot",
        "create",
        "configure",
        "screenshot",
        "input",
        "apps",
        "apk_install",
    }
    assert MECHANICS[CapabilityFlag.CONFIGURE] == ("_configure",)
    assert MECHANICS[CapabilityFlag.SCREENSHOT] == ("_screenshot",)


def test_display_state_to_spec() -> None:
    assert DisplayState(419, 633, 160).to_spec() == {
        "display.width": 419,
        "display.height": 633,
        "display.dpi": 160,
    }
    assert DisplayState(None, None, None).to_spec() == {}


def test_gate_first_and_typed_refusal() -> None:
    fake = Fake()
    with pytest.raises(FeatureNotSupportedError):
        asyncio.run(fake.click(1, 2))
    assert fake.calls == []


def test_configure_resolves_group_timeout_and_passes_overlay() -> None:
    fake = Fake()
    overlay: ConfigSpec = {"display.width": 419, "display.height": 633}
    assert asyncio.run(fake.configure(overlay)) == DisplayState(1, 2, 3)
    assert fake.calls == [("_configure", CONFIG_TIMEOUT)]
    assert asyncio.run(fake.configure(overlay, timeout=None)) == DisplayState(1, 2, 3)
    assert fake.calls[-1] == ("_configure", None)


def test_invalid_timeout_and_spec_rejected_before_io() -> None:
    fake = Fake()
    with pytest.raises(InvalidArgumentError):
        asyncio.run(fake.screenshot(timeout=0))
    with pytest.raises(InvalidArgumentError):
        asyncio.run(fake.configure("x"))  # ty: ignore[invalid-argument-type]
    assert fake.calls == []


def test_close_guards_ops_and_identity_survives() -> None:
    fake = Fake()
    asyncio.run(fake.aclose())
    asyncio.run(fake.close())
    with pytest.raises(ControllerClosedError):
        asyncio.run(fake.screenshot())
    assert fake.identity == REF
    assert asyncio.run(fake.is_reachable()) == (False, "unknown")


def test_timeout_resolver_rules() -> None:
    assert resolve_timeout(None, SNAPSHOT_TIMEOUT, operation="screenshot") is None
    assert resolve_timeout(1.5, SNAPSHOT_TIMEOUT, operation="screenshot") == 1.5
    with pytest.raises(InvalidArgumentError):
        resolve_timeout(0, SNAPSHOT_TIMEOUT, operation="screenshot")


def test_probe_reasons_pinned() -> None:
    assert {"ok", "no-adb", "timeout", "unknown", "offline", "unauthorized", "disconnected"} <= set(
        PROBE_REASONS
    )
    assert DisplayConfig(width=1).width == 1

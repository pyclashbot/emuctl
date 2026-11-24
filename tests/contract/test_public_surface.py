from __future__ import annotations

import asyncio
import importlib
import logging
import subprocess
import sys
import textwrap
import types
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

import emuctl
from emuctl._discovery import BackendStatus, DiscoveryReport
from emuctl._identification import BackendId, InstanceRef
from emuctl.config.model import DisplayConfig, EmuctlConfig
from emuctl.errors import (
    BackendNotAvailableError,
    FeatureNotSupportedError,
    InvalidArgumentError,
)
from emuctl.port import AsyncEmulatorController

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "CONTRACT.md"

#: §3's multi-instance concurrency promise, quoted verbatim and pinned here so
#: the wording cannot drift without a deliberate contract change surfacing a
#: public-surface test failure.
MULTI_INSTANCE_PROMISE = (
    "N independent controllers per process are supported; no global registry, locks, or kills."
)

#: Deliberate, exactly-per-contract public surface. NOT exported: registry/
#: selection machinery (available_backends/get_registry/resolve_backend/
#: load_adapter/BackendSpec — discover() is the ONLY enumeration path,
#: §1 invariant 11 + §3 Boundary), §8-deferred members (is_running,
#: resolve_target, run_vendor_cmd, stop_app, screen_size), and internals
#: (sanitize, wait_until, _proc).
EXPECTED_EXPORTS: frozenset[str] = frozenset(
    {
        # Construction + enumeration (§3/§5).
        "connect",
        "connect_sync",
        "EmulatorController",
        "discover",
        "select_instance",
        "BackendStatus",
        "DiscoveryReport",
        # Identity + platform (§3).
        "BackendId",
        "InstanceRef",
        "InstanceRecord",
        "Platform",
        "current_platform",
        # Serial validation (§3 security control).
        "is_valid_serial",
        "validate_serial",
        # Error taxonomy (§2).
        "EmuctlError",
        "InvalidArgumentError",
        "InstanceNotFoundError",
        "InvalidSerialError",
        "ExtrasMissingError",
        "ExecutableNotFoundError",
        "ConfigError",
        "ConfigValidationError",
        "OperationTimeoutError",
        "VendorCommandError",
        "DeviceStateError",
        "FeatureNotSupportedError",
        "BackendNotAvailableError",
        "BackendNotFoundError",
        "EmulatorNotReadyError",
        "ControllerClosedError",
        "AmbiguousInstanceError",
        "SyncCallInsideEventLoopError",
        "resolve_error_code",
        # Timing sentinels + group constants (§2).
        "DEFAULT",
        "INTERACTION_TIMEOUT",
        "SNAPSHOT_TIMEOUT",
        "QUERY_TIMEOUT",
        "CONFIG_TIMEOUT",
        "APP_TIMEOUT",
        "LIFECYCLE_TIMEOUT",
        "PROVISION_TIMEOUT",
        # Port pins (§1).
        "CapabilityFlag",
        "ReadyCheck",
        "ProbeResult",
        "DisplayState",
        "AsyncEmulatorController",
        "AdbController",
        # Config models + precedence + gate (§4).
        "DisplayConfig",
        "AdbConfig",
        "EmuctlConfig",
        "ConfigSpec",
        "CreateSpec",
        "ConfigWarning",
        "ResolvedConfig",
        "SCHEMA_VERSION",
        "resolve_config",
        "ensure_config_supported",
        # Screenshot data helper (§4).
        "png_dimensions",
    }
)

#: Names that must NEVER resolve on the package (enumeration machinery, §8
#: deferrals, internals).
FORBIDDEN_EXPORTS: tuple[str, ...] = (
    "available_backends",
    "get_registry",
    "resolve_backend",
    "load_adapter",
    "BackendSpec",
    "is_running",
    "resolve_target",
    "run_vendor_cmd",
    "stop_app",
    "screen_size",
    "sanitize",
    "wait_until",
)


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n.startswith("emuctl.backends")]:
        if name not in before:
            evicted = sys.modules.pop(name)
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, child, None) is evicted:
                delattr(parent, child)


# ── the export list itself ──


def test_public_surface_is_exactly_the_contract_list() -> None:
    assert set(emuctl.__all__) == EXPECTED_EXPORTS
    missing = sorted(name for name in emuctl.__all__ if not hasattr(emuctl, name))
    assert missing == [], f"exports advertised but unresolvable: {missing}"


@pytest.mark.parametrize("name", sorted(EXPECTED_EXPORTS))
def test_every_export_is_freshly_importable(name: str) -> None:
    value = getattr(importlib.import_module("emuctl"), name)
    assert value is not None


def test_registry_machinery_is_not_public() -> None:
    for name in FORBIDDEN_EXPORTS:
        assert not hasattr(emuctl, name), name
        assert name not in emuctl.__all__, name


def test_multi_instance_promise_wording_is_pinned() -> None:
    assert MULTI_INSTANCE_PROMISE in CONTRACT_PATH.read_text(encoding="utf-8")


# ── import silence (§6 lazy package rule / §7.10) ──


def test_import_pulls_no_backends_then_connect_loads_adapters() -> None:
    script = textwrap.dedent(
        """
        import asyncio, sys
        import emuctl
        leaked = sorted(m for m in sys.modules if m.startswith("emuctl.backends"))
        assert leaked == [], f"package import pulled backends: {leaked}"
        print("CLEAN")
        controller = asyncio.run(emuctl.connect("adb:127.0.0.1:5555"))
        loaded = [m for m in sys.modules if m.startswith("emuctl.backends")]
        assert "emuctl.backends._registry" in loaded, loaded
        assert "emuctl.backends.adb" in loaded, loaded
        assert controller.identity == emuctl.InstanceRef(
            backend=emuctl.BackendId.ADB, key="127.0.0.1:5555"
        )
        print("CONNECTED")
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    # No stdout, no stderr, no trace: the package import + connect are silent.
    assert proc.stdout.splitlines() == ["CLEAN", "CONNECTED"]
    assert proc.stderr == "", proc.stderr


# ── construction-time refusals through the package surface (§3) ──


@pytest.mark.parametrize(
    ("target", "exception"),
    [
        ("emulator-5554", InvalidArgumentError),  # bare serial → prefix hint
        ("myvm", InvalidArgumentError),  # bare name
        ("127.0.0.1:5555", InvalidArgumentError),  # bare host:port
        ("foo:bar", InvalidArgumentError),  # unknown head
        ("adb:", InvalidArgumentError),  # empty key
        (123, InvalidArgumentError),  # non-string non-ref
        ("google-play:anything", BackendNotAvailableError),  # spec None
    ],
)
def test_connect_refusals_raise_at_construction(target: Any, exception: type[Exception]) -> None:
    before = sorted(m for m in sys.modules if m.startswith("emuctl.backends."))
    with pytest.raises(exception):
        asyncio.run(emuctl.connect(target))
    after = sorted(m for m in sys.modules if m.startswith("emuctl.backends."))
    # Only the sanctioned registry seam may appear (connect reaches the
    # selection gate through the function-local _registry import); no adapter
    # module ever loads for a refusal.
    assert after == sorted(set(before) | {"emuctl.backends._registry"}), after


def test_removed_backend_refusal_imports_nothing() -> None:
    before = sorted(m for m in sys.modules if m.startswith("emuctl.backends."))
    with pytest.raises(BackendNotAvailableError) as excinfo:
        asyncio.run(emuctl.connect("google-play:anything"))
    assert excinfo.value.reason == "not-implemented-this-version"
    after = sorted(m for m in sys.modules if m.startswith("emuctl.backends."))
    assert after == sorted(set(before) | {"emuctl.backends._registry"}), after


# ── fake-scripted adapter through the package surface ──


class _IoFreeDummy(AsyncEmulatorController):
    constructed: list[str] = []

    def __init__(self, ref: InstanceRef, config: EmuctlConfig | None = None) -> None:
        super().__init__(ref)
        self.bound_config = config
        _IoFreeDummy.constructed.append(str(ref))

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        raise AssertionError("construction must never touch the transport")


def _install_fake_module(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    attr: str,
    *,
    instances: tuple[Any, ...] = (),
) -> None:
    module = types.ModuleType(module_name)
    module.__dict__[attr] = _IoFreeDummy

    async def list_instances(config: EmuctlConfig) -> tuple[Any, ...]:
        assert isinstance(config, EmuctlConfig)
        return tuple(instances)

    async def get_instance(config: EmuctlConfig, ref: InstanceRef) -> Any:
        assert isinstance(config, EmuctlConfig) and isinstance(ref, InstanceRef)
        return None

    module.__dict__["list_instances"] = list_instances
    module.__dict__["get_instance"] = get_instance
    monkeypatch.setitem(sys.modules, module_name, module)


def _adb_record(key: str) -> Any:
    from emuctl._identification import InstanceRecord

    return InstanceRecord(
        ref=InstanceRef(backend=BackendId.ADB, key=key),
        running=True,
        serial=key,
        details={"transport": "adb"},
    )


def test_connect_binds_identity_and_touches_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    cfg = EmuctlConfig()
    _IoFreeDummy.constructed = []
    first = asyncio.run(emuctl.connect("adb:emulator-5554", config=cfg))
    second = asyncio.run(emuctl.connect(InstanceRef(backend=BackendId.ADB, key="emulator-5554")))
    assert isinstance(first, _IoFreeDummy) and isinstance(second, _IoFreeDummy)
    assert first is not second
    assert first.identity == second.identity
    assert first.bound_config is cfg
    assert second.bound_config == EmuctlConfig()
    assert _IoFreeDummy.constructed == ["adb:emulator-5554", "adb:emulator-5554"]


def test_connect_config_gate_refuses_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    gated = EmuctlConfig(display=DisplayConfig(width=800, height=600))
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(emuctl.connect("adb:emulator-5554", config=gated))
    assert excinfo.value.feature == "configure"


def test_discover_rows_via_package_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    from emuctl._identification import InstanceRecord

    rows = tuple(
        InstanceRecord(
            ref=InstanceRef(backend=BackendId.ADB, key=key),
            running=True,
            serial=key,
            details={"transport": "adb"},
        )
        for key in ("emulator-5554", "127.0.0.1:5555")
    )
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController", instances=rows)
    report = asyncio.run(emuctl.discover([BackendId.ADB]))
    assert isinstance(report, DiscoveryReport)
    (row,) = report.backends
    assert isinstance(row, BackendStatus)
    assert row.available is True and row.reason == "ok"
    assert [str(record.ref) for record in row.instances] == [
        "adb:emulator-5554",
        "adb:127.0.0.1:5555",
    ]
    import json

    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["backends"][0]["instances"][0]["ref"] == "adb:emulator-5554"


# ── logging discipline survives the public surface (§2/§7.4) ──


def test_package_logger_carries_exactly_the_null_handler() -> None:
    package_logger = logging.getLogger("emuctl")
    nulls = [h for h in package_logger.handlers if isinstance(h, logging.NullHandler)]
    assert len(package_logger.handlers) == 1 and len(nulls) == 1

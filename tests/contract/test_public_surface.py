"""§7 conformance (public surface, §1/§3/§4/§5/§6): the package-level export
list, import silence (no backend modules at `import emuctl`, adapters load
only when connect()/discover() touch the registry), construction-time typed
refusals through the package surface, the stale-identity §3 behavior on ops,
the multi-instance concurrency promise, and the enumeration/deferred-surface
boundaries."""

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
from emuctl._platforms import Platform
from emuctl.config.model import DisplayConfig, EmuctlConfig, MemuConfig
from emuctl.errors import (
    BackendNotAvailableError,
    FeatureNotSupportedError,
    InstanceNotFoundError,
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
        "InvalidSerialError",
        "ExtrasMissingError",
        "ExecutableNotFoundError",
        "ConfigError",
        "ConfigValidationError",
        "OperationTimeoutError",
        "VendorCommandError",
        "DeviceStateError",
        "InstanceNotFoundError",
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
    """Leave no `emuctl.backends*` trace: package-surface tests import the
    registry at runtime and predecessor tests own the no-leak invariant."""
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
    """discover() is the ONLY enumeration path (§1 invariant 11, §3 Boundary):
    registry/selection functions never leak onto the package."""
    for name in FORBIDDEN_EXPORTS:
        assert not hasattr(emuctl, name), name
        assert name not in emuctl.__all__, name


def test_multi_instance_promise_wording_is_pinned() -> None:
    assert MULTI_INSTANCE_PROMISE in CONTRACT_PATH.read_text(encoding="utf-8")


# ── import silence (§6 lazy package rule / §7.10) ──


def test_import_pulls_no_backends_then_connect_loads_adapters() -> None:
    """Fresh interpreter: `import emuctl` imports ZERO emuctl.backends* modules
    (and prints nothing); calling connect() resolves the registry and imports
    the adapter lazily — and nothing else."""
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
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
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
    """Selection refusals raise HERE, at construction (§3/§5) — canonical
    targets only, no deferred first-call I/O failure, no shape inference.
    Every refusal fires before load_adapter, so no adapter module appears."""
    before = sorted(m for m in sys.modules if m.startswith("emuctl.backends."))
    with pytest.raises(exception):
        asyncio.run(emuctl.connect(target))
    after = sorted(m for m in sys.modules if m.startswith("emuctl.backends."))
    # Only the sanctioned registry seam may appear (connect reaches the
    # selection gate through the function-local _registry import); no adapter
    # module ever loads for a refusal.
    assert after == sorted(set(before) | {"emuctl.backends._registry"}), after


def test_connect_platform_refusal_imports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec-resolved refusal on the wrong host: platform →
    BackendNotAvailableError("platform-unsupported") before any import, with
    the selection order surviving package-level access (registry gate first)."""
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: False)
    before = set(sys.modules)
    with pytest.raises(BackendNotAvailableError) as excinfo:
        asyncio.run(emuctl.connect("memu:pyclashbot-136"))
    assert excinfo.value.reason == "platform-unsupported"
    assert [
        name for name in sys.modules if name not in before and name.startswith("emuctl.backends.")
    ] == []


# ── fake-scripted adapter through the package surface ──


class _IoFreeDummy(AsyncEmulatorController):
    """Port subclass binding the resolved config; construction records the
    binding and never touches the transport."""

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
    """Shadow an adapter module with a BackendDiscovery-shaped fake (same seam
    as test_discovery.py: a fake module in sys.modules satisfies load_adapter's
    import_module lookup)."""
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
    """Construction is cheap (§3/§5): ref + config bound, zero transport I/O,
    and two connects for the same target are INDEPENDENT controllers (§3's
    multi-instance promise — no shared state, no registry, no locks)."""
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
    """§4's config↔capability gate runs AT CONSTRUCTION through the package
    surface: plain ADB never declares CONFIGURE, so [display] is a typed
    FeatureNotSupportedError before any I/O — never silent-ignore."""
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    gated = EmuctlConfig(display=DisplayConfig(width=800, height=600))
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(emuctl.connect("adb:emulator-5554", config=gated))
    assert excinfo.value.feature == "configure"


def test_discover_rows_via_package_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """discover() through the package enumerates the scripted fake's rows,
    isolated per backend, with the JSON-safe projection intact (§5's CLI
    shape is fed from this surface)."""
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


# ── stale identity: InstanceNotFoundError surfaces on ops (§3) ──


class _FakeResult:
    returncode = 0
    stdout = b""
    stderr = b""


class _FakeExecutor:
    """Scripted memuc executor: `listvms` yields NO rows (the VM vanished)."""

    def __init__(self, calls: list[list[str]]) -> None:
        self._calls = calls

    async def run(
        self, argv: Sequence[str], *, operation: str, timeout: float | None
    ) -> _FakeResult:
        self._calls.append(list(argv))
        return _FakeResult()


def _scripted_memu(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]
) -> tuple[Any, EmuctlConfig]:
    """Windows-host predicate plus the scripted memuc transport key. Platform
    truth is the registry's, patched exactly like test_discovery/test_registry;
    config sets the explicit memuc path so the transport seam resolves without
    touching the host."""
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: True)
    memu = importlib.import_module("emuctl.backends.memu")
    monkeypatch.setattr(memu, "ProcessExecutor", lambda: _FakeExecutor(calls))
    cfg = EmuctlConfig(memu=MemuConfig(memuc_path="memuc"))
    return memu, cfg


def test_stale_identity_raises_instance_not_found_on_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3 identity lifetime: delete/disconnect ⇒ the ref goes stale ⇒ ops
    raise InstanceNotFoundError (payload = canonical str(ref)) — through the
    REAL MEmu adapter scripted at the _proc seam. Construction bound the ref
    with zero vendor I/O (cheap construction); ONLY the op observes that the
    listvms row vanished, and the typed refusal fires BEFORE any argv runs."""
    calls: list[list[str]] = []
    _, cfg = _scripted_memu(monkeypatch, calls)

    controller = asyncio.run(emuctl.connect("memu:pyclashbot-136", config=cfg))
    assert calls == []  # construction is cheap discovery — no vendor I/O
    with pytest.raises(InstanceNotFoundError) as excinfo:
        asyncio.run(controller.screenshot())
    assert excinfo.value.instance_ref == "memu:pyclashbot-136"
    # Exactly the stale-ref observation ran (one bounded listvms row fetch —
    # the transport lookup), and NO boot/interaction argv followed.
    assert len(calls) == 1 and "listvms" in calls[0]


def test_stale_identity_is_exported_and_renderable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The taxonomy pin (§2/§7.1): the error CLASS is public; the payload
    carries the canonical ref string (never the object); rendering is stable."""
    _scripted_memu(monkeypatch, [])
    controller = asyncio.run(
        emuctl.connect(
            "memu:pyclashbot-136",
            config=EmuctlConfig(memu=MemuConfig(memuc_path="memuc")),
        )
    )
    with pytest.raises(emuctl.InstanceNotFoundError) as excinfo:
        asyncio.run(controller.screenshot())
    exc = excinfo.value
    assert exc.error_code == "instance_not_found"
    assert exc.instance_ref == "memu:pyclashbot-136"
    assert "memu:pyclashbot-136" in str(exc)


# ── logging discipline survives the public surface (§2/§7.4) ──


def test_package_logger_carries_exactly_the_null_handler() -> None:
    package_logger = logging.getLogger("emuctl")
    nulls = [h for h in package_logger.handlers if isinstance(h, logging.NullHandler)]
    assert len(package_logger.handlers) == 1 and len(nulls) == 1

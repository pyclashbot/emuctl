"""§7 conformance (discovery, §3): deterministic ordering, per-backend
isolation, offline probe-skipping, canonical-only connect with
construction-time refusals, and spec-driven key validation."""

import asyncio
import dataclasses
import json
import sys
import types
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from emuctl._discovery import (
    BACKEND_STATUS_REASONS,
    BackendStatus,
    DiscoveryReport,
    connect,
    discover,
)
from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl._platforms import Platform
from emuctl.config.model import DisplayConfig, EmuctlConfig
from emuctl.errors import (
    BackendNotAvailableError,
    BackendNotFoundError,
    ExtrasMissingError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    InvalidSerialError,
)
from emuctl.port import AsyncEmulatorController


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    """Leave no `emuctl.backends*` trace: connect()/discover() import the
    registry at runtime, and these tests run before `test_identity`'s
    no-backend-modules invariant — evict every runtime trace afterwards.

    Eviction also clears the parent-package attribute aliases: pytest's
    string-target `setattr` resolves `a.b.c` by walking attributes from the
    top package, while real imports resolve through `sys.modules` — a
    sys.modules-only eviction leaves ghost aliases behind and patches land
    on objects the code under test never reads.
    """
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n.startswith("emuctl.backends")]:
        if name not in before:
            evicted = sys.modules.pop(name)
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, child, None) is evicted:
                delattr(parent, child)


class _ConfigDummy(AsyncEmulatorController):
    """Port subclass binding the resolved config (the connect() convention)."""

    def __init__(self, ref: InstanceRef, config: EmuctlConfig | None = None) -> None:
        super().__init__(ref)
        self.bound_config = config

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        return b""


def _install_fake_module(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    attr: str,
    *,
    instances: tuple[InstanceRecord, ...] = (),
    probe_error: BaseException | None = None,
) -> None:
    """Shadow a stub adapter module with a BackendDiscovery-shaped fake."""
    module = types.ModuleType(module_name)
    module.__dict__[attr] = _ConfigDummy

    async def list_instances(config: EmuctlConfig) -> tuple[InstanceRecord, ...]:
        assert isinstance(config, EmuctlConfig)
        if probe_error is not None:
            raise probe_error
        return tuple(instances)

    async def get_instance(config: EmuctlConfig, ref: InstanceRef) -> InstanceRecord | None:
        assert isinstance(config, EmuctlConfig)
        assert isinstance(ref, InstanceRef)
        return None

    module.__dict__["list_instances"] = list_instances
    module.__dict__["get_instance"] = get_instance
    monkeypatch.setitem(sys.modules, module_name, module)


@pytest.fixture
def windows_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pretend Windows with all deps present: every spec row resolves.

    Returns the registry module object; patching is object-form (no dotted
    string resolution) so the patch provably lands on the module the code
    under test reads.
    """
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: True)
    return _registry


@pytest.fixture
def macos_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pretend macOS (object-form patching, see `windows_host`)."""
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.MACOS)
    return _registry


def _adb_record(key: str = "emulator-5554") -> InstanceRecord:
    return InstanceRecord(
        ref=InstanceRef(backend=BackendId.ADB, key=key),
        running=True,
        serial=key,
        details={"transport": "adb"},
    )


def test_row_reasons_pinned() -> None:
    assert (
        frozenset(
            {
                "ok",
                "offline",
                "not-implemented-this-version",
                "platform-unsupported",
                "extras-missing",
                "adapter-unavailable",
                "probe-failed",
            }
        )
        == BACKEND_STATUS_REASONS
    )


def test_discover_default_order_is_by_backend_id(windows_host: Any) -> None:
    report = asyncio.run(discover(offline=True))
    assert [status.backend for status in report.backends] == sorted(
        BackendId, key=lambda item: item.value
    )
    assert report.offline is True


def test_discover_subset_sorted_and_deduped(windows_host: Any) -> None:
    report = asyncio.run(discover([BackendId.MEMU, "adb", BackendId.MEMU], offline=True))
    assert [status.backend for status in report.backends] == [BackendId.ADB, BackendId.MEMU]


@pytest.mark.parametrize("backends", [["nope"], ["adb", "nope"], [123]])
def test_discover_unknown_ids_raise(backends: list[Any], windows_host: Any) -> None:
    with pytest.raises(BackendNotFoundError):
        asyncio.run(discover(backends))


@pytest.mark.parametrize("scalar", ["adb", BackendId.ADB])
def test_discover_refuses_a_bare_scalar_selector(
    scalar: str | BackendId, windows_host: Any
) -> None:
    """§3 pins NO scalar form for `backends` (sequence-or-None only). A str is
    itself iterable, so without the typed refusal `discover("adb")` would
    silently decompose into an empty selection — _discovery's ``_wanted_ids``
    raises the typed ``InvalidArgumentError`` instead (§3's pinned signature
    reads)."""
    with pytest.raises(InvalidArgumentError) as excinfo:
        asyncio.run(discover(scalar))
    assert excinfo.value.parameter == "backends"


def test_discover_offline_probes_nothing(
    windows_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline performs no probes AND no enumeration: load_adapter is never
    # reached, rows report resolved-but-not-probed. (§3's parenthetical about
    # config-store enumeration still running is deferred errata — it needs an
    # offline knob on the BackendDiscovery seam first.)
    calls: list[str] = []
    registry = windows_host

    real_load_adapter = registry.load_adapter

    def counting(spec: Any) -> Any:
        calls.append(spec.module)
        return real_load_adapter(spec)

    monkeypatch.setattr(registry, "load_adapter", counting)
    report = asyncio.run(discover(offline=True))
    assert calls == []
    by_id = {status.backend: status for status in report.backends}
    # No single host resolves every row: Windows resolves adb/memu/bs5 while
    # Air (macOS-only) reports its platform refusal; unimplemented rows stay.
    for backend in (BackendId.ADB, BackendId.MEMU, BackendId.BLUESTACKS):
        assert by_id[backend].available is True
        assert by_id[backend].reason == "offline"
        assert by_id[backend].instances == ()
    assert by_id[BackendId.BLUESTACKS_AIR].available is False
    assert by_id[BackendId.BLUESTACKS_AIR].reason == "platform-unsupported"
    for backend in (BackendId.GOOGLE_PLAY, BackendId.AVD):
        assert by_id[backend].available is False
        assert by_id[backend].reason == "not-implemented-this-version"


def test_discover_online_reports_stub_adapters_honestly(
    windows_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Remaining adapter modules are docstring stubs: the online path must
    # report "adapter-unavailable" rows, never raise. The ADB row is live:
    # §7 pins the live tier as opt-in/marked and NEVER required, so its
    # enumeration runs fully offline against a scripted executor (same
    # _spawn seam as test_adb.py) — identical on machines with and without
    # a real `adb` binary.
    import importlib

    from emuctl._proc import ProcessExecutor

    calls: list[list[str]] = []
    fixture = (
        b"* daemon not running; starting now at tcp:5037\n"
        b"* daemon started successfully\n"
        b"List of devices attached\n"
        b"emulator-5554\tdevice\n"
        b"127.0.0.1:5555\tdevice\n"
        b"ABCDEF123456\toffline\n"
        b"ZYX987\tunauthorized\n"
    )
    memuc_fixture = (
        b"read failed\n"
        b"0,MEmu 1,Default Android,0,0\n"
        b"1,pyclashbot-136,Default Medium Android,1,4200\n"
        b"garbage without commas\n"
    )

    class _FakeHandle:
        def __init__(self) -> None:
            self.returncode: int | None = 0

        @property
        def pid(self) -> int | None:
            return 4242

        def terminate(self) -> None: ...
        def kill(self) -> None: ...

        async def wait(self) -> int:
            return 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return (memuc_fixture if any("listvms" in a for a in calls[-1]) else fixture, b"")

    async def _spawn(argv: Sequence[str]) -> _FakeHandle:
        calls.append(list(argv))
        return _FakeHandle()

    def _scripted_executor() -> ProcessExecutor:
        return ProcessExecutor(_spawn=_spawn)  # type: ignore[arg-type]

    adb_module = importlib.import_module("emuctl.backends.adb")
    monkeypatch.setattr(adb_module, "ProcessExecutor", _scripted_executor)
    memu_module = importlib.import_module("emuctl.backends.memu")
    monkeypatch.setattr(memu_module, "ProcessExecutor", _scripted_executor)
    # The BlueStacks family rows are LIVE conf-FILE readers now (no vendor
    # stdout parsing — files are sanctioned, stdout prose is not): script the
    # path resolution to an honest no-candidate store so the family rows read
    # deterministically offline (same scripted-fake shape as the ADB/MEmu
    # fixtures above — no real BlueStacks install is consulted).
    stub_paths = types.ModuleType("_stub_paths")
    stub_paths.__dict__["conf_path"] = lambda cfg, platform=None: None
    stub_paths.__dict__["mim_meta_path"] = lambda cfg, platform=None: None
    for backed in ("emuctl.backends.bluestacks.bs5", "emuctl.backends.bluestacks.air"):
        sys.modules[backed] = importlib.import_module(backed)
        monkeypatch.setattr(sys.modules[backed], "_paths", stub_paths)
    report = asyncio.run(discover())
    assert report.offline is False
    by_id = {status.backend: status for status in report.backends}
    # The ADB row: probed offline, banner lines skipped, rows honest.
    assert by_id[BackendId.ADB].available is True
    assert by_id[BackendId.ADB].reason == "ok"
    assert [str(r.ref) for r in by_id[BackendId.ADB].instances] == [
        "adb:emulator-5554",
        "adb:127.0.0.1:5555",
        "adb:ABCDEF123456",
        "adb:ZYX987",
    ]
    assert [r.running for r in by_id[BackendId.ADB].instances] == [True, True, False, False]
    assert len(calls) == 2  # scripted, offline: adb devices + memuc listvms
    assert calls[0][-1] == "devices" and "listvms" in calls[1]
    assert all("daemon" not in str(row.ref) for row in by_id[BackendId.ADB].instances)
    # The BlueStacks 5 row is a live conf-file reader: honest empty enumeration
    # on the scripted no-candidate store (never adapter-unavailable, never a
    # vendor probe; run state is conf-invisible → rows would set running False).
    assert by_id[BackendId.BLUESTACKS].available is True
    assert by_id[BackendId.BLUESTACKS].reason == "ok"
    assert by_id[BackendId.BLUESTACKS].instances == ()
    # MEMU is now a live adapter: its enumeration reads listvms rows
    # offline — name-keyed refs, running from the running column only,
    # unparseable lines ("read failed", comma-less noise) skipped.
    assert by_id[BackendId.MEMU].available is True
    assert by_id[BackendId.MEMU].reason == "ok"
    assert [str(r.ref) for r in by_id[BackendId.MEMU].instances] == [
        "memu:MEmu 1",
        "memu:pyclashbot-136",
    ]
    assert [r.running for r in by_id[BackendId.MEMU].instances] == [False, True]
    assert [r.details["index"] for r in by_id[BackendId.MEMU].instances] == ["0", "1"]
    # Remaining docstring stubs report honestly; Air is macOS-only so a
    # Windows host reports its platform refusal instead.
    assert by_id[BackendId.BLUESTACKS_AIR].reason == "platform-unsupported"
    for backend in (BackendId.GOOGLE_PLAY, BackendId.AVD):
        assert by_id[backend].reason == "not-implemented-this-version"


def test_discover_row_per_refusal_mode(macos_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS host: memu is platform-gated; air RESOLVES now but its conf-file
    # enumeration reads through scripted no-candidate paths (offline honesty
    # — no real /Applications/BlueStacks.app is consulted).
    importlib = __import__("importlib")
    for name in ("emuctl.backends.memu", "emuctl.backends.bluestacks.air"):
        sys.modules.pop(name, None)
    stub_paths = types.ModuleType("_stub_paths")
    stub_paths.__dict__["conf_path"] = lambda cfg, platform=None: None
    stub_paths.__dict__["mim_meta_path"] = lambda cfg, platform=None: None
    air = importlib.import_module("emuctl.backends.bluestacks.air")
    monkeypatch.setattr(air, "_paths", stub_paths)
    report = asyncio.run(discover([BackendId.MEMU, BackendId.BLUESTACKS_AIR]))
    by_id = {status.backend: status for status in report.backends}
    assert by_id[BackendId.MEMU].reason == "platform-unsupported"
    assert by_id[BackendId.MEMU].available is False
    assert by_id[BackendId.BLUESTACKS_AIR].reason == "ok"
    assert by_id[BackendId.BLUESTACKS_AIR].available is True
    assert by_id[BackendId.BLUESTACKS_AIR].instances == ()


def test_discover_convention_violation_is_adapter_unavailable(
    windows_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Valid port-subclass attr but NO `list_instances` member: the
    # BackendDiscovery convention is violated, so the row is
    # adapter-unavailable (never a raise, never a probe).
    module = types.ModuleType("emuctl.backends.adb")
    module.__dict__["AdbController"] = _ConfigDummy

    async def get_instance(config: EmuctlConfig, ref: InstanceRef) -> InstanceRecord | None:
        assert isinstance(config, EmuctlConfig)
        assert isinstance(ref, InstanceRef)
        return None

    module.__dict__["get_instance"] = get_instance
    assert "list_instances" not in module.__dict__
    monkeypatch.setitem(sys.modules, "emuctl.backends.adb", module)
    report = asyncio.run(discover([BackendId.ADB]))
    (row,) = report.backends
    assert row.available is False
    assert row.reason == "adapter-unavailable"
    assert "(BackendDiscovery convention)" in (row.detail or "")


def test_discover_per_backend_isolation(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # One backend enumerates, one backend's probe explodes: rows, not failure.
    _install_fake_module(
        monkeypatch, "emuctl.backends.adb", "AdbController", instances=(_adb_record(),)
    )
    _install_fake_module(
        monkeypatch,
        "emuctl.backends.memu",
        "MemuController",
        probe_error=RuntimeError("memuc is dead"),
    )
    report = asyncio.run(discover([BackendId.ADB, BackendId.MEMU]))
    by_id = {status.backend: status for status in report.backends}
    adb = by_id[BackendId.ADB]
    assert adb.available is True
    assert adb.reason == "ok"
    assert adb.instances == (_adb_record(),)
    memu = by_id[BackendId.MEMU]
    assert memu.available is False
    assert memu.reason == "probe-failed"
    assert memu.instances == ()
    assert "memuc is dead" in (memu.detail or "")


def test_discover_report_is_json_safe(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(
        monkeypatch, "emuctl.backends.adb", "AdbController", instances=(_adb_record(),)
    )
    report = asyncio.run(discover([BackendId.ADB, BackendId.GOOGLE_PLAY]))
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["offline"] is False
    assert [row["backend"] for row in payload["backends"]] == ["adb", "google-play"]
    assert payload["backends"][0]["instances"][0]["ref"] == "adb:emulator-5554"
    assert payload["backends"][0]["instances"][0]["details"] == {"transport": "adb"}


def test_status_and_report_are_frozen() -> None:
    status = BackendStatus(backend=BackendId.ADB, available=True, reason="ok")
    report = DiscoveryReport(backends=(status,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        status.available = False  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.offline = True  # ty: ignore[invalid-assignment]
    with pytest.raises(InvalidArgumentError):
        BackendStatus(backend=BackendId.ADB, available=True, reason="bogus")


@pytest.mark.parametrize("target", ["emulator-5554", "myvm", "127.0.0.1:5555", "foo:bar"])
def test_connect_rejects_bare_and_unknown_shapes(target: str) -> None:
    with pytest.raises(InvalidArgumentError) as excinfo:
        asyncio.run(connect(target))
    assert excinfo.value.parameter == "target"


@pytest.mark.parametrize("target", ["adb:", "adb:   "])
def test_connect_rejects_empty_keys(target: str) -> None:
    with pytest.raises(InvalidArgumentError):
        asyncio.run(connect(target))


def test_connect_rejects_non_string_targets() -> None:
    with pytest.raises(InvalidArgumentError) as excinfo:
        asyncio.run(connect(123))  # ty: ignore[invalid-argument-type]
    assert excinfo.value.parameter == "target"


def test_connect_refusal_not_implemented() -> None:
    with pytest.raises(BackendNotAvailableError) as excinfo:
        asyncio.run(connect("google-play:anything"))
    assert excinfo.value.reason == "not-implemented-this-version"


def test_connect_refusal_platform(macos_host: Any) -> None:
    with pytest.raises(BackendNotAvailableError) as excinfo:
        asyncio.run(connect(InstanceRef(backend=BackendId.MEMU, key="pyclashbot-136")))
    assert excinfo.value.reason == "platform-unsupported"


def test_connect_refusal_missing_dep(monkeypatch: pytest.MonkeyPatch) -> None:
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.WINDOWS)
    # Hermetic: simulate the missing [memu] extra via the registry's
    # dep-probe seam, independent of the ambient package store.
    monkeypatch.setattr(_registry, "_dep_available", lambda name: False)
    with pytest.raises(ExtrasMissingError) as excinfo:
        asyncio.run(connect("memu:pyclashbot-136"))
    assert excinfo.value.extra == "memu"


@pytest.mark.parametrize(
    ("platform", "target", "exc"),
    [
        (Platform.LINUX, "google-play:anything", BackendNotAvailableError),
        (Platform.MACOS, "memu:pyclashbot-136", BackendNotAvailableError),
        (Platform.WINDOWS, "memu:pyclashbot-136", ExtrasMissingError),
        (Platform.LINUX, "adb:has space", InvalidSerialError),
        (Platform.LINUX, "emulator-5554", InvalidArgumentError),
    ],
)
def test_connect_refusal_imports_no_adapter(
    monkeypatch: pytest.MonkeyPatch, platform: Platform, target: str, exc: type[Exception]
) -> None:
    """Every selection refusal fires before any adapter import: no
    `emuctl.backends.*` module appears in `sys.modules` during the refusal."""
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: platform)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: False)
    before = set(sys.modules)
    with pytest.raises(exc):
        asyncio.run(connect(target))
    assert [
        name for name in sys.modules if name not in before and name.startswith("emuctl.backends.")
    ] == []


def test_connect_validates_adb_keys_against_serial_allowlist() -> None:
    with pytest.raises(InvalidSerialError):
        asyncio.run(connect("adb:has space"))


def test_connect_validates_vendor_keys_via_spec(windows_host: Any) -> None:
    with pytest.raises(InvalidArgumentError) as excinfo:
        asyncio.run(connect("memu:has\nnewline"))
    assert excinfo.value.parameter == "key"


def test_connect_binds_resolved_config(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    controller = asyncio.run(connect("adb:emulator-5554"))
    assert controller.identity == InstanceRef(backend=BackendId.ADB, key="emulator-5554")
    assert isinstance(controller, _ConfigDummy)
    assert controller.bound_config == EmuctlConfig()
    cfg = EmuctlConfig()
    again = asyncio.run(
        connect(InstanceRef(backend=BackendId.ADB, key="emulator-5554"), config=cfg)
    )
    assert isinstance(again, _ConfigDummy)
    assert again.bound_config is cfg


def test_connect_runs_config_capability_gate(
    windows_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    gated = EmuctlConfig(display=DisplayConfig(width=800, height=600))
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(connect("adb:emulator-5554", config=gated))
    assert excinfo.value.feature == "configure"


def test_connect_rejects_bad_config_object(
    windows_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    with pytest.raises(InvalidArgumentError) as excinfo:
        asyncio.run(connect("adb:emulator-5554", config="x"))  # ty: ignore[invalid-argument-type]
    assert excinfo.value.parameter == "config"

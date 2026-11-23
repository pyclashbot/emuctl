import asyncio
import json
import sys
import types
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from emuctl._discovery import (
    BACKEND_STATUS_REASONS,
    connect,
    discover,
)
from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl._platforms import Platform
from emuctl.config.model import DisplayConfig, EmuctlConfig
from emuctl.errors import (
    BackendNotAvailableError,
    BackendNotFoundError,
    InvalidArgumentError,
    InvalidSerialError,
)
from emuctl.port import AsyncEmulatorController, CapabilityFlag, DisplayState


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


class _ConfigDummy(AsyncEmulatorController):
    FEATURES = frozenset({CapabilityFlag.CONFIGURE})

    def __init__(self, ref: InstanceRef, config: EmuctlConfig | None = None) -> None:
        super().__init__(ref)
        self.bound_config = config

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        return b""

    async def _configure(self, spec: Any, timeout: float | None) -> DisplayState:
        return DisplayState(None, None, None)


def _install_fake_module(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    attr: str,
    *,
    instances: tuple[InstanceRecord, ...] = (),
    probe_error: BaseException | None = None,
) -> None:
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
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: True)
    return _registry


@pytest.fixture
def macos_host(monkeypatch: pytest.MonkeyPatch) -> Any:
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
    # Default discovery walks the registry: v0.1 ships ADB only.
    assert [status.backend for status in report.backends] == [BackendId.ADB]
    assert report.offline is True


def test_discover_subset_sorted_and_deduped(windows_host: Any) -> None:
    report = asyncio.run(discover([BackendId.GOOGLE_PLAY, "adb", BackendId.GOOGLE_PLAY], offline=True))
    assert [status.backend for status in report.backends] == [
        BackendId.ADB,
        BackendId.GOOGLE_PLAY,
    ]


@pytest.mark.parametrize("backends", [["nope"], ["adb", "nope"], [123]])
def test_discover_unknown_ids_raise(backends: list[Any], windows_host: Any) -> None:
    with pytest.raises(BackendNotFoundError):
        asyncio.run(discover(backends))


def test_discover_offline_probes_nothing(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert by_id[BackendId.ADB].available is True
    assert by_id[BackendId.ADB].reason == "offline"
    assert by_id[BackendId.ADB].instances == ()


def test_discover_online_adb_rows(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
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
    )

    class _FakeHandle:
        returncode: int | None = 0

        @property
        def pid(self) -> int | None:
            return 4242

        def terminate(self) -> None: ...
        def kill(self) -> None: ...

        async def wait(self) -> int:
            return 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return (fixture, b"")

    async def _spawn(argv: Sequence[str]) -> _FakeHandle:
        calls.append(list(argv))
        return _FakeHandle()

    adb_module = importlib.import_module("emuctl.backends.adb")
    monkeypatch.setattr(
        adb_module,
        "ProcessExecutor",
        lambda: ProcessExecutor(_spawn=_spawn),  # type: ignore[arg-type]
    )
    report = asyncio.run(discover())
    by_id = {status.backend: status for status in report.backends}
    assert by_id[BackendId.ADB].available is True
    assert by_id[BackendId.ADB].reason == "ok"
    assert [str(r.ref) for r in by_id[BackendId.ADB].instances] == [
        "adb:emulator-5554",
        "adb:127.0.0.1:5555",
        "adb:ABCDEF123456",
    ]
    assert [r.running for r in by_id[BackendId.ADB].instances] == [True, True, False]
    assert len(calls) == 1 and calls[0][-1] == "devices"


def test_discover_removed_rows_are_not_implemented(macos_host: Any) -> None:
    report = asyncio.run(discover([BackendId.ADB, BackendId.MEMU, BackendId.BLUESTACKS_AIR], offline=True))
    by_id = {status.backend: status for status in report.backends}
    assert by_id[BackendId.ADB].reason == "offline"
    assert by_id[BackendId.MEMU].reason == "not-implemented-this-version"
    assert by_id[BackendId.BLUESTACKS_AIR].reason == "not-implemented-this-version"


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


def test_discover_isolation_adb_keeps_working(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController", instances=(_adb_record(),))
    report = asyncio.run(discover([BackendId.ADB, BackendId.MEMU]))
    by_id = {status.backend: status for status in report.backends}
    assert by_id[BackendId.ADB].available is True
    assert by_id[BackendId.ADB].instances == (_adb_record(),)
    assert by_id[BackendId.MEMU].reason == "not-implemented-this-version"


def test_discover_report_is_json_safe(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController", instances=(_adb_record(),))
    report = asyncio.run(discover([BackendId.ADB, BackendId.GOOGLE_PLAY]))
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["offline"] is False
    assert [row["backend"] for row in payload["backends"]] == ["adb", "google-play"]
    assert payload["backends"][0]["instances"][0]["ref"] == "adb:emulator-5554"
    assert payload["backends"][0]["instances"][0]["details"] == {"transport": "adb"}


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


def test_connect_refusal_removed_backend() -> None:
    with pytest.raises(BackendNotAvailableError) as excinfo:
        asyncio.run(connect(InstanceRef(backend=BackendId.MEMU, key="pyclashbot-136")))
    assert excinfo.value.reason == "not-implemented-this-version"


@pytest.mark.parametrize(
    ("platform", "target", "exc"),
    [
        (Platform.LINUX, "google-play:anything", BackendNotAvailableError),
        (Platform.MACOS, "memu:pyclashbot-136", BackendNotAvailableError),
        (Platform.WINDOWS, "bluestacks:any", BackendNotAvailableError),
        (Platform.LINUX, "adb:has space", InvalidSerialError),
        (Platform.LINUX, "emulator-5554", InvalidArgumentError),
    ],
)
def test_connect_refusal_imports_no_adapter(
    monkeypatch: pytest.MonkeyPatch, platform: Platform, target: str, exc: type[Exception]
) -> None:
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: platform)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: False)
    before = set(sys.modules)
    with pytest.raises(exc):
        asyncio.run(connect(target))
    assert [name for name in sys.modules if name not in before and name.startswith("emuctl.backends.")] == []


def test_connect_validates_adb_keys_against_serial_allowlist() -> None:
    with pytest.raises(InvalidSerialError):
        asyncio.run(connect("adb:has space"))


def test_connect_binds_resolved_config(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    controller = asyncio.run(connect("adb:emulator-5554"))
    assert controller.identity == InstanceRef(backend=BackendId.ADB, key="emulator-5554")
    assert isinstance(controller, _ConfigDummy)
    assert controller.bound_config == EmuctlConfig()
    cfg = EmuctlConfig()
    again = asyncio.run(connect(InstanceRef(backend=BackendId.ADB, key="emulator-5554"), config=cfg))
    assert isinstance(again, _ConfigDummy)
    assert again.bound_config is cfg


def test_connect_accepts_display_for_adb_configure(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    gated = EmuctlConfig(display=DisplayConfig(width=800, height=600))
    controller = asyncio.run(connect("adb:emulator-5554", config=gated))
    assert isinstance(controller, _ConfigDummy)


def test_connect_rejects_bad_config_object(windows_host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_module(monkeypatch, "emuctl.backends.adb", "AdbController")
    with pytest.raises(InvalidArgumentError) as excinfo:
        asyncio.run(connect("adb:emulator-5554", config="x"))  # ty: ignore[invalid-argument-type]
    assert excinfo.value.parameter == "config"

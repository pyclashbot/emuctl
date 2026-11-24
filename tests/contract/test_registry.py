import sys
import types
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._platforms import Platform
from emuctl.errors import (
    BackendNotAvailableError,
    BackendNotFoundError,
    ExtrasMissingError,
    InvalidArgumentError,
    InvalidSerialError,
)
from emuctl.port import AsyncEmulatorController

ADB_MODULE = "emuctl.backends.adb"

#: Removed vendor adapters/deps: v0.1 has no modules and must never import them.
REMOVED_MODULES = (
    "emuctl.backends.memu",
    "emuctl.backends.bluestacks.bs5",
    "emuctl.backends.avd",
    "pymemuc",
)

NEVER_IMPORT = (ADB_MODULE, *REMOVED_MODULES)


@pytest.fixture
def reg() -> Any:
    from emuctl.backends import _registry

    return _registry


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


@pytest.fixture
def no_backend_imports(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    stashed: dict[str, Any] = {}
    for name in NEVER_IMPORT:
        if name in sys.modules:
            stashed[name] = sys.modules.pop(name)
    for name in NEVER_IMPORT:
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield
    for name, module in stashed.items():
        sys.modules[name] = module


def test_pinned_backend_ids_survive_trimming() -> None:
    assert {item.value for item in BackendId} == {
        "adb",
        "memu",
        "bluestacks",
        "google-play",
        "avd",
        "bluestacks-air",
    }


def test_registry_has_the_adb_bluestacks_air_and_google_play_rows(reg: Any) -> None:
    registry = reg.get_registry()
    assert set(registry) == {BackendId.ADB, BackendId.BLUESTACKS_AIR, BackendId.GOOGLE_PLAY}
    assert isinstance(registry[BackendId.ADB], reg.BackendSpec)
    assert isinstance(registry[BackendId.BLUESTACKS_AIR], reg.BackendSpec)
    assert isinstance(registry[BackendId.GOOGLE_PLAY], reg.BackendSpec)


def test_bluestacks_air_spec_is_pinned(reg: Any) -> None:
    spec = reg.get_registry()[BackendId.BLUESTACKS_AIR]
    assert spec.backend is BackendId.BLUESTACKS_AIR
    assert spec.extra == "bluestacks"
    assert spec.module == "emuctl.backends.bluestacks.air"
    assert spec.adapter_attr == "BlueStacksAirController"
    assert spec.supported_platforms == frozenset({Platform.MACOS})
    assert spec.dep_modules == ()
    assert spec.description.strip() != ""
    assert spec.key_semantics.strip() != ""
    assert callable(spec.validate_key)


def test_google_play_spec_is_pinned(reg: Any) -> None:
    spec = reg.get_registry()[BackendId.GOOGLE_PLAY]
    assert spec.backend is BackendId.GOOGLE_PLAY
    assert spec.extra == "google-play"
    assert spec.module == "emuctl.backends.google_play"
    assert spec.adapter_attr == "GooglePlayController"
    assert spec.supported_platforms == frozenset({Platform.WINDOWS})
    assert spec.dep_modules == ()
    assert spec.description.strip() != ""
    assert spec.key_semantics.strip() != ""
    assert callable(spec.validate_key)


def test_adb_spec_is_pinned(reg: Any) -> None:
    spec = reg.get_registry()[BackendId.ADB]
    assert spec.backend is BackendId.ADB
    assert spec.extra is None
    assert spec.module == ADB_MODULE
    assert spec.adapter_attr == "AdbController"
    assert spec.supported_platforms == frozenset({Platform.WINDOWS, Platform.MACOS, Platform.LINUX})
    assert spec.dep_modules == ()
    assert spec.description.strip() != ""
    assert spec.key_semantics.strip() != ""
    assert callable(spec.validate_key)


def test_get_registry_snapshot_is_immutable(reg: Any) -> None:
    first = reg.get_registry()
    with pytest.raises(TypeError):
        first[BackendId.ADB] = None
    assert reg.get_registry() == first
    assert reg.get_registry() is not first


def test_get_registry_import_free(reg: Any, no_backend_imports: None) -> None:
    reg.get_registry()
    assert [name for name in NEVER_IMPORT if name in sys.modules] == []


def test_refusal_unknown_id_first(reg: Any, no_backend_imports: None) -> None:
    with pytest.raises(BackendNotFoundError) as excinfo:
        reg.resolve_backend("nope")
    assert excinfo.value.backend_id == "nope"
    assert [name for name in NEVER_IMPORT if name in sys.modules] == []


@pytest.mark.parametrize(
    "backend",
    [
        BackendId.MEMU,
        BackendId.BLUESTACKS,
        BackendId.AVD,
    ],
)
def test_removed_backends_refuse_cleanly(reg: Any, backend: BackendId, no_backend_imports: None) -> None:
    with pytest.raises(BackendNotAvailableError) as excinfo:
        reg.resolve_backend(backend)
    assert excinfo.value.reason == "not-implemented-this-version"
    assert backend.value in (excinfo.value.detail or "")
    assert [name for name in NEVER_IMPORT if name in sys.modules] == []


@pytest.mark.parametrize("platform", [Platform.WINDOWS, Platform.MACOS, Platform.LINUX])
def test_available_backends_is_adb_only_without_vendor_deps(
    reg: Any, platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reg, "current_platform", lambda: platform)
    monkeypatch.setattr(reg, "_dep_available", lambda name: False)
    expected = [BackendId.ADB]
    if platform is Platform.WINDOWS:
        expected.append(BackendId.GOOGLE_PLAY)
    if platform is Platform.MACOS:
        expected.append(BackendId.BLUESTACKS_AIR)
    assert [spec.backend for spec in reg.available_backends()] == expected


def test_available_backends_includes_bluestacks_air_on_macos(reg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.MACOS)
    assert [spec.backend for spec in reg.available_backends()] == [
        BackendId.ADB,
        BackendId.BLUESTACKS_AIR,
    ]


def test_available_backends_includes_google_play_on_windows(reg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.WINDOWS)
    assert [spec.backend for spec in reg.available_backends()] == [
        BackendId.ADB,
        BackendId.GOOGLE_PLAY,
    ]


def test_bluestacks_air_selection_refuses_platform(
    reg: Any, no_backend_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.WINDOWS)
    with pytest.raises(BackendNotAvailableError) as excinfo:
        reg.resolve_backend(BackendId.BLUESTACKS_AIR)
    assert excinfo.value.reason == "platform-unsupported"
    assert "macos" in (excinfo.value.detail or "")


def test_google_play_selection_refuses_platform(
    reg: Any, no_backend_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.LINUX)
    with pytest.raises(BackendNotAvailableError) as excinfo:
        reg.resolve_backend(BackendId.GOOGLE_PLAY)
    assert excinfo.value.reason == "platform-unsupported"
    assert "windows" in (excinfo.value.detail or "")
    assert [name for name in NEVER_IMPORT if name in sys.modules] == []


class _DummyController(AsyncEmulatorController):
    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        return b""


def _dummy_spec(module: str, attr: str, *, dep_modules: tuple[str, ...] = (), extra: str | None = None) -> Any:
    from emuctl.backends._registry import BackendSpec

    return BackendSpec(
        backend=BackendId.ADB,
        extra=extra,
        module=module,
        adapter_attr=attr,
        supported_platforms=frozenset({Platform.LINUX}),
        dep_modules=dep_modules,
        description="test-only dummy",
        key_semantics="test-only dummy",
        validate_key=lambda key: None,
    )


def test_load_adapter_success_with_dummy_module(reg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("emuctl_test_dummy_adapter")
    module.__dict__["DummyController"] = _DummyController
    monkeypatch.setitem(sys.modules, "emuctl_test_dummy_adapter", module)
    assert reg.load_adapter(_dummy_spec("emuctl_test_dummy_adapter", "DummyController")) is _DummyController


@pytest.mark.parametrize("attr", [object, str, 42, None])
def test_load_adapter_rejects_non_port_subclass(reg: Any, monkeypatch: pytest.MonkeyPatch, attr: Any) -> None:
    module = types.ModuleType("emuctl_test_bad_adapter")
    module.__dict__["Adapter"] = attr
    monkeypatch.setitem(sys.modules, "emuctl_test_bad_adapter", module)
    with pytest.raises(InvalidArgumentError) as excinfo:
        reg.load_adapter(_dummy_spec("emuctl_test_bad_adapter", "Adapter"))
    assert excinfo.value.parameter == "adapter_attr"


def test_load_adapter_missing_attr(reg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "emuctl_test_empty_adapter", types.ModuleType("x"))
    with pytest.raises(InvalidArgumentError) as excinfo:
        reg.load_adapter(_dummy_spec("emuctl_test_empty_adapter", "Missing"))
    assert excinfo.value.parameter == "adapter_attr"


def test_load_adapter_missing_dep_never_imports(
    reg: Any, no_backend_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = types.ModuleType("emuctl_test_unreached_adapter")
    module.__dict__["Adapter"] = _DummyController
    monkeypatch.setitem(sys.modules, "emuctl_test_unreached_adapter", module)
    spec = _dummy_spec(
        "emuctl_test_unreached_adapter",
        "Adapter",
        dep_modules=("definitely_missing_dep_xyz",),
        extra="test-extra",
    )
    with pytest.raises(ExtrasMissingError) as excinfo:
        reg.load_adapter(spec)
    assert excinfo.value.extra == "test-extra"


def test_load_adapter_missing_module_maps_to_extra(reg: Any) -> None:
    spec = _dummy_spec("emuctl_test_no_such_module_xyz", "Adapter", extra="test-extra")
    with pytest.raises(ExtrasMissingError) as excinfo:
        reg.load_adapter(spec)
    assert excinfo.value.extra == "test-extra"


def test_backend_spec_is_frozen(reg: Any) -> None:
    import dataclasses

    spec = reg.get_registry()[BackendId.ADB]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.extra = "x"


@pytest.mark.parametrize("key", ["127.0.0.1:5555", "emulator-5554", "ABCDEF123456", "transport_id:5"])
def test_adb_key_rule_accepts_serials(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.ADB]
    assert spec.validate_key(key) is None


@pytest.mark.parametrize("key", ["has space", "semi;colon", "a\nb", "", "  "])
def test_adb_key_rule_rejects_non_serials(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.ADB]
    with pytest.raises(InvalidArgumentError):
        spec.validate_key(key)
    if key.strip():
        with pytest.raises(InvalidSerialError):
            spec.validate_key(key)


def test_key_structural_rule_still_lives_in_identity() -> None:
    with pytest.raises(InvalidArgumentError):
        InstanceRef(backend=BackendId.MEMU, key="   ")


@pytest.mark.parametrize("key", ["pyclashbot-136", "My VM 2", "BlueStacks Air"])
def test_bluestacks_air_key_rule_accepts_vm_names(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.BLUESTACKS_AIR]
    assert spec.validate_key(key) is None


@pytest.mark.parametrize("key", ["", "   ", "has\nnewline", "has\rcarriage"])
def test_bluestacks_air_key_rule_rejects(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.BLUESTACKS_AIR]
    with pytest.raises(InvalidArgumentError) as excinfo:
        spec.validate_key(key)
    assert excinfo.value.parameter == "key"


@pytest.mark.parametrize("key", ["localhost:6520", "127.0.0.1:5555", "emulator-5554"])
def test_google_play_key_rule_accepts_serials(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.GOOGLE_PLAY]
    assert spec.validate_key(key) is None


@pytest.mark.parametrize("key", ["has space", "a\nb", "", "  "])
def test_google_play_key_rule_rejects(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.GOOGLE_PLAY]
    with pytest.raises(InvalidArgumentError):
        spec.validate_key(key)


def test_spec_row_conformance_adb(reg: Any) -> None:
    spec = reg.get_registry()[BackendId.ADB]
    adapter = reg.load_adapter(spec)
    assert isinstance(adapter, type) and issubclass(adapter, AsyncEmulatorController)
    module = sys.modules[spec.module]
    assert callable(getattr(module, "list_instances", None))
    assert callable(getattr(module, "get_instance", None))


def test_spec_row_conformance_bluestacks_air(reg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = reg.get_registry()[BackendId.BLUESTACKS_AIR]
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.MACOS)
    adapter = reg.load_adapter(spec)
    assert isinstance(adapter, type) and issubclass(adapter, AsyncEmulatorController)
    module = sys.modules[spec.module]
    assert callable(getattr(module, "list_instances", None))
    assert callable(getattr(module, "get_instance", None))


def test_spec_row_conformance_google_play(reg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = reg.get_registry()[BackendId.GOOGLE_PLAY]
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.WINDOWS)
    adapter = reg.load_adapter(spec)
    assert isinstance(adapter, type) and issubclass(adapter, AsyncEmulatorController)
    module = sys.modules[spec.module]
    assert callable(getattr(module, "list_instances", None))
    assert callable(getattr(module, "get_instance", None))

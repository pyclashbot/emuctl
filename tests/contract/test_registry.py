"""§7 conformance (registry, §3): pinned spec table, fixed refusal order,
import-free selection, load_adapter discipline, find_spec honesty."""

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
)
from emuctl.port import AsyncEmulatorController

#: Adapter + vendored-dep modules that selection must never import.
NEVER_IMPORT = (
    "emuctl.backends.adb",
    "emuctl.backends.memu",
    "emuctl.backends.bluestacks",
    "emuctl.backends.bluestacks.bs5",
    "emuctl.backends.bluestacks.air",
    "pymemuc",
)


@pytest.fixture
def reg() -> Any:
    """Lazy registry access (runtime, never collection time).

    Importing `emuctl.backends` at test-module top level would plant it in
    `sys.modules` during COLLECTION — before any test runs — and break
    `test_identity`'s no-backend-modules invariant. Every registry touch in
    this file goes through this fixture instead.
    """
    from emuctl.backends import _registry

    return _registry


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    """Leave no `emuctl.backends*` trace: runtime imports + fakes are evicted
    after each test so later tests observe a clean `sys.modules`.

    Eviction also clears the parent-package attribute aliases (see
    `test_discovery`, which documents why sys.modules-only eviction is not
    enough when dotted-string patching is in play)."""
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
    """Evict (and keep evicted) every adapter/dep module for the test."""
    stashed: dict[str, Any] = {}
    for name in NEVER_IMPORT:
        if name in sys.modules:
            stashed[name] = sys.modules.pop(name)
    for name in NEVER_IMPORT:
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield
    for name, module in stashed.items():
        sys.modules[name] = module


def test_registry_covers_every_backend_id(reg: Any) -> None:
    assert set(reg.get_registry()) == set(BackendId)


@pytest.mark.parametrize(
    ("backend", "extra", "module", "adapter_attr", "platforms", "deps"),
    [
        (
            BackendId.ADB,
            None,
            "emuctl.backends.adb",
            "AdbController",
            frozenset({Platform.WINDOWS, Platform.MACOS, Platform.LINUX}),
            (),
        ),
        (
            BackendId.MEMU,
            "memu",
            "emuctl.backends.memu",
            "MemuController",
            frozenset({Platform.WINDOWS}),
            ("pymemuc",),
        ),
        (
            BackendId.BLUESTACKS,
            "bluestacks",
            "emuctl.backends.bluestacks.bs5",
            "BlueStacks5Controller",
            frozenset({Platform.WINDOWS}),
            (),
        ),
        (
            BackendId.BLUESTACKS_AIR,
            "bluestacks",
            "emuctl.backends.bluestacks.air",
            "BlueStacksAirController",
            frozenset({Platform.MACOS}),
            (),
        ),
    ],
)
def test_spec_table_pinned_rows(
    reg: Any,
    backend: BackendId,
    extra: str | None,
    module: str,
    adapter_attr: str,
    platforms: frozenset[Platform],
    deps: tuple[str, ...],
) -> None:
    spec = reg.get_registry()[backend]
    assert isinstance(spec, reg.BackendSpec)
    assert spec.backend is backend
    assert spec.extra == extra
    assert spec.module == module
    assert spec.adapter_attr == adapter_attr
    assert spec.supported_platforms == platforms
    assert spec.dep_modules == deps
    assert spec.description.strip() != ""
    assert spec.key_semantics.strip() != ""
    assert callable(spec.validate_key)


@pytest.mark.parametrize("backend", [BackendId.GOOGLE_PLAY, BackendId.AVD])
def test_spec_table_not_implemented_rows_are_none(reg: Any, backend: BackendId) -> None:
    assert reg.get_registry()[backend] is None


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


def test_refusal_not_implemented_before_platform(reg: Any, no_backend_imports: None) -> None:
    # GOOGLE_PLAY has no platform row at all: the None-spec refusal fires
    # without any platform evaluation, on every host.
    with pytest.raises(BackendNotAvailableError) as excinfo:
        reg.resolve_backend(BackendId.GOOGLE_PLAY)
    assert excinfo.value.reason == "not-implemented-this-version"
    assert [name for name in NEVER_IMPORT if name in sys.modules] == []


def test_refusal_platform_before_missing_dep(
    reg: Any, no_backend_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MEMU off-Windows with pymemuc ALSO missing: platform wins.
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.MACOS)
    with pytest.raises(BackendNotAvailableError) as excinfo:
        reg.resolve_backend(BackendId.MEMU)
    assert excinfo.value.reason == "platform-unsupported"
    assert "windows" in (excinfo.value.detail or "")
    assert [name for name in NEVER_IMPORT if name in sys.modules] == []


def test_refusal_missing_dep_last(
    reg: Any, no_backend_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MEMU on Windows without pymemuc: the dep refusal names the extra.
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.WINDOWS)
    assert "pymemuc" not in sys.modules
    with pytest.raises(ExtrasMissingError) as excinfo:
        reg.resolve_backend(BackendId.MEMU)
    assert excinfo.value.extra == "memu"
    assert "pymemuc" not in sys.modules
    assert [name for name in NEVER_IMPORT if name in sys.modules] == []


def test_find_spec_honesty(
    reg: Any, no_backend_imports: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dependency probing uses find_spec (no import of the vendored dep)."""
    import importlib.util

    monkeypatch.setattr(reg, "current_platform", lambda: Platform.WINDOWS)
    real_find_spec = importlib.util.find_spec
    calls: list[str] = []

    def spy(name: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr("importlib.util.find_spec", spy)
    with pytest.raises(ExtrasMissingError):
        reg.resolve_backend(BackendId.MEMU)
    assert "pymemuc" in calls
    assert "pymemuc" not in sys.modules


def test_available_backends_platform_and_deps(reg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(reg, "_dep_available", lambda name: True)
    assert [spec.backend for spec in reg.available_backends()] == [
        BackendId.ADB,
        BackendId.BLUESTACKS,
        BackendId.MEMU,
    ]
    monkeypatch.setattr(reg, "_dep_available", lambda name: False)
    assert [spec.backend for spec in reg.available_backends()] == [
        BackendId.ADB,
        BackendId.BLUESTACKS,
    ]
    monkeypatch.setattr(reg, "current_platform", lambda: Platform.LINUX)
    assert [spec.backend for spec in reg.available_backends()] == [BackendId.ADB]


class _DummyController(AsyncEmulatorController):
    """Minimal port subclass: no flags, only the abstract seam implemented."""

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        return b""


def _dummy_spec(
    module: str, attr: str, *, dep_modules: tuple[str, ...] = (), extra: str | None = None
) -> Any:
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
    assert (
        reg.load_adapter(_dummy_spec("emuctl_test_dummy_adapter", "DummyController"))
        is _DummyController
    )


@pytest.mark.parametrize("attr", [object, str, 42, None])
def test_load_adapter_rejects_non_port_subclass(
    reg: Any, monkeypatch: pytest.MonkeyPatch, attr: Any
) -> None:
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
        extra="memu",
    )
    with pytest.raises(ExtrasMissingError) as excinfo:
        reg.load_adapter(spec)
    assert excinfo.value.extra == "memu"


def test_load_adapter_missing_module_maps_to_extra(reg: Any) -> None:
    spec = _dummy_spec("emuctl_test_no_such_module_xyz", "Adapter", extra="memu")
    with pytest.raises(ExtrasMissingError) as excinfo:
        reg.load_adapter(spec)
    assert excinfo.value.extra == "memu"


def test_backend_spec_is_frozen(reg: Any) -> None:
    import dataclasses

    spec = reg.get_registry()[BackendId.ADB]
    assert isinstance(spec, reg.BackendSpec)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.extra = "x"


@pytest.mark.parametrize(
    "key", ["127.0.0.1:5555", "emulator-5554", "ABCDEF123456", "transport_id:5"]
)
def test_adb_key_rule_accepts_serials(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.ADB]
    assert isinstance(spec, reg.BackendSpec)
    assert spec.validate_key(key) is None


@pytest.mark.parametrize("key", ["has space", "semi;colon", "a\nb", "", "  "])
def test_adb_key_rule_rejects_non_serials(reg: Any, key: str) -> None:
    from emuctl.errors import InvalidSerialError

    spec = reg.get_registry()[BackendId.ADB]
    assert isinstance(spec, reg.BackendSpec)
    with pytest.raises(InvalidArgumentError):
        spec.validate_key(key)
    if key.strip():
        with pytest.raises(InvalidSerialError):
            spec.validate_key(key)


@pytest.mark.parametrize("key", ["pyclashbot-136", "My VM 2", "bluestacks-conf"])
def test_vendor_key_rule_accepts_names(reg: Any, key: str) -> None:
    for backend in (BackendId.MEMU, BackendId.BLUESTACKS, BackendId.BLUESTACKS_AIR):
        spec = reg.get_registry()[backend]
        assert isinstance(spec, reg.BackendSpec)
        assert spec.validate_key(key) is None


@pytest.mark.parametrize("key", ["", "   ", "has\nnewline", "has\rcarriage"])
def test_vendor_key_rule_rejects(reg: Any, key: str) -> None:
    spec = reg.get_registry()[BackendId.MEMU]
    assert isinstance(spec, reg.BackendSpec)
    with pytest.raises(InvalidArgumentError) as excinfo:
        spec.validate_key(key)
    assert excinfo.value.parameter == "key"


def test_key_structural_rule_still_lives_in_identity() -> None:
    with pytest.raises(InvalidArgumentError):
        InstanceRef(backend=BackendId.MEMU, key="   ")

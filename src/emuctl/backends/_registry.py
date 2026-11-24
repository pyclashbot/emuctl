from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from importlib import util as _import_util
from types import MappingProxyType
from typing import Final

from emuctl._identification import BackendId
from emuctl._platforms import Platform, current_platform
from emuctl._serials import validate_serial
from emuctl.errors import (
    BackendNotAvailableError,
    BackendNotFoundError,
    ExtrasMissingError,
    InvalidArgumentError,
)
from emuctl.port import AsyncEmulatorController

__all__ = [
    "BackendSpec",
    "get_registry",
    "available_backends",
    "resolve_backend",
    "load_adapter",
]


#: Control characters rejected in vendor instance names (log/render safety).
_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")


def _validate_vm_name(key: str) -> None:
    """Vendor instance names: non-blank text with no control characters."""
    if not isinstance(key, str) or not key.strip():
        raise InvalidArgumentError(
            parameter="key",
            constraint="must be a non-empty instance name",
        )
    if _CONTROL_RE.search(key) is not None:
        raise InvalidArgumentError(
            parameter="key",
            constraint="must not contain control characters",
        )


@dataclass(frozen=True, slots=True)
class BackendSpec:
    backend: BackendId
    extra: str | None
    module: str
    adapter_attr: str
    supported_platforms: frozenset[Platform]
    dep_modules: tuple[str, ...]
    description: str
    key_semantics: str
    validate_key: Callable[[str], None]


_SPEC_TABLE: Final[dict[BackendId, BackendSpec | None]] = {
    BackendId.ADB: BackendSpec(
        backend=BackendId.ADB,
        extra=None,
        module="emuctl.backends.adb",
        adapter_attr="AdbController",
        supported_platforms=frozenset({Platform.WINDOWS, Platform.MACOS, Platform.LINUX}),
        dep_modules=(),
        description=("Plain ADB devices through the system ADB server (core backend; no extra required)."),
        key_semantics=(
            "Transport serial as listed by the local ADB server "
            "(e.g. '127.0.0.1:5555', 'emulator-5554'); positional, never referential."
        ),
        validate_key=validate_serial,
    ),
    BackendId.MEMU: BackendSpec(
        backend=BackendId.MEMU,
        extra="memu",
        module="emuctl.backends.memu",
        adapter_attr="MemuController",
        supported_platforms=frozenset({Platform.WINDOWS}),
        dep_modules=("pymemuc",),
        description=(
            "MEmu emulator driven through memuc (Windows only; 'pymemuc' ships with the [memu] extra)."
        ),
        key_semantics=(
            "MEmu VM name as shown by memuc (e.g. 'pyclashbot-136'); referential — "
            "survives stop/start and reboots."
        ),
        validate_key=_validate_vm_name,
    ),
}


def get_registry() -> Mapping[BackendId, BackendSpec | None]:
    return MappingProxyType(dict(_SPEC_TABLE))


def _dep_available(module_name: str) -> bool:
    try:
        return _import_util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def available_backends() -> list[BackendSpec]:
    host = current_platform()
    usable = [
        spec
        for spec in _SPEC_TABLE.values()
        if spec is not None
        and host in spec.supported_platforms
        and all(_dep_available(dep) for dep in spec.dep_modules)
    ]
    return sorted(usable, key=lambda spec: spec.backend.value)


def resolve_backend(backend: BackendId | str) -> BackendSpec:
    if isinstance(backend, BackendId):
        wanted = backend
    elif isinstance(backend, str):
        wanted = BackendId.from_string(backend)
    else:
        raise BackendNotFoundError(backend_id=repr(backend))
    if wanted not in _SPEC_TABLE:
        raise BackendNotAvailableError(
            reason="not-implemented-this-version",
            detail=(
                f"backend '{wanted.value}' is not implemented in this version (it remains on its stack branch)"
            ),
        )
    spec = _SPEC_TABLE[wanted]
    if spec is None:
        raise BackendNotAvailableError(
            reason="not-implemented-this-version",
            detail=f"backend '{wanted.value}' is not implemented in this version",
        )
    host = current_platform()
    if host not in spec.supported_platforms:
        supported = ", ".join(sorted(platform.value for platform in spec.supported_platforms))
        alternatives = [item.backend.value for item in available_backends()]
        detail = (
            f"backend '{wanted.value}' runs on {supported}; this host is {host.value}; "
            f"available on this host: {', '.join(alternatives) if alternatives else 'none'}"
        )
        raise BackendNotAvailableError(reason="platform-unsupported", detail=detail)
    for dep in spec.dep_modules:
        if not _dep_available(dep):
            raise ExtrasMissingError(
                extra=spec.extra if spec.extra is not None else dep,
                detail=(
                    f"backend '{wanted.value}' needs module '{dep}' (install the '{spec.extra}' extra)"
                    if spec.extra is not None
                    else f"backend '{wanted.value}' needs module '{dep}'"
                ),
            )
    return spec


def load_adapter(spec: BackendSpec) -> type[AsyncEmulatorController]:
    if not isinstance(spec, BackendSpec):
        raise InvalidArgumentError(
            parameter="spec",
            constraint="must be a BackendSpec (resolve it via resolve_backend)",
        )
    for dep in spec.dep_modules:
        if not _dep_available(dep):
            raise ExtrasMissingError(
                extra=spec.extra if spec.extra is not None else dep,
                detail=f"cannot load backend '{spec.backend.value}': module '{dep}' is missing",
            )
    try:
        module = import_module(spec.module)
    except ModuleNotFoundError:
        if spec.extra is not None:
            raise ExtrasMissingError(
                extra=spec.extra,
                detail=(
                    f"cannot load backend '{spec.backend.value}': "
                    f"module '{spec.module}' is missing "
                    f"(install the '{spec.extra}' extra)"
                ),
            ) from None
        raise
    adapter = getattr(module, spec.adapter_attr, None)
    if not (isinstance(adapter, type) and issubclass(adapter, AsyncEmulatorController)):
        raise InvalidArgumentError(
            parameter="adapter_attr",
            constraint=(f"'{spec.module}.{spec.adapter_attr}' must name an AsyncEmulatorController subclass"),
        )
    return adapter

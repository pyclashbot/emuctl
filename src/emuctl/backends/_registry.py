"""§3 — The spec table: _SPEC_TABLE, get_registry, available_backends,
resolve_backend, load_adapter.

The table is the SOLE platform truth (adapters never declare platforms) and
the only module allowed to name import-facing backend paths (§6 rule 3).
Selection (`resolve_backend`) raises BEFORE any import, in the contract's
fixed refusal order; `load_adapter` is the ONLY dynamic adapter importer.

Interpretation decisions (CONTRACT §3 wins; these fill its silences):

- ``adapter_attr`` spellings (``AdbController``, ``MemuController``,
  ``BlueStacks5Controller``, ``BlueStacksAirController``) are NOT pinned by
  the baked contract — only the Air module path
  (``emuctl.backends.bluestacks.air``) is. The ``<Vendor>Controller`` scheme
  mirrors the port's ``AdbController`` middle-layer naming; adapter PRs treat
  these spellings as pinned from here on. Name-collision note: core
  ``AdbController`` (§6 middle layer, ADB-family interaction primitives) is a
  different class from the future ``emuctl.backends.adb.AdbController``
  (concrete adapter subclass inheriting it).
- Key validators are core-owned callables referenced by the table (this file
  is core-owned per §6): ADB keys are transport serials, so the ADB row
  references the §3 serial allowlist (`validate_serial`) directly; vendor
  instance names require non-blank text with no control characters
  (log/render safety, matching the sanitize discipline).
- ``load_adapter`` converts a missing adapter module or missing third-party
  dep into ``ExtrasMissingError`` (never raw ``ModuleNotFoundError``); a
  present module whose attr is not a port-ABC subclass is an
  ``InvalidArgumentError`` naming ``adapter_attr`` (a wiring violation, like
  the port's ``TypeError`` gate, expressed in the taxonomy). A missing core
  (``extra=None``) module is a broken install and propagates unwrapped —
  unreachable for in-package modules.
- Dependency checks use ``importlib.util.find_spec`` only — the vendored
  dependency is never imported for its side effects.
"""

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
    """One pinned row of the §3 spec table: everything selection needs,
    decided BEFORE any backend module is imported."""

    backend: BackendId
    extra: str | None  # PEP 685 extra name; None for adb (core)
    module: str  # adapter import target (load_adapter ONLY)
    adapter_attr: str  # port-ABC subclass attribute on `module`
    supported_platforms: frozenset[Platform]  # THE sole platform-gating truth
    dep_modules: tuple[str, ...]  # find_spec'd, never imported for side effects
    description: str
    key_semantics: str  # per-backend identity meaning (§3: positional vs referential)
    validate_key: Callable[[str], None]  # selection-time key rule

    def __post_init__(self) -> None:
        backend = self.backend
        if isinstance(backend, str):
            backend = BackendId.from_string(backend)
        if not isinstance(backend, BackendId):
            raise InvalidArgumentError(
                parameter="backend",
                constraint="must be a BackendId (e.g. BackendId.ADB)",
            )
        object.__setattr__(self, "backend", backend)
        if self.extra is not None and (not isinstance(self.extra, str) or not self.extra):
            raise InvalidArgumentError(
                parameter="extra",
                constraint="must be a non-empty extra name or None",
            )
        for field in ("module", "adapter_attr", "description", "key_semantics"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise InvalidArgumentError(
                    parameter=field,
                    constraint="must be a non-empty string",
                )
        platforms = self.supported_platforms
        if not isinstance(platforms, frozenset) or not platforms:
            raise InvalidArgumentError(
                parameter="supported_platforms",
                constraint="must be a non-empty frozenset of Platform",
            )
        if not all(isinstance(item, Platform) for item in platforms):
            raise InvalidArgumentError(
                parameter="supported_platforms",
                constraint="must contain only Platform members",
            )
        deps = self.dep_modules
        if not isinstance(deps, tuple) or not all(isinstance(item, str) for item in deps):
            raise InvalidArgumentError(
                parameter="dep_modules",
                constraint="must be a tuple of module names",
            )
        object.__setattr__(self, "dep_modules", tuple(deps))
        if not callable(self.validate_key):
            raise InvalidArgumentError(
                parameter="validate_key",
                constraint="must be a callable taking the key",
            )


#: Module-level, pinned (§3): ADB full spec (core, all platforms); MEMU with
#: its pymemuc dep (Windows); BLUESTACKS BS5 (Windows, HD-Player/HD-Adb.exe,
#: MIM, private ADB server on 127.0.0.1:5041); BLUESTACKS_AIR (macOS
#: Apple-Silicon Air, same [bluestacks] extra, family-package module,
#: /Applications/BlueStacks.app + bundled hd-adb, single bluestacks.conf
#: instance); GOOGLE_PLAY and AVD map to None (not implemented this version).
_SPEC_TABLE: Final[dict[BackendId, BackendSpec | None]] = {
    BackendId.ADB: BackendSpec(
        backend=BackendId.ADB,
        extra=None,
        module="emuctl.backends.adb",
        adapter_attr="AdbController",
        supported_platforms=frozenset({Platform.WINDOWS, Platform.MACOS, Platform.LINUX}),
        dep_modules=(),
        description=(
            "Plain ADB devices through the system ADB server (core backend; no extra required)."
        ),
        key_semantics=(
            "Transport serial as listed by the local ADB server "
            "(e.g. '127.0.0.1:5555', 'emulator-5554'); positional, never referential."
        ),
        validate_key=validate_serial,  # §3 serial allowlist, referenced directly
    ),
    BackendId.MEMU: BackendSpec(
        backend=BackendId.MEMU,
        extra="memu",
        module="emuctl.backends.memu",
        adapter_attr="MemuController",
        supported_platforms=frozenset({Platform.WINDOWS}),
        dep_modules=("pymemuc",),
        description=(
            "MEmu emulator driven through memuc "
            "(Windows only; 'pymemuc' ships with the [memu] extra)."
        ),
        key_semantics=(
            "MEmu VM name as shown by memuc (e.g. 'pyclashbot-136'); referential — "
            "survives stop/start and reboots."
        ),
        validate_key=_validate_vm_name,
    ),
    BackendId.BLUESTACKS: BackendSpec(
        backend=BackendId.BLUESTACKS,
        extra="bluestacks",
        module="emuctl.backends.bluestacks.bs5",
        adapter_attr="BlueStacks5Controller",
        supported_platforms=frozenset({Platform.WINDOWS}),
        dep_modules=(),
        description=(
            "BlueStacks 5 on Windows (HD-Player with Multi-Instance Manager; "
            "private ADB server on 127.0.0.1:5041)."
        ),
        key_semantics=("BlueStacks 5 instance name from the Multi-Instance Manager; referential."),
        validate_key=_validate_vm_name,
    ),
    BackendId.BLUESTACKS_AIR: BackendSpec(
        backend=BackendId.BLUESTACKS_AIR,
        extra="bluestacks",
        module="emuctl.backends.bluestacks.air",
        adapter_attr="BlueStacksAirController",
        supported_platforms=frozenset({Platform.MACOS}),
        dep_modules=(),
        description=(
            "BlueStacks Air on macOS (Apple Silicon; /Applications/BlueStacks.app "
            "with bundled hd-adb; single config-store instance)."
        ),
        key_semantics=(
            "Air instance key from bluestacks.conf (single instance at v0; "
            "MimMeta synthesized when cold); referential."
        ),
        validate_key=_validate_vm_name,
    ),
    BackendId.GOOGLE_PLAY: None,
    BackendId.AVD: None,
}


def get_registry() -> Mapping[BackendId, BackendSpec | None]:
    """The static spec table. Import-free and side-effect-free: resolves no
    platforms, touches no dependencies, imports no backend modules."""
    return MappingProxyType(dict(_SPEC_TABLE))


def _dep_available(module_name: str) -> bool:
    """Honesty probe for one dependency: find_spec only, never an import."""
    try:
        return _import_util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def available_backends() -> list[BackendSpec]:
    """Concrete specs usable on this host: platform ⊇ host AND deps resolve.

    Deterministic ordering by backend id. Imports nothing."""
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
    """THE single selection gate, fixed order (§3): unknown id →
    BackendNotFoundError · spec None →
    BackendNotAvailableError("not-implemented-this-version") · platform →
    BackendNotAvailableError("platform-unsupported", naming supported
    platforms and the backends available on this host) · deps →
    ExtrasMissingError. Raises BEFORE any import; imports nothing."""
    if isinstance(backend, BackendId):
        wanted = backend
    elif isinstance(backend, str):
        wanted = BackendId.from_string(backend)
    else:
        raise BackendNotFoundError(backend_id=repr(backend))
    try:
        spec = _SPEC_TABLE[wanted]
    except KeyError:
        raise BackendNotFoundError(backend_id=wanted.value) from None
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
                    f"backend '{wanted.value}' needs module '{dep}' "
                    f"(install the '{spec.extra}' extra)"
                    if spec.extra is not None
                    else f"backend '{wanted.value}' needs module '{dep}'"
                ),
            )
    return spec


def load_adapter(spec: BackendSpec) -> type[AsyncEmulatorController]:
    """The ONLY adapter importer: import the spec's module, verify the adapter
    attr is an `AsyncEmulatorController` subclass, return the class.

    Missing third-party deps (or a missing extra-owned adapter module) raise
    `ExtrasMissingError`, never raw `ModuleNotFoundError`; a present module
    whose attr is not a port subclass raises `InvalidArgumentError`.

    EXPLICIT interpretation decision: §3's "never a raw ModuleNotFoundError"
    applies to adapter/extra failures only. An `extra=None` row whose import
    chain fails is a broken core install — it surfaces the raw
    `ModuleNotFoundError` unmapped (CLI exit 9: unmapped = broken install).
    """
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
        # extra=None row whose chain fails: broken core install — surface the
        # raw ModuleNotFoundError unmapped (CLI exit 9), never ExtrasMissing.
        raise
    adapter = getattr(module, spec.adapter_attr, None)
    if not (isinstance(adapter, type) and issubclass(adapter, AsyncEmulatorController)):
        raise InvalidArgumentError(
            parameter="adapter_attr",
            constraint=(
                f"'{spec.module}.{spec.adapter_attr}' must name an AsyncEmulatorController subclass"
            ),
        )
    return adapter

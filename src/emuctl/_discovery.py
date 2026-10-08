"""§3 — Discovery plumbing: connect(), discover(), DiscoveryReport, BackendStatus.

Discovery answers "what plausibly exists and is it up"; it guarantees nothing
about driveability — readiness begins at construction and the boot primitive.
Read-only, bounded, per-backend isolated: a dead vendor becomes a
`BackendStatus` row derived from the observed failure itself, never a global
explosion. Deterministic ordering by backend id; no caching. `offline=True`
performs no probes and no enumeration — rows report resolved-but-not-probed
("offline"). Honoring §3's parenthetical (config-store enumeration still
running while offline) is deferred contract errata: it requires an offline
knob on the `BackendDiscovery` seam (`list_instances(config)` takes no
offline parameter today, so offline enumeration is un-implementable). Cold
`adb devices` may start the local ADB server (documented exception).

Interpretation decisions (CONTRACT §3 wins; these fill its silences):

- `connect()` lives here (this module's title line pins it): canonical
  targets ONLY (`InstanceRef` or `'backend:key'` string — bare shapes are
  uniformly `InvalidArgumentError` via `InstanceRef.parse`), refusals raise
  at construction in registry order, then `spec.validate_key`, then the §4
  config↔capability gate against the loaded adapter's declared set, then
  construction binding the resolved config (`config=` kwarg — the convention
  adapter PRs implement against). `connect_sync` is NOT here (§5 owns it).
- Row `reason` vocabulary (closed, JSON-safe): "ok" (probed, instances
  listed), "offline" (resolved but not probed — `offline=True`), plus the
  failure derivations "not-implemented-this-version",
  "platform-unsupported", "extras-missing", "adapter-unavailable"
  (load failed or the module lacks the `list_instances` convention member),
  "probe-failed" (`list_instances` raised). `available` means "selectable":
  true for "ok"/"offline" rows only.
- Static import-linter forbids core→backends static imports, and §6 allows
  dynamic adapter imports ONLY via `load_adapter` — so this module never
  calls `importlib` itself. After `load_adapter` imports the adapter module,
  the `BackendDiscovery` convention members (`list_instances`,
  `get_instance`) are reached through the already-loaded module object in
  `sys.modules` (a lookup, not an import) via function-local registry
  access. Adapters do not exist yet, so the online path honestly reports
  "adapter-unavailable" rows rather than failing.
- `CancelledError` (a `BaseException`) is never caught: per-backend
  isolation catches `Exception` only, so cancellation still propagates.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl.config.model import EmuctlConfig, ensure_config_supported
from emuctl.errors import (
    BackendNotAvailableError,
    ExtrasMissingError,
    InvalidArgumentError,
)
from emuctl.log import sanitize
from emuctl.port import AsyncEmulatorController

__all__ = [
    "BackendStatus",
    "DiscoveryReport",
    "BACKEND_STATUS_REASONS",
    "connect",
    "discover",
]

logger = logging.getLogger(__name__)

#: Closed row-reason vocabulary (additive-only, like §1's PROBE_REASONS).
BACKEND_STATUS_REASONS: Final[frozenset[str]] = frozenset(
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


@dataclass(frozen=True, slots=True)
class BackendStatus:
    """One backend's discovery outcome: availability + observed instances."""

    backend: BackendId
    available: bool
    reason: str
    instances: tuple[InstanceRecord, ...] = ()
    detail: str | None = None

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
        if not isinstance(self.available, bool):
            raise InvalidArgumentError(
                parameter="available",
                constraint="must be a bool",
            )
        if not isinstance(self.reason, str) or self.reason not in BACKEND_STATUS_REASONS:
            raise InvalidArgumentError(
                parameter="reason",
                constraint=f"must be one of {sorted(BACKEND_STATUS_REASONS)}",
            )
        instances = tuple(self.instances)
        if not all(isinstance(item, InstanceRecord) for item in instances):
            raise InvalidArgumentError(
                parameter="instances",
                constraint="must all be InstanceRecord",
            )
        object.__setattr__(self, "instances", instances)
        if self.detail is not None and not isinstance(self.detail, str):
            raise InvalidArgumentError(
                parameter="detail",
                constraint="must be a string or None",
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (plain dicts/lists/strings/bools/None)."""
        return {
            "backend": self.backend.value,
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
            "instances": [
                {
                    "ref": str(record.ref),
                    "backend": record.ref.backend.value,
                    "key": record.ref.key,
                    "running": record.running,
                    "serial": record.serial,
                    "details": dict(record.details),
                }
                for record in self.instances
            ],
        }


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """The full discovery outcome, ordered deterministically by backend id."""

    backends: tuple[BackendStatus, ...]
    offline: bool = False

    def __post_init__(self) -> None:
        statuses = tuple(self.backends)
        if not all(isinstance(item, BackendStatus) for item in statuses):
            raise InvalidArgumentError(
                parameter="backends",
                constraint="must all be BackendStatus",
            )
        object.__setattr__(self, "backends", statuses)
        if not isinstance(self.offline, bool):
            raise InvalidArgumentError(
                parameter="offline",
                constraint="must be a bool",
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection (one JSON doc shape for the CLI surface)."""
        return {
            "offline": self.offline,
            "backends": [status.to_dict() for status in self.backends],
        }


def _resolve_config(config: EmuctlConfig | None) -> EmuctlConfig:
    """Default programmatic consumers to a bare `EmuctlConfig` (files are sugar)."""
    if config is None:
        return EmuctlConfig()
    if not isinstance(config, EmuctlConfig):
        raise InvalidArgumentError(
            parameter="config",
            constraint="must be an EmuctlConfig or None",
        )
    return config


def _resolve_target(target: InstanceRef | str) -> InstanceRef:
    """Canonical targets only: `InstanceRef` passes through, strings parse via
    `InstanceRef.parse` (bare shapes uniformly `InvalidArgumentError`)."""
    if isinstance(target, InstanceRef):
        return target
    if isinstance(target, str):
        return InstanceRef.parse(target)
    raise InvalidArgumentError(
        parameter="target",
        constraint="must be an InstanceRef or canonical 'backend:key' string "
        "(e.g. 'adb:127.0.0.1:5555')",
    )


async def connect(
    target: InstanceRef | str, *, config: EmuctlConfig | None = None
) -> AsyncEmulatorController:
    """Select, validate, gate, and construct the adapter for `target`.

    Selection refusals raise HERE, at construction — never on first call. No
    discovery, no search, no boot (the adapter constructor binds the resolved
    ref + config only). No `logger=` parameter — logging is stdlib-name-based.
    """
    # Function-local: §6 forbids core→backends static imports.
    from emuctl.backends._registry import load_adapter, resolve_backend

    ref = _resolve_target(target)
    logger.debug("connect target=%s", sanitize(str(ref)))
    spec = resolve_backend(ref.backend)
    spec.validate_key(ref.key)
    cfg = _resolve_config(config)
    adapter_cls = load_adapter(spec)
    ensure_config_supported(cfg, adapter_cls.FEATURES)
    controller = adapter_cls(ref, config=cfg)
    logger.debug("connect bound backend=%s", sanitize(ref.backend.value))
    return controller


def _wanted_ids(backends: Iterable[BackendId | str] | None) -> list[BackendId]:
    """Normalize the `discover` selector: unknown ids raise NOW
    (`BackendNotFoundError`), never as per-backend rows."""
    if backends is None:
        # Function-local: §6 forbids core→backends static imports.
        from emuctl.backends._registry import get_registry

        return sorted(get_registry(), key=lambda item: item.value)
    wanted: list[BackendId] = []
    for raw in backends:
        item = raw if isinstance(raw, BackendId) else BackendId.from_string(raw)
        if item not in wanted:
            wanted.append(item)
    return sorted(wanted, key=lambda item: item.value)


async def _status_for(backend: BackendId, *, config: EmuctlConfig, offline: bool) -> BackendStatus:
    """One backend's row: registry availability first, vendor probe second,
    each failure derived into the row itself (never raised)."""
    # Function-local: §6 forbids core→backends static imports.
    from emuctl.backends._registry import load_adapter, resolve_backend

    try:
        resolved = resolve_backend(backend)
    except BackendNotAvailableError as exc:
        return BackendStatus(backend=backend, available=False, reason=exc.reason, detail=exc.detail)
    except ExtrasMissingError as exc:
        return BackendStatus(
            backend=backend,
            available=False,
            reason="extras-missing",
            detail=f"install the '{exc.extra}' extra to enable backend '{backend.value}'",
        )
    if offline:
        return BackendStatus(backend=backend, available=True, reason="offline")
    try:
        load_adapter(resolved)
    except Exception as exc:
        return BackendStatus(
            backend=backend,
            available=False,
            reason="adapter-unavailable",
            detail=sanitize(f"{type(exc).__name__}: {exc}")[:200],
        )
    # The module object `load_adapter` imported (a lookup, not an import —
    # dynamic imports happen ONLY via `load_adapter`).
    module = sys.modules.get(resolved.module)
    probe = getattr(module, "list_instances", None) if module is not None else None
    if not callable(probe):
        return BackendStatus(
            backend=backend,
            available=False,
            reason="adapter-unavailable",
            detail=(
                f"module '{resolved.module}' exposes no list_instances "
                "(BackendDiscovery convention)"
            ),
        )
    try:
        found = tuple(await probe(config))
    except Exception as exc:
        return BackendStatus(
            backend=backend,
            available=False,
            reason="probe-failed",
            detail=sanitize(f"{type(exc).__name__}: {exc}")[:200],
        )
    return BackendStatus(backend=backend, available=True, reason="ok", instances=found)


async def discover(
    backends: Iterable[BackendId | str] | None = None,
    *,
    config: EmuctlConfig | None = None,
    offline: bool = False,
) -> DiscoveryReport:
    """Enumerate plausibly-present instances per backend, isolated per backend.

    Raises only for unknown ids; every other failure becomes a row.
    Deterministic ordering by backend id; no caching. `offline=True`
    performs no probes and no enumeration (resolved-but-not-probed rows;
    see the module docstring for the deferred §3-parenthetical errata).
    """
    wanted = _wanted_ids(backends)
    cfg = _resolve_config(config)
    logger.debug(
        "discover backends=%s offline=%s",
        sanitize(",".join(item.value for item in wanted)),
        offline,
    )
    rows = [await _status_for(item, config=cfg, offline=offline) for item in wanted]
    return DiscoveryReport(backends=tuple(rows), offline=offline)

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
    backend: BackendId
    available: bool
    reason: str
    instances: tuple[InstanceRecord, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
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
    backends: tuple[BackendStatus, ...]
    offline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "offline": self.offline,
            "backends": [status.to_dict() for status in self.backends],
        }


def _resolve_config(config: EmuctlConfig | None) -> EmuctlConfig:
    if config is None:
        return EmuctlConfig()
    if not isinstance(config, EmuctlConfig):
        raise InvalidArgumentError(
            parameter="config",
            constraint="must be an EmuctlConfig or None",
        )
    return config


def _resolve_target(target: InstanceRef | str) -> InstanceRef:
    if isinstance(target, InstanceRef):
        return target
    if isinstance(target, str):
        return InstanceRef.parse(target)
    raise InvalidArgumentError(
        parameter="target",
        constraint="must be an InstanceRef or canonical 'backend:key' string (e.g. 'adb:127.0.0.1:5555')",
    )


async def connect(target: InstanceRef | str, *, config: EmuctlConfig | None = None) -> AsyncEmulatorController:
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
    if backends is None:
        from emuctl.backends._registry import get_registry

        return sorted(get_registry(), key=lambda item: item.value)
    wanted: list[BackendId] = []
    for raw in backends:
        item = raw if isinstance(raw, BackendId) else BackendId.from_string(raw)
        if item not in wanted:
            wanted.append(item)
    return sorted(wanted, key=lambda item: item.value)


async def _status_for(backend: BackendId, *, config: EmuctlConfig, offline: bool) -> BackendStatus:
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
    module = sys.modules.get(resolved.module)
    probe = getattr(module, "list_instances", None) if module is not None else None
    if not callable(probe):
        return BackendStatus(
            backend=backend,
            available=False,
            reason="adapter-unavailable",
            detail=(f"module '{resolved.module}' exposes no list_instances (BackendDiscovery convention)"),
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
    wanted = _wanted_ids(backends)
    cfg = _resolve_config(config)
    logger.debug(
        "discover backends=%s offline=%s",
        sanitize(",".join(item.value for item in wanted)),
        offline,
    )
    rows = [await _status_for(item, config=cfg, offline=offline) for item in wanted]
    return DiscoveryReport(backends=tuple(rows), offline=offline)

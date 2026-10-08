"""§3 — Identity: BackendId, InstanceRef, InstanceRecord.

Platform truth lives solely in ``emuctl._platforms`` (imported here, never
redefined). Key validation seam: construction enforces the structural rules
(non-empty string, stripped); backend-specific ``spec.validate_key`` binds at
selection time once the spec table lands in a later PR — this module performs
no backend import (STATIC core→backend imports would violate the §6 layers
contract), so there is deliberately no registry hook here yet.

Parse rule (§3): ``InstanceRef.parse`` accepts canonical ``backend:key`` with
a KNOWN backend only. Every other input — colon-less bare shapes (bare
serial/name) AND colon-containing bare shapes (``host:port``,
``transport_id:N``) AND unknown ``head:key`` prefixes — is an
``InvalidArgumentError`` with the canonical-prefix hint. No shape inference.
``BackendNotFoundError`` is the domain of ``BackendId.from_string`` and
registry selection, never of ``parse``.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from emuctl._platforms import Platform, current_platform
from emuctl.errors import BackendNotFoundError, InvalidArgumentError

__all__ = [
    "Platform",
    "current_platform",
    "BackendId",
    "InstanceRef",
    "InstanceRecord",
]

#: PEP 685-style separator runs: case-fold, then collapse every run of
#: ``-_.``/``/`` into a single ``-`` before the value lookup.
_NORMALIZE_RE: Final[re.Pattern[str]] = re.compile(r"[-_./]+")


class BackendId(enum.StrEnum):
    """Pinned backend vocabulary (§3). Values are the canonical wire spellings."""

    ADB = "adb"
    MEMU = "memu"
    BLUESTACKS = "bluestacks"
    GOOGLE_PLAY = "google-play"
    AVD = "avd"
    BLUESTACKS_AIR = "bluestacks-air"

    @classmethod
    def from_string(cls, raw: str) -> BackendId:
        """Normalize ``raw`` (case-fold, ``_``/``.``/``/`` → ``-``) and look it up.

        Unknown ids raise ``BackendNotFoundError`` carrying the original text.
        Pure and I/O-free.
        """
        if not isinstance(raw, str):
            raise BackendNotFoundError(backend_id=repr(raw))
        candidate = _NORMALIZE_RE.sub("-", raw.strip().lower())
        try:
            return cls(candidate)
        except ValueError:
            raise BackendNotFoundError(backend_id=raw) from None


@dataclass(frozen=True, slots=True)
class InstanceRef:
    """Unambiguous address. Identity = (backend, key) ONLY.

    Transport serial/port, display labels, and run state are observations on
    :class:`InstanceRecord`, never identity. No inference from serial, name,
    host, port, or shape; parsing and canonicalization are pure (no I/O).
    """

    backend: BackendId
    key: str

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
        key = self.key
        if not isinstance(key, str) or not key.strip():
            raise InvalidArgumentError(
                parameter="key",
                constraint="must be a non-empty string",
            )
        object.__setattr__(self, "key", key.strip())
        # Backend-specific key shapes (vendor key semantics) are enforced via
        # spec.validate_key at selection time once the spec table exists; the
        # structural check above is the construction-time rule at v0.

    def __str__(self) -> str:
        """Canonical ``backend:key`` spelling (round-trips through :meth:`parse`)."""
        return f"{self.backend.value}:{self.key}"

    @classmethod
    def parse(cls, text: str) -> InstanceRef:
        """Parse a canonical ``backend:key`` string. Pure and I/O-free.

        A bare serial, host:port, or name (no ``backend:`` prefix) is an
        ``InvalidArgumentError`` telling the caller to prefix (e.g.
        ``'adb:127.0.0.1:5555'``) — no shape inference, no fan-out. The same
        ``InvalidArgumentError`` covers colon-containing bare shapes (e.g.
        ``'127.0.0.1:5555'``, ``'transport_id:5'``) and unknown ``head:key``
        prefixes: parse never infers a backend from shape, and parse failures
        are uniformly ``InvalidArgumentError``. ``BackendNotFoundError``
        remains the domain of ``BackendId.from_string`` and registry
        selection.
        """
        if not isinstance(text, str) or ":" not in text:
            raise InvalidArgumentError(
                parameter="target",
                constraint=(
                    f"use canonical 'backend:key' form (e.g. 'adb:127.0.0.1:5555'); got {text!r}"
                ),
            )
        head, _, key = text.partition(":")
        if not head.strip():
            raise InvalidArgumentError(
                parameter="target",
                constraint=(
                    f"use canonical 'backend:key' form (e.g. 'adb:127.0.0.1:5555'); got {text!r}"
                ),
            )
        try:
            backend = BackendId.from_string(head)
        except BackendNotFoundError:
            raise InvalidArgumentError(
                parameter="target",
                constraint=(
                    f"use canonical 'backend:key' form (e.g. 'adb:127.0.0.1:5555'); got {text!r}"
                ),
            ) from None
        return cls(backend=backend, key=key)


@dataclass(frozen=True, slots=True)
class InstanceRecord:
    """Point-in-time observation of an instance; not identity."""

    ref: InstanceRef
    running: bool
    serial: str | None
    details: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, InstanceRef):
            raise InvalidArgumentError(
                parameter="ref",
                constraint="must be an InstanceRef",
            )
        if not isinstance(self.running, bool):
            raise InvalidArgumentError(
                parameter="running",
                constraint="must be a bool",
            )
        if self.serial is not None and (not isinstance(self.serial, str) or not self.serial):
            raise InvalidArgumentError(
                parameter="serial",
                constraint="must be a non-empty string or None",
            )
        details = self.details
        if not isinstance(details, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in details.items()
        ):
            raise InvalidArgumentError(
                parameter="details",
                constraint="must be a mapping of str to str",
            )
        # Freeze the observation: a plain dict stays mutable behind a frozen
        # dataclass, so seal a copy (equality with plain mappings is preserved).
        object.__setattr__(self, "details", MappingProxyType(dict(details)))

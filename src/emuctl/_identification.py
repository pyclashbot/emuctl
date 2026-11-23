from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass
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

_NORMALIZE_RE: Final[re.Pattern[str]] = re.compile(r"[-_./]+")


class BackendId(enum.StrEnum):
    ADB = "adb"
    MEMU = "memu"
    BLUESTACKS = "bluestacks"
    GOOGLE_PLAY = "google-play"
    AVD = "avd"
    BLUESTACKS_AIR = "bluestacks-air"

    @classmethod
    def from_string(cls, raw: str) -> BackendId:
        if not isinstance(raw, str):
            raise BackendNotFoundError(backend_id=repr(raw))
        candidate = _NORMALIZE_RE.sub("-", raw.strip().lower())
        try:
            return cls(candidate)
        except ValueError:
            raise BackendNotFoundError(backend_id=raw) from None


@dataclass(frozen=True, slots=True)
class InstanceRef:
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

    def __str__(self) -> str:
        return f"{self.backend.value}:{self.key}"

    @classmethod
    def parse(cls, text: str) -> InstanceRef:
        if not isinstance(text, str) or ":" not in text:
            raise InvalidArgumentError(
                parameter="target",
                constraint=(f"use canonical 'backend:key' form (e.g. 'adb:127.0.0.1:5555'); got {text!r}"),
            )
        head, _, key = text.partition(":")
        if not head.strip():
            raise InvalidArgumentError(
                parameter="target",
                constraint=(f"use canonical 'backend:key' form (e.g. 'adb:127.0.0.1:5555'); got {text!r}"),
            )
        try:
            backend = BackendId.from_string(head)
        except BackendNotFoundError:
            raise InvalidArgumentError(
                parameter="target",
                constraint=(f"use canonical 'backend:key' form (e.g. 'adb:127.0.0.1:5555'); got {text!r}"),
            ) from None
        return cls(backend=backend, key=key)


@dataclass(frozen=True, slots=True)
class InstanceRecord:
    ref: InstanceRef
    running: bool
    serial: str | None
    details: Mapping[str, str]

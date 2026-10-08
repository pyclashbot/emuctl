"""§2 — Timeout table: the DEFAULT sentinel and per-op-group constants."""

from __future__ import annotations

import logging
import math
from typing import Final

from emuctl.errors import InvalidArgumentError

__all__ = [
    "DEFAULT",
    "TimeoutParam",
    "INTERACTION_TIMEOUT",
    "SNAPSHOT_TIMEOUT",
    "QUERY_TIMEOUT",
    "CONFIG_TIMEOUT",
    "APP_TIMEOUT",
    "LIFECYCLE_TIMEOUT",
    "PROVISION_TIMEOUT",
    "resolve_timeout",
]

logger = logging.getLogger(__name__)


class _DefaultType:
    """Type of the DEFAULT sentinel (unique instance below)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "DEFAULT"

    def __copy__(self) -> _DefaultType:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _DefaultType:
        return self


#: Sentinel selecting the op's group constant; distinct from None (explicit unbounded).
DEFAULT: Final[_DefaultType] = _DefaultType()

TimeoutParam = float | None | _DefaultType

#: interaction (click/swipe/key/text) 10 · snapshot 15 · query 20 · config 20 ·
#: app (install_apk, start_app) 60 · lifecycle (start/stop/restart) 120 · provision (create) 180.
INTERACTION_TIMEOUT: Final[float] = 10.0
SNAPSHOT_TIMEOUT: Final[float] = 15.0
QUERY_TIMEOUT: Final[float] = 20.0
CONFIG_TIMEOUT: Final[float] = 20.0
APP_TIMEOUT: Final[float] = 60.0
LIFECYCLE_TIMEOUT: Final[float] = 120.0
PROVISION_TIMEOUT: Final[float] = 180.0


def resolve_timeout(timeout: TimeoutParam, group_default: float, *, operation: str) -> float | None:
    """Resolve an op's `timeout` kwarg: DEFAULT → group constant, None → unbounded.

    `timeout <= 0` (or non-finite / non-numeric) raises InvalidArgumentError
    immediately, before any I/O. None is explicit unbounded: sanctioned,
    discouraged, DEBUG-logged.
    """
    if timeout is DEFAULT:
        return group_default
    if timeout is None:
        logger.debug("op=%s unbounded timeout (None); discouraged but explicit", operation)
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidArgumentError(
            parameter="timeout",
            constraint="must be a positive number of seconds, DEFAULT, or None",
        )
    if timeout <= 0 or not math.isfinite(timeout):
        raise InvalidArgumentError(parameter="timeout", constraint="must be > 0 seconds")
    return float(timeout)

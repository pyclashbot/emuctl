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
    __slots__ = ()

    def __repr__(self) -> str:
        return "DEFAULT"


DEFAULT: Final[_DefaultType] = _DefaultType()

TimeoutParam = float | None | _DefaultType

INTERACTION_TIMEOUT: Final[float] = 10.0
SNAPSHOT_TIMEOUT: Final[float] = 15.0
QUERY_TIMEOUT: Final[float] = 20.0
CONFIG_TIMEOUT: Final[float] = 20.0
APP_TIMEOUT: Final[float] = 60.0
LIFECYCLE_TIMEOUT: Final[float] = 120.0
PROVISION_TIMEOUT: Final[float] = 180.0


def resolve_timeout(timeout: TimeoutParam, group_default: float, *, operation: str) -> float | None:
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

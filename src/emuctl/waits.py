from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable

from emuctl.errors import InvalidArgumentError, OperationTimeoutError
from emuctl.timing import DEFAULT

__all__ = ["wait_until"]

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.05


def _validate_timeout(timeout: float) -> float:
    if timeout is DEFAULT or timeout is None:
        raise InvalidArgumentError(
            parameter="timeout",
            constraint="wait_until requires an explicit positive timeout in seconds",
        )
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidArgumentError(
            parameter="timeout",
            constraint="wait_until requires an explicit positive timeout in seconds",
        )
    if timeout <= 0 or not math.isfinite(timeout):
        raise InvalidArgumentError(parameter="timeout", constraint="must be > 0 seconds")
    return float(timeout)


async def wait_until(
    predicate: Callable[[], bool | Awaitable[bool]],
    *,
    operation: str,
    timeout: float,
) -> None:
    budget = _validate_timeout(timeout)
    start = time.monotonic()
    deadline = start + budget
    checks = 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise OperationTimeoutError(operation=operation, deadline=deadline, elapsed=now - start)
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        checks += 1
        if result:
            logger.debug("op=%s ready after checks=%d", operation, checks)
            return None
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(_POLL_INTERVAL_S, max(remaining, 0.0)))

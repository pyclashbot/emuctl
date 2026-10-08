"""§7.2 conformance: deadline honored, timeout payloads, no cancel conflation."""

import asyncio
import inspect
import logging
import time
from typing import Any

import pytest

from emuctl import timing
from emuctl.errors import InvalidArgumentError, OperationTimeoutError
from emuctl.timing import (
    APP_TIMEOUT,
    CONFIG_TIMEOUT,
    DEFAULT,
    INTERACTION_TIMEOUT,
    LIFECYCLE_TIMEOUT,
    PROVISION_TIMEOUT,
    QUERY_TIMEOUT,
    SNAPSHOT_TIMEOUT,
    resolve_timeout,
)
from emuctl.waits import wait_until


def test_group_constants_pinned() -> None:
    assert INTERACTION_TIMEOUT == 10
    assert SNAPSHOT_TIMEOUT == 15
    assert QUERY_TIMEOUT == 20
    assert CONFIG_TIMEOUT == 20
    assert APP_TIMEOUT == 60
    assert LIFECYCLE_TIMEOUT == 120
    assert PROVISION_TIMEOUT == 180


def test_default_sentinel_unique() -> None:
    assert DEFAULT is not None
    assert not isinstance(DEFAULT, float)
    assert repr(DEFAULT) == "DEFAULT"
    assert timing.DEFAULT is DEFAULT


def test_resolve_timeout_default_and_value() -> None:
    assert resolve_timeout(DEFAULT, 120, operation="start") == 120
    assert resolve_timeout(5, 120, operation="start") == 5.0


def test_resolve_timeout_none_is_explicit_unbounded_with_debug_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="emuctl.timing"):
        assert resolve_timeout(None, 120, operation="start") is None
    assert any(r.levelno == logging.DEBUG and r.name == "emuctl.timing" for r in caplog.records)


@pytest.mark.parametrize("bad", [0, -1, -0.5, float("nan"), float("inf"), "10", True])
def test_resolve_timeout_rejects_non_positive(bad: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        resolve_timeout(bad, 120, operation="start")


def test_wait_until_timeout_required_keyword_only() -> None:
    sig = inspect.signature(wait_until)
    param = sig.parameters["timeout"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_wait_until_returns_on_truthy() -> None:
    calls: list[int] = []

    def predicate() -> bool:
        calls.append(1)
        return len(calls) >= 3

    assert asyncio.run(wait_until(predicate, operation="ready", timeout=5.0)) is None
    assert len(calls) == 3


def test_wait_until_supports_async_predicate() -> None:
    async def predicate() -> bool:
        return True

    assert asyncio.run(wait_until(predicate, operation="ready", timeout=5.0)) is None


def test_wait_until_rejects_bad_timeout_before_any_io() -> None:
    calls: list[int] = []

    def predicate() -> bool:
        calls.append(1)
        return True

    with pytest.raises(InvalidArgumentError):
        asyncio.run(wait_until(predicate, operation="ready", timeout=0))
    with pytest.raises(InvalidArgumentError):
        asyncio.run(
            wait_until(predicate, operation="ready", timeout=None)  # ty: ignore[invalid-argument-type]
        )
    with pytest.raises(InvalidArgumentError):
        asyncio.run(
            wait_until(predicate, operation="ready", timeout=DEFAULT)  # ty: ignore[invalid-argument-type]
        )
    assert calls == []


def test_wait_until_predicate_exceptions_propagate_unchanged() -> None:
    marker = ValueError("boom")

    def predicate() -> bool:
        raise marker

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(wait_until(predicate, operation="ready", timeout=5.0))
    assert excinfo.value is marker


def test_wait_until_expiry_raises_operation_timeout() -> None:
    before = time.monotonic()
    with pytest.raises(OperationTimeoutError) as excinfo:
        asyncio.run(wait_until(lambda: False, operation="boot", timeout=0.15))
    exc = excinfo.value
    assert exc.operation == "boot"
    # deadline is the absolute monotonic deadline (start + budget), not the duration.
    assert exc.deadline == pytest.approx(before + 0.15, abs=1.0)
    assert exc.deadline is not None and exc.deadline > 0.15
    # Windows timer granularity (≈15.6 ms default tick) can report an elapsed
    # a hair under the nominal budget even though the full budget was awaited.
    assert exc.elapsed is not None and exc.elapsed >= 0.15 - 0.02
    assert exc.elapsed < 5.0


def test_wait_until_deadline_honored_under_injected_slowness() -> None:
    async def slow_false() -> bool:
        await asyncio.sleep(0.3)
        return False

    with pytest.raises(OperationTimeoutError):
        asyncio.run(wait_until(slow_false, operation="boot", timeout=0.05))


def test_wait_until_cancellation_not_conflated_with_timeout() -> None:
    async def main() -> None:
        task = asyncio.ensure_future(wait_until(lambda: False, operation="boot", timeout=30.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

"""§7 conformance (sync facade, §5/§2/§7.5): the portal bridge's locked
mechanics — lazy named portal through the sole `_call` funnel, running-loop
detection pre-portal, kwarg-for-kwarg parity with the `.aio` surface, timeout
parity (DEFAULT/None/pre-portal invalids), exception identity across the
portal, ReadyCheck normalization (worker-thread re-entry, async predicates,
predicate failures, the shared restart budget), and close() semantics
(idempotent, permanent, cancel-first, transport-only, from-any-thread)."""

from __future__ import annotations

import asyncio
import functools
import inspect
import struct
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

import pytest
from anyio.from_thread import BlockingPortal

from emuctl._identification import BackendId, InstanceRef
from emuctl._proc import ProcessExecutor
from emuctl._sync import PORTAL_THREAD_NAME, EmulatorController, connect_sync
from emuctl.config.model import CreateSpec, DisplayConfig, EmuctlConfig
from emuctl.errors import (
    BackendNotAvailableError,
    ControllerClosedError,
    EmulatorNotReadyError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    OperationTimeoutError,
    SyncCallInsideEventLoopError,
    VendorCommandError,
    resolve_error_code,
)
from emuctl.port import AsyncEmulatorController, CapabilityFlag
from emuctl.timing import (
    APP_TIMEOUT,
    CONFIG_TIMEOUT,
    INTERACTION_TIMEOUT,
    LIFECYCLE_TIMEOUT,
    PROVISION_TIMEOUT,
    QUERY_TIMEOUT,
    SNAPSHOT_TIMEOUT,
)

SERIAL = "emulator-5554"
REF = InstanceRef(backend=BackendId.ADB, key=SERIAL)


def _sync(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run an async test body to completion on a fresh loop (repo convention)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    """Leave no `emuctl.backends*` trace (see test_registry/test_discovery)."""
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n.startswith("emuctl.backends")]:
        if name not in before:
            evicted = sys.modules.pop(name)
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, child, None) is evicted:
                delattr(parent, child)


def _png(width: int = 800, height: int = 600) -> bytes:
    """Minimal bytes png_dimensions accepts (signature + IHDR; no CRC check)."""
    return (
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _thread_names() -> set[str]:
    return {t.name for t in threading.enumerate()}


def _wait_for_thread_teardown(deadline_s: float = 5.0) -> None:
    """Bounded wait for portal-thread OS teardown to settle after close().

    close() joins the portal coroutine, but on a loaded runner the pinned
    portal thread (or a lingering AnyIO to_thread worker it used) can outlive
    the call by a scheduling beat. Poll (50 ms) until both vanish; fail if
    they outlive the window — the contract is that close() JOINS the thread,
    so leaving a survivor past the deadline is a real failure.
    """

    def _survivors() -> set[str]:
        return {name for name in _thread_names() if PORTAL_THREAD_NAME in name or "AnyIO" in name}

    deadline = time.monotonic() + deadline_s
    while _survivors():
        assert time.monotonic() < deadline, (
            f"{PORTAL_THREAD_NAME!r}/AnyIO teardown did not settle within "
            f"{deadline_s}s after close() (threads: {sorted(_thread_names())})"
        )
        time.sleep(0.05)


def _wait_for_no_controller_threads(before: set[str], deadline_s: float = 5.0) -> None:
    """Bounded wait for a settled absence of OUR background threads.

    Windows CI: close()'s deferred ``emuctl-portal-join`` helper can spawn a
    scheduling beat behind a clean teardown sample, so a one-shot global
    thread-set snapshot can contradict itself on a loaded runner. Poll the
    §5 no-leak invariant — no portal/AnyIO/join thread that postdates
    ``before`` — instead of demanding the runner's exact global thread set.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        ours = {
            name
            for name in _thread_names() - before
            if PORTAL_THREAD_NAME in name or "AnyIO" in name
        }
        if not ours:
            return
        assert time.monotonic() < deadline, f"our threads survived: {sorted(ours)}"
        time.sleep(0.05)


class _FakeController(AsyncEmulatorController):
    """In-memory fake of the port: records every op + the budget the async
    layer resolved (timeout parity evidence), optional failure, optional
    externally-released hang (abandoned-caller shape)."""

    FEATURES = frozenset(CapabilityFlag)

    def __init__(self, ref: InstanceRef = REF) -> None:
        super().__init__(ref)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.budgets: dict[str, float | None] = {}
        self.stop_count = 0
        self.fail: VendorCommandError | None = None
        self.release: threading.Event | None = None
        self._png = _png()

    def _record(self, op: str, *args: Any, timeout: float | None = None, **extra: Any) -> None:
        self.calls.append((op, args))
        if timeout is not None or op not in self.budgets:
            self.budgets[op] = timeout
        if self.fail is not None:
            raise self.fail

    async def _pause(self, timeout: float | None) -> None:
        if self.release is not None:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        elif timeout is not None:
            await asyncio.sleep(min(timeout, 0.0))

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        raise VendorCommandError(
            command=list(argv),
            returncode=1,
            output_reason="exit",
            stderr_excerpt="fake",
        )

    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> Any:
        self._record("_get_instance", ref, timeout=timeout)
        return None

    async def _start(self, timeout: float | None) -> None:
        await self._pause(None)
        self._record("_start", timeout=timeout)

    async def _stop(self, timeout: float | None) -> None:
        self.stop_count += 1
        await self._pause(None)
        self._record("_stop", timeout=timeout)

    async def _is_running(self, timeout: float | None) -> bool:
        self._record("_is_running", timeout=timeout)
        return False

    async def _create(self, spec: CreateSpec, timeout: float | None) -> InstanceRef:
        await self._pause(None)
        self._record("_create", spec, timeout=timeout)
        return InstanceRef(backend=self._ref.backend, key=spec.name)

    async def _configure(self, spec: Any, timeout: float | None) -> None:
        await self._pause(None)
        self._record("_configure", spec, timeout=timeout)

    async def _screenshot(self, timeout: float | None) -> bytes:
        await self._pause(timeout)
        self._record("_screenshot", timeout=timeout)
        return self._png

    async def _click(
        self, x: int, y: int, *, count: int, interval: float, timeout: float | None
    ) -> None:
        await self._pause(None)
        self._record("_click", x, y, count=count, interval=interval, timeout=timeout)

    async def _swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int,
        timeout: float | None,
    ) -> None:
        await self._pause(None)
        self._record("_swipe", x1, y1, x2, y2, duration_ms=duration_ms, timeout=timeout)

    async def _key(self, code: str | int, timeout: float | None) -> None:
        await self._pause(None)
        self._record("_key", code, timeout=timeout)

    async def _text(self, value: str, timeout: float | None) -> None:
        await self._pause(None)
        self._record("_text", value, timeout=timeout)

    async def _start_app(self, package: str, timeout: float | None) -> None:
        await self._pause(None)
        self._record("_start_app", package, timeout=timeout)

    async def _is_app_installed(self, package: str, timeout: float | None) -> bool:
        await self._pause(None)
        self._record("_is_app_installed", package, timeout=timeout)
        return True

    async def _install_apk(self, apk_path: Any, timeout: float | None) -> None:
        await self._pause(None)
        self._record("_install_apk", apk_path, timeout=timeout)


@contextmanager
def _facade(slow_or_fail: Any = None) -> Generator[EmulatorController]:
    """A facade over a fresh fake, closed on exit if still open."""
    fake = _FakeController()
    if slow_or_fail is not None:
        fake.fail = slow_or_fail
    controller = EmulatorController(fake)
    try:
        yield controller
    finally:
        if not controller._closed:
            controller.close()


def _spy_funnel(monkeypatch: Any) -> list[str]:
    """Patch `EmulatorController._call` to record every funnel entry (the §5
    "one-line delegation through _call()" pin)."""
    ops: list[str] = []
    original = EmulatorController._call

    def spy(self: EmulatorController, op: str, *args: Any, **kwargs: Any) -> Any:
        ops.append(op)
        return original(self, op, *args, **kwargs)

    monkeypatch.setattr(EmulatorController, "_call", spy)
    return ops


# ── lazy portal + funnel (§5) ──


def test_aio_only_usage_creates_zero_threads() -> None:
    fake = _FakeController()
    controller = EmulatorController(fake)
    before = _thread_names()
    assert controller.aio is fake
    assert CapabilityFlag.INPUT in controller.features
    assert controller.has_feature(CapabilityFlag.SCREENSHOT)
    _wait_for_no_controller_threads(before)
    assert controller._portal is None
    assert fake.calls == []  # no I/O at all


def test_portal_lazily_started_named_and_sole_funnel(monkeypatch: Any) -> None:
    ops = _spy_funnel(monkeypatch)
    with _facade() as controller:
        assert PORTAL_THREAD_NAME not in _thread_names()
        png = controller.screenshot()
        assert isinstance(png, bytes) and png.startswith(b"\x89PNG")
        assert PORTAL_THREAD_NAME in _thread_names()
        portal_threads = [t for t in threading.enumerate() if t.name == PORTAL_THREAD_NAME]
        assert len(portal_threads) == 1
        assert ops == ["screenshot"]  # the SOLE funnel, once per op
        controller.is_app_installed("com.example")
        assert ops == ["screenshot", "is_app_installed"]


def test_second_facade_gets_its_own_portal() -> None:
    with _facade() as first, _facade() as second:
        first.screenshot()
        second.screenshot()
        portals = [t for t in threading.enumerate() if t.name == PORTAL_THREAD_NAME]
        assert len(portals) == 2  # one per facade, never process-global


# ── running-loop detection BEFORE portal work (§5) ──


def test_loop_detection_fires_pre_portal_with_hatch() -> None:
    controller = EmulatorController(_FakeController())
    before = _thread_names()
    messages: list[str] = []

    async def inside() -> None:
        with pytest.raises(SyncCallInsideEventLoopError) as caught:
            controller.click(1, 2)
        messages.append(str(caught.value))

    asyncio.run(inside())
    assert messages, "the error never fired inside the loop"
    assert "await emulator.aio.click" in messages[0]
    _wait_for_no_controller_threads(before)
    assert controller._portal is None


# ── kwarg-for-kwarg parity (§5) ──

#: Every public `.aio` op that has a sync twin (§5 parity block).
TWIN_OPS = (
    "restart",
    "click",
    "swipe",
    "key",
    "text",
    "screenshot",
    "is_reachable",
    "install_apk",
    "start_app",
    "is_app_installed",
    "create",
    "configure",
    "start",
    "stop",
    "aclose",
    "close",
)


@pytest.mark.parametrize("op", TWIN_OPS)
def test_sync_twin_signature_matches_async_counterpart(op: str) -> None:
    def shape(obj: Any) -> list[tuple[str, Any, Any]]:
        params = list(inspect.signature(obj).parameters.values())[1:]  # minus self
        return [(p.name, p.kind, p.default) for p in params]

    assert shape(getattr(EmulatorController, op)) == shape(getattr(AsyncEmulatorController, op)), op


# ── timeout parity (§2/§5) ──


def test_default_resolves_to_group_constant_at_both_layers() -> None:
    with _facade() as controller:
        controller.screenshot()
        controller.click(1, 2)
        controller.swipe(1, 2, 3, 4)
        controller.key("KEYCODE_ENTER")
        controller.text("hi")
        controller.is_app_installed("com.example")
        controller.is_reachable()
        controller.start_app("com.example")
        controller.install_apk("x.apk")
        controller.create(CreateSpec(name="fresh"))
        controller.configure()
        controller.start()
        controller.stop()
        fake = cast(_FakeController, controller.aio)
        assert fake.budgets["_screenshot"] == SNAPSHOT_TIMEOUT
        assert fake.budgets["_click"] == INTERACTION_TIMEOUT
        assert fake.budgets["_swipe"] == INTERACTION_TIMEOUT
        assert fake.budgets["_key"] == INTERACTION_TIMEOUT
        assert fake.budgets["_text"] == INTERACTION_TIMEOUT
        assert fake.budgets["_is_app_installed"] == QUERY_TIMEOUT
        assert fake.budgets["_start_app"] == APP_TIMEOUT
        assert fake.budgets["_install_apk"] == APP_TIMEOUT
        create_budget = fake.budgets["_create"]
        assert create_budget is not None
        assert (
            abs(create_budget - PROVISION_TIMEOUT) < 1.0
        )  # the REMAINING slice, not the full budget anew
        assert fake.budgets["_configure"] == CONFIG_TIMEOUT
        assert fake.budgets["_start"] == LIFECYCLE_TIMEOUT
        assert fake.budgets["_stop"] == LIFECYCLE_TIMEOUT


def test_none_is_explicit_unbounded_end_to_end() -> None:
    with _facade() as controller:
        controller.screenshot(timeout=None)
        controller.start(timeout=None)
        fake = cast(_FakeController, controller.aio)
        assert fake.budgets["_screenshot"] is None
        assert fake.budgets["_start"] is None


def test_invalid_timeout_raises_pre_portal_thread_free() -> None:
    controller = EmulatorController(_FakeController())
    before = _thread_names()
    for bad in (-1, 0, "soon", True, float("inf")):
        with pytest.raises(InvalidArgumentError):
            controller.screenshot(timeout=bad)  # ty: ignore[invalid-argument-type]
    _wait_for_no_controller_threads(before)
    assert controller._portal is None
    assert cast(_FakeController, controller.aio).calls == []


# ── exception identity across the portal (§5: no bridge rewrapping) ──


def test_typed_errors_cross_the_portal_same_class_and_code() -> None:
    boom = VendorCommandError(
        command=["adb", "shell"],
        returncode=1,
        output_reason="exit",
        stderr_excerpt="device not found",
    )
    with _facade(boom) as controller:
        with pytest.raises(VendorCommandError) as caught:
            controller.screenshot()
        err = caught.value
        assert type(err) is VendorCommandError
        assert err.command == boom.command
        assert err.returncode == boom.returncode
        assert err.output_reason == boom.output_reason
        assert err.stderr_excerpt == boom.stderr_excerpt
        assert resolve_error_code(err) == resolve_error_code(boom)


def test_feature_gate_refusal_crosses_the_portal() -> None:
    class _InputOnly(_FakeController):
        FEATURES = frozenset({CapabilityFlag.INPUT})

    gate = EmulatorController(_InputOnly())
    with pytest.raises(FeatureNotSupportedError) as caught:
        gate.screenshot()
    assert resolve_error_code(caught.value) == "feature_not_supported"
    gate.close()


def test_not_ready_error_carries_cause_across_the_portal() -> None:
    with _facade() as controller:
        fake = cast(_FakeController, controller.aio)

        def failing_pred(anything: Any) -> bool:
            raise RuntimeError("boom")

        with pytest.raises(EmulatorNotReadyError) as caught:
            controller.restart(ready=failing_pred)
        assert caught.value.reason == "ready_check_failed"
        assert isinstance(caught.value.cause, RuntimeError)
        assert caught.value.cause.args == ("boom",)
        assert resolve_error_code(caught.value) == "not_ready"
        assert fake.calls, "restart never ran"


# ── ReadyCheck normalization (§5 / §7.5) ──


def test_sync_predicate_reenters_the_funnel_without_deadlock(
    monkeypatch: Any,
) -> None:
    ops = _spy_funnel(monkeypatch)
    with _facade() as controller:
        collected: list[Any] = []

        def pred(controller_arg: Any) -> bool:
            # The §7.5 shape: a sync predicate calling facade methods — normal
            # funnel, no re-entry into the loop, no deadlock.
            collected.append(controller_arg is controller)
            collected.append(controller.screenshot())
            collected.append(controller.is_app_installed("com.example.app"))
            return True

        controller.restart(ready=pred)
        assert collected[0] is True  # invoked against the FACADE
        assert collected[-1] is True
        # funnel entries: restart itself + the two predicate calls
        assert ops == ["restart", "screenshot", "is_app_installed"]


def test_async_predicate_runs_on_the_portal_loop() -> None:
    with _facade() as controller:
        threads: list[str] = []
        controllers: list[Any] = []

        async def pred(async_controller: Any) -> bool:
            threads.append(threading.current_thread().name)
            controllers.append(async_controller is controller.aio)
            return True

        controller.restart(ready=pred)
        assert threads == [PORTAL_THREAD_NAME]  # awaited on the loop
        assert controllers == [True]  # with the async controller


def test_sync_callable_returning_awaitable_is_normalized() -> None:
    with _facade() as controller:
        ran: list[str] = []

        def pred(controller_arg: Any) -> Any:  # sync def, awaitable answer
            ran.append(threading.current_thread().name)
            return _delayed_true(controller_arg)

        async def _delayed_true(_ctl: Any) -> bool:
            await asyncio.sleep(0.01)
            return True

        controller.restart(ready=pred)
        assert ran and ran[0] != PORTAL_THREAD_NAME


def test_shared_budget_caps_a_hanging_predicate() -> None:
    release = threading.Event()

    with _facade() as controller:

        def hanging_pred(_controller: Any) -> bool:
            release.wait(5.0)
            return True

        started = time.monotonic()
        with pytest.raises(EmulatorNotReadyError) as caught:
            controller.restart(timeout=0.5, ready=hanging_pred)
        elapsed = time.monotonic() - started
        release.set()
        assert elapsed < 3.0, "the shared remaining budget was not honored"
        assert caught.value.reason == "timeout"
        assert isinstance(caught.value.cause, OperationTimeoutError)
        assert caught.value.cause.operation == "restart"


def test_predicate_may_interact_and_wait_stays_budgeted() -> None:
    with _facade() as controller:
        frames: list[bytes] = []

        def interactive_pred(_controller: Any) -> bool:
            # The predicate MAY interact (§5: launch-and-verify patterns); the
            # terminal check itself is one-shot, so interactions happen here.
            for _ in range(3):
                frames.append(_controller.screenshot())
            return True

        controller.restart(ready=interactive_pred)
        assert len(frames) == 3
        fake = cast(_FakeController, controller.aio)
        assert fake.calls.count(("_screenshot", ())) >= 3


# ── close() semantics (§5/§3) ──


def test_close_is_idempotent_permanent_and_joins_the_thread() -> None:
    before = _thread_names()
    with _facade() as controller:
        controller.screenshot()  # portal starts
        assert PORTAL_THREAD_NAME in _thread_names()
        controller.close()
        controller.close()  # idempotent
        _wait_for_thread_teardown()  # bounded join-pin, no racy snapshot
        assert PORTAL_THREAD_NAME not in _thread_names()
        assert not any("AnyIO" in name for name in _thread_names())
        _wait_for_no_controller_threads(before)
        assert controller._closed  # permanent
        with pytest.raises(ControllerClosedError):
            controller.screenshot()
        with pytest.raises(ControllerClosedError):
            controller.restart()
    _wait_for_no_controller_threads(before)


def test_close_releases_transport_and_never_stops_the_emulator() -> None:
    with _facade() as controller:
        fake = cast(_FakeController, controller.aio)
        controller.close()
    assert fake._closed  # transport released (aclose ran)
    assert fake.stop_count == 0  # NEVER a stop side effect
    assert ("stop", tuple()) not in fake.calls


def test_sync_aclose_twin_funnels_without_killing_the_facade() -> None:
    with _facade() as controller:
        fake = cast(_FakeController, controller.aio)
        controller.aclose()
        assert fake._closed
        assert fake.stop_count == 0
        # the async port's own closed-guard surfaces through the funnel:
        with pytest.raises(ControllerClosedError):
            controller.text("nope")


def test_cm_exit_closes_and_rejects_later_ops() -> None:
    before = _thread_names()
    png: bytes | None = None
    with EmulatorController(_FakeController()) as controller:
        png = controller.screenshot()
    assert png is not None and len(png) > 8
    _wait_for_thread_teardown()  # bounded join-pin, no racy snapshot
    assert not any("AnyIO" in name for name in _thread_names())
    _wait_for_no_controller_threads(before)
    with pytest.raises(ControllerClosedError):
        controller.click(1, 2)


def test_close_from_aio_only_facade_never_starts_a_thread() -> None:
    before = _thread_names()
    controller = EmulatorController(_FakeController())
    controller.close()
    _wait_for_no_controller_threads(before)
    assert controller._closed
    with pytest.raises(ControllerClosedError):
        controller.screenshot()


def test_abandoned_in_flight_caller_reraises_controller_closed() -> None:
    release = threading.Event()
    fake = _FakeController()
    fake.release = release  # the op hangs until the test lets go
    controller = EmulatorController(fake)
    closer = threading.Timer(0.25, controller.close)
    try:
        started = time.monotonic()
        closer.start()
        with pytest.raises(ControllerClosedError) as caught:
            controller.screenshot(timeout=30.0)
        assert time.monotonic() - started < 3.0  # cancel-first, not 30s
        assert isinstance(caught.value, ControllerClosedError)
    finally:
        closer.cancel()
        release.set()
    assert fake.stop_count == 0  # the abandoned op was never stopped-into


def test_close_inside_predicate_worker_completes_without_deadlock() -> None:
    """The §7.5 deadlock checker: close() invoked from a sync ReadyCheck
    predicate running in the portal's worker thread."""
    release = threading.Event()
    with _facade() as controller:

        def closing_pred(_controller: Any) -> bool:
            controller.close()
            release.wait(5.0)  # force the abandoned-caller path deterministically
            return True

        with pytest.raises(ControllerClosedError):
            # the outer restart is cancelled by close() like any in-flight op
            controller.restart(ready=closing_pred)
        release.set()
    assert controller._closed
    with pytest.raises(ControllerClosedError):
        controller.screenshot()


def test_op_on_dead_facade_is_typed_even_for_internal_drain() -> None:
    with _facade() as controller:
        controller.close()
        with pytest.raises(ControllerClosedError):
            controller.create(CreateSpec(name="x"))


def test_close_from_portal_loop_predicate_completes_without_deadlock() -> None:
    """The docstring's claimed shape: close() invoked from a predicate running
    ON the portal loop completes (non-blocking drain, no deadlock), the portal
    drains, and the abandoned caller gets the typed error."""
    deadline = time.monotonic() + 5.0
    with _facade() as controller:
        controller.is_app_installed("com.example")  # portal started

        async def closing_pred(_controller: Any) -> bool:
            controller.close()
            await asyncio.sleep(0.05)  # let the non-blocking drain run first
            return True

        with pytest.raises(ControllerClosedError):
            controller.restart(ready=closing_pred)
        assert controller._closed
        while PORTAL_THREAD_NAME in _thread_names():
            assert time.monotonic() < deadline, "portal thread never drained"
            time.sleep(0.01)
    assert controller._closed
    with pytest.raises(ControllerClosedError):
        controller.screenshot()


def test_close_vs_first_call_race_all_outcomes_typed_and_no_leak() -> None:
    """Stress pin: close() racing the first op; the loser always raises
    ``ControllerClosedError``, and no portal thread ever survives."""
    before = _thread_names()
    for _ in range(25):
        controller = EmulatorController(_FakeController())
        barrier = threading.Barrier(2)
        outcomes: list[BaseException | None] = []

        def opener(controller=controller, barrier=barrier, outcomes=outcomes) -> None:
            barrier.wait(2)
            try:
                controller.screenshot(timeout=0.2)
            except BaseException as exc:  # noqa: BLE001 - the pinned loser shape
                outcomes.append(exc)
            else:
                outcomes.append(None)

        def closer(controller=controller, barrier=barrier) -> None:
            barrier.wait(2)
            controller.close()

        opener_thread = threading.Thread(target=opener)
        closer_thread = threading.Thread(target=closer)
        opener_thread.start()
        closer_thread.start()
        opener_thread.join(5.0)
        closer_thread.join(5.0)
        assert not opener_thread.is_alive()
        assert not closer_thread.is_alive()
        for outcome in outcomes:
            assert outcome is None or isinstance(outcome, ControllerClosedError), outcome
        assert controller._closed
        _wait_for_thread_teardown()  # per-iteration bounded join-pin
        _wait_for_no_controller_threads(before)
        controller.close()  # idempotent, joins nothing leaked
    _wait_for_no_controller_threads(before)


def test_keyboard_interrupt_abandons_and_facade_stays_usable(monkeypatch: Any) -> None:
    """An interrupt through the funnel is abandon (§5): the op raises KI and
    the still-running facade remains usable afterwards."""
    with _facade() as controller:
        controller.screenshot()  # portal started

        def interrupted(_self: Any, _fn: Any, *args: Any, **kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(BlockingPortal, "call", interrupted)
        with pytest.raises(KeyboardInterrupt):
            controller.screenshot()
        monkeypatch.undo()
        png = controller.screenshot()  # the facade is NOT poisoned
        assert png.startswith(b"\x89PNG")
        controller.close()
    assert controller._closed


# ── connect_sync (§5 construction, gate chain run synchronously) ──


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("not-a-ref", InvalidArgumentError),
        (123, InvalidArgumentError),
        ("nope:key", InvalidArgumentError),
        ("avd:whatever", BackendNotAvailableError),
        ("google-play:pkg", BackendNotAvailableError),
    ],
)
def test_connect_sync_refusals_raise_pre_portal_thread_free(
    target: Any, expected: type[BaseException]
) -> None:
    before = _thread_names()
    with pytest.raises(expected):
        connect_sync(target)
    _wait_for_no_controller_threads(before)


def test_connect_sync_config_refusals_are_pre_portal() -> None:
    before = _thread_names()
    with pytest.raises(InvalidArgumentError):
        connect_sync("adb:emulator-5554", config="nope")  # ty: ignore[invalid-argument-type]
    # §4 capability gate runs at construction: plain ADB never declares CONFIGURE
    gated = EmuctlConfig(display=DisplayConfig(width=1080, height=1920))
    with pytest.raises(FeatureNotSupportedError):
        connect_sync("adb:emulator-5554", config=gated)
    _wait_for_no_controller_threads(before)


def test_connect_sync_happy_path_binds_and_closes_threadless() -> None:
    before = _thread_names()
    facade = connect_sync("adb:127.0.0.1:5555")
    assert isinstance(facade, EmulatorController)
    assert CapabilityFlag.SCREENSHOT in facade.features
    assert facade._portal is None  # construction starts NO portal
    facade.close()  # aio-only teardown: zero threads, transport-only
    _wait_for_no_controller_threads(before)


# ── real adapter over the _proc/_executor seam (adapter-test precedent) ──


class _FakeHandle:
    """Minimal child-process double for the ``_spawn`` seam."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode

    @property
    def pid(self) -> int | None:
        return 4242

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return (self._stdout, self._stderr)


def _spawn_factory(
    calls: list[list[str]],
    respond: Callable[[list[str]], _FakeHandle],
) -> Callable[[Sequence[str]], Any]:
    async def _spawn(argv: Sequence[str]) -> _FakeHandle:
        calls.append(list(argv))
        return respond(list(argv))

    return _spawn


def _handle(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
) -> Callable[[list[str]], _FakeHandle]:
    return lambda argv: _FakeHandle(stdout=stdout, stderr=stderr, returncode=returncode)


def _adb_facade(
    calls: list[list[str]], respond: Callable[[list[str]], _FakeHandle]
) -> EmulatorController:
    """importlib lazily: never at collection time (adapter-test precedent)."""
    import importlib

    adb_module = importlib.import_module("emuctl.backends.adb")
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    controller = adb_module.AdbController(REF, config=EmuctlConfig(), _executor=executor)
    return EmulatorController(controller)


def test_facade_drives_the_real_adb_transport_argv_exact() -> None:
    calls: list[list[str]] = []
    facade = _adb_facade(calls, _handle(stdout=_png()))
    try:
        png = facade.screenshot()
        assert png == _png()
        facade.click(10, 20, count=1, interval=0.0)
        facade.text("hello")
    finally:
        facade.close()
    shots = [tuple(c[-3:]) for c in calls]
    assert ("exec-out", "screencap", "-p") in shots
    assert any(tuple(c[-5:]) == ("shell", "input", "tap", "10", "20") for c in calls)
    assert any(tuple(c[-2:]) == ("text", "hello") for c in calls)


def test_vendor_failure_crosses_the_facade_typed() -> None:
    calls: list[list[str]] = []
    facade = _adb_facade(calls, _handle(stderr=b"boom", returncode=1))
    try:
        with pytest.raises(VendorCommandError) as caught:
            facade.screenshot()
        assert caught.value.output_reason == "exit"
        assert caught.value.returncode == 1
        assert resolve_error_code(caught.value) == "vendor_command_failed"
    finally:
        facade.close()

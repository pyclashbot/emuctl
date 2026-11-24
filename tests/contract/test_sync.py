from __future__ import annotations

import asyncio
import functools
import inspect
import os
import struct
import subprocess
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
    FeatureNotSupportedError,
    InvalidArgumentError,
    SyncCallInsideEventLoopError,
    VendorCommandError,
    resolve_error_code,
)
from emuctl.port import AsyncEmulatorController, CapabilityFlag, DisplayState
from emuctl.timing import (
    APP_TIMEOUT,
    CONFIG_TIMEOUT,
    INTERACTION_TIMEOUT,
    QUERY_TIMEOUT,
    SNAPSHOT_TIMEOUT,
)

SERIAL = "emulator-5554"
REF = InstanceRef(backend=BackendId.ADB, key=SERIAL)


def _sync(fn: Callable[..., Any]) -> Callable[..., Any]:

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
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
    deadline = time.monotonic() + deadline_s
    while True:
        ours = {name for name in _thread_names() - before if PORTAL_THREAD_NAME in name or "AnyIO" in name}
        if not ours:
            return
        assert time.monotonic() < deadline, f"our threads survived: {sorted(ours)}"
        time.sleep(0.05)


class _FakeController(AsyncEmulatorController):
    FEATURES = frozenset(
        {
            CapabilityFlag.CONFIGURE,
            CapabilityFlag.SCREENSHOT,
            CapabilityFlag.INPUT,
            CapabilityFlag.APPS,
            CapabilityFlag.APK_INSTALL,
        }
    )

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

    async def _configure(self, spec: Any, timeout: float | None) -> DisplayState:
        await self._pause(None)
        self._record("_configure", spec, timeout=timeout)
        return DisplayState(None, None, None)

    async def _screenshot(self, timeout: float | None) -> bytes:
        await self._pause(timeout)
        self._record("_screenshot", timeout=timeout)
        return self._png

    async def _click(self, x: int, y: int, *, count: int, interval: float, timeout: float | None) -> None:
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
    "click",
    "swipe",
    "key",
    "text",
    "screenshot",
    "is_reachable",
    "install_apk",
    "start_app",
    "is_app_installed",
    "configure",
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
        controller.configure()
        fake = cast(_FakeController, controller.aio)
        assert fake.budgets["_screenshot"] == SNAPSHOT_TIMEOUT
        assert fake.budgets["_click"] == INTERACTION_TIMEOUT
        assert fake.budgets["_swipe"] == INTERACTION_TIMEOUT
        assert fake.budgets["_key"] == INTERACTION_TIMEOUT
        assert fake.budgets["_text"] == INTERACTION_TIMEOUT
        assert fake.budgets["_is_app_installed"] == QUERY_TIMEOUT
        assert fake.budgets["_start_app"] == APP_TIMEOUT
        assert fake.budgets["_install_apk"] == APP_TIMEOUT
        assert fake.budgets["_configure"] == CONFIG_TIMEOUT


def test_none_is_explicit_unbounded_end_to_end() -> None:
    with _facade() as controller:
        controller.screenshot(timeout=None)
        fake = cast(_FakeController, controller.aio)
        assert fake.budgets["_screenshot"] is None


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


def test_op_on_dead_facade_is_typed_even_for_internal_drain() -> None:
    with _facade() as controller:
        controller.close()
        with pytest.raises(ControllerClosedError):
            controller.screenshot()


def test_keyboard_interrupt_abandons_and_facade_stays_usable(monkeypatch: Any) -> None:
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
        ("memu:pkg", BackendNotAvailableError),
    ],
)
def test_connect_sync_refusals_raise_pre_portal_thread_free(target: Any, expected: type[BaseException]) -> None:
    before = _thread_names()
    with pytest.raises(expected):
        connect_sync(target)
    _wait_for_no_controller_threads(before)


def test_connect_sync_config_refusals_are_pre_portal() -> None:
    before = _thread_names()
    with pytest.raises(InvalidArgumentError):
        connect_sync("adb:emulator-5554", config="nope")  # ty: ignore[invalid-argument-type]
    # Plain ADB now declares CONFIGURE, so a display request is accepted at
    # construction and starts no portal thread until the first op.
    configured = connect_sync(
        "adb:emulator-5554",
        config=EmuctlConfig(display=DisplayConfig(width=1080, height=1920)),
    )
    try:
        assert configured.has_feature(CapabilityFlag.CONFIGURE)
    finally:
        configured.close()
    _wait_for_no_controller_threads(before)


class _FakeHandle:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout, self._stderr, self._returncode = stdout, stderr, returncode

    @property
    def pid(self) -> int | None:
        return 4242

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int:
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


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


def _adb_facade(calls: list[list[str]], respond: Callable[[list[str]], _FakeHandle]) -> EmulatorController:
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


# ── interpreter exit: close() is the required exit path (§5, no atexit) ──


@pytest.mark.skipif(os.name == "nt", reason="the fake adb shim is a POSIX executable")
def test_interpreter_exit_after_close_is_clean(tmp_path: Any) -> None:
    adb = tmp_path / "fake-adb"
    adb.write_text(f"#!{sys.executable}\nprint('')\n", encoding="utf-8")
    adb.chmod(0o755)
    code = f"""
import emuctl
from emuctl.config.model import AdbConfig, EmuctlConfig

facade = emuctl.connect_sync(
    "adb:127.0.0.1:5555",
    config=EmuctlConfig(adb=AdbConfig(path={str(adb)!r})),
)
assert facade.is_reachable().ok is True
facade.close()
facade.close()  # idempotent
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_configure_returns_display_state_across_the_portal() -> None:
    with _facade() as controller:
        assert controller.configure() == DisplayState(None, None, None)


def test_sync_restored_lifecycle_gates(monkeypatch: Any) -> None:
    with _facade() as controller:
        for op in (
            lambda: controller.create(CreateSpec(name="vm")),
            controller.start,
            controller.stop,
            controller.restart,
        ):
            with pytest.raises(FeatureNotSupportedError):
                op()

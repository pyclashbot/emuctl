from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import os
import threading
from concurrent.futures import CancelledError
from typing import TYPE_CHECKING, Any, Final

import anyio
from anyio import CancelScope
from anyio.from_thread import start_blocking_portal

from emuctl._discovery import _resolve_config, _resolve_target
from emuctl.config.model import ConfigSpec, ensure_config_supported
from emuctl.errors import (
    ControllerClosedError,
    InvalidArgumentError,
    SyncCallInsideEventLoopError,
)
from emuctl.log import sanitize
from emuctl.port import AsyncEmulatorController, DisplayState
from emuctl.timing import (
    APP_TIMEOUT,
    CONFIG_TIMEOUT,
    DEFAULT,
    INTERACTION_TIMEOUT,
    LIFECYCLE_TIMEOUT,
    PROVISION_TIMEOUT,
    QUERY_TIMEOUT,
    SNAPSHOT_TIMEOUT,
    TimeoutParam,
    resolve_timeout,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from anyio.from_thread import BlockingPortal

    from emuctl._identification import InstanceRef
    from emuctl.config.model import CreateSpec, EmuctlConfig
    from emuctl.port import CapabilityFlag, ProbeResult, ReadyCheck

__all__ = ["EmulatorController", "PORTAL_THREAD_NAME", "connect_sync"]

logger = logging.getLogger(__name__)

PORTAL_THREAD_NAME: Final[str] = "emuctl-portal"

_MISPATCH: Final[object] = object()


def _check_timeout(timeout: TimeoutParam, group: float, operation: str) -> None:
    resolve_timeout(timeout, group, operation=operation)


def _is_async_predicate(predicate: Callable[[Any], Any]) -> bool:
    target = inspect.unwrap(predicate)
    if inspect.iscoroutinefunction(target):
        return True
    if isinstance(predicate, functools.partial):
        return _is_async_predicate(predicate.func)
    return False


def _run_predicate(predicate: Callable[[Any], Any], target: Any) -> Any:
    outcome = predicate(target)
    if inspect.iscoroutine(outcome):
        outcome.close()
        return _MISPATCH
    if inspect.isawaitable(outcome):
        return _MISPATCH
    return outcome


def _bridge_ready(facade: EmulatorController, predicate: ReadyCheck) -> ReadyCheck:

    async def evaluate_ready(controller: AsyncEmulatorController) -> Any:
        if _is_async_predicate(predicate):
            outcome = predicate(controller)
            if inspect.isawaitable(outcome):
                return await outcome
            return outcome
        outcome = await anyio.to_thread.run_sync(_run_predicate, predicate, facade, abandon_on_cancel=True)
        if outcome is _MISPATCH:
            # Misdispatched sync path: re-invoke with the async controller ON
            # the loop (harmless: the earlier probe never executed).
            outcome = predicate(controller)
            if inspect.isawaitable(outcome):
                return await outcome
            return outcome
        return outcome

    return evaluate_ready


class EmulatorController:
    def __init__(self, controller: AsyncEmulatorController) -> None:
        self._ctl = controller
        self._portal_cm: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._portal_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._portal_lock = threading.Lock()
        self._close_lock = threading.Lock()
        logger.debug("facade bound controller=%s", sanitize(str(self._ctl.identity)))

    def __del__(self) -> None: ...

    def _ensure_portal(self) -> BlockingPortal:
        if self._closed:
            raise ControllerClosedError()
        portal = self._portal
        if portal is not None:
            return portal
        with self._portal_lock:
            portal = self._portal
            if portal is not None:
                return portal
            if self._closed:
                raise ControllerClosedError()
            self._portal_cm = start_blocking_portal(backend="asyncio", name=PORTAL_THREAD_NAME)
            self._portal = portal = self._portal_cm.__enter__()
            self._portal_loop = portal.start_task_soon(asyncio.get_running_loop).result()
            logger.debug("portal started thread=%s", PORTAL_THREAD_NAME)
            return portal

    def _call(self, op: str, /, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise ControllerClosedError()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SyncCallInsideEventLoopError(
                detail=f"sync op {op}() cannot run inside an event loop — "
                f"escape hatch: `await emulator.aio.{op}(...)` instead"
            )
        portal = self._ensure_portal()
        if self._closed:
            raise ControllerClosedError()
        try:
            return portal.call(self._call_on_loop, op, args, kwargs)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            if self._closed and isinstance(exc, (CancelledError, RuntimeError)):
                raise ControllerClosedError() from exc
            raise

    async def _call_on_loop(self, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        call = getattr(self._ctl, op)
        return await call(*args, **kwargs)

    @property
    def aio(self) -> AsyncEmulatorController:
        return self._ctl

    @property
    def features(self) -> frozenset[CapabilityFlag]:
        return self._ctl.features

    def has_feature(self, flag: CapabilityFlag) -> bool:
        return self._ctl.has_feature(flag)

    def create(self, spec: CreateSpec, *, timeout: TimeoutParam = DEFAULT) -> InstanceRef:
        _check_timeout(timeout, PROVISION_TIMEOUT, "create")
        return self._call("create", spec, timeout=timeout)

    def configure(self, spec: ConfigSpec | None = None, *, timeout: TimeoutParam = DEFAULT) -> DisplayState:
        _check_timeout(timeout, CONFIG_TIMEOUT, "configure")
        return self._call("configure", spec, timeout=timeout)

    def start(self, *, timeout: TimeoutParam = DEFAULT) -> None:
        _check_timeout(timeout, LIFECYCLE_TIMEOUT, "start")
        return self._call("start", timeout=timeout)

    def stop(self, *, timeout: TimeoutParam = DEFAULT) -> None:
        _check_timeout(timeout, LIFECYCLE_TIMEOUT, "stop")
        return self._call("stop", timeout=timeout)

    def restart(
        self,
        *,
        app: str | None = None,
        ready: ReadyCheck | None = None,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        _check_timeout(timeout, LIFECYCLE_TIMEOUT, "restart")
        if ready is not None and not callable(ready):
            raise InvalidArgumentError(
                parameter="ready",
                constraint="must be a callable ReadyCheck or None",
            )
        wrapped: ReadyCheck | None = None if ready is None else _bridge_ready(self, ready)
        return self._call("restart", app=app, ready=wrapped, timeout=timeout)

    # ── interaction / apps / probe twins ──

    def screenshot(self, *, timeout: TimeoutParam = DEFAULT) -> bytes:
        _check_timeout(timeout, SNAPSHOT_TIMEOUT, "screenshot")
        return self._call("screenshot", timeout=timeout)

    def click(
        self,
        x: int,
        y: int,
        *,
        count: int = 1,
        interval: float = 0.0,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        _check_timeout(timeout, INTERACTION_TIMEOUT, "click")
        return self._call("click", x, y, count=count, interval=interval, timeout=timeout)

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int = 300,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        _check_timeout(timeout, INTERACTION_TIMEOUT, "swipe")
        return self._call("swipe", x1, y1, x2, y2, duration_ms=duration_ms, timeout=timeout)

    def key(self, code: str | int, *, timeout: TimeoutParam = DEFAULT) -> None:
        _check_timeout(timeout, INTERACTION_TIMEOUT, "key")
        return self._call("key", code, timeout=timeout)

    def text(self, value: str, *, timeout: TimeoutParam = DEFAULT) -> None:
        _check_timeout(timeout, INTERACTION_TIMEOUT, "text")
        return self._call("text", value, timeout=timeout)

    def install_apk(self, apk_path: str | os.PathLike[str], *, timeout: TimeoutParam = DEFAULT) -> None:
        _check_timeout(timeout, APP_TIMEOUT, "install_apk")
        return self._call("install_apk", apk_path, timeout=timeout)

    def start_app(self, package: str, *, timeout: TimeoutParam = DEFAULT) -> None:
        _check_timeout(timeout, APP_TIMEOUT, "start_app")
        return self._call("start_app", package, timeout=timeout)

    def is_app_installed(self, package: str, *, timeout: TimeoutParam = DEFAULT) -> bool:
        _check_timeout(timeout, QUERY_TIMEOUT, "is_app_installed")
        return self._call("is_app_installed", package, timeout=timeout)

    def is_reachable(self, *, timeout: TimeoutParam = DEFAULT) -> ProbeResult:
        _check_timeout(timeout, QUERY_TIMEOUT, "is_reachable")
        return self._call("is_reachable", timeout=timeout)

    def aclose(self) -> None:
        return self._call("aclose")

    async def _drain_close(self) -> None:
        with CancelScope(shield=True):
            await self._ctl.aclose()

    async def _drain_and_stop(self, portal: BlockingPortal) -> None:
        with CancelScope(shield=True):
            await self._ctl.aclose()
        with CancelScope(shield=True):
            await portal.stop(cancel_remaining=True)

    @staticmethod
    def _exit_portal_cm(cm: AbstractContextManager[BlockingPortal]) -> None:
        try:
            cm.__exit__(None, None, None)
        except RuntimeError:
            logger.debug("portal CM exit dismissed a teardown shape")

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            with self._portal_lock:
                cm = self._portal_cm
                portal = self._portal
                loop = self._portal_loop
                if cm is not None:
                    self._portal = None
                    self._portal_cm = None
                    self._portal_loop = None
        if cm is None:
            anyio.run(self._drain_close)
            return
        if portal is None:
            raise ControllerClosedError()
        caller_loop: asyncio.AbstractEventLoop | None = None
        with contextlib.suppress(RuntimeError):
            caller_loop = asyncio.get_running_loop()
        if loop is not None and caller_loop is loop:
            caller_loop.create_task(self._drain_and_stop(portal))
            threading.Thread(target=self._exit_portal_cm, args=(cm,), name="emuctl-portal-join").start()
            return
        portal.start_task_soon(self._drain_close)
        with contextlib.suppress(RuntimeError, CancelledError):
            portal.call(portal.stop, True)
        cm.__exit__(None, None, None)
        logger.debug("facade closed ref=%s", sanitize(str(self._ctl.identity)))

    def __enter__(self) -> EmulatorController:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()


def connect_sync(target: InstanceRef | str, *, config: EmuctlConfig | None = None) -> EmulatorController:
    from emuctl.backends._registry import load_adapter, resolve_backend

    ref = _resolve_target(target)
    logger.debug("connect_sync target=%s", sanitize(str(ref)))
    spec = resolve_backend(ref.backend)
    spec.validate_key(ref.key)
    cfg = _resolve_config(config)
    adapter_cls = load_adapter(spec)
    ensure_config_supported(cfg, adapter_cls.FEATURES)
    controller = adapter_cls(ref, config=cfg)
    logger.debug("connect_sync bound backend=%s", sanitize(ref.backend.value))
    return EmulatorController(controller)

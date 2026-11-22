"""§5 — The sync facade: the AnyIO blocking-portal bridge, confined to this module.

The port is async-only (§1); this module derives the sync surface from it,
never a second implementation. Mechanics LOCKED by §5 (quote-checked):

- one lazily-started portal per facade (``start_blocking_portal(backend=
  "asyncio")`` — pinned asyncio), thread named ``emuctl-portal``;
  per-call portals and process-global shared portals are rejected — one
  portal per facade object; ``_call()`` is the SOLE funnel to the portal;
``.aio``-only usage starts nothing (zero threads);
- running event-loop detection fires BEFORE portal work →
  ``SyncCallInsideEventLoopError`` carrying the ``await emulator.aio.<op>()``
  escape hatch in its message; no trio-sniffing at v0 (asyncio detection
  only);
- ``close()`` is idempotent and cancel-first: the transport-only ``aclose``
  drains as a portal task BEFORE the portal's task-group shutdown cancels
  every remaining task (§5: "anyio CM-exit cancels pending tasks"), so an
  abandoned in-flight caller re-raises ``ControllerClosedError`` — with the
  documented honesty that an abandoned op may have executed anyway; the
  facade is dead after ``close()`` (permanent) but reusable after an
  abandoned (interrupted) call; ``close()`` is the only member sanctioned
  from any thread (§3), so it never blocks on the portal from the portal's
  own loop thread (deadlock avoidance — see ``close()``);
- cancellation = abandon: an interrupt during a sync call leaves the portal
  task running to its own bounded timeout; no emulator stop is ever issued
  by cleanup (``aclose``/``close`` release transport only — never boot
  state); ``__del__`` is a no-op; facades are unpicklable (their locks are
  not picklable — document-only, construct inside worker processes);
- parity: every public ``.aio`` op has a sync twin with kwarg-for-kwarg
  identity and the SAME ``timeout: DEFAULT`` sentinel; exceptions cross the
  portal as the SAME class/payloads/``error_code`` (the very exception
  object crosses — no bridge rewrapping), except the sanctioned close-race
  close-race conversion of a cancellation into ``ControllerClosedError``;
- ReadyCheck normalization (§5): the sync twin wraps the caller's predicate
  once and hands the wrapper to the async port, whose ``_eval_ready`` owns
  the shared remaining restart budget — the bridge normalizes ONLY the
  predicate shape: a sync predicate runs in a portal WORKER THREAD against
  the facade object itself (its internal ``emulator.screenshot()``-style
  calls re-run the normal funnel; the §7.5 deadlock scenario), an async
  predicate (``async def``, or a sync callable caught returning an
  awaitable — the un-awaited probe coroutine is closed and the predicate
  re-invoked with the async controller on the loop) is awaited on the
  portal loop with the async controller; a predicate that raises propagates
  as ``restart()``'s ``EmulatorNotReadyError(cause=…)`` (that wrapping is
  the async port's, not a bridge rewrap).

Interpretation decisions (CONTRACT §5 wins; these fill its silences):

- ``connect_sync`` replicates ``connect()``'s construction gate chain
  (resolve_target → resolve_backend → validate_key → resolve config →
  load_adapter → §4 capability gate → bind) SYNCHRONOUSLY, so refusals
  raise with NO portal/thread started (§3: "Selection refusals raise HERE,
  at construction"). It reaches ``emuctl.backends._registry`` through the
  same sanctioned function-local seam as ``_discovery.connect`` (§6,
  mirrored in the import-linter carve-out).
- ``aclose()`` (sync) is the plain funnel twin: portal keeps running; the
  facade-level permanent death is ``close()``'s exclusive job (§5: "the
  facade is reusable after a cancelled call; permanently closed after
  ``close()``").
- Sync twins validate the ``timeout`` kwarg through ``resolve_timeout`` at
  the sync surface — invalid values raise ``InvalidArgumentError``
  pre-portal, no thread ever started — then forward it VERBATIM so the
  async port resolves DEFAULT against its own group constant (same groups,
  pinned in the timing table; asserted by conformance).
- ``close()`` detection of "called from the portal's own thread" keys on the
  portal loop captured at portal start time (a clean anyio-supported
  snapshot: ``start_task_soon(asyncio.get_running_loop)`` once, no reads of
  anyio privates); in that branch the drain is driven directly on the
  already-running loop (non-blocking schedule) because no blocking call
  may wait on the portal from inside it. Worker threads spawned by
  ``to_thread`` (the ReadyCheck path) are NOT the portal thread: they take
  the normal funnel path, which is exactly what makes predicate-driven
  ``close()`` deadlock-free under ``abandon_on_cancel=True``.
"""

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
from emuctl.config.model import ensure_config_supported
from emuctl.errors import (
    ControllerClosedError,
    InvalidArgumentError,
    SyncCallInsideEventLoopError,
)
from emuctl.log import sanitize
from emuctl.port import AsyncEmulatorController
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
    from emuctl.config.model import ConfigSpec, CreateSpec, EmuctlConfig
    from emuctl.port import CapabilityFlag, ProbeResult, ReadyCheck

__all__ = ["EmulatorController", "PORTAL_THREAD_NAME", "connect_sync"]

logger = logging.getLogger(__name__)

#: Pinned portal thread name (§5: "thread named `emuctl-portal`").
PORTAL_THREAD_NAME: Final[str] = "emuctl-portal"

#: Marker returned by a worker-thread predicate invocation whose sync-callable
#: returned an awaitable (misdispatched async path): the probe coroutine is
#: closed and the predicate re-invoked with the async controller on the loop.
_MISPATCH: Final[object] = object()


def _check_timeout(timeout: TimeoutParam, group: float, operation: str) -> None:
    """Sync-surface validation of a twin's ``timeout`` kwarg (raise-only).

    ``resolve_timeout`` owns the rules (DEFAULT → group constant, None →
    sanctioned unbounded, invalid → InvalidArgumentError); the twin keeps
    the verdict to itself and forwards the argument VERBATIM so the async
    port resolves DEFAULT against the same group constant.
    """
    resolve_timeout(timeout, group, operation=operation)


def _is_async_predicate(predicate: Callable[[Any], Any]) -> bool:
    """True for ``async def`` predicates (unwrapping rebinds/partials). Pure."""
    target = inspect.unwrap(predicate)
    if inspect.iscoroutinefunction(target):
        return True
    if isinstance(predicate, functools.partial):
        return _is_async_predicate(predicate.func)  # type: ignore[arg-type]
    return False


def _run_predicate(predicate: Callable[[Any], Any], target: Any) -> Any:
    """Invoke a predicate in a worker thread; tag an awaitable answer as a
    misdispatched async path (the un-awaited probe coroutine is closed — its
    body has not started, so closing loses nothing; per §5 misdispatched sync
    paths are caught, not trusted)."""
    outcome = predicate(target)
    if inspect.iscoroutine(outcome):
        outcome.close()
        return _MISPATCH
    if inspect.isawaitable(outcome):  # non-coroutine awaitable (futures, ...)
        return _MISPATCH
    return outcome


def _bridge_ready(facade: EmulatorController, predicate: ReadyCheck) -> ReadyCheck:
    """ReadyCheck normalization (§5): wrap once, hand to the async port.

    The async port's ``_eval_ready`` (the shared-remaining-budget owner)
    calls the wrapper with the async controller; the wrapper normalizes the
    predicate SHAPE only:

    - async predicates: invoked with the async controller, awaited on the
      portal loop;
    - sync predicates: invoked in a portal WORKER THREAD against the FACADE
      itself, so internal ``emulator.screenshot()``-style calls run the
      normal funnel and cannot re-enter the loop (§7.5's deadlock-free
      scenario — the worker is not the portal thread).
    """

    async def evaluate_ready(controller: AsyncEmulatorController) -> Any:
        if _is_async_predicate(predicate):
            outcome = predicate(controller)
            if inspect.isawaitable(outcome):
                return await outcome
            return outcome
        outcome = await anyio.to_thread.run_sync(
            _run_predicate, predicate, facade, abandon_on_cancel=True
        )
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
    """The §5 sync facade over one bound ``AsyncEmulatorController``, built
    only by :func:`connect_sync` (never user-constructed; not user-picklable).

    Every I/O op is a one-line delegation through :meth:`_call`; ``.aio``
    never starts the portal; ``close()`` is the only member sanctioned from
    any thread (§3).
    """

    def __init__(self, controller: AsyncEmulatorController) -> None:
        self._ctl = controller
        self._portal_cm: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._portal_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._portal_lock = threading.Lock()
        self._close_lock = threading.Lock()
        logger.debug("facade bound controller=%s", sanitize(str(self._ctl.identity)))

    def __repr__(self) -> str:
        return f"<EmulatorController {str(self._ctl.identity)!r}>"

    def __del__(self) -> None:
        """§5 no-op: ``__del__`` performs nothing; there is **no atexit** hook.

        Shutdown is explicit. **``close()`` is the required exit path**: it
        stops the portal thread and joins it. ``aclose()`` is transport-only —
        it does not stop the portal, so a process that calls only ``aclose()``
        can hang during interpreter shutdown in the blocking-portal thread
        join (observed on 3.12/3.13); call ``close()`` (or use the facade as a
        context manager) on every exit path.
        """

    # ── the funnel ──

    def _ensure_portal(self) -> BlockingPortal:
        """Lazily start THE one portal for this facade (thread-safe).

        A racing ``close()`` snapshots and clears portal state under
        :attr:`_portal_lock` after raising the closed flag, so a call that
        loses that race either drains the freshly-started portal itself
        (no leak) or finds the state cleared here (raises).
        """
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
            # Capture the portal's running loop ONCE, on the portal loop
            # itself (clean anyio mechanism — no anyio private state).
            self._portal_loop = portal.start_task_soon(asyncio.get_running_loop).result()
            logger.debug("portal started thread=%s", PORTAL_THREAD_NAME)
            return portal

    def _call(self, op: str, /, *args: Any, **kwargs: Any) -> Any:
        """The SOLE funnel from the sync surface to the portal loop.

        Order LOCKED by §5: closed check → running-loop detection (neither
        starts any thread) → lazy portal start → re-check against a racing
        ``close()`` → portal call. Cancellation or anyio's portal-stopping
        ``RuntimeError`` shapes caused by a racing ``close()`` convert to
        ``ControllerClosedError`` (§5's abandoned-caller rule); other
        exceptions cross unchanged.
        """
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
            # Lost a race with close(): the loser always gets the typed error.
            raise ControllerClosedError()
        try:
            return portal.call(self._call_on_loop, op, args, kwargs)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise  # abandon: the portal task keeps running (§5)
            if self._closed and isinstance(exc, (CancelledError, RuntimeError)):
                # Close-race shapes: the cancelled task funnel, or anyio's
                # "portal is not running" / "event loop closed" RuntimeError.
                raise ControllerClosedError() from exc
            raise

    async def _call_on_loop(self, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Portal-loop side of the funnel: verbatim forward to the async port."""
        call = getattr(self._ctl, op)
        return await call(*args, **kwargs)

    # ── portal-free declared members (zero threads, zero I/O) ──

    @property
    def aio(self) -> AsyncEmulatorController:
        """The native async view; never starts the portal (§5)."""
        return self._ctl

    @property
    def features(self) -> frozenset[CapabilityFlag]:
        """Declared set; no I/O, no gate, no portal (§5)."""
        return self._ctl.features

    def has_feature(self, flag: CapabilityFlag) -> bool:
        """Declared-set membership; portal-free (§5)."""
        return self._ctl.has_feature(flag)

    # ── lifecycle twins ──

    def create(self, spec: CreateSpec, *, timeout: TimeoutParam = DEFAULT) -> InstanceRef:
        _check_timeout(timeout, PROVISION_TIMEOUT, "create")
        return self._call("create", spec, timeout=timeout)

    def configure(self, spec: ConfigSpec | None = None, *, timeout: TimeoutParam = DEFAULT) -> None:
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
        """THE boot primitive's sync twin (§1.6/§5 ReadyCheck normalization)."""
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

    def install_apk(
        self, apk_path: str | os.PathLike[str], *, timeout: TimeoutParam = DEFAULT
    ) -> None:
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
        """Transport-only release through the funnel; the facade keeps its
        ``.aio`` view but every gated op thereafter raises
        ``ControllerClosedError`` (the async port's ``_ensure_open``).
        Permanent facade death is :meth:`close`."""
        return self._call("aclose")

    # ── teardown: the ONLY member sanctioned from any thread (§3) ──

    async def _drain_close(self) -> None:
        """Transport-only drain (§1 invariant 11): shielded so the portal's
        imminent cancel-first shutdown cannot kill the release."""
        with CancelScope(shield=True):
            await self._ctl.aclose()

    async def _drain_and_stop(self, portal: BlockingPortal) -> None:
        """From-portal-thread close drain: release transport in a shielded
        step, then trigger the portal's cancel-first shutdown from inside the
        loop (both are non-blocking for ``close()``, which returns without
        waiting; the CM join happens on the helper thread)."""
        with CancelScope(shield=True):
            await self._ctl.aclose()
        with CancelScope(shield=True):
            await portal.stop(cancel_remaining=True)

    @staticmethod
    def _exit_portal_cm(cm: AbstractContextManager[BlockingPortal]) -> None:
        """Join the portal thread — from a HELPER thread, never the portal's
        own (the CM's ``__exit__`` is a blocking portal call + join; that is
        exactly what the from-portal-loop path must not do).
        """
        try:
            cm.__exit__(None, None, None)
        except RuntimeError:
            # Portal already stopping/stopped (teardown shapes in the CM's
            # own stop call); the join in its ``finally`` already ran.
            logger.debug("portal CM exit dismissed a teardown shape")

    def close(self) -> None:
        """Permanent shutdown: idempotent, cancel-first, transport-only, and
        safe from ANY thread (§3) — including the portal's own loop thread.

        Steps (portal running): the transport release drains as a portal
        task FIRST (shielded), then the portal's task-group shutdown cancels
        every remaining task — an abandoned in-flight caller re-raises
        ``ControllerClosedError`` (documented honesty: an abandoned op may
        have executed anyway). Never stops any emulator.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            # Snapshot AND clear the portal state under the same lock that
            # guards lazy start: a first-op that loses this race either
            # started before us (we drain its portal here — no leak) or
            # finds the state cleared in _ensure_portal() (typed refusal).
            with self._portal_lock:
                cm = self._portal_cm
                portal = self._portal
                loop = self._portal_loop
                if cm is not None:
                    self._portal = None
                    self._portal_cm = None
                    self._portal_loop = None
        if cm is None:
            # ``.aio``-only facade (portal never started, zero threads):
            # drain the release on a throwaway loop (anyio confined here).
            anyio.run(self._drain_close)
            return
        if portal is None:  # unreachable: _portal is set with _portal_cm
            raise ControllerClosedError()
        caller_loop: asyncio.AbstractEventLoop | None = None
        with contextlib.suppress(RuntimeError):
            caller_loop = asyncio.get_running_loop()
        if loop is not None and caller_loop is loop:
            # Deadlock guard: called from the portal's own loop thread (the
            # loop was captured at portal start) — no blocking call may wait
            # on the portal from inside it. The transport release drains as
            # a non-blocking task on the already-running loop, and the CM
            # exit (stop + join) runs on a helper thread; close() returns
            # without blocking.
            caller_loop.create_task(self._drain_and_stop(portal))
            threading.Thread(
                target=self._exit_portal_cm, args=(cm,), name="emuctl-portal-join"
            ).start()
            return
        portal.start_task_soon(self._drain_close)
        # cancel-first (§5); RuntimeError = portal already stopping,
        # CancelledError = the stop task self-cancelled during group teardown.
        with contextlib.suppress(RuntimeError, CancelledError):
            portal.call(portal.stop, True)
        cm.__exit__(None, None, None)  # joins the portal thread
        logger.debug("facade closed ref=%s", sanitize(str(self._ctl.identity)))

    # ── CM shape (§5: __enter__/__exit__ only; start nothing, boot explicit) ──

    def __enter__(self) -> EmulatorController:
        """Starts NOTHING — boot stays the explicit :meth:`restart` (§5)."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()


def connect_sync(
    target: InstanceRef | str, *, config: EmuctlConfig | None = None
) -> EmulatorController:
    """§5's sync twin of :func:`emuctl._discovery.connect`, run SYNCHRONOUSLY.

    Selection refusals raise HERE, at construction — never on first call,
    and with NO portal/thread started. Full gate chain (identical order):
    resolve_target → resolve_backend → validate_key → resolve config →
    load_adapter → §4 capability gate → bind.

    No ``logger=`` parameter — logging is stdlib-name-based (§3).
    """
    # Function-local: §6 forbids core→backends static imports (the same
    # sanctioned seam as _discovery.connect, mirrored in the import-linter
    # carve-out).
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

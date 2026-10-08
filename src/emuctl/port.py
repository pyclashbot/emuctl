"""§1 — The async-only port: capability flags, ProbeResult, MechanicsTable, AsyncEmulatorController.

The port is async-only; the sync surface (§5) derives from it, never a second
implementation. Payload types (InstanceRef, ConfigSpec, CreateSpec) are
§3/§4-owned; exceptions are §2-owned; timeouts resolve through timing.py
(§2's group table). Logging is DEBUG-only narration through sanitized
strings; CancelledError is never caught, converted, or logged as an error.

Interpretation decisions (CONTRACT §1 wins; these fill its silences):

- Mechanics (``_start`` …) are ``...``-bodied stubs, not abstractmethods: the
  wiring gate (``__init_subclass__``) is the enforcement site, so a minimal
  adapter declaring one flag stays instantiable. Only ``_vendor_run`` is
  abstract.
- ``restart()`` phases call the public gated methods (gate-consistent funnel),
  except ``_is_running`` (no public twin — ``is_running`` is §8-deferred) and
  the sync ``_attach()`` hook. ``configure`` runs only when CONFIGURE is
  declared — an explicit capability check, never a silent no-op.
- ``restart()`` maps deadline expiry to ``EmulatorNotReadyError`` with reason
  ``"timeout"`` and other mid-phase failures to phase-token reasons; the
  contract's example tokens (``timeout``, ``start_failed``,
  ``app_not_installed``, ``menu_not_reached``) anchor the vocabulary.
- ``create()``'s existence pre-check routes through the overridable
  ``_get_instance`` hook (default: absent); backend adapters wire it to their
  module-level discovery seam in later PRs.
- ``is_reachable()`` is ungated: gating it would contradict the probes
  exemption ("never raises"). The port default has no observation seam and
  returns data; AdbController overrides it with the real transport probe.
"""

# ruff: noqa: B027 — mechanics are deliberately non-abstract `...` stubs: the
# wiring gate (__init_subclass__) is the enforcement site, so minimal adapters
# stay instantiable. Abstract decorators would force every fake to implement
# every mechanic and defeat the per-declared-flag gate.

from __future__ import annotations

import abc
import asyncio
import enum
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from typing import Any, Final, NamedTuple, TypeVar, cast

from emuctl._identification import InstanceRecord, InstanceRef
from emuctl.config.model import ConfigSpec, CreateSpec
from emuctl.errors import (
    ControllerClosedError,
    EmulatorNotReadyError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    OperationTimeoutError,
)
from emuctl.log import sanitize
from emuctl.png import png_dimensions
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

__all__ = [
    "CapabilityFlag",
    "ReadyCheck",
    "ProbeResult",
    "PROBE_REASONS",
    "MECHANICS",
    "AsyncEmulatorController",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CapabilityFlag(enum.Enum):
    """Additive-only capability vocabulary; values pinned (never enum.auto())."""

    BOOT = "boot"  # start / stop / restart
    CREATE = "create"  # create()
    CONFIGURE = "configure"  # configure()
    SCREENSHOT = "screenshot"  # screenshot()
    INPUT = "input"  # click / swipe / key / text
    APPS = "apps"  # start_app / is_app_installed
    APK_INSTALL = "apk_install"  # install_apk (separately gated; Google Play cannot)


ReadyCheck = Callable[["AsyncEmulatorController"], "bool | Awaitable[bool]"]


class ProbeResult(NamedTuple):
    """The probes exemption: data-without-raise. Never truthy-check; check `.ok`."""

    ok: bool
    reason: str


#: Pinned probe-reason vocabulary (additive-only). "ok" and "no-adb" are §1's
#: pins; offline/unauthorized/disconnected mirror §2's DeviceStateError states
#: as probe data; "not-found" names an absent ref; "timeout" reports a probe
#: that exceeded its budget without raising; "unknown" is the port default
#: (this layer has no observation seam — AdbController overrides it).
PROBE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "no-adb",
        "offline",
        "unauthorized",
        "disconnected",
        "not-found",
        "timeout",
        "unknown",
    }
)


#: Port-owned, module-level, pinned like the flags. Satisfaction rule: every
#: mechanic named for a declared flag is a concrete member somewhere in the
#: MRO (enforced by __init_subclass__ at class definition, zero I/O).
MECHANICS: Final[dict[CapabilityFlag, tuple[str, ...]]] = {
    CapabilityFlag.BOOT: ("_start", "_stop", "_is_running"),
    CapabilityFlag.CREATE: ("_create",),
    CapabilityFlag.CONFIGURE: ("_configure",),
    CapabilityFlag.SCREENSHOT: ("_screenshot",),
    CapabilityFlag.INPUT: ("_click", "_swipe", "_key", "_text"),
    CapabilityFlag.APPS: ("_start_app", "_is_app_installed"),
    CapabilityFlag.APK_INSTALL: ("_install_apk",),
}


def _is_stub_body(func: object) -> bool:
    """True when `func` is a `...`/`pass`/docstring-only stub, not a real body.

    A real body references globals/attributes (imported helpers, self
    mechanics) or binds non-trivial constants; a stub only loads None,
    Ellipsis, or its own docstring. `co_names` carries the attribute/global
    check so one-line delegations (`return await self._vendor_run(...)`)
    count as concrete.
    """
    code = getattr(func, "__code__", None)
    if code is None:
        return False
    if getattr(code, "co_names", ()):
        return False
    doc = getattr(func, "__doc__", None)
    return all(
        const is None or const is Ellipsis or (isinstance(const, str) and const == doc)
        for const in code.co_consts
    )


def _mechanic_problem(cls: type, name: str) -> str | None:
    """None when `name` resolves to a concrete member of `cls`, else a reason."""
    try:
        member = inspect.getattr_static(cls, name)
    except AttributeError:
        return "missing"
    if getattr(member, "__isabstractmethod__", False):
        return "abstract"
    target = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
    if target is None or target is Ellipsis:
        return "still a '...' stub"
    if inspect.isfunction(target) and _is_stub_body(target):
        return "still a '...' stub"
    return None


class AsyncEmulatorController(abc.ABC):
    """Async-only vendor-agnostic controller port (§1).

    Adapters declare FEATURES in the class body and implement the mechanics
    MECHANICS names for those flags; public ops are gated defaults delegating
    to the mechanics. Construction binds the resolved InstanceRef only —
    never discovers, boots, or attaches.
    """

    FEATURES: frozenset[CapabilityFlag] = frozenset()

    def __init__(self, ref: InstanceRef, **adapter_kwargs: object) -> None:
        """Bind the ref `connect()` resolved. No discovery, no boot, no attach.

        Never raises EmulatorNotReadyError. `adapter_kwargs` is reserved for
        subclass transport knobs (consumed by the adapter's own __init__);
        leftovers reaching this layer are ignored for forward compatibility.
        """
        if not isinstance(ref, InstanceRef):
            raise InvalidArgumentError(
                parameter="ref",
                constraint="must be an InstanceRef (connect() resolves it)",
            )
        self._ref = ref
        self._closed = False
        logger.debug("bind ref=%s", sanitize(str(ref)))

    def __init_subclass__(cls, /, **kw: object) -> None:
        """Wiring-time gate (zero I/O): every mechanic named for each declared
        flag is a concrete member somewhere in the MRO (not a `...` body).
        Violations fail loudly at class definition with a TypeError naming
        the missing mechanics.
        """
        super().__init_subclass__(**kw)
        features = cls.FEATURES
        if not isinstance(features, frozenset):
            raise TypeError(
                f"{cls.__qualname__}.FEATURES must be a frozenset[CapabilityFlag], "
                f"got {type(features).__name__}"
            )
        for member in features:
            if not isinstance(member, CapabilityFlag):
                raise TypeError(
                    f"{cls.__qualname__}.FEATURES must contain only CapabilityFlag "
                    f"members, got {member!r}"
                )
        problems: list[str] = []
        for flag in sorted(features, key=lambda item: item.value):
            missing = [name for name in MECHANICS[flag] if _mechanic_problem(cls, name) is not None]
            if missing:
                problems.append(f"{flag.value!r} needs {', '.join(missing)}")
        if problems:
            raise TypeError(
                f"{cls.__qualname__} declares FEATURES "
                f"{sorted(flag.value for flag in features)} but "
                + "; ".join(problems)
                + " — provide concrete bodies (not '...') somewhere in the MRO"
            )

    @property
    def identity(self) -> InstanceRef:
        """The bound ref. Zero I/O; works after close."""
        return self._ref

    @property
    def features(self) -> frozenset[CapabilityFlag]:
        """The declared set. No I/O, no gate."""
        return self.FEATURES

    def has_feature(self, flag: CapabilityFlag) -> bool:
        """Declared-set membership. No I/O, no gate."""
        return flag in self.FEATURES

    def _require(self, flag: CapabilityFlag) -> None:
        """Shared gate: undeclared flag ⇒ FeatureNotSupportedError. No I/O."""
        if flag in self.FEATURES:
            return
        feature = flag.value if isinstance(flag, CapabilityFlag) else str(flag)
        raise FeatureNotSupportedError(feature=feature)

    def _ensure_open(self) -> None:
        """Closed-controller guard: ops after aclose() ⇒ ControllerClosedError."""
        if self._closed:
            raise ControllerClosedError()

    # ── transport seam ──
    @abc.abstractmethod
    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        """Ungated internal seam. Nonzero rc / unusable output → VendorCommandError."""
        ...

    def _attach(self) -> None:
        """Concrete no-op hook (not a MechanicsTable mechanic).

        ADB-family backends override it to run `adb connect` against their
        bound server; plain ADB inherits the no-op.
        """
        return None

    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> InstanceRecord | None:
        """Existence probe backing create()'s pre-check. Default: absent (None).

        Backend adapters override this to consult their module-level
        discovery seam (`get_instance`); the port layer cannot reach backend
        modules (§6 layers contract), so the default observes nothing.
        """
        return None

    # ── mechanics (wiring-gate-checked stubs; adapters provide real bodies) ──
    async def _start(self, timeout: float | None) -> None:
        """Boot the bound instance. Predicate discipline (§1.4): failure raises."""
        ...

    async def _stop(self, timeout: float | None) -> None:
        """Stop the bound instance (instance-scoped kills only)."""
        ...

    async def _is_running(self, timeout: float | None) -> bool:  # ty: ignore[empty-body]
        """Running observation: False only when the observation itself succeeded."""
        ...

    async def _create(self, spec: CreateSpec, timeout: float | None) -> InstanceRef:  # ty: ignore[empty-body]
        """Provision a new instance; returns the new instance's ref."""
        ...

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> None:
        """Apply config: None re-applies the bound config, else a targeted overlay."""
        ...

    async def _screenshot(self, timeout: float | None) -> bytes:  # ty: ignore[empty-body]
        """Capture the current framebuffer as PNG bytes."""
        ...

    async def _click(
        self, x: int, y: int, *, count: int, interval: float, timeout: float | None
    ) -> None:
        """Click at absolute device pixels (never re-scaled)."""
        ...

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
        """Swipe in absolute device pixels."""
        ...

    async def _key(self, code: str | int, timeout: float | None) -> None:
        """Send a raw keycode/keyevent symbol."""
        ...

    async def _text(self, value: str, timeout: float | None) -> None:
        """Type text (escaping owned by the shared default)."""
        ...

    async def _start_app(self, package: str, timeout: float | None) -> None:
        """Launch + bounded spawn-verify (no stdout parsing)."""
        ...

    async def _is_app_installed(self, package: str, timeout: float | None) -> bool:  # ty: ignore[empty-body]
        """Installed observation: False only when the observation succeeded."""
        ...

    async def _install_apk(self, apk_path: str | os.PathLike[str], timeout: float | None) -> None:
        """Install an APK (separately gated; Google Play cannot)."""
        ...

    # ── lifecycle (gated defaults over the mechanics) ──
    async def create(self, spec: CreateSpec, *, timeout: TimeoutParam = DEFAULT) -> InstanceRef:
        """Backend-scoped: ignores the bound identity; returns the new ref.

        Begins with an existence pre-check through the discovery seam
        (`_get_instance`): an existing name raises InvalidArgumentError
        (parameter="spec.name", constraint="already provisioned").
        """
        self._require(CapabilityFlag.CREATE)
        self._ensure_open()
        if not isinstance(spec, CreateSpec):
            raise InvalidArgumentError(
                parameter="spec",
                constraint="must be a CreateSpec (name is required at v0)",
            )
        budget = resolve_timeout(timeout, PROVISION_TIMEOUT, operation="create")
        candidate = InstanceRef(backend=self._ref.backend, key=spec.name)
        if await self._get_instance(candidate, budget) is not None:
            raise InvalidArgumentError(parameter="spec.name", constraint="already provisioned")
        return await self._create(spec, budget)

    async def configure(
        self, spec: ConfigSpec | None = None, *, timeout: TimeoutParam = DEFAULT
    ) -> None:
        """Gated CONFIGURE: None re-applies the bound config; else an overlay."""
        self._require(CapabilityFlag.CONFIGURE)
        self._ensure_open()
        if spec is not None and not isinstance(spec, Mapping):
            raise InvalidArgumentError(
                parameter="spec",
                constraint="must be a ConfigSpec mapping or None",
            )
        budget = resolve_timeout(timeout, CONFIG_TIMEOUT, operation="configure")
        await self._configure(spec, budget)

    async def start(self, *, timeout: TimeoutParam = DEFAULT) -> None:
        """Boot the bound instance (gated BOOT)."""
        self._require(CapabilityFlag.BOOT)
        self._ensure_open()
        budget = resolve_timeout(timeout, LIFECYCLE_TIMEOUT, operation="start")
        await self._start(budget)

    async def stop(self, *, timeout: TimeoutParam = DEFAULT) -> None:
        """Stop the bound instance (gated BOOT; instance-scoped kills only)."""
        self._require(CapabilityFlag.BOOT)
        self._ensure_open()
        budget = resolve_timeout(timeout, LIFECYCLE_TIMEOUT, operation="stop")
        await self._stop(budget)

    async def restart(
        self,
        *,
        app: str | None = None,
        ready: ReadyCheck | None = None,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        """THE boot primitive. One attempt; None or EmulatorNotReadyError.

        Never False, never internal retries, not idempotent. Phases share one
        remaining deadline (no per-phase config at v0): stop-if-running →
        configure-while-stopped → start → _attach → settle (first screenshot
        decodes non-empty) → [app: launch + bounded spawn-verify] → terminal
        check (ready(...) if given, else spawn-present-if-app, else
        screenshot-decodes).

        Reason vocabulary (additive): "timeout" (any phase exceeded the shared
        deadline), "stop_failed" | "configure_failed" | "start_failed" |
        "attach_failed" | "settle_failed" | "app_start_failed" (phase raised),
        "app_not_installed" (spawn-verify / spawn-present negative),
        "menu_not_reached" (ready check answered False), "ready_check_failed"
        (ready check raised). Adapters MAY override wholesale (re-gating BOOT
        at the head); an override inherits no default body.
        """
        self._require(CapabilityFlag.BOOT)
        self._ensure_open()
        if ready is not None and not callable(ready):
            raise InvalidArgumentError(
                parameter="ready",
                constraint="must be a callable ReadyCheck or None",
            )
        budget = resolve_timeout(timeout, LIFECYCLE_TIMEOUT, operation="restart")
        logger.debug("restart ref=%s app=%s", sanitize(str(self._ref)), sanitize(str(app)))
        start = time.monotonic()
        deadline = None if budget is None else start + budget

        def remaining(operation: str) -> float | None:
            if deadline is None:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                raise EmulatorNotReadyError(
                    reason="timeout",
                    cause=OperationTimeoutError(
                        operation=operation,
                        deadline=deadline,
                        elapsed=time.monotonic() - start,
                    ),
                )
            return left

        async def phase(
            operation: str,
            reason: str,
            call: Callable[[float | None], Coroutine[Any, Any, T]],
        ) -> T:
            """Run one phase under the shared remainder; map failures (§1.6).

            CancelledError propagates unmodified (never converted); gate and
            state errors (FeatureNotSupported, ControllerClosed, invalid
            arguments — adapter wiring bugs, not readiness) propagate raw.
            """
            fresh = remaining(operation)
            try:
                if fresh is None:
                    return await call(None)
                return await asyncio.wait_for(call(fresh), fresh)
            except asyncio.CancelledError:
                raise
            except EmulatorNotReadyError:
                raise
            except OperationTimeoutError as exc:
                raise EmulatorNotReadyError(reason="timeout", cause=exc) from exc
            except TimeoutError as exc:
                raise EmulatorNotReadyError(
                    reason="timeout",
                    cause=OperationTimeoutError(
                        operation=operation,
                        deadline=deadline,
                        elapsed=time.monotonic() - start,
                    ),
                ) from exc
            except (
                FeatureNotSupportedError,
                ControllerClosedError,
                InvalidArgumentError,
            ):
                raise
            except Exception as exc:
                raise EmulatorNotReadyError(reason=reason, cause=exc) from exc

        async def attach(budget_left: float | None) -> None:
            produced: object = self._attach()
            if inspect.isawaitable(produced):
                pending = cast("Coroutine[Any, Any, None]", produced)
                if budget_left is None:
                    await pending
                else:
                    await asyncio.wait_for(pending, budget_left)

        # 1. stop-if-running
        if await phase("restart", "stop_failed", self._is_running):
            await phase("restart", "stop_failed", lambda left: self.stop(timeout=left))
        # 2. configure-while-stopped (declared capability only — never silent)
        if self.has_feature(CapabilityFlag.CONFIGURE):
            await phase(
                "restart", "configure_failed", lambda left: self.configure(None, timeout=left)
            )
        # 3. start
        await phase("restart", "start_failed", lambda left: self.start(timeout=left))
        # 4. _attach
        await phase("restart", "attach_failed", attach)
        # 5. settle: first screenshot decodes non-empty (public funnel: gate owns
        # the refusal when SCREENSHOT is undeclared)
        self._check_frame(
            await phase("restart", "settle_failed", lambda left: self.screenshot(timeout=left))
        )
        # 6. app: launch + bounded spawn-verify
        if app is not None:
            await phase(
                "restart", "app_start_failed", lambda left: self.start_app(app, timeout=left)
            )
        # 7. terminal check
        if ready is not None:
            await self._eval_ready(ready, start, deadline, remaining)
        elif app is not None:
            present = await phase(
                "restart",
                "app_not_installed",
                lambda left: self.is_app_installed(app, timeout=left),
            )
            if not present:
                raise EmulatorNotReadyError(reason="app_not_installed", cause=None)
        else:
            self._check_frame(
                await phase("restart", "settle_failed", lambda left: self.screenshot(timeout=left))
            )
        logger.debug("restart ready ref=%s", sanitize(str(self._ref)))
        return None

    async def _eval_ready(
        self,
        ready: ReadyCheck,
        start: float,
        deadline: float | None,
        remaining: Callable[[str], float | None],
    ) -> None:
        """Evaluate the terminal ReadyCheck under the shared remainder.

        A raising check becomes EmulatorNotReadyError (cause=the error); a
        False answer becomes reason "menu_not_reached". Cancellation
        propagates unmodified.
        """
        try:
            outcome = ready(self)
        except Exception as exc:
            raise EmulatorNotReadyError(reason="ready_check_failed", cause=exc) from exc
        if inspect.isawaitable(outcome):
            pending = outcome
            fresh = remaining("restart")
            try:
                outcome = await pending if fresh is None else await asyncio.wait_for(pending, fresh)
            except asyncio.CancelledError:
                raise
            except OperationTimeoutError as exc:
                raise EmulatorNotReadyError(reason="timeout", cause=exc) from exc
            except TimeoutError as exc:
                raise EmulatorNotReadyError(
                    reason="timeout",
                    cause=OperationTimeoutError(
                        operation="restart",
                        deadline=deadline,
                        elapsed=time.monotonic() - start,
                    ),
                ) from exc
            except Exception as exc:
                raise EmulatorNotReadyError(reason="ready_check_failed", cause=exc) from exc
        if not outcome:
            raise EmulatorNotReadyError(reason="menu_not_reached", cause=None)

    @staticmethod
    def _check_frame(frame: bytes) -> None:
        """Settle validation: non-empty PNG decodable by the core helper."""
        if not frame:
            raise EmulatorNotReadyError(reason="settle_failed", cause=None)
        try:
            png_dimensions(frame)
        except (ValueError, TypeError) as exc:
            raise EmulatorNotReadyError(reason="settle_failed", cause=exc) from exc

    # ── interaction / apps (gated defaults; implemented once in AdbController) ──
    async def screenshot(self, *, timeout: TimeoutParam = DEFAULT) -> bytes:
        """Current framebuffer as PNG bytes (gated SCREENSHOT)."""
        self._require(CapabilityFlag.SCREENSHOT)
        self._ensure_open()
        budget = resolve_timeout(timeout, SNAPSHOT_TIMEOUT, operation="screenshot")
        return await self._screenshot(budget)

    async def click(
        self,
        x: int,
        y: int,
        *,
        count: int = 1,
        interval: float = 0.0,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        """Click at absolute device pixels (gated INPUT)."""
        self._require(CapabilityFlag.INPUT)
        self._ensure_open()
        budget = resolve_timeout(timeout, INTERACTION_TIMEOUT, operation="click")
        await self._click(x, y, count=count, interval=interval, timeout=budget)

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int = 300,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        """Swipe in absolute device pixels (gated INPUT)."""
        self._require(CapabilityFlag.INPUT)
        self._ensure_open()
        budget = resolve_timeout(timeout, INTERACTION_TIMEOUT, operation="swipe")
        await self._swipe(x1, y1, x2, y2, duration_ms=duration_ms, timeout=budget)

    async def key(self, code: str | int, *, timeout: TimeoutParam = DEFAULT) -> None:
        """Send a raw keycode/keyevent symbol (gated INPUT)."""
        self._require(CapabilityFlag.INPUT)
        self._ensure_open()
        budget = resolve_timeout(timeout, INTERACTION_TIMEOUT, operation="key")
        await self._key(code, budget)

    async def text(self, value: str, *, timeout: TimeoutParam = DEFAULT) -> None:
        """Type text; escaping owned by the shared default (gated INPUT)."""
        self._require(CapabilityFlag.INPUT)
        self._ensure_open()
        budget = resolve_timeout(timeout, INTERACTION_TIMEOUT, operation="text")
        await self._text(value, budget)

    async def install_apk(
        self, apk_path: str | os.PathLike[str], *, timeout: TimeoutParam = DEFAULT
    ) -> None:
        """Install an APK (gated APK_INSTALL)."""
        self._require(CapabilityFlag.APK_INSTALL)
        self._ensure_open()
        budget = resolve_timeout(timeout, APP_TIMEOUT, operation="install_apk")
        await self._install_apk(apk_path, budget)

    async def start_app(self, package: str, *, timeout: TimeoutParam = DEFAULT) -> None:
        """Launch + bounded spawn-verify, no stdout parsing (gated APPS)."""
        self._require(CapabilityFlag.APPS)
        self._ensure_open()
        budget = resolve_timeout(timeout, APP_TIMEOUT, operation="start_app")
        await self._start_app(package, budget)

    async def is_app_installed(self, package: str, *, timeout: TimeoutParam = DEFAULT) -> bool:
        """Installed state as data; a failed observation raises (gated APPS)."""
        self._require(CapabilityFlag.APPS)
        self._ensure_open()
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="is_app_installed")
        return await self._is_app_installed(package, budget)

    async def is_reachable(self, *, timeout: TimeoutParam = DEFAULT) -> ProbeResult:
        """Probe as data — never raises, never gated (probes exemption).

        Port-level default: this layer has no observation seam, so it reports
        unknown; AdbController overrides with the real transport probe. The
        timeout kwarg exists for override parity and is ignored here (raising
        on it would violate the never-raises contract).
        """
        return ProbeResult(ok=False, reason="unknown")

    async def aclose(self) -> None:
        """Idempotent; releases transport only (never stops the emulator).

        Adapters override to release their transport state, then
        `await super().aclose()`. Safe to call repeatedly and on a fresh
        controller; no gate, no closed-check.
        """
        self._closed = True
        logger.debug("aclose ref=%s", sanitize(str(self._ref)))

    async def close(self) -> None:
        """Alias of aclose(): idempotent transport-only release."""
        await self.aclose()

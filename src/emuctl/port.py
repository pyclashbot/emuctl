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
  the sync ``_attach()`` hook. The configure phase funnels through
  ``configure()`` unconditionally: a BOOT-without-CONFIGURE adapter surfaces
  the derived ``FeatureNotSupportedError`` from that gate (§1 invariant 3,
  never a silent skip) and must override ``restart()`` wholesale to opt out
  (§1.6 sanctions the override).
- ``restart()`` maps deadline expiry to ``EmulatorNotReadyError`` with reason
  ``"timeout"`` and other mid-phase failures to phase-token reasons; the
  contract's example tokens (``timeout``, ``start_failed``,
  ``app_not_installed``, ``menu_not_reached``) anchor the vocabulary.
- ``create()``'s existence pre-check routes through the overridable
  ``_get_instance`` hook (default: absent); backend adapters wire it to their
  module-level discovery seam in later PRs.
- ``is_reachable()`` is ungated: gating it would contradict the probes
  exemption ("never raises"). The port default has no observation seam and
  returns data; :class:`AdbController` (below) overrides it with the real
  transport probe so every ADB-speaking adapter inherits the shared probe.
- ``AdbController`` (§1's ``class AdbController(AsyncEmulatorController,
  abc.ABC)``) lives HERE, core-side and port-adjacent: §6's sharing tiers pin
  the ADB-family middle layer so every ADB-speaking adapter can inherit it
  without a sideways family import. It carries the concrete mechanics once
  over raw ADB (interaction verbs, screenshot, app verbs, install, the
  scoping machinery ``_run_adb``/``_device_argv``/``_server_argv`` and the
  serial layer-3 re-check) implemented against the transport state the
  concrete adapter binds in its ``__init__`` — and, per §6's promotion tier,
  the shared echo-probe (``is_reachable``, the probe word, and the
  stderr-failure classifier) that two+ ADB-speaking families (plain ADB and
  MEmu) already want; backend adapters now inherit the probe instead of
  carrying private copies. Remotely backend-specific truth — the FEATURES
  set, the concrete ``_vendor_run`` through the ``_proc.py`` executor, the
  construction binding of ``AdbConfig``, and the module-level
  ``BackendDiscovery`` convention functions — stays in the backend module
  (``emuctl.backends.adb``).
  ``_vendor_run`` stays abstract here, keeping this class uninstantiable.
- Bounded spawn-verify (§1 ``start_app`` / §2 "one deadline per call,
  distributed internally"): the launch and the ``pidof`` verify share the
  SAME op-level budget — deadline established once, the remaining slice fed
  to :func:`emuctl.waits.wait_until`'s poll loop (the single sanctioned
  internal loop). Expiry raises ``OperationTimeoutError`` (§2: expiry never
  degrades to a value; ``EmulatorNotReadyError`` is a ``restart()``-only
  surface), never a silent pass. The launch phase uses a syskeys-free
  ``monkey`` invocation (headless images have no physical keys; the default
  SYS_KEYS slice aborts with rc 251), falls back once to the historical plain
  invocation, and never treats rc 0 as proof: a short acceptance window fails
  the launch if ``pidof`` never reports the process. No stdout is read (§1.9).
"""

# ruff: noqa: B027 — mechanics are deliberately non-abstract `...` stubs: the
# wiring gate (__init_subclass__) is the enforcement site, so minimal adapters
# stay instantiable. Abstract decorators would force every fake to implement
# every mechanic and defeat the per-declared-flag gate.

from __future__ import annotations

import abc
import asyncio
import contextlib
import dis
import enum
import inspect
import logging
import math
import os
import time
import types
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from typing import Any, Final, NamedTuple, TypeVar

from emuctl._identification import InstanceRecord, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl._proc import _validate_argv as _require_argv
from emuctl._serials import ADB_SERVER_VERBS, validate_serial
from emuctl.config.model import ConfigSpec, CreateSpec
from emuctl.errors import (
    ControllerClosedError,
    DeviceStateError,
    EmulatorNotReadyError,
    ExecutableNotFoundError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    OperationTimeoutError,
    VendorCommandError,
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
from emuctl.waits import wait_until

__all__ = [
    "CapabilityFlag",
    "ReadyCheck",
    "ProbeResult",
    "PROBE_REASONS",
    "MECHANICS",
    "AsyncEmulatorController",
    "AdbController",
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


class DisplayState(NamedTuple):
    """Observed display geometry (pixels + density) before a configure call.

    ``None`` means the backend could not observe that field (no readback seam);
    an ADB controller always returns concrete values. Restore explicitly by
    re-applying ``to_spec()`` — the library never restores silently.
    """

    width: int | None
    height: int | None
    dpi: int | None

    def to_spec(self) -> dict[str, int]:
        """Canonical ``ConfigSpec`` overlay for this observed state."""
        spec: dict[str, int] = {}
        if self.width is not None and self.height is not None:
            spec["display.width"] = self.width
            spec["display.height"] = self.height
        if self.dpi is not None:
            spec["display.dpi"] = self.dpi
        return spec


#: Pinned probe-reason vocabulary (additive-only). "ok" and "no-adb" are §1's
#: pins; offline/unauthorized/disconnected mirror §2's DeviceStateError states
#: as probe data; "timeout" reports a probe that exceeded its budget without
#: raising; "unknown" is the port default (this layer has no observation seam
#: — the concrete ADB adapter overrides it). The former "not-found" token is
#: cut until a producer exists (probe paths emit no absent-ref vocabulary).
PROBE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "no-adb",
        "offline",
        "unauthorized",
        "disconnected",
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


#: Opcodes permitted in a raise-only mechanic body besides the raise itself:
#: coroutine framing plus the implicit cleanup epilogue 3.11+ appends to every
#: coroutine (`RETURN_GENERATOR` … `CALL_INTRINSIC_1`/`RERAISE`), loads of the
#: exception object and its constructor arguments (globals, locals, constants,
#: closures, attribute reads — never stores), and pure value construction
#: (calls, containers, string formatting, operators). Anything else — returns,
#: awaits, stores, imports, jumps — marks a real body. Version-hedged
#: (`PRECALL`/`CALL_FUNCTION` for ≤3.11, `PUSH_NULL` for ≥3.13); an
#: unrecognized opcode fails toward "concrete", so a future-VM raise form
#: would need allowlisting here.
_RAISE_ONLY_OPS: Final[frozenset[str]] = frozenset(
    {
        "RETURN_GENERATOR",
        "POP_TOP",
        "RESUME",
        "CACHE",
        "NOP",
        "CALL_INTRINSIC_1",
        "RERAISE",
        "LOAD_GLOBAL",
        "LOAD_NAME",
        "LOAD_FAST",
        "LOAD_CONST",
        "LOAD_DEREF",
        "LOAD_CLOSURE",
        "LOAD_CLASSDEREF",
        "COPY_FREE_VARS",
        "MAKE_CELL",
        "PUSH_NULL",
        "LOAD_ASSERTION_ERROR",
        "LOAD_ATTR",
        "LOAD_METHOD",
        "LOAD_SUPER_ATTR",
        "CALL",
        "PRECALL",
        "CALL_FUNCTION",
        "CALL_FUNCTION_EX",
        "CALL_FUNCTION_KW",
        "KW_NAMES",
        "BUILD_TUPLE",
        "BUILD_LIST",
        "BUILD_MAP",
        "BUILD_CONST_KEY_MAP",
        "BUILD_STRING",
        "FORMAT_VALUE",
        "FORMAT_SIMPLE",
        "FORMAT_WITH_SPEC",
        "CONVERT_VALUE",
        "BINARY_OP",
        "BINARY_SUBSCR",
        "UNARY_POSITIVE",
        "UNARY_NEGATIVE",
        "UNARY_NOT",
        "UNARY_INVERT",
        "COMPARE_OP",
        "IS_OP",
        "CONTAINS_OP",
        "RAISE_VARARGS",
    }
)


def _is_raise_only_body(func: object) -> bool:
    """True when `func`'s only effect is raising (never returns or awaits).

    `dis`-based: the stream must contain an explicit `RAISE_VARARGS` and every
    opcode must be framing, a load, or pure value construction (see
    _RAISE_ONLY_OPS). Genuine one-line delegations
    (`return await self._vendor_run(...)`) carry `GET_AWAITABLE` /
    `RETURN_VALUE` and stay concrete. Conditional raises (jump opcodes) and
    non-disassemblable callables read as concrete — conservative misses.
    """
    code = getattr(func, "__code__", None)
    if not isinstance(code, types.CodeType):
        return False
    try:
        opnames = [ins.opname for ins in dis.get_instructions(code)]
    except Exception:
        return False
    return "RAISE_VARARGS" in opnames and all(op in _RAISE_ONLY_OPS for op in opnames)


def _is_stub_body(func: object) -> bool:
    """True when `func` is a `...`/`pass`/docstring-only stub — or a raise-only
    body — not a real body.

    Recognized stub forms: `...` / `pass` / docstring-only bodies (constants
    only, no global/attribute references), plus straight-line raise-only
    bodies (`raise NotImplementedError(...)`, `raise <global>`, bare `raise`;
    see _is_raise_only_body). A real body delegates, returns, or awaits —
    one-line delegations (`return await self._vendor_run(...)`) count as
    concrete via `co_names` and the opcode check alike.
    """
    if _is_raise_only_body(func):
        return True
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
        subclass transport knobs: adapters consume their own before calling
        `super().__init__()` — leftovers reaching this layer raise
        InvalidArgumentError naming the unknown keys (fail-fast on typos;
        never silently ignored).
        """
        if not isinstance(ref, InstanceRef):
            raise InvalidArgumentError(
                parameter="ref",
                constraint="must be an InstanceRef (connect() resolves it)",
            )
        if adapter_kwargs:
            raise InvalidArgumentError(
                parameter=", ".join(sorted(adapter_kwargs)),
                constraint="unknown adapter kwargs "
                "(each adapter consumes its own before super().__init__())",
            )
        self._ref = ref
        self._closed = False
        logger.debug("bind ref=%s", sanitize(str(ref)))

    def __init_subclass__(cls, /, **kw: object) -> None:
        """Wiring-time gate (zero I/O): every mechanic named for each declared
        flag is a concrete member somewhere in the MRO (not a `...` body).
        Raise-only bodies (`raise NotImplementedError`) count as stubs.
        Declaring CREATE additionally requires a concrete `_get_instance`
        override (the create() existence pre-check seam); the port default
        stays for non-CREATE controllers only. Violations fail loudly at class
        definition with a TypeError naming the missing mechanics.
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
        if CapabilityFlag.CREATE in features:
            seam = _mechanic_problem(cls, "_get_instance")
            if seam is not None:
                problems.append(f"'create' needs a concrete '_get_instance' override ({seam})")
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

        Backend adapters override this to consult their discovery seam; the
        override MUST honor the passed deadline (the caller's slice — create()
        and MEmu's lazy transport derivation both share it; a fresh
        query-group hard-code would violate §2's one-deadline rule). The port
        layer cannot reach backend modules (§6 layers contract), so the
        default observes nothing.
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
        """Provision a new instance; returns the new instance's ref.

        ``timeout`` is the REMAINING slice of create()'s ONE shared provision
        deadline (create() resolves the budget once and threads the remaining
        time — §2; ``None`` = explicit unbounded).
        """
        ...

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:  # ty: ignore[empty-body]
        """Apply config: None re-applies the bound config, else a targeted overlay.

        Returns the display observed immediately BEFORE the apply (fields are
        ``None`` when the backend has no display readback seam).
        """
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

        §2 deadline discipline: the provision budget is resolved ONCE here
        and the pre-check + the provisioning mechanic share it — `_create`
        is handed the REMAINING slice (never a fresh full budget), so the
        provable wall time of create() never exceeds the caller's budget.
        """
        self._require(CapabilityFlag.CREATE)
        self._ensure_open()
        if not isinstance(spec, CreateSpec):
            raise InvalidArgumentError(
                parameter="spec",
                constraint="must be a CreateSpec (name is required at v0)",
            )
        budget = resolve_timeout(timeout, PROVISION_TIMEOUT, operation="create")
        start = time.monotonic()
        deadline = None if budget is None else start + budget
        candidate = InstanceRef(backend=self._ref.backend, key=spec.name)
        if await self._get_instance(candidate, budget) is not None:
            raise InvalidArgumentError(parameter="spec.name", constraint="already provisioned")
        left: float | None = None
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                # Pre-check consumed the budget: expiry is a typed failure
                # (§2), never a non-positive slice handed to the mechanic.
                raise OperationTimeoutError(
                    operation="create",
                    deadline=deadline,
                    elapsed=time.monotonic() - start,
                )
        # The remaining slice — `left` is the shared provision deadline (§2).
        return await self._create(spec, left)

    async def configure(
        self, spec: ConfigSpec | None = None, *, timeout: TimeoutParam = DEFAULT
    ) -> DisplayState:
        """Gated CONFIGURE: None re-applies the bound config; else an overlay.

        Returns the display geometry observed immediately before applying the
        overlay. Restore explicitly by calling ``configure(state.to_spec())``;
        the library never assumes or performs a restore.
        """
        self._require(CapabilityFlag.CONFIGURE)
        self._ensure_open()
        if spec is not None and not isinstance(spec, Mapping):
            raise InvalidArgumentError(
                parameter="spec",
                constraint="must be a ConfigSpec mapping or None",
            )
        budget = resolve_timeout(timeout, CONFIG_TIMEOUT, operation="configure")
        return await self._configure(spec, budget)

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
        configure-while-stopped (always funnels through configure(); an
        undeclared CONFIGURE surfaces the gate — wholesale override is the
        opt-out) → start → _attach → settle (first screenshot
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
            # TRACKED PAIR with emuctl.backends.memu.MemuController.restart()'s
            # phase() re-raise list (§6 sharing tiers — keep both files in
            # sync when editing either list).
            except (
                FeatureNotSupportedError,
                ControllerClosedError,
                InvalidArgumentError,
            ):
                raise
            except Exception as exc:
                raise EmulatorNotReadyError(reason=reason, cause=exc) from exc

        async def attach(budget_left: float | None) -> None:
            self._attach()  # sync concrete no-op hook (§1); never awaited

        # 1. stop-if-running
        if await phase("restart", "stop_failed", self._is_running):
            await phase("restart", "stop_failed", lambda left: self.stop(timeout=left))
        # 2. configure-while-stopped (unconditional funnel — the configure()
        # gate owns the refusal when CONFIGURE is undeclared; §1 invariant 3)
        await phase("restart", "configure_failed", lambda left: self.configure(None, timeout=left))
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


# ── ADB-family middle layer (§1's AdbController, §6 sharing tiers) ──

#: Device-shell metacharacters backslash-escaped by the shared ``input text``
#: rule (adb joins shell argv with spaces, so the device shell would
#: otherwise interpret these). Space is handled separately (→ ``%s``).
_TEXT_META: Final[frozenset[str]] = frozenset("\"';&|<>()$`\\*?~#!")

#: ``monkey`` intent plumbing for package-only launches (no activity known).
_LAUNCHER_CATEGORY: Final[str] = "android.intent.category.LAUNCHER"

#: How long a launch that exited 0 gets to produce a process before it is
#: treated as a failed launch (never the full app budget): rc 0 is not proof
#: that anything started (Android's ``am`` can print "Activity not started"
#: and still exit 0). No stdout is consulted; the ``pidof`` status decides.
_LAUNCH_CONFIRM_TIMEOUT_S: Final[float] = 10.0


#: Hosts where adb would use its local server anyway. Emitting `-H` for these
#: marks the server REMOTE, and adb then refuses to auto-start it ("cannot
#: start server on remote host"), breaking cold starts on a custom `-P` port.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})


#: How long the best-effort pointer release may take (bounded cleanup).
_POINTER_RELEASE_TIMEOUT_S: Final[float] = 2.0

#: How long a lazy transport re-attach may take before the next op proceeds.
_TRANSPORT_REATTACH_TIMEOUT_S: Final[float] = 2.0


def _server_host_args(host: str) -> list[str]:
    """`-H` only for a genuinely remote server host (pure)."""
    return [] if host in _LOOPBACK_HOSTS else ["-H", host]


def _escape_input_text(value: str) -> str:
    """Apply the shared ``adb shell input text`` escaping rule (pure).

    Space → ``%s``; device-shell metacharacters → backslash-escaped;
    everything else (including ``%``) literal. See ``AdbController._text`` for
    the ``%s``-types-as-space caveat.
    """
    parts: list[str] = []
    for char in value:
        if char == " ":
            parts.append("%s")
        elif char in _TEXT_META:
            parts.append("\\" + char)
        else:
            parts.append(char)
    return "".join(parts)


#: The only canonical display leaves the ADB backend can translate.
_ADB_DISPLAY_KEYS: Final[frozenset[str]] = frozenset(
    {"display.width", "display.height", "display.dpi"}
)


def _parse_wm_size(text: str) -> tuple[int, int] | None:
    """Pick the effective ``wm size`` geometry (override wins; pure)."""
    physical: tuple[int, int] | None = None
    override: tuple[int, int] | None = None
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        label = label.strip().lower()
        if label not in ("physical size", "override size"):
            continue
        width, x, height = value.strip().partition("x")
        if not x or not width.isdigit() or not height.isdigit():
            continue
        if int(width) <= 0 or int(height) <= 0:
            continue
        if label == "override size":
            override = (int(width), int(height))
        else:
            physical = (int(width), int(height))
    return override if override is not None else physical


def _parse_wm_density(text: str) -> int | None:
    """Pick the effective ``wm density`` value (override wins; pure)."""
    physical: int | None = None
    override: int | None = None
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        label = label.strip().lower()
        if label not in ("physical density", "override density"):
            continue
        token = value.strip()
        if not token.isdigit() or int(token) <= 0:
            continue
        if label == "override density":
            override = int(token)
        else:
            physical = int(token)
    return override if override is not None else physical


def _require_positive_int(name: str, value: object) -> int:
    """Display overlay values: ints (never bools) >= 1 (pure)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidArgumentError(parameter=name, constraint="must be an int >= 1")
    return value


def _require_package(package: object) -> str:
    """Package names: non-empty text with no whitespace (pure)."""
    if (
        not isinstance(package, str)
        or not package
        or any(char.isspace() or ord(char) < 32 for char in package)
    ):
        raise InvalidArgumentError(
            parameter="package",
            constraint="must be a non-empty package name with no whitespace",
        )
    return package


def _require_coord(name: str, value: object) -> int:
    """Absolute device pixels: ints only (bools are never coords), >= 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError(parameter=name, constraint="must be an int device pixel")
    if value < 0:
        raise InvalidArgumentError(
            parameter=name, constraint="must be >= 0 (absolute device pixels)"
        )
    return value


def _stderr_excerpt(stderr: bytes) -> str:
    """Sanitized ≤200-char diagnostic excerpt (logging/error payloads only)."""
    return sanitize(stderr.decode("utf-8", errors="replace"))


#: Probe word for the shared ADB-family transport probe: ``shell echo
#: <word>``; the output is ignored (never parsed). Promoted to core per §6's
#: sharing tiers (two+ ADB-speaking families — plain ADB and MEmu — use it).
_PROBE_WORD: Final[str] = "emuctl"


def _classify_probe_failure(stderr_excerpt: str) -> str:
    """Map a failed probe's stderr to the pinned probe-reason vocabulary.

    stderr tokens only — stdout is never consulted (pure). Core-side (§6
    promotion): every ADB-speaking adapter inherits this classifier through
    ``AdbController.is_reachable`` instead of carrying a private copy.
    """
    lowered = stderr_excerpt.lower()
    if "unauthorized" in lowered:
        return "unauthorized"
    if "offline" in lowered:
        return "offline"
    return "disconnected"


def _classify_transport_state(stderr_excerpt: str) -> str | None:
    """Map a vendor failure's stderr to §2's DeviceStateError vocabulary.

    Only explicit transport-degradation shapes qualify — a normal command
    failure (``INSTALL_FAILED_*``, ``package not found``) must stay a
    ``VendorCommandError``. stderr only; stdout is never consulted (pure).
    """
    lowered = stderr_excerpt.lower()
    if "unauthorized" in lowered:
        return "unauthorized"
    if "offline" in lowered:
        return "offline"
    disconnected_tokens = (
        "no devices/emulators found",
        "device not found",
        "connection reset",
        "broken pipe",
        "protocol fault",
        "cannot connect to daemon",
    )
    if any(token in lowered for token in disconnected_tokens):
        return "disconnected"
    if "not found" in lowered and "device" in lowered:
        return "disconnected"
    return None


class AdbController(AsyncEmulatorController, abc.ABC):
    """§1's ONE ADB middle layer (core-side, port-adjacent per §6).

    Implements the interaction primitives ONCE over raw ADB so every
    ADB-speaking adapter inherits them without a sideways family import
    (§6 sharing tiers: "core, port-adjacent (all ADB-speaking adapters
    inherit; no second middle layer)"). Carried concretely:

    - the scoping machinery ``_run_adb``/``_device_argv``/``_server_argv``
      (server verbs never scoped; nothing sniffed from argv) and the
      per-call layer-3 serial re-check;
    - click/swipe/key/text (text escaping owned here), screenshot,
      install_apk, start_app (bounded spawn-verify through the sanctioned
      ``waits.wait_until`` loop), is_app_installed.

    ``_vendor_run`` stays abstract (inherited from the port): the concrete
    adapter routes it through its own ``_proc.py`` executor. So does
    transport state — the annotations below are bound by the concrete
    adapter's ``__init__`` from the resolved ``EmuctlConfig`` (§1:
    ``config.adb`` is the ONLY system-server selector); this layer only
    reads them. FEATURES and discovery remain backend-module truth;
    ``is_reachable`` is the SHARED probe implemented at this layer (§6
    promotion tier) and inherited as-is.
    """

    #: Transport state, bound by the concrete adapter's __init__ (read-only here).
    adb_path: str | None
    adb_server_port: int | None
    _adb_host: str
    _serial: str
    _executor: ProcessExecutor
    _config: Any

    def _prefix(self) -> list[str]:
        """Server selector: binary + (remote ``-H``) + ``-P``.

        ``-H`` makes adb treat the server as REMOTE and refuse to auto-start it
        ("cannot start server on remote host"), so it is emitted only for a
        genuinely remote host. Loopback defaults to the plain local server
        (``-P`` only) and keeps the documented cold-start behaviour.
        """
        binary = self.adb_path if self.adb_path else "adb"
        prefix = [binary, *_server_host_args(self._adb_host)]
        if self.adb_server_port is not None:
            prefix += ["-P", str(self.adb_server_port)]
        return prefix

    def _device_argv(self, tail: Sequence[str]) -> list[str]:
        """Full device-scoped argv: prefix + exactly one ``-s <transport>``.

        Re-validates the transport at every call (§3's third enforcement
        layer). Pure.
        """
        args = _require_argv(tail)
        validate_serial(self._serial)
        return [*self._prefix(), "-s", self._serial, *args]

    def _server_argv(self, tail: Sequence[str]) -> list[str]:
        """Full server-verb argv: prefix, never ``-s``.

        The head word must be in the pinned ``ADB_SERVER_VERBS`` exclusion
        list — the declaration (``server_verb=True``) selects the shape, the
        list polices it; nothing is ever sniffed from argv.
        """
        args = _require_argv(tail)
        if args[0] not in ADB_SERVER_VERBS:
            raise InvalidArgumentError(
                parameter="argv",
                constraint=(f"server-verb call must head with one of {sorted(ADB_SERVER_VERBS)}"),
            )
        return [*self._prefix(), *args]

    async def _reattach_transport(self, timeout: float) -> None:
        """Lazily re-issue ``adb connect`` after a classified transport loss.

        A single socket blip otherwise poisons a long-lived facade forever:
        adb forgets the transport and every later op fails until someone
        connects again. This runs at the START of the next op (never as a
        retry of the failed one), as a bounded server verb, and swallows its
        own failure so the op's own typed error remains authoritative. The
        budget is the caller op's remaining slice, capped at
        :data:`_TRANSPORT_REATTACH_TIMEOUT_S` — never a fresh timeout (§2).
        """
        argv = self._server_argv(["connect", self._serial])
        logger.debug("adb op=reattach argv=%s", [sanitize(a) for a in argv])
        try:
            await self._executor.run(argv, operation="reattach", timeout=timeout)
        except Exception:
            logger.debug("adb reattach failed serial=%s", sanitize(self._serial))

    async def _run_adb(
        self,
        tail: Sequence[str],
        *,
        binary: bool,
        operation: str,
        timeout: float | None,
        server_verb: bool = False,
    ) -> bytes | str:
        """Execute one adb argv: scoped, bounded, cancel-safe (internal seam).

        Returns raw stdout bytes (``binary=True``) or decoded text. Nonzero
        rc → ``DeviceStateError`` for explicit transport degradation, else
        ``VendorCommandError(output_reason="exit")``; stdout is returned for
        the caller to verify, never interpreted here. ``CancelledError``
        propagates unmodified (the executor owns abandonment).
        """
        if getattr(self, "_transport_stale", False):
            self._transport_stale = False
            budget = _TRANSPORT_REATTACH_TIMEOUT_S
            if timeout is not None:
                budget = min(budget, timeout)
            if budget > 0:
                await self._reattach_transport(budget)
        argv = self._server_argv(tail) if server_verb else self._device_argv(tail)
        logger.debug("adb op=%s argv=%s", sanitize(operation), [sanitize(a) for a in argv])
        try:
            result: ProcessResult = await self._executor.run(
                argv, operation=operation, timeout=timeout
            )
        except OSError as exc:
            raise ExecutableNotFoundError(
                tool=argv[0],
                searched=[argv[0]],
                detail=f"could not execute the adb binary: {type(exc).__name__}",
            ) from exc
        if result.returncode != 0:
            excerpt = _stderr_excerpt(result.stderr)
            state = _classify_transport_state(excerpt)
            if state is not None:
                # §2: mid-op transport degradation is DeviceStateError, not a
                # vendor command failure (the observation itself failed). Mark
                # the transport for a lazy re-attach on the NEXT op; never
                # retry this one (§2: no exception-driven retries).
                self._transport_stale = True
                raise DeviceStateError(serial=self._serial, state=state)
            raise VendorCommandError(
                command=argv,
                returncode=result.returncode,
                output_reason="exit",
                stderr_excerpt=excerpt,
            )
        if binary:
            return result.stdout
        return result.stdout.decode("utf-8", errors="replace")

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:
        """Apply display size/density over ``wm`` and return the PREVIOUS state.

        Spec shape (canonical §4 leaves only): ``display.width`` +
        ``display.height`` (both required together) and/or ``display.dpi``,
        each an int >= 1. ``None`` re-applies the bound ``DisplayConfig``'s
        non-None leaves. Reads are machine-readable ``wm`` observations with a
        pinned line grammar (override wins, else physical) and fail closed;
        commands key on exit status only. Everything shares ONE config-group
        deadline, distributed across reads and writes. Applying the same
        geometry twice performs reads but no writes (idempotent, no churn).
        """
        if spec is None:
            display = self._config.display
            width, height, dpi = display.width, display.height, display.dpi
        else:
            for key in spec:
                if not isinstance(key, str) or key not in _ADB_DISPLAY_KEYS:
                    raise InvalidArgumentError(
                        parameter="spec",
                        constraint=(
                            "adb configure supports display.width, display.height and display.dpi"
                        ),
                    )
            width = spec.get("display.width")
            height = spec.get("display.height")
            dpi = spec.get("display.dpi")
            if width is not None:
                width = _require_positive_int("display.width", width)
            if height is not None:
                height = _require_positive_int("display.height", height)
            if dpi is not None:
                dpi = _require_positive_int("display.dpi", dpi)
            if (width is None) != (height is None):
                raise InvalidArgumentError(
                    parameter="spec",
                    constraint="display.width and display.height must be supplied together",
                )
        if (width is None) != (height is None):
            raise InvalidArgumentError(
                parameter="display.width",
                constraint="display.width and display.height must be configured together",
            )
        size = (width, height) if width is not None and height is not None else None
        if size is None and dpi is None:
            return DisplayState(None, None, None)  # nothing to apply/observe

        start = time.monotonic()
        deadline = None if timeout is None else start + float(timeout)

        def left() -> float | None:
            if deadline is None:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationTimeoutError(
                    operation="configure", deadline=deadline, elapsed=time.monotonic() - start
                )
            return remaining

        size_text = await self._run_adb(
            ["shell", "wm", "size"], binary=False, operation="configure", timeout=left()
        )
        density_text = await self._run_adb(
            ["shell", "wm", "density"], binary=False, operation="configure", timeout=left()
        )
        assert isinstance(size_text, str) and isinstance(density_text, str)
        observed_size = _parse_wm_size(size_text)
        if observed_size is None:
            raise VendorCommandError(
                command=self._device_argv(["shell", "wm", "size"]),
                returncode=0,
                output_reason="unusable_output",
                stderr_excerpt="",
                detail="wm size returned no valid Physical/Override size line",
            )
        observed_dpi = _parse_wm_density(density_text)
        if observed_dpi is None:
            raise VendorCommandError(
                command=self._device_argv(["shell", "wm", "density"]),
                returncode=0,
                output_reason="unusable_output",
                stderr_excerpt="",
                detail="wm density returned no valid Physical/Override density line",
            )
        previous = DisplayState(observed_size[0], observed_size[1], observed_dpi)

        if size is not None and size != observed_size:
            await self._run_adb(
                ["shell", "wm", "size", f"{size[0]}x{size[1]}"],
                binary=False,
                operation="configure",
                timeout=left(),
            )
        if dpi is not None and dpi != observed_dpi:
            await self._run_adb(
                ["shell", "wm", "density", str(dpi)],
                binary=False,
                operation="configure",
                timeout=left(),
            )
        return previous

    async def _screenshot(self, timeout: float | None) -> bytes:
        """``exec-out screencap -p`` binary transport; PNG-sniffed (§4 S2)."""
        tail = ["exec-out", "screencap", "-p"]
        raw = await self._run_adb(tail, binary=True, operation="screenshot", timeout=timeout)
        assert isinstance(raw, bytes)
        try:
            png_dimensions(raw)
        except (ValueError, TypeError) as exc:
            raise VendorCommandError(
                command=self._device_argv(tail),
                returncode=0,
                output_reason="unusable_output",
                stderr_excerpt="",
                detail=f"screenshot returned {len(raw)} bytes that do not decode as PNG",
            ) from exc
        return raw

    async def _release_pointer(self, operation: str) -> None:
        """Best-effort lift of a pointer stuck DOWN after an interrupted gesture.

        A tap/swipe killed between the device's DOWN and UP leaves
        InputDispatcher in "already down" state, after which every later
        ``input tap`` exits 0 and does nothing. One bounded, error-swallowed
        ``motionevent UP`` restores input without masking the original
        timeout/cancellation (which the caller re-raises immediately after).
        """
        with contextlib.suppress(Exception):
            await self._run_adb(
                ["shell", "input", "motionevent", "UP", "0", "0"],
                binary=False,
                operation=operation,
                timeout=_POINTER_RELEASE_TIMEOUT_S,
            )

    async def _click(
        self, x: int, y: int, *, count: int, interval: float, timeout: float | None
    ) -> None:
        """Raw device pixels, never re-scaled (§4 INV-C1).

        One shared deadline for the whole multi-click burst (§2: one deadline
        per call, distributed internally): the remaining slice — never the
        full budget anew — is fed to each additional tap.
        """
        _require_coord("x", x)
        _require_coord("y", y)
        if isinstance(count, bool) or not isinstance(count, int):
            raise InvalidArgumentError(parameter="count", constraint="must be an int >= 1")
        if count < 1:
            raise InvalidArgumentError(parameter="count", constraint="must be >= 1")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
        ):
            raise InvalidArgumentError(
                parameter="interval",
                constraint="must be a finite number of seconds >= 0",
            )
        if interval < 0:
            raise InvalidArgumentError(parameter="interval", constraint="must be >= 0 seconds")
        start = time.monotonic()
        deadline = None if timeout is None else start + timeout
        for index in range(count):
            headroom = timeout if deadline is None or index == 0 else deadline - time.monotonic()
            if headroom is not None and headroom <= 0:
                raise OperationTimeoutError(
                    operation="click",
                    deadline=deadline,
                    elapsed=time.monotonic() - start,
                )
            try:
                await self._run_adb(
                    ["shell", "input", "tap", str(x), str(y)],
                    binary=False,
                    operation="click",
                    timeout=headroom,
                )
            except (OperationTimeoutError, VendorCommandError, DeviceStateError):
                # CancelledError is NOT caught (§2/§5: it propagates
                # unmodified and adds no cleanup delay).
                await self._release_pointer("click")
                raise
            if interval and index < count - 1:
                await asyncio.sleep(float(interval))

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
        """Raw device pixels, never re-scaled (§4 INV-C1)."""
        _require_coord("x1", x1)
        _require_coord("y1", y1)
        _require_coord("x2", x2)
        _require_coord("y2", y2)
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
            raise InvalidArgumentError(parameter="duration_ms", constraint="must be an int >= 0")
        if duration_ms < 0:
            raise InvalidArgumentError(parameter="duration_ms", constraint="must be >= 0")
        try:
            await self._run_adb(
                [
                    "shell",
                    "input",
                    "swipe",
                    str(x1),
                    str(y1),
                    str(x2),
                    str(y2),
                    str(duration_ms),
                ],
                binary=False,
                operation="swipe",
                timeout=timeout,
            )
        except (OperationTimeoutError, VendorCommandError, DeviceStateError):
            # CancelledError is NOT caught (§2/§5).
            await self._release_pointer("swipe")
            raise

    async def _key(self, code: str | int, timeout: float | None) -> None:
        """Raw keycode/keyevent symbol, passed through verbatim."""
        if isinstance(code, bool):
            raise InvalidArgumentError(
                parameter="code", constraint="must be a keycode int or keyevent symbol"
            )
        if isinstance(code, int):
            if code < 0:
                raise InvalidArgumentError(parameter="code", constraint="must be >= 0")
            token = str(code)
        elif isinstance(code, str):
            if not code:
                raise InvalidArgumentError(
                    parameter="code", constraint="must be a non-empty keyevent symbol"
                )
            token = code
        else:
            raise InvalidArgumentError(
                parameter="code", constraint="must be a keycode int or keyevent symbol"
            )
        await self._run_adb(
            ["shell", "input", "keyevent", token],
            binary=False,
            operation="key",
            timeout=timeout,
        )

    async def _text(self, value: str, timeout: float | None) -> None:
        """Type text under the shared escaping rule (see ``_escape_input_text``)."""
        if not isinstance(value, str):
            raise InvalidArgumentError(parameter="value", constraint="must be a string")
        if not value:
            raise InvalidArgumentError(parameter="value", constraint="must be non-empty")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise InvalidArgumentError(
                parameter="value",
                constraint="must not contain control characters (use key() for ENTER)",
            )
        await self._run_adb(
            ["shell", "input", "text", _escape_input_text(value)],
            binary=False,
            operation="text",
            timeout=timeout,
        )

    async def _install_apk(self, apk_path: str | os.PathLike[str], timeout: float | None) -> None:
        """Bare ``adb install`` (no flags — predictable overwrite semantics)."""
        if isinstance(apk_path, os.PathLike):
            apk = os.fspath(apk_path)
        elif isinstance(apk_path, str):
            apk = apk_path
        else:
            raise InvalidArgumentError(
                parameter="apk_path",
                constraint="must be a path to an APK file",
            )
        if not apk:
            raise InvalidArgumentError(parameter="apk_path", constraint="must be a non-empty path")
        await self._run_adb(
            ["install", apk], binary=False, operation="install_apk", timeout=timeout
        )

    async def _start_app(self, package: str, timeout: float | None) -> None:
        """Cold launch: force-stop, launcher intent, bounded ``pidof`` verify (§1).

        A warm device can be mid-run on a popup; ``monkey`` alone would resume
        whatever screen Clash is on and burn the caller's restart budget. The
        force-stop therefore precedes the launch, and all three phases
        (force-stop, launch, verify) share the SAME op-level budget (§2: one
        deadline per call, distributed internally) — the deadline is
        established once; the first slice carries the op-level budget and the
        later phases are handed the shrinking remainder, never a full budget
        anew. Force-stop and launch failures surface as
        ``VendorCommandError`` (nonzero rc / unusable output); deadline expiry
        in any phase raises ``OperationTimeoutError`` (§2: expiry never
        degrades to a value). The verify predicate keys on the ``pidof`` exit
        status only (0 = a process exists); its stdout is consumed, never read
        for the decision.

        The launch phase uses package-scoped ``monkey`` under that one
        deadline. On headless platform images (observed: some images) monkey's
        default event mix injects SYS_KEYS events and aborts with rc 251
        because the image has no physical keys; the primary invocation
        therefore disables that event class with ``--pct-syskeys 0``. If that
        invocation still exits nonzero, one historical plain event-mix
        invocation is tried (for monkey builds that reject the option), under
        the same shrinking budget. ``monkey`` is not
        contract-mandated — §1 requires "launch + bounded spawn-verify (no
        stdout parsing)"; both invocations decide from exit status only. If
        both fail, one ``VendorCommandError`` carries the fallback's payload
        plus both return codes in ``detail``, so operators see the original
        failure too.

        A successful launch command is not treated as proof of a launch
        (§1.9): the process must appear within
        :data:`_LAUNCH_CONFIRM_TIMEOUT_S` of the launch, or the call raises
        ``OperationTimeoutError`` with a detail explaining that the command
        exited 0 but no process was observed — it never silently burns the
        full app budget polling for a launch that never happened.

        Explicit unbounded (``timeout=None``, sanctioned-but-discouraged)
        degrades the verify to a single exit-status probe — polling requires
        a finite budget shape, and a second unsanctioned loop is not
        invented to fake it.
        """
        pkg = _require_package(package)
        start = time.monotonic()
        deadline = None if timeout is None else start + float(timeout)

        def remaining() -> float | None:
            """The one shared deadline's remaining slice (§2)."""
            if deadline is None:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                raise OperationTimeoutError(
                    operation="start_app",
                    deadline=deadline,
                    elapsed=time.monotonic() - start,
                )
            return left

        await self._run_adb(
            ["shell", "am", "force-stop", pkg],
            binary=False,
            operation="start_app",
            timeout=timeout,
        )
        try:
            await self._run_adb(
                [
                    "shell",
                    "monkey",
                    "-p",
                    pkg,
                    "-c",
                    _LAUNCHER_CATEGORY,
                    "--pct-syskeys",
                    "0",
                    "1",
                ],
                binary=False,
                operation="start_app",
                timeout=remaining(),
            )
        except VendorCommandError as primary_error:
            # The syskeys-free mix is the primary launch: headless images have
            # no physical keys, so monkey's default SYS_KEYS slice aborts with
            # rc 251 even though the platform is healthy. The fallback below
            # keeps the historical mix for monkey builds that reject the
            # option; it is a different invocation, not a blind retry, and it
            # spends the SAME shrinking op-level budget (``remaining()``),
            # never a fresh full timeout. Only exit status decides; stdout is
            # never parsed (§1.9).
            try:
                await self._run_adb(
                    ["shell", "monkey", "-p", pkg, "-c", _LAUNCHER_CATEGORY, "1"],
                    binary=False,
                    operation="start_app",
                    timeout=remaining(),
                )
            except VendorCommandError as fallback_error:
                detail = (
                    f"syskeys-free monkey launch failed with rc={primary_error.returncode}; "
                    f"plain monkey fallback failed with rc={fallback_error.returncode}"
                )
                if primary_error.stderr_excerpt:
                    detail += f"; primary stderr: {primary_error.stderr_excerpt}"
                raise VendorCommandError(
                    command=fallback_error.command,
                    returncode=fallback_error.returncode,
                    output_reason=fallback_error.output_reason,
                    stderr_excerpt=fallback_error.stderr_excerpt,
                    detail=detail,
                ) from primary_error
            logger.debug(
                "start_app syskeys-free monkey rc=%s failed; plain monkey fallback launched %s",
                primary_error.returncode,
                sanitize(pkg),
            )
        if deadline is None:
            # Explicit unbounded (§2: sanctioned, discouraged): polling is a
            # finite-budget shape (waits.wait_until requires a positive
            # timeout), so the verify degrades to a single exit-status probe —
            # never a silent pass, never a second unsanctioned loop.
            await self._run_adb(
                ["shell", "pidof", pkg], binary=False, operation="start_app", timeout=None
            )
            return
        headroom = remaining()
        assert headroom is not None

        async def spawn_present() -> bool:
            try:
                await self._run_adb(
                    ["shell", "pidof", pkg],
                    binary=False,
                    operation="start_app",
                    timeout=deadline - time.monotonic(),
                )
            except VendorCommandError:
                return False  # absent this poll; keep polling to the shared deadline
            return True

        # rc 0 is not proof of a launch: bound the acceptance window so a
        # silent no-op (observed with ``am start -p``: exit 0, nothing started)
        # fails in seconds instead of burning the whole app budget.
        confirm = min(headroom, _LAUNCH_CONFIRM_TIMEOUT_S)
        try:
            await wait_until(spawn_present, operation="start_app", timeout=confirm)
        except OperationTimeoutError as exc:
            raise OperationTimeoutError(
                operation="start_app",
                deadline=exc.deadline,
                elapsed=exc.elapsed,
                detail=(
                    "launch command exited 0 but pidof found no process for "
                    f"{sanitize(pkg)} within {confirm:.1f}s; treating the launch "
                    "as failed (launch stdout is never parsed, §1.9)"
                ),
            ) from exc

    async def _is_app_installed(self, package: str, timeout: float | None) -> bool:
        """Exact ``package:<pkg>`` line match on ``pm`` output (§1.9).

        ``pm`` applies its own substring filter server-side, so the
        client-side exact-line check is required for correctness (a
        ``com.foo`` query also lists ``com.foobar``). No substrings, no
        regex, no free-text parsing.
        """
        pkg = _require_package(package)
        out = await self._run_adb(
            ["shell", "pm", "list", "packages", pkg],
            binary=False,
            operation="is_app_installed",
            timeout=timeout,
        )
        assert isinstance(out, str)
        wanted = f"package:{pkg}"
        return any(line.strip() == wanted for line in out.splitlines())

    async def is_reachable(self, *, timeout: TimeoutParam = DEFAULT) -> ProbeResult:
        """Probe as data — never raises, never gated (probes exemption).

        The SHARED ADB-family transport probe (§6 promotion tier: implemented
        once at this middle layer; backend adapters inherit it, their
        transport hooks — e.g. lazy serial resolution — engage through the
        overridden ``_run_adb``). Device-scoped ``shell echo``; the decision
        keys on exit status plus a stderr-token scan only (stdout is never
        consulted): rc 0 → "ok"; budget expiry → "timeout"; missing binary →
        "no-adb"; nonzero rc → stderr-token scan (unauthorized/offline/
        disconnected); anything else (including a stale backend ref) →
        "disconnected". Two backend facts map through the shared catch-all:
        a stale backend ref (surfacing as the identity error from a lazy
        resolution inside ``_run_adb``) and any unclassified vendor failure
        are probe DATA, never raises.

        The whole probe (transport resolution included — it happens inside
        one ``_run_adb`` call) is wall-clock bounded by the ONE query budget:
        with a finite budget the call itself is wrapped, so provable wall
        time never exceeds the caller's slice. ``CancelledError`` propagates
        unmodified (the executor owns abandonment). No gate, no closed-check:
        a closed transport reports what it observes, as data.
        """
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="is_reachable")
        try:
            if budget is None:
                await self._run_adb(
                    ["shell", "echo", _PROBE_WORD],
                    binary=False,
                    operation="is_reachable",
                    timeout=None,
                )
            else:
                await asyncio.wait_for(
                    self._run_adb(
                        ["shell", "echo", _PROBE_WORD],
                        binary=False,
                        operation="is_reachable",
                        timeout=budget,
                    ),
                    budget,
                )
            # asyncio.TimeoutError aliases TimeoutError on 3.11+; kept explicit
            # for the probes exemption's readability (both map to "timeout").
        except OperationTimeoutError:
            return ProbeResult(ok=False, reason="timeout")
        except TimeoutError:
            return ProbeResult(ok=False, reason="timeout")
        except ExecutableNotFoundError:
            return ProbeResult(ok=False, reason="no-adb")
        except DeviceStateError as exc:
            return ProbeResult(ok=False, reason=exc.state)
        except VendorCommandError as exc:
            return ProbeResult(ok=False, reason=_classify_probe_failure(exc.stderr_excerpt))
        except Exception:
            return ProbeResult(ok=False, reason="disconnected")
        return ProbeResult(ok=True, reason="ok")

# ruff: noqa: B027 — mechanics are deliberately non-abstract `...` stubs: the

from __future__ import annotations

import abc
import asyncio
import dis
import enum
import inspect
import logging
import os
import time
import types
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from typing import Any, Final, NamedTuple, TypeVar

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
    "AdbController",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CapabilityFlag(enum.Enum):
    BOOT = "boot"
    CREATE = "create"
    CONFIGURE = "configure"
    SCREENSHOT = "screenshot"
    INPUT = "input"
    APPS = "apps"
    APK_INSTALL = "apk_install"


ReadyCheck = Callable[["AsyncEmulatorController"], "bool | Awaitable[bool]"]


class ProbeResult(NamedTuple):
    ok: bool
    reason: str


class DisplayState(NamedTuple):
    width: int | None
    height: int | None
    dpi: int | None

    def to_spec(self) -> dict[str, int]:
        spec: dict[str, int] = {}
        if self.width is not None and self.height is not None:
            spec["display.width"] = self.width
            spec["display.height"] = self.height
        if self.dpi is not None:
            spec["display.dpi"] = self.dpi
        return spec


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


MECHANICS: Final[dict[CapabilityFlag, tuple[str, ...]]] = {
    CapabilityFlag.BOOT: ("_start", "_stop", "_is_running"),
    CapabilityFlag.CREATE: ("_create",),
    CapabilityFlag.CONFIGURE: ("_configure",),
    CapabilityFlag.SCREENSHOT: ("_screenshot",),
    CapabilityFlag.INPUT: ("_click", "_swipe", "_key", "_text"),
    CapabilityFlag.APPS: ("_start_app", "_is_app_installed"),
    CapabilityFlag.APK_INSTALL: ("_install_apk",),
}


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
    code = getattr(func, "__code__", None)
    if not isinstance(code, types.CodeType):
        return False
    try:
        opnames = [ins.opname for ins in dis.get_instructions(code)]
    except Exception:
        return False
    return "RAISE_VARARGS" in opnames and all(op in _RAISE_ONLY_OPS for op in opnames)


def _is_stub_body(func: object) -> bool:
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
    FEATURES: frozenset[CapabilityFlag] = frozenset()

    def __init__(self, ref: InstanceRef, **adapter_kwargs: object) -> None:
        if not isinstance(ref, InstanceRef):
            raise InvalidArgumentError(
                parameter="ref",
                constraint="must be an InstanceRef (connect() resolves it)",
            )
        if adapter_kwargs:
            raise InvalidArgumentError(
                parameter=", ".join(sorted(adapter_kwargs)),
                constraint="unknown adapter kwargs (each adapter consumes its own before super().__init__())",
            )
        self._ref = ref
        self._closed = False
        logger.debug("bind ref=%s", sanitize(str(ref)))

    def __init_subclass__(cls, /, **kw: object) -> None:
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
                    f"{cls.__qualname__}.FEATURES must contain only CapabilityFlag members, got {member!r}"
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
        return self._ref

    @property
    def features(self) -> frozenset[CapabilityFlag]:
        return self.FEATURES

    def has_feature(self, flag: CapabilityFlag) -> bool:
        return flag in self.FEATURES

    def _require(self, flag: CapabilityFlag) -> None:
        if flag in self.FEATURES:
            return
        feature = flag.value if isinstance(flag, CapabilityFlag) else str(flag)
        raise FeatureNotSupportedError(feature=feature)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ControllerClosedError()

    @abc.abstractmethod
    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str: ...

    def _attach(self) -> None:
        return None

    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> InstanceRecord | None:
        return None

    # mechanics stubs; adapters provide bodies and the wiring gate checks them
    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> InstanceRecord | None:
        return None

    async def _start(self, timeout: float | None) -> None: ...

    async def _stop(self, timeout: float | None) -> None: ...

    async def _is_running(self, timeout: float | None) -> bool:  # ty: ignore[empty-body]
        ...

    async def _create(self, spec: CreateSpec, timeout: float | None) -> InstanceRef: ...  # ty: ignore[empty-body]

    async def create(self, spec: CreateSpec, *, timeout: TimeoutParam = DEFAULT) -> InstanceRef:
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
        self._require(CapabilityFlag.BOOT)
        self._ensure_open()
        budget = resolve_timeout(timeout, LIFECYCLE_TIMEOUT, operation="start")
        await self._start(budget)

    async def stop(self, *, timeout: TimeoutParam = DEFAULT) -> None:
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
        png_dimensions(await phase("restart", "settle_failed", lambda left: self.screenshot(timeout=left)))
        # 6. app: launch + bounded spawn-verify
        if app is not None:
            await phase("restart", "app_start_failed", lambda left: self.start_app(app, timeout=left))
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
            png_dimensions(await phase("restart", "settle_failed", lambda left: self.screenshot(timeout=left)))
        logger.debug("restart ready ref=%s", sanitize(str(self._ref)))
        return None

    async def _eval_ready(
        self,
        ready: ReadyCheck,
        start: float,
        deadline: float | None,
        remaining: Callable[[str], float | None],
    ) -> None:
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

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:  # ty: ignore[empty-body]
        ...

    async def _screenshot(self, timeout: float | None) -> bytes:  # ty: ignore[empty-body]
        ...

    async def _click(self, x: int, y: int, *, count: int, interval: float, timeout: float | None) -> None: ...

    async def _swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int,
        timeout: float | None,
    ) -> None: ...

    async def _key(self, code: str | int, timeout: float | None) -> None: ...

    async def _text(self, value: str, timeout: float | None) -> None: ...

    async def _start_app(self, package: str, timeout: float | None) -> None: ...

    async def _is_app_installed(self, package: str, timeout: float | None) -> bool:  # ty: ignore[empty-body]
        ...

    async def _install_apk(self, apk_path: str | os.PathLike[str], timeout: float | None) -> None: ...

    async def screenshot(self, *, timeout: TimeoutParam = DEFAULT) -> bytes:
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
        self._require(CapabilityFlag.INPUT)
        self._ensure_open()
        budget = resolve_timeout(timeout, INTERACTION_TIMEOUT, operation="swipe")
        await self._swipe(x1, y1, x2, y2, duration_ms=duration_ms, timeout=budget)

    async def key(self, code: str | int, *, timeout: TimeoutParam = DEFAULT) -> None:
        self._require(CapabilityFlag.INPUT)
        self._ensure_open()
        budget = resolve_timeout(timeout, INTERACTION_TIMEOUT, operation="key")
        await self._key(code, budget)

    async def text(self, value: str, *, timeout: TimeoutParam = DEFAULT) -> None:
        self._require(CapabilityFlag.INPUT)
        self._ensure_open()
        budget = resolve_timeout(timeout, INTERACTION_TIMEOUT, operation="text")
        await self._text(value, budget)

    async def install_apk(self, apk_path: str | os.PathLike[str], *, timeout: TimeoutParam = DEFAULT) -> None:
        self._require(CapabilityFlag.APK_INSTALL)
        self._ensure_open()
        budget = resolve_timeout(timeout, APP_TIMEOUT, operation="install_apk")
        await self._install_apk(apk_path, budget)

    async def start_app(self, package: str, *, timeout: TimeoutParam = DEFAULT) -> None:
        self._require(CapabilityFlag.APPS)
        self._ensure_open()
        budget = resolve_timeout(timeout, APP_TIMEOUT, operation="start_app")
        await self._start_app(package, budget)

    async def is_app_installed(self, package: str, *, timeout: TimeoutParam = DEFAULT) -> bool:
        self._require(CapabilityFlag.APPS)
        self._ensure_open()
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="is_app_installed")
        return await self._is_app_installed(package, budget)

    async def is_reachable(self, *, timeout: TimeoutParam = DEFAULT) -> ProbeResult:
        return ProbeResult(ok=False, reason="unknown")

    async def aclose(self) -> None:
        self._closed = True
        logger.debug("aclose ref=%s", sanitize(str(self._ref)))

    async def close(self) -> None:
        await self.aclose()


_TEXT_META: Final[frozenset[str]] = frozenset("\"';&|<>()$`\\*?~#!")

_LAUNCHER_CATEGORY: Final[str] = "android.intent.category.LAUNCHER"

_LAUNCH_CONFIRM_TIMEOUT_S: Final[float] = 10.0


_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})


_POINTER_RELEASE_TIMEOUT_S: Final[float] = 2.0

_TRANSPORT_REATTACH_TIMEOUT_S: Final[float] = 2.0

from emuctl._adb import AdbController  # noqa: E402

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from emuctl.log import sanitize

__all__ = [
    "EmuctlError",
    "InvalidArgumentError",
    "InvalidSerialError",
    "ExtrasMissingError",
    "ExecutableNotFoundError",
    "ConfigError",
    "ConfigValidationError",
    "OperationTimeoutError",
    "VendorCommandError",
    "InstanceNotFoundError",
    "AmbiguousInstanceError",
    "DeviceStateError",
    "FeatureNotSupportedError",
    "BackendNotAvailableError",
    "BackendNotFoundError",
    "EmulatorNotReadyError",
    "ControllerClosedError",
    "SyncCallInsideEventLoopError",
    "resolve_error_code",
]


def _reconstruct(cls: type[EmuctlError], state: dict[str, Any]) -> EmuctlError:
    obj = cls.__new__(cls)
    Exception.__init__(obj)
    obj.__dict__.update(state)
    return obj


class EmuctlError(Exception):
    error_code: ClassVar[str] = "emuctl_error"
    suggestion: ClassVar[str] = ""
    payload: ClassVar[tuple[str, ...]] = ()

    def __init__(self, *, detail: str | None = None) -> None:
        super().__init__()
        self.detail = detail

    def __str__(self) -> str:
        parts = [f"{name}={sanitize(repr(getattr(self, name)))}" for name in type(self).payload]
        text = self.error_code if not parts else f"{self.error_code}: {', '.join(parts)}"
        if self.detail:
            text += f": {sanitize(self.detail)}"
        return text

    def __reduce__(
        self,
    ) -> tuple[Callable[..., EmuctlError], tuple[type[EmuctlError], dict[str, Any]]]:
        return (_reconstruct, (type(self), dict(self.__dict__)))


class InvalidArgumentError(EmuctlError):
    error_code: ClassVar[str] = "invalid_argument"
    suggestion: ClassVar[str] = "Check the value of '{parameter}': {constraint}."
    payload: ClassVar[tuple[str, ...]] = ("parameter", "constraint")

    def __init__(self, *, parameter: str, constraint: str, detail: str | None = None) -> None:
        super().__init__(detail=detail)
        self.parameter = parameter
        self.constraint = constraint


class InvalidSerialError(InvalidArgumentError):
    error_code: ClassVar[str] = "invalid_serial"
    suggestion: ClassVar[str] = (
        "Use the canonical 'backend:key' form (e.g. 'adb:127.0.0.1:5555'); got '{serial}'."
    )
    payload: ClassVar[tuple[str, ...]] = ("serial",)

    def __init__(self, *, serial: str, detail: str | None = None) -> None:
        EmuctlError.__init__(self, detail=detail)
        self.serial = serial


class ExtrasMissingError(EmuctlError):
    error_code: ClassVar[str] = "extras_missing"
    suggestion: ClassVar[str] = "Install the '{extra}' extra: pip install 'emuctl[{extra}]'."
    payload: ClassVar[tuple[str, ...]] = ("extra",)

    def __init__(self, *, extra: str, detail: str | None = None) -> None:
        super().__init__(detail=detail)
        self.extra = extra


class ExecutableNotFoundError(EmuctlError):
    error_code: ClassVar[str] = "executable_not_found"
    suggestion: ClassVar[str] = "Install '{tool}' and ensure it is on PATH."
    payload: ClassVar[tuple[str, ...]] = ("tool", "searched")

    def __init__(
        self,
        *,
        tool: str,
        searched: Sequence[str],
        detail: str | None = None,
    ) -> None:
        super().__init__(detail=detail)
        self.tool = tool
        self.searched = tuple(searched)


class ConfigError(EmuctlError):
    error_code: ClassVar[str] = "config"
    suggestion: ClassVar[str] = "Check config layer '{layer}' at '{path}': {reason}."
    payload: ClassVar[tuple[str, ...]] = ("layer", "path", "reason", "value")

    def __init__(
        self,
        *,
        layer: str,
        path: str,
        reason: str,
        value: str | int | float | bool | None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail=detail)
        self.layer = layer
        self.path = path
        self.reason = reason
        self.value = value


class ConfigValidationError(ConfigError):
    error_code: ClassVar[str] = "config_validation"
    suggestion: ClassVar[str] = "Fix config value at '{path}' in layer '{layer}': expected {expected}."
    payload: ClassVar[tuple[str, ...]] = ("layer", "path", "value", "expected")

    def __init__(
        self,
        *,
        layer: str,
        path: str,
        value: str | int | float | bool | None,
        expected: str,
        detail: str | None = None,
    ) -> None:
        EmuctlError.__init__(self, detail=detail)
        self.layer = layer
        self.path = path
        self.value = value
        self.expected = expected


class OperationTimeoutError(EmuctlError):
    error_code: ClassVar[str] = "timeout"
    suggestion: ClassVar[str] = "Retry with a larger timeout for '{operation}'."
    payload: ClassVar[tuple[str, ...]] = ("operation", "deadline", "elapsed")

    def __init__(
        self,
        *,
        operation: str,
        deadline: float | None,
        elapsed: float | None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail=detail)
        self.operation = operation
        self.deadline = None if deadline is None else float(deadline)
        self.elapsed = None if elapsed is None else float(elapsed)


class VendorCommandError(EmuctlError):
    error_code: ClassVar[str] = "vendor_command_failed"
    suggestion: ClassVar[str] = "Re-run the vendor command manually to reproduce; check device state."
    payload: ClassVar[tuple[str, ...]] = (
        "command",
        "returncode",
        "output_reason",
        "stderr_excerpt",
    )

    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int | None,
        output_reason: str,
        stderr_excerpt: str,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail=detail)
        self.command = tuple(command)
        self.returncode = returncode
        self.output_reason = output_reason
        self.stderr_excerpt = stderr_excerpt


class InstanceNotFoundError(EmuctlError):
    """No such instance. instance_not_found → exit 4.

    Payload carries canonical str(ref), never the object.
    """

    error_code: ClassVar[str] = "instance_not_found"
    suggestion: ClassVar[str] = (
        "List instances with 'emuctl list' and use a canonical ref; got '{instance_ref}'."
    )
    payload: ClassVar[tuple[str, ...]] = ("instance_ref",)

    def __init__(self, *, instance_ref: str, detail: str | None = None) -> None:
        super().__init__(detail=detail)
        self.instance_ref = instance_ref


class AmbiguousInstanceError(EmuctlError):
    """Selector matched several instances. ambiguous_target → exit 6."""

    error_code: ClassVar[str] = "ambiguous_target"
    suggestion: ClassVar[str] = "Disambiguate '{selector}' with a canonical 'backend:key' ref."
    payload: ClassVar[tuple[str, ...]] = ("selector", "matched")

    def __init__(
        self,
        *,
        selector: str,
        matched: Sequence[str],
        detail: str | None = None,
    ) -> None:
        super().__init__(detail=detail)
        self.selector = selector
        self.matched = tuple(matched)


class DeviceStateError(EmuctlError):
    error_code: ClassVar[str] = "device_state"
    suggestion: ClassVar[str] = "Reconnect or reboot the device at '{serial}' (state: {state})."
    payload: ClassVar[tuple[str, ...]] = ("serial", "state")

    def __init__(self, *, serial: str, state: str, detail: str | None = None) -> None:
        super().__init__(detail=detail)
        self.serial = serial
        self.state = state


class FeatureNotSupportedError(EmuctlError):
    error_code: ClassVar[str] = "feature_not_supported"
    suggestion: ClassVar[str] = (
        "Query 'emuctl probe' for supported features; '{feature}' is not supported by this backend."
    )
    payload: ClassVar[tuple[str, ...]] = ("feature",)

    def __init__(self, *, feature: str, detail: str | None = None) -> None:
        super().__init__(detail=detail)
        self.feature = feature


class BackendNotAvailableError(EmuctlError):
    error_code: ClassVar[str] = "backend_not_available"
    suggestion: ClassVar[str] = "Reason: {reason}."
    payload: ClassVar[tuple[str, ...]] = ("reason",)

    def __init__(self, *, reason: str, detail: str | None = None) -> None:
        super().__init__(detail=detail)
        self.reason = reason


class BackendNotFoundError(EmuctlError):
    error_code: ClassVar[str] = "backend_not_found"
    suggestion: ClassVar[str] = "Use a known backend id; got '{backend_id}'."
    payload: ClassVar[tuple[str, ...]] = ("backend_id",)

    def __init__(self, *, backend_id: str, detail: str | None = None) -> None:
        super().__init__(detail=detail)
        self.backend_id = backend_id


class EmulatorNotReadyError(EmuctlError):
    error_code: ClassVar[str] = "not_ready"
    suggestion: ClassVar[str] = "Restart and wait for readiness; phase '{reason}' did not complete."
    payload: ClassVar[tuple[str, ...]] = ("reason", "cause")

    def __init__(
        self,
        *,
        reason: str,
        cause: BaseException | str | None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail=detail)
        self.reason = reason
        self.cause = cause


class ControllerClosedError(EmuctlError):
    error_code: ClassVar[str] = "controller_closed"
    suggestion: ClassVar[str] = "The controller is closed; construct a new one with connect()."
    payload: ClassVar[tuple[str, ...]] = ()


class SyncCallInsideEventLoopError(EmuctlError):
    error_code: ClassVar[str] = "sync_call_inside_loop"
    suggestion: ClassVar[str] = "Use 'await emulator.aio.<op>()' inside a running event loop."
    payload: ClassVar[tuple[str, ...]] = ()


def resolve_error_code(exc: BaseException) -> str:
    for klass in type(exc).__mro__:
        code = klass.__dict__.get("error_code")
        if isinstance(code, str):
            return code
    return "emuctl_error"

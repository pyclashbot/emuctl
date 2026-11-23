from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import math
import os
import time
from collections.abc import Sequence
from typing import Any, Final

from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl._proc import _validate_argv as _require_argv
from emuctl._serials import ADB_SERVER_VERBS, validate_serial
from emuctl.config.model import ConfigSpec
from emuctl.errors import (
    DeviceStateError,
    ExecutableNotFoundError,
    InvalidArgumentError,
    OperationTimeoutError,
    VendorCommandError,
)
from emuctl.log import sanitize
from emuctl.png import png_dimensions
from emuctl.port import AsyncEmulatorController, DisplayState, ProbeResult
from emuctl.timing import (
    DEFAULT,
    QUERY_TIMEOUT,
    TimeoutParam,
    resolve_timeout,
)
from emuctl.waits import wait_until

logger = logging.getLogger(__name__)

_TEXT_META: Final[frozenset[str]] = frozenset("\"';&|<>()$`\\*?~#!")
_LAUNCHER_CATEGORY: Final[str] = "android.intent.category.LAUNCHER"
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_POINTER_RELEASE_TIMEOUT_S: Final[float] = 2.0
_TRANSPORT_REATTACH_TIMEOUT_S: Final[float] = 2.0
_LAUNCH_CONFIRM_TIMEOUT_S: Final[float] = 10.0


def _server_host_args(host: str) -> list[str]:
    return [] if host in _LOOPBACK_HOSTS else ["-H", host]


def _escape_input_text(value: str) -> str:
    parts: list[str] = []
    for char in value:
        if char == " ":
            parts.append("%s")
        elif char in _TEXT_META:
            parts.append("\\" + char)
        else:
            parts.append(char)
    return "".join(parts)


_ADB_DISPLAY_KEYS: Final[frozenset[str]] = frozenset({"display.width", "display.height", "display.dpi"})


def _parse_wm_size(text: str) -> tuple[int, int] | None:
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
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidArgumentError(parameter=name, constraint="must be an int >= 1")
    return value


def _require_package(package: object) -> str:
    if not isinstance(package, str) or not package or any(char.isspace() or ord(char) < 32 for char in package):
        raise InvalidArgumentError(
            parameter="package",
            constraint="must be a non-empty package name with no whitespace",
        )
    return package


def _require_coord(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError(parameter=name, constraint="must be an int device pixel")
    if value < 0:
        raise InvalidArgumentError(parameter=name, constraint="must be >= 0 (absolute device pixels)")
    return value


def _stderr_excerpt(stderr: bytes) -> str:
    return sanitize(stderr.decode("utf-8", errors="replace"))


_PROBE_WORD: Final[str] = "emuctl"


def _classify_probe_failure(stderr_excerpt: str) -> str:
    lowered = stderr_excerpt.lower()
    if "unauthorized" in lowered:
        return "unauthorized"
    if "offline" in lowered:
        return "offline"
    return "disconnected"


def _classify_transport_state(stderr_excerpt: str) -> str | None:
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
    adb_path: str | None
    adb_server_port: int | None
    _adb_host: str
    _serial: str
    _executor: ProcessExecutor
    _config: Any

    def _prefix(self) -> list[str]:
        binary = self.adb_path if self.adb_path else "adb"
        prefix = [binary, *_server_host_args(self._adb_host)]
        if self.adb_server_port is not None:
            prefix += ["-P", str(self.adb_server_port)]
        return prefix

    def _device_argv(self, tail: Sequence[str]) -> list[str]:
        args = _require_argv(tail)
        validate_serial(self._serial)
        return [*self._prefix(), "-s", self._serial, *args]

    def _server_argv(self, tail: Sequence[str]) -> list[str]:
        args = _require_argv(tail)
        if args[0] not in ADB_SERVER_VERBS:
            raise InvalidArgumentError(
                parameter="argv",
                constraint=(f"server-verb call must head with one of {sorted(ADB_SERVER_VERBS)}"),
            )
        return [*self._prefix(), *args]

    async def _reattach_transport(self, timeout: float) -> None:
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
            result: ProcessResult = await self._executor.run(argv, operation=operation, timeout=timeout)
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
        if spec is None:
            display = self._config.display
            width, height, dpi = display.width, display.height, display.dpi
        else:
            for key in spec:
                if not isinstance(key, str) or key not in _ADB_DISPLAY_KEYS:
                    raise InvalidArgumentError(
                        parameter="spec",
                        constraint=("adb configure supports display.width, display.height and display.dpi"),
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
            return DisplayState(None, None, None)

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
        with contextlib.suppress(Exception):
            await self._run_adb(
                ["shell", "input", "motionevent", "UP", "0", "0"],
                binary=False,
                operation=operation,
                timeout=_POINTER_RELEASE_TIMEOUT_S,
            )

    async def _click(self, x: int, y: int, *, count: int, interval: float, timeout: float | None) -> None:
        _require_coord("x", x)
        _require_coord("y", y)
        if isinstance(count, bool) or not isinstance(count, int):
            raise InvalidArgumentError(parameter="count", constraint="must be an int >= 1")
        if count < 1:
            raise InvalidArgumentError(parameter="count", constraint="must be >= 1")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval):
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
            await self._release_pointer("swipe")
            raise

    async def _key(self, code: str | int, timeout: float | None) -> None:
        if isinstance(code, bool):
            raise InvalidArgumentError(parameter="code", constraint="must be a keycode int or keyevent symbol")
        if isinstance(code, int):
            if code < 0:
                raise InvalidArgumentError(parameter="code", constraint="must be >= 0")
            token = str(code)
        elif isinstance(code, str):
            if not code:
                raise InvalidArgumentError(parameter="code", constraint="must be a non-empty keyevent symbol")
            token = code
        else:
            raise InvalidArgumentError(parameter="code", constraint="must be a keycode int or keyevent symbol")
        await self._run_adb(
            ["shell", "input", "keyevent", token],
            binary=False,
            operation="key",
            timeout=timeout,
        )

    async def _text(self, value: str, timeout: float | None) -> None:
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
        await self._run_adb(["install", apk], binary=False, operation="install_apk", timeout=timeout)

    async def _start_app(self, package: str, timeout: float | None) -> None:
        pkg = _require_package(package)
        start = time.monotonic()
        deadline = None if timeout is None else start + float(timeout)

        def remaining() -> float | None:
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
            await self._run_adb(["shell", "pidof", pkg], binary=False, operation="start_app", timeout=None)
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
                return False
            return True

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

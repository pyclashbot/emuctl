"""§1/§3 — Plain-ADB backend: AdbController + the BackendDiscovery seam.

``AdbController`` is the ONE ADB middle layer (CONTRACT §1): a concrete
:class:`emuctl.port.AsyncEmulatorController` subclass positioned at
``emuctl.backends.adb`` that implements the interaction primitives once over
raw ADB. The registry pins this module's ``adapter_attr`` to
``"AdbController"``, so this class doubles as the plain-ADB concrete adapter;
future ADB-speaking adapters (e.g. BlueStacks Air's bundled hd-adb) subclass
it and fill in their own lifecycle mechanics.

Transport: ``adb_path``/``adb_server_port`` (+ server host) are bound at
construction from the resolved :class:`EmuctlConfig` — ``config.adb`` is the
ONLY system-server selector. Construction is cheap: no discovery, no boot, no
probe, never raises ``EmulatorNotReadyError``. ``aclose()`` releases the
transport only (sweeps executor stragglers; never stops anything).

Interpretation decisions (CONTRACT wins; these fill its silences):

- argv prefix is always ``[<adb_path or "adb">, "-H", host, "-P", port]``
  (``-P`` omitted only when the bound port is ``None``). Device commands
  append exactly ``-s <validated transport>`; the pinned
  ``ADB_SERVER_VERBS`` set (``emuctl/_serials.py``, single site) is the only
  exclusion, selected by an explicit ``server_verb=True`` caller declaration
  — never sniffed from argv (a ``server_verb=True`` call whose head word is
  not in the pinned set is caller misuse → ``InvalidArgumentError``).
- ``_vendor_run`` is always device-scoped (auto ``-s``). Module-level
  discovery (``adb devices``) bypasses it through the shared private runner
  with ``server_verb=True``.
- App verbs use the standard package-only ADB shapes: ``pm list packages``
  with exact ``package:<pkg>`` line verification (machine-readable
  verification per §1.9 — never substring/regex/free-text matching);
  ``monkey -p <pkg> -c android.intent.category.LAUNCHER 1`` for launch plus a
  bounded ``pidof`` spawn-verify keyed on exit status only (stdout is
  consumed, never read for the decision); bare ``install`` for APKs.
- ``input text`` escaping: space → ``%s`` (adb joins ``shell`` argv with
  spaces, so raw spaces would split on-device), device-shell metacharacters
  (``" ' ; & | < > ( ) $ ` \\ * ? ~ # !``) backslash-escaped, everything else
  literal. A literal ``%`` is passed through: adb's ``input text`` maps
  ``%s`` to space, so a user-supplied ``%s`` sequence types as a space
  (inherited adb behavior, documented here rather than silently mangled).
  Control characters (including CR/LF) are caller misuse — ``key()`` owns
  ENTER.
- ``is_reachable()`` probes ``shell echo emuctl`` (device-scoped) and maps
  the outcome to data only: rc 0 → ``("ok")``; nonzero → stderr-token scan
  (``unauthorized``/``offline``/``disconnected`` — stderr, never stdout);
  missing binary → ``"no-adb"``; budget expiry → ``"timeout"``. stdout is
  never parsed for the decision. ``CancelledError`` propagates unmodified.
- Discovery enumeration (``adb devices``) is the sanctioned stdout-reading
  probe: rows become ``InstanceRecord``\\ s with ``running=True`` only for
  the exact state word ``device``; every other state (offline/unauthorized/
  …) is ``running=False`` with the raw word preserved in
  ``details["adb_state"]``. Unparseable lines and non-allowlist serials are
  skipped, never laundered into refs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Sequence
from typing import Final

from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl._serials import ADB_SERVER_VERBS, is_valid_serial, validate_serial
from emuctl.config.model import EmuctlConfig
from emuctl.errors import (
    ExecutableNotFoundError,
    InvalidArgumentError,
    OperationTimeoutError,
    VendorCommandError,
)
from emuctl.log import sanitize
from emuctl.png import png_dimensions
from emuctl.port import AsyncEmulatorController, CapabilityFlag, ProbeResult
from emuctl.timing import (
    DEFAULT,
    QUERY_TIMEOUT,
    TimeoutParam,
    resolve_timeout,
)

__all__ = ["AdbController", "list_instances", "get_instance"]

logger = logging.getLogger(__name__)

#: Device-shell metacharacters backslash-escaped by the shared ``input text``
#: rule (adb joins shell argv with spaces, so the device shell would
#: otherwise interpret these). Space is handled separately (→ ``%s``).
_TEXT_META: Final[frozenset[str]] = frozenset("\"';&|<>()$`\\*?~#!")

#: ``monkey`` intent plumbing for package-only launches (no activity known).
_LAUNCHER_CATEGORY: Final[str] = "android.intent.category.LAUNCHER"

#: Probe word: ``shell echo <word>``; the output is ignored (never parsed).
_PROBE_WORD: Final[str] = "emuctl"


def _escape_input_text(value: str) -> str:
    """Apply the shared ``adb shell input text`` escaping rule (pure).

    Space → ``%s``; device-shell metacharacters → backslash-escaped;
    everything else (including ``%``) literal. See the module docstring for
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


def _require_argv(tail: Sequence[str]) -> list[str]:
    """Enforce argv-only tails before any I/O (never a bare string)."""
    if isinstance(tail, str) or not isinstance(tail, Sequence):
        raise InvalidArgumentError(
            parameter="argv",
            constraint="must be a sequence of program arguments, never a command string",
        )
    args = list(tail)
    if not args:
        raise InvalidArgumentError(parameter="argv", constraint="must be non-empty")
    for element in args:
        if not isinstance(element, str):
            raise InvalidArgumentError(
                parameter="argv",
                constraint="every argv element must be str (argv-only; never shell)",
            )
    return args


def _require_config(config: EmuctlConfig | None) -> EmuctlConfig:
    """Default programmatic consumers to a bare config (files are sugar)."""
    if config is None:
        return EmuctlConfig()
    if not isinstance(config, EmuctlConfig):
        raise InvalidArgumentError(
            parameter="config",
            constraint="must be an EmuctlConfig or None",
        )
    return config


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


def _classify_probe_failure(stderr_excerpt: str) -> str:
    """Map a failed probe's stderr to the pinned probe-reason vocabulary.

    stderr tokens only — stdout is never consulted (pure).
    """
    lowered = stderr_excerpt.lower()
    if "unauthorized" in lowered:
        return "unauthorized"
    if "offline" in lowered:
        return "offline"
    return "disconnected"


class AdbController(AsyncEmulatorController):
    """The ONE ADB middle layer + the plain-ADB concrete adapter (§1).

    Declares exactly what plain ADB supports: SCREENSHOT / INPUT / APPS /
    APK_INSTALL. No BOOT / CREATE / CONFIGURE — ``restart()``/``start()``/
    ``stop()``/``create()``/``configure()`` raise the derived
    ``FeatureNotSupportedError`` through the port funnel (§1 invariant 3,
    never a silent skip); ``_attach`` inherits the port no-op.
    """

    FEATURES: frozenset[CapabilityFlag] = frozenset(
        {
            CapabilityFlag.SCREENSHOT,
            CapabilityFlag.INPUT,
            CapabilityFlag.APPS,
            CapabilityFlag.APK_INSTALL,
        }
    )

    def __init__(
        self,
        ref: InstanceRef,
        *,
        config: EmuctlConfig | None = None,
        _executor: ProcessExecutor | None = None,
    ) -> None:
        """Bind the ref + resolved config. No discovery, no boot, no probe.

        ``config`` is the ``connect()`` convention (selection already ran the
        registry + capability gates). ``_executor`` is the single private
        injection seam (transport-constructor-level per §6): tests pass
        ``ProcessExecutor(_spawn=...)``; production uses the real spawner.
        """
        super().__init__(ref)
        cfg = _require_config(config)
        validate_serial(ref.key)  # fail fast; per-call re-checks follow
        self._config = cfg
        self.adb_path: str | None = cfg.adb.path
        self.adb_server_port: int | None = cfg.adb.port
        self._adb_host: str = cfg.adb.host
        self._serial: str = ref.key
        self._executor = _executor if _executor is not None else ProcessExecutor()
        logger.debug("bind adb ref=%s", sanitize(str(ref)))

    def _prefix(self) -> list[str]:
        """Server selector: binary + ``-H``/``-P`` (deterministic, always)."""
        binary = self.adb_path if self.adb_path else "adb"
        prefix = [binary, "-H", self._adb_host]
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
        rc → ``VendorCommandError(output_reason="exit")``; stdout is returned
        for the caller to verify, never interpreted here. ``CancelledError``
        propagates unmodified (the executor owns abandonment).
        """
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
            raise VendorCommandError(
                command=argv,
                returncode=result.returncode,
                output_reason="exit",
                stderr_excerpt=_stderr_excerpt(result.stderr),
            )
        if binary:
            return result.stdout
        return result.stdout.decode("utf-8", errors="replace")

    async def _vendor_run(
        self,
        argv: Sequence[str],
        *,
        binary: bool = False,
        timeout: TimeoutParam = DEFAULT,
    ) -> bytes | str:
        """Ungated internal seam: always device-scoped (auto ``-s``).

        Server verbs never flow through here — module-level discovery uses
        the runner with ``server_verb=True`` instead. ``timeout`` resolves
        against the query group (mechanics carry their own op-group budgets
        and call the runner directly).
        """
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="vendor_run")
        return await self._run_adb(argv, binary=binary, operation="vendor_run", timeout=budget)

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

    async def _click(
        self, x: int, y: int, *, count: int, interval: float, timeout: float | None
    ) -> None:
        """Raw device pixels, never re-scaled (§4 INV-C1)."""
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
        for index in range(count):
            await self._run_adb(
                ["shell", "input", "tap", str(x), str(y)],
                binary=False,
                operation="click",
                timeout=timeout,
            )
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
        await self._run_adb(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
            binary=False,
            operation="swipe",
            timeout=timeout,
        )

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
        """Type text under the shared escaping rule (see module docstring)."""
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
        """Launcher-intent launch + bounded ``pidof`` spawn-verify.

        Verification keys on the ``pidof`` exit status only (0 = a process
        exists); its stdout is consumed, never read for the decision. A
        negative verify is ``VendorCommandError(output_reason="exit")`` —
        never a silent launch-and-hope.
        """
        pkg = _require_package(package)
        await self._run_adb(
            ["shell", "monkey", "-p", pkg, "-c", _LAUNCHER_CATEGORY, "1"],
            binary=False,
            operation="start_app",
            timeout=timeout,
        )
        verify = ["shell", "pidof", pkg]
        await self._run_adb(verify, binary=False, operation="start_app", timeout=timeout)

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
        """Probe as data — never raises on observation failure (probes exemption).

        Device-scoped ``shell echo``; the decision keys on exit status plus a
        stderr-token scan only (stdout is never consulted). ``CancelledError``
        propagates unmodified. No gate, no closed-check: a closed transport
        reports what it observes, as data.
        """
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="is_reachable")
        try:
            await self._run_adb(
                ["shell", "echo", _PROBE_WORD],
                binary=False,
                operation="is_reachable",
                timeout=budget,
            )
        except OperationTimeoutError:
            return ProbeResult(ok=False, reason="timeout")
        except ExecutableNotFoundError:
            return ProbeResult(ok=False, reason="no-adb")
        except OSError:
            return ProbeResult(ok=False, reason="no-adb")
        except VendorCommandError as exc:
            return ProbeResult(ok=False, reason=_classify_probe_failure(exc.stderr_excerpt))
        except Exception:
            return ProbeResult(ok=False, reason="disconnected")
        return ProbeResult(ok=True, reason="ok")

    async def aclose(self) -> None:
        """Idempotent; releases transport only (never stops the emulator)."""
        await self._executor.close()
        await super().aclose()


def _discovery_argv(cfg: EmuctlConfig) -> list[str]:
    """Server-verb argv for ``adb devices`` from the resolved config (pure)."""
    binary = cfg.adb.path if cfg.adb.path else "adb"
    return [binary, "-H", cfg.adb.host, "-P", str(cfg.adb.port), "devices"]


async def list_instances(
    config: EmuctlConfig, *, _executor: ProcessExecutor | None = None
) -> tuple[InstanceRecord, ...]:
    """Enumerate plausibly-present devices via ``adb devices`` (read-only, bounded).

    The sanctioned stdout-reading probe (§3): cold ``adb devices`` may start
    the local ADB server (documented exception). Vendor/process failures
    propagate (``discover()`` isolates them into ``BackendStatus`` rows).
    """
    cfg = _require_config(config)
    executor = _executor if _executor is not None else ProcessExecutor()
    argv = _discovery_argv(cfg)
    logger.debug("adb op=list_instances argv=%s", [sanitize(a) for a in argv])
    result: ProcessResult = await executor.run(
        argv, operation="list_instances", timeout=QUERY_TIMEOUT
    )
    if result.returncode != 0:
        raise VendorCommandError(
            command=argv,
            returncode=result.returncode,
            output_reason="exit",
            stderr_excerpt=_stderr_excerpt(result.stderr),
        )
    text = result.stdout.decode("utf-8", errors="replace")
    lines = text.splitlines()
    records: list[InstanceRecord] = []
    for line in lines[1:] if lines else []:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if not is_valid_serial(serial):
            continue
        records.append(
            InstanceRecord(
                ref=InstanceRef(backend=BackendId.ADB, key=serial),
                running=state == "device",
                serial=serial,
                details={"adb_state": state},
            )
        )
    logger.debug("adb list_instances rows=%d", len(records))
    return tuple(records)


async def get_instance(
    config: EmuctlConfig, ref: InstanceRef, *, _executor: ProcessExecutor | None = None
) -> InstanceRecord | None:
    """Return the record for ``ref`` or ``None`` when absent (never raises it).

    Non-ADB refs are not ours → ``None`` (no cross-backend fan-out).
    """
    if not isinstance(ref, InstanceRef):
        raise InvalidArgumentError(
            parameter="ref",
            constraint="must be an InstanceRef",
        )
    if ref.backend is not BackendId.ADB:
        return None
    for record in await list_instances(config, _executor=_executor):
        if record.ref.key == ref.key:
            return record
    return None

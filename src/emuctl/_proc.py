"""§2/§6 — Subprocess discipline: the internal async executor.

The single choke point for every vendor/child process interaction in this
repo (CONTRACT §2 cancellation/timeout/cleanup, §6 sharing tiers):

- argv-only spawning, forever: never ``shell=True``, never a
  string-resolved command in argv (a bare string or non-str element is
  caller misuse → ``InvalidArgumentError``, before any I/O).
- Bounded timeout: expiry raises ``OperationTimeoutError`` (via
  ``timing.resolve_timeout`` semantics + the shared ``DEFAULT`` sentinel),
  never conflated with cancellation.
- Cancellation: the caller's ``asyncio.CancelledError`` propagates
  UNMODIFIED; the child is abandoned with ``terminate()`` → a bounded
  (~2 s) reap wait under shield → ``kill()``. No GC-based kill exists
  anywhere here (no ``__del__``); ``close()`` is the explicit sweeper.
- Scoped kills only: the kill target is ever just the child handle this
  executor spawned. Global sweeps (``taskkill /IM xxx.exe``-style, or any
  enumeration of processes beyond our own handle) are banned — there is no
  code path that can express them.
- Windows consoles stay hidden (``CREATE_NO_WINDOW``); stdout/stderr are
  gathered for diagnostics only and NEVER used for control flow; dynamic
  strings are DEBUG-narrated through ``log.sanitize`` only.

Private injection seam: ``ProcessExecutor(..., _spawn=...)``. ``_spawn``
receives the validated argv list and returns an awaitable process handle
exposing ``terminate``/``kill``/``wait``/``communicate``/``returncode``/
``pid`` (mirroring ``asyncio.subprocess.Process``). Tests replay recorded
subprocess transcripts through it without spawning real processes. The seam
is private (leading underscore, not exported) and is the ONLY seam — never
a plugin registry.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from emuctl.errors import InvalidArgumentError, OperationTimeoutError
from emuctl.log import sanitize
from emuctl.timing import DEFAULT, QUERY_TIMEOUT, TimeoutParam, resolve_timeout

__all__ = ["ProcessExecutor", "ProcessResult", "run_process"]

logger = logging.getLogger(__name__)

#: Bounded reap grace after terminate() before escalating to kill() (§2).
_TERMINATE_GRACE_S = 2.0


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of one child run: raw bytes plus the exit status.

    Diagnostics only — a nonzero ``returncode`` is data, never a control
    signal (the ``_vendor_run`` layer maps it to ``VendorCommandError``).
    """

    stdout: bytes
    stderr: bytes
    returncode: int


class _ProcessHandle(Protocol):
    """Minimal child-process surface the executor touches (structural).

    Mirrors the slice of ``asyncio.subprocess.Process`` used here so the
    ``_spawn`` seam can replay scripted fakes without real processes.
    """

    @property
    def pid(self) -> int | None: ...
    @property
    def returncode(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...
    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]: ...


#: The single private spawn-seam type: validated argv in, awaitable handle out.
_SpawnFn = Callable[[Sequence[str]], Awaitable[_ProcessHandle]]


def _creationflags() -> int:
    """Windows console suppression, factored for unit tests without Windows."""
    if sys.platform == "win32":
        flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return int(flag)
    return 0


async def _default_spawn(argv: Sequence[str]) -> _ProcessHandle:
    """Real spawner: argv-only exec with piped outputs and no window."""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=PIPE,
        stderr=PIPE,
        stdin=DEVNULL,
        creationflags=_creationflags(),
    )


async def _default_spawn_detached(argv: Sequence[str]) -> _ProcessHandle:
    """Real DETACHED spawner: argv-only exec, outputs discarded (DEVNULL).

    A GUI child (an emulator player binary) writes nothing anyone reads; its
    stdout/stderr are therefore DEVNULL — no pipe fill can ever deadlock it.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=DEVNULL,
        stderr=DEVNULL,
        stdin=DEVNULL,
        creationflags=_creationflags(),
    )


def _validate_argv(argv: Sequence[str]) -> list[str]:
    """Enforce argv-only: a non-empty sequence of str, never a bare string."""
    if isinstance(argv, str) or not isinstance(argv, Sequence):
        raise InvalidArgumentError(
            parameter="argv",
            constraint="must be a sequence of program arguments, never a command string",
        )
    args = list(argv)
    if not args:
        raise InvalidArgumentError(parameter="argv", constraint="must be non-empty")
    for element in args:
        if not isinstance(element, str):
            raise InvalidArgumentError(
                parameter="argv",
                constraint="every argv element must be str (argv-only; never shell)",
            )
    return args


class ProcessExecutor:
    """Internal async executor: bounded, cancel-safe, scoped child runs.

    Tracks live children so ``close()`` can sweep stragglers; ``close()``
    is idempotent and the executor stays usable afterwards.
    """

    def __init__(self, *, _spawn: _SpawnFn | None = None) -> None:
        """Bind the spawner. `_spawn` is THE private injection seam (see module
        docstring): tests pass a transcript-replaying fake; production uses
        the real argv-only spawner. Not exported, never a registry. The same
        seam (when injected) also serves :meth:`spawn_detached`, so tests
        replay GUI launches too; production detached spawns use the DEVNULL
        spawner above."""
        self._spawn_fn: _SpawnFn = _spawn if _spawn is not None else _default_spawn
        self._spawn_detached_fn: _SpawnFn = (
            self._spawn_fn if _spawn is not None else _default_spawn_detached
        )
        self._live: set[_ProcessHandle] = set()

    def _track(self, proc: _ProcessHandle) -> None:
        self._live.add(proc)

    def _untrack(self, proc: _ProcessHandle) -> None:
        self._live.discard(proc)

    async def _abandon(self, proc: _ProcessHandle) -> None:
        """Best-effort scoped kill: terminate → bounded reap → kill.

        Never raises — cleanup must not mask the timeout/cancellation it
        serves. Targets only `proc`, never a sweep.
        """
        with suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), _TERMINATE_GRACE_S)
            return
        except Exception:
            logger.debug("proc reap timed out; escalating to kill")
        with suppress(ProcessLookupError, OSError):
            proc.kill()
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), _TERMINATE_GRACE_S)

    async def run(
        self, argv: Sequence[str], *, operation: str, timeout: TimeoutParam = DEFAULT
    ) -> ProcessResult:
        """Run one child to completion: ``(stdout, stderr, returncode)``.

        `operation` names the op for timeout payloads/narration (required,
        no default — every deadline must be attributable). `timeout`
        follows ``timing.resolve_timeout`` against the query group default;
        ``None`` is explicit unbounded. Expiry abandons the child and raises
        ``OperationTimeoutError``; cancellation abandons under shield and
        re-raises the caller's ``CancelledError`` unmodified. Nonzero exit
        is returned as data, never raised.
        """
        args = _validate_argv(argv)
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation=operation)
        logger.debug("proc run op=%s argv=%s", sanitize(operation), [sanitize(a) for a in args])
        start = time.monotonic()
        deadline = start + budget if budget is not None else None
        proc: _ProcessHandle | None = None
        try:
            try:
                if budget is None:
                    proc = await self._spawn_fn(args)
                    self._track(proc)
                    stdout, stderr = await proc.communicate()
                else:

                    async def _spawn_and_communicate() -> tuple[bytes, bytes]:
                        nonlocal proc
                        spawned = await self._spawn_fn(args)
                        proc = spawned
                        self._track(spawned)
                        return await spawned.communicate()

                    stdout, stderr = await asyncio.wait_for(_spawn_and_communicate(), budget)
            except TimeoutError:
                elapsed = time.monotonic() - start
                if proc is not None:
                    logger.debug("proc abandon op=%s", sanitize(operation))
                    await self._abandon(proc)
                raise OperationTimeoutError(
                    operation=operation, deadline=deadline, elapsed=elapsed
                ) from None
            except asyncio.CancelledError:
                if proc is not None:
                    logger.debug("proc abandon op=%s", sanitize(operation))
                    await asyncio.shield(self._abandon(proc))
                raise
            assert proc is not None
            returncode = proc.returncode
            if returncode is None:
                returncode = await proc.wait()
            logger.debug(
                "proc done op=%s rc=%d out=%dB err=%dB",
                sanitize(operation),
                returncode,
                len(stdout),
                len(stderr),
            )
            return ProcessResult(stdout=stdout, stderr=stderr, returncode=returncode)
        finally:
            if proc is not None:
                self._untrack(proc)

    async def spawn_detached(
        self, argv: Sequence[str], *, operation: str = "spawn_detached"
    ) -> _ProcessHandle:
        """Fire-and-forget DETACHED child spawn: the §6 single-place owner of
        launches whose child must OUTLIVE the call (emulator GUI players).

        The returned handle is deliberately NOT tracked: ``close()`` sweeps
        only tracked children, so a detached emulator is never terminated or
        killed by executor teardown (close() never stops the emulator, §1).
        There is no timeout and no await of the child — the caller holds the
        handle for run-state observation only. stdout/stderr go to DEVNULL
        (the production spawner): a GUI child can never be held by an
        un-drained pipe. argv-only validation identical to ``run()`` fires
        BEFORE any I/O (a bare string or non-str element raises
        ``InvalidArgumentError``); Windows console suppression matches
        ``run()`` (``CREATE_NO_WINDOW``). Dynamic strings are DEBUG-narrated
        through ``log.sanitize`` only.

        Returns the same structural handle type as the ``_spawn`` seam
        (``terminate``/``kill``/``wait``/``communicate``/``returncode``/
        ``pid``); the emitter is never awaited past spawn.
        """
        args = _validate_argv(argv)
        logger.debug(
            "proc spawn-detached op=%s argv=%s",
            sanitize(operation),
            [sanitize(a) for a in args],
        )
        return await self._spawn_detached_fn(args)

    async def close(self) -> None:
        """Idempotent: abandon every still-tracked straggler, then return.

        Safe to call repeatedly and safe on an empty set; the executor
        remains usable afterwards.
        """
        for proc in list(self._live):
            await self._abandon(proc)
            self._untrack(proc)


async def run_process(
    argv: Sequence[str], *, operation: str, timeout: TimeoutParam = DEFAULT
) -> ProcessResult:
    """One-shot helper over an ephemeral :class:`ProcessExecutor`.

    Creates an executor, runs once, closes (sweeping stragglers). Long-lived
    owners (controllers) hold their own executor instead.
    """
    executor = ProcessExecutor()
    try:
        return await executor.run(argv, operation=operation, timeout=timeout)
    finally:
        await executor.close()

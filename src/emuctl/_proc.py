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

_TERMINATE_GRACE_S = 2.0


@dataclass(frozen=True, slots=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    returncode: int


class _ProcessHandle(Protocol):
    @property
    def pid(self) -> int | None: ...
    @property
    def returncode(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...
    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]: ...


_SpawnFn = Callable[[Sequence[str]], Awaitable[_ProcessHandle]]


def _creationflags() -> int:
    if sys.platform == "win32":
        flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return int(flag)
    return 0


async def _default_spawn(argv: Sequence[str]) -> _ProcessHandle:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=PIPE,
        stderr=PIPE,
        stdin=DEVNULL,
        creationflags=_creationflags(),
    )


async def _default_spawn_detached(argv: Sequence[str]) -> _ProcessHandle:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=DEVNULL,
        stderr=DEVNULL,
        stdin=DEVNULL,
        creationflags=_creationflags(),
    )


def _validate_argv(argv: Sequence[str]) -> list[str]:
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
    def __init__(self, *, _spawn: _SpawnFn | None = None) -> None:
        self._spawn_fn: _SpawnFn = _spawn if _spawn is not None else _default_spawn
        self._spawn_detached_fn: _SpawnFn = self._spawn_fn if _spawn is not None else _default_spawn_detached
        self._live: set[_ProcessHandle] = set()

    def _track(self, proc: _ProcessHandle) -> None:
        self._live.add(proc)

    def _untrack(self, proc: _ProcessHandle) -> None:
        self._live.discard(proc)

    async def _abandon(self, proc: _ProcessHandle) -> None:
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
                raise OperationTimeoutError(operation=operation, deadline=deadline, elapsed=elapsed) from None
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

    async def spawn_detached(self, argv: Sequence[str], *, operation: str = "spawn_detached") -> _ProcessHandle:
        args = _validate_argv(argv)
        logger.debug(
            "proc spawn-detached op=%s argv=%s",
            sanitize(operation),
            [sanitize(a) for a in args],
        )
        return await self._spawn_detached_fn(args)

    async def close(self) -> None:
        for proc in list(self._live):
            await self._abandon(proc)
            self._untrack(proc)


async def run_process(argv: Sequence[str], *, operation: str, timeout: TimeoutParam = DEFAULT) -> ProcessResult:
    executor = ProcessExecutor()
    try:
        return await executor.run(argv, operation=operation, timeout=timeout)
    finally:
        await executor.close()

"""Shared fake-transport doubles for the CLI conformance suite.

The CLI consumes the SAME public surface every other consumer does
(``connect()`` / ``discover()``), so its tests replay subprocess transcripts
through the port's private injection seam exactly like ``test_adb.py`` does: a
scripted ``_spawn`` bound through ``emuctl._proc._default_spawn`` BEFORE
``connect()`` constructs the adapter's executor (§6: the seam is
transport-constructor-level, never a plugin registry). No real vendor binary is
ever executed.

Not a pytest module (no ``test_`` prefix): shared helpers, collected by import.

Offline-tier interpretation ledger (§7 close-out): §7's "replayed subprocess
transcripts ... backed by recorded real-hardware byte fixtures" is realized
at v0 as the tier THIS module implements — synthesized in-process fixtures
(``ADB_DEVICES``/``MEMU_VMS``, the minimal-PNG bytes above) + scripted
``_spawn`` doubles bound through ``emuctl._proc._default_spawn`` (§6's
recorded-fixture seam). No real-hardware capture exists in the repo and no
live tier run is required for CI; recording genuine device transcripts is
deferred out of v0 until a real-attached run is available — this file is
where captured fixtures would land, byte-format unchanged.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Sequence
from typing import Any

#: Minimal bytes `emuctl.png.png_dimensions` accepts (signature + IHDR; §4 S2).
PNG: bytes = (
    bytes((137, 80, 78, 71, 13, 10, 26, 10))
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">II", 800, 600)
    + b"\x08\x02\x00\x00\x00"
)

#: An `adb devices` server response with one running and one offline device.
ADB_DEVICES: bytes = (
    b"List of devices attached\n"
    b"emulator-5554\tdevice\n"
    b"127.0.0.1:5555\tdevice product:vsys model:phone\n"
    b"ABCDEF123456\toffline\n"
)

#: A `memuc listvms` response: index,title,top_level,running,pid.
MEMU_VMS: bytes = b"0,pyclashbot-136,MEmu,1,4242\n"


class FakeHandle:
    """Minimal child-process double for the ``_spawn`` seam (mirrors the slice
    of ``asyncio.subprocess.Process`` the executor touches)."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int:
        return 4242

    @property
    def returncode(self) -> int:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return (self._stdout, self._stderr)


class HangingHandle(FakeHandle):
    """A child that never finishes (drives the §2 timeout paths)."""

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        import asyncio

        await asyncio.sleep(3600)
        return (b"", b"")  # pragma: no cover - never reached


def ok(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> FakeHandle:
    """The boring child: rc 0 (or the given one) with the given output."""
    return FakeHandle(stdout=stdout, stderr=stderr, returncode=returncode)


def failing(
    stdout: bytes = b"", stderr: bytes = b"adb: device offline", returncode: int = 1
) -> FakeHandle:
    """A child that exits nonzero — the §2 rc gate maps it to VendorCommandError."""
    return FakeHandle(stdout=stdout, stderr=stderr, returncode=returncode)


class Recorder:
    """A stdout double capturing the two channels §5's output contract pins:
    text documents/lines, and RAW bytes (only ``screenshot --stdout``).

    ``broken=True`` turns every write into ``BrokenPipeError`` — the pipe
    discipline a real closed sink produces, without closing anything real.
    """

    def __init__(self, *, broken: bool = False) -> None:
        self.text = ""
        self.raw = b""
        self.broken = broken

    @property
    def buffer(self) -> Recorder:
        return self

    def write(self, data: bytes | str) -> int:
        if self.broken:
            raise BrokenPipeError(32, "Broken pipe")
        if isinstance(data, bytes):
            self.raw += data
        else:
            self.text += data
        return len(data)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError(32, "Broken pipe")

    def close(self) -> None:  # pragma: no cover - context-manager hygiene
        return None


def spawn_from(
    calls: list[list[str]],
    respond: Callable[[list[str]], FakeHandle | BaseException],
) -> Callable[[Sequence[str]], Any]:
    """Record every argv, then answer through ``respond`` (an exception raises)."""

    async def _spawn(argv: Sequence[str]) -> FakeHandle:
        calls.append(list(argv))
        outcome = respond(list(argv))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _spawn


def install_transport(monkeypatch: Any, spawn: Callable[[Sequence[str]], Any]) -> None:
    """Bind the scripted spawner through the port's private seam.

    Patched BEFORE ``connect()`` runs, so the adapter's constructor picks it up
    (``ProcessExecutor`` reads ``_default_spawn`` at construction time).
    """
    monkeypatch.setattr("emuctl._proc._default_spawn", spawn)


def adb_transport(
    calls: list[list[str]],
    *,
    devices: bytes = ADB_DEVICES,
    screencap: bytes = PNG,
    installed: bytes | None = b"package:com.example.game\n",
    respond_extra: Callable[[list[str]], FakeHandle | BaseException] | None = None,
) -> Callable[[Sequence[str]], Any]:
    """A plain-ADB transport script (server verbs, echo probe, interaction)."""

    def respond(argv: list[str]) -> FakeHandle | BaseException:
        if respond_extra is not None:
            outcome = respond_extra(argv)
            if outcome is not None:
                return outcome
        if argv[-1] == "devices":
            return ok(stdout=devices)
        if "screencap" in argv:
            return ok(stdout=screencap)
        if "echo" in argv:
            return ok(stdout=b"emuctl\n")
        if "pidof" in argv:
            return ok(stdout=b"1234\n")
        if "packages" in argv:
            return ok(stdout=installed if installed is not None else b"")
        return ok()

    return spawn_from(calls, respond)


def memu_transport(
    calls: list[list[str]],
    *,
    vms: bytes = MEMU_VMS,
    screencap: bytes = PNG,
    installed: bytes | None = b"package:com.example.game\n",
    respond_extra: Callable[[list[str]], FakeHandle | BaseException] | None = None,
) -> Callable[[Sequence[str]], Any]:
    """An MEmu transport script (memuc lifecycle/config + derived ADB verbs)."""

    def respond(argv: list[str]) -> FakeHandle | BaseException:
        if respond_extra is not None:
            outcome = respond_extra(argv)
            if outcome is not None:
                return outcome
        if "listvms" in argv:
            return ok(stdout=vms)
        if "screencap" in argv:
            return ok(stdout=screencap)
        if "echo" in argv:
            return ok(stdout=b"emuctl\n")
        if "pidof" in argv:
            return ok(stdout=b"1234\n")
        if "packages" in argv:
            return ok(stdout=installed if installed is not None else b"")
        return ok()

    return spawn_from(calls, respond)

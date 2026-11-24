"""N independent controllers in one process: per-instance adb isolation.

Two ``EmulatorController`` facades are bound to distinct serials, each with its
own injected ``ProcessExecutor`` standing in for the adb binary. Every argv a
shim observes must carry only its own serial/port/arguments, with no call ever
leaking into the sibling's executor. One facade can be closed while the other
keeps working, double-close is a no-op, and no portal threads survive teardown.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, cast

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._proc import ProcessExecutor
from emuctl._sync import PORTAL_THREAD_NAME, EmulatorController
from emuctl.config.model import AdbConfig, DisplayConfig, EmuctlConfig
from emuctl.errors import ControllerClosedError
from emuctl.port import DisplayState

if TYPE_CHECKING:
    from emuctl.backends.adb import AdbController

SERIAL_A = "emulator-5554"
SERIAL_B = "emulator-5556"
PORT_A = 5037
PORT_B = 5038

# Observed device values reported by each fake adb; requested display configs
# are deliberately different so a cross-wired configure is visible in argv.
PHYSICAL_A = (1080, 2400, 480)
PHYSICAL_B = (720, 1600, 360)
DISPLAY_A = DisplayConfig(width=1080, height=1920, dpi=420)
DISPLAY_B = DisplayConfig(width=720, height=1280, dpi=300)


def _png(width: int = 800, height: int = 600) -> bytes:
    return (
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


class _FakeHandle:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._returncode = returncode

    @property
    def pid(self) -> int | None:
        return 4242

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return (self._stdout, b"")


class _InstanceShim:
    """A per-instance adb stand-in that records argv and answers wm/screencap."""

    def __init__(self, serial: str, port: int, physical: tuple[int, int, int], png: bytes) -> None:
        self.serial = serial
        self.port = port
        self.physical = physical
        self.png = png
        self.calls: list[list[str]] = []

    def _tail(self, argv: list[str]) -> list[str]:
        # Drop the prefix through "-s <serial>" so responses depend only on the
        # device verb, never on which instance asked.
        index = argv.index(self.serial)
        return argv[index + 1 :]

    def _respond(self, argv: list[str]) -> _FakeHandle:
        tail = self._tail(argv)
        if tail == ["exec-out", "screencap", "-p"]:
            return _FakeHandle(stdout=self.png)
        if tail == ["shell", "wm", "size"]:
            width, height, _ = self.physical
            return _FakeHandle(stdout=f"Physical size: {width}x{height}\n".encode())
        if tail == ["shell", "wm", "density"]:
            _, _, dpi = self.physical
            return _FakeHandle(stdout=f"Physical density: {dpi}\n".encode())
        return _FakeHandle()

    async def spawn(self, argv: Sequence[str]) -> _FakeHandle:
        recorded = list(argv)
        self.calls.append(recorded)
        return self._respond(recorded)

    @property
    def tails(self) -> list[list[str]]:
        return [self._tail(argv) for argv in self.calls]

    def joined_calls(self) -> str:
        return "\n".join(" ".join(argv) for argv in self.calls)


def _make(ref_serial: str, port: int, display: DisplayConfig, shim: _InstanceShim) -> EmulatorController:
    ref = InstanceRef(backend=BackendId.ADB, key=ref_serial)
    config = EmuctlConfig(adb=AdbConfig(port=port), display=display)
    from emuctl.backends.adb import AdbController

    executor = ProcessExecutor(_spawn=shim.spawn)  # type: ignore[arg-type]
    return EmulatorController(AdbController(ref, config=config, _executor=executor))


def _thread_names() -> set[str]:
    return {thread.name for thread in threading.enumerate()}


def _wait_for_no_controller_threads(before: set[str], deadline_s: float = 5.0) -> None:
    deadline = time.monotonic() + deadline_s
    while True:
        ours = {name for name in _thread_names() - before if PORTAL_THREAD_NAME in name or "AnyIO" in name}
        if not ours:
            return
        assert time.monotonic() < deadline, f"controller threads survived close(): {sorted(ours)}"
        time.sleep(0.05)


def _assert_own_identity_only(shim: _InstanceShim) -> None:
    assert shim.calls, "shim observed no adb calls"
    for argv in shim.calls:
        assert "-s" in argv
        assert argv[argv.index("-s") + 1] == shim.serial
        assert "-P" in argv
        assert argv[argv.index("-P") + 1] == str(shim.port)


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n.startswith("emuctl.backends")]:
        if name not in before:
            evicted = sys.modules.pop(name)
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, child, None) is evicted:
                delattr(parent, child)


def test_interleaved_operations_stay_on_their_own_instance() -> None:
    shim_a = _InstanceShim(SERIAL_A, PORT_A, PHYSICAL_A, _png(1080, 1920))
    shim_b = _InstanceShim(SERIAL_B, PORT_B, PHYSICAL_B, _png(720, 1280))
    a = _make(SERIAL_A, PORT_A, DISPLAY_A, shim_a)
    b = _make(SERIAL_B, PORT_B, DISPLAY_B, shim_b)
    try:
        ctl_a = cast("AdbController", a.aio)
        ctl_b = cast("AdbController", b.aio)
        assert ctl_a is not ctl_b
        assert ctl_a._executor is not ctl_b._executor

        # Interleave across both facades; each must only ever drive its shim.
        assert a.screenshot() == shim_a.png
        assert b.screenshot() == shim_b.png
        a.click(10, 20)
        b.click(30, 40)
        previous_a = a.configure()
        previous_b = b.configure()
        assert a.screenshot() == shim_a.png
        assert b.screenshot() == shim_b.png

        assert previous_a == DisplayState(*PHYSICAL_A)
        assert previous_b == DisplayState(*PHYSICAL_B)

        # Requested display values reach only their own device, as set commands.
        assert ["shell", "wm", "size", "1080x1920"] in shim_a.tails
        assert ["shell", "wm", "density", "420"] in shim_a.tails
        assert ["shell", "wm", "size", "720x1280"] in shim_b.tails
        assert ["shell", "wm", "density", "300"] in shim_b.tails

        assert ["shell", "input", "tap", "10", "20"] in shim_a.tails
        assert ["shell", "input", "tap", "30", "40"] in shim_b.tails

        _assert_own_identity_only(shim_a)
        _assert_own_identity_only(shim_b)

        calls_a = shim_a.joined_calls()
        calls_b = shim_b.joined_calls()
        assert SERIAL_B not in calls_a and str(PORT_B) not in calls_a
        assert SERIAL_A not in calls_b and str(PORT_A) not in calls_b
        assert "720x1280" not in calls_a and "420" in calls_a
        assert "1080x1920" not in calls_b and "300" in calls_b
    finally:
        a.close()
        b.close()


def test_close_one_leaves_the_other_working_and_double_close_is_safe() -> None:
    before = _thread_names()
    shim_a = _InstanceShim(SERIAL_A, PORT_A, PHYSICAL_A, _png(1080, 1920))
    shim_b = _InstanceShim(SERIAL_B, PORT_B, PHYSICAL_B, _png(720, 1280))
    a = _make(SERIAL_A, PORT_A, DISPLAY_A, shim_a)
    b = _make(SERIAL_B, PORT_B, DISPLAY_B, shim_b)
    try:
        assert a.screenshot() == shim_a.png
        assert b.screenshot() == shim_b.png

        a.close()
        a.close()  # idempotent

        with pytest.raises(ControllerClosedError):
            a.screenshot()

        # The sibling facade keeps its own portal and shim untouched.
        assert b.screenshot() == shim_b.png
        b.click(5, 6)
        assert b.configure() == DisplayState(*PHYSICAL_B)
        assert ["shell", "input", "tap", "5", "6"] in shim_b.tails

        calls_b = shim_b.joined_calls()
        assert SERIAL_A not in calls_b and str(PORT_A) not in calls_b
        _assert_own_identity_only(shim_b)

        b.close()
        b.close()  # idempotent
    finally:
        a.close()
        b.close()

    _wait_for_no_controller_threads(before)


def test_per_instance_executor_and_server_port_are_not_shared() -> None:
    shim_a = _InstanceShim(SERIAL_A, PORT_A, PHYSICAL_A, _png())
    shim_b = _InstanceShim(SERIAL_B, PORT_B, PHYSICAL_B, _png())
    a = _make(SERIAL_A, PORT_A, DISPLAY_A, shim_a)
    b = _make(SERIAL_B, PORT_B, DISPLAY_B, shim_b)
    try:
        ctl_a = cast("AdbController", a.aio)
        ctl_b = cast("AdbController", b.aio)
        assert ctl_a.adb_server_port == PORT_A
        assert ctl_b.adb_server_port == PORT_B
        assert ctl_a._executor._live is not ctl_b._executor._live
        assert ctl_a._serial == SERIAL_A
        assert ctl_b._serial == SERIAL_B

        a.screenshot()
        b.screenshot()
        for argv in shim_a.calls:
            assert argv[argv.index("-P") + 1] == str(PORT_A)
        for argv in shim_b.calls:
            assert argv[argv.index("-P") + 1] == str(PORT_B)
    finally:
        a.close()
        b.close()

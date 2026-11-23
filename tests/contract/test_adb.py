import asyncio
import functools
import os
import struct
import sys
import time
import types
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl.config.model import AdbConfig, CreateSpec, DisplayConfig, EmuctlConfig
from emuctl.errors import (
    DeviceStateError,
    ExecutableNotFoundError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    InvalidSerialError,
    OperationTimeoutError,
    VendorCommandError,
)
from emuctl.port import CapabilityFlag, DisplayState

SERIAL = "emulator-5554"
PREFIX = ["adb", "-P", "5037"]
SCOPED = [*PREFIX, "-s", SERIAL]


def _sync(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


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


@pytest.fixture
def adb_module() -> Any:
    import importlib

    return importlib.import_module("emuctl.backends.adb")


def _png(width: int = 800, height: int = 600) -> bytes:
    return (
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


class _FakeHandle:
    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._hang = hang
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int | None:
        return 4242

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return (self._stdout, self._stderr)


def _spawn_factory(
    calls: list[list[str]],
    respond: Callable[[list[str]], _FakeHandle | BaseException],
) -> Callable[[Sequence[str]], Awaitable[_FakeHandle]]:
    async def _spawn(argv: Sequence[str]) -> _FakeHandle:
        calls.append(list(argv))
        outcome = respond(list(argv)) if callable(respond) else respond
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _spawn


def _ok(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(stdout=stdout, stderr=stderr, returncode=returncode)

    return respond


def _make(adb_module: Any, calls: list[list[str]], respond: Any, **kwargs: Any) -> Any:
    ref = InstanceRef(backend=BackendId.ADB, key=kwargs.pop("key", SERIAL))
    config = kwargs.pop("config", EmuctlConfig())
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    return adb_module.AdbController(ref, config=config, _executor=executor)


# ── construction: cheap, binds config, never probes ──


def test_construction_binds_transport_and_probes_nothing(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    assert ctl.adb_path is None
    assert ctl.adb_server_port == 5037
    assert ctl.identity == InstanceRef(backend=BackendId.ADB, key=SERIAL)
    assert calls == []


@_sync
async def test_click_argv(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.click(100, 200)
    assert calls == [[*SCOPED, "shell", "input", "tap", "100", "200"]]


@_sync
async def test_cancelled_click_propagates_without_pointer_release(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, lambda argv: _FakeHandle(hang=True))
    task = asyncio.ensure_future(ctl.click(1, 2))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all("motionevent" not in argv for argv in calls)


@_sync
async def test_click_timeout_releases_a_stuck_pointer(adb_module: Any) -> None:
    calls: list[list[str]] = []
    hangs = {"tap": 0}

    def respond(argv: list[str]) -> _FakeHandle:
        if "tap" in argv and hangs["tap"] == 0:
            hangs["tap"] += 1
            return _FakeHandle(hang=True)
        return _FakeHandle()

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(OperationTimeoutError):
        await ctl.click(1, 2, timeout=0.05)
    assert calls[-1][-6:] == ["shell", "input", "motionevent", "UP", "0", "0"]


@_sync
async def test_successful_click_never_sends_pointer_release(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.click(1, 2)
    assert all("motionevent" not in argv for argv in calls)


@_sync
async def test_swipe_timeout_releases_a_stuck_pointer(adb_module: Any) -> None:
    calls: list[list[str]] = []
    hangs = {"swipe": 0}

    def respond(argv: list[str]) -> _FakeHandle:
        if "swipe" in argv and hangs["swipe"] == 0:
            hangs["swipe"] += 1
            return _FakeHandle(hang=True)
        return _FakeHandle()

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(OperationTimeoutError):
        await ctl.swipe(1, 2, 3, 4, duration_ms=100, timeout=0.05)
    assert calls[-1][-6:] == ["shell", "input", "motionevent", "UP", "0", "0"]


@pytest.mark.parametrize("body", [b"not-a-png", b"", _png()[:10]])
@_sync
async def test_screenshot_corruption_maps_unusable_output(adb_module: Any, body: bytes) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=body))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.screenshot()
    assert excinfo.value.output_reason == "unusable_output"
    assert len(calls) == 1  # one attempt: no repair, no retry


def _display_responder(
    *,
    size_out: bytes = b"Physical size: 1080x2400\n",
    density_out: bytes = b"Physical density: 420\n",
    write_hang: bool = False,
) -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        if argv[-3:] == ["shell", "wm", "size"]:
            return _FakeHandle(stdout=size_out)
        if argv[-3:] == ["shell", "wm", "density"]:
            return _FakeHandle(stdout=density_out)
        if "wm" in argv and write_hang:
            return _FakeHandle(hang=True)
        return _FakeHandle()

    return respond


@_sync
async def test_configure_display_exact_argv_and_previous_state(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _display_responder())
    previous = await ctl.configure({"display.width": 419, "display.height": 633, "display.dpi": 160})
    assert previous == DisplayState(1080, 2400, 420)
    assert previous.to_spec() == {
        "display.width": 1080,
        "display.height": 2400,
        "display.dpi": 420,
    }
    assert calls == [
        [*SCOPED, "shell", "wm", "size"],
        [*SCOPED, "shell", "wm", "density"],
        [*SCOPED, "shell", "wm", "size", "419x633"],
        [*SCOPED, "shell", "wm", "density", "160"],
    ]


@_sync
async def test_configure_none_reapplies_bound_display(adb_module: Any) -> None:
    calls: list[list[str]] = []
    cfg = EmuctlConfig(display=DisplayConfig(width=419, height=633, dpi=160))
    ctl = _make(adb_module, calls, _display_responder(), config=cfg)
    previous = await ctl.configure()
    assert previous == DisplayState(1080, 2400, 420)
    assert calls[-2:] == [
        [*SCOPED, "shell", "wm", "size", "419x633"],
        [*SCOPED, "shell", "wm", "density", "160"],
    ]


@_sync
async def test_configure_is_idempotent_and_skips_equal_writes(adb_module: Any) -> None:
    calls: list[list[str]] = []
    responder = _display_responder(
        size_out=b"Physical size: 1080x2400\nOverride size: 419x633\n",
        density_out=b"Physical density: 420\nOverride density: 160\n",
    )
    ctl = _make(adb_module, calls, responder)
    previous = await ctl.configure({"display.width": 419, "display.height": 633, "display.dpi": 160})
    assert previous == DisplayState(419, 633, 160)
    assert calls == [
        [*SCOPED, "shell", "wm", "size"],
        [*SCOPED, "shell", "wm", "density"],
    ]  # reads only: no vendor churn for an identical request


@pytest.mark.parametrize(
    "spec",
    [
        {"display.width": 419},
        {"display.height": 633},
        {"display.width": 0, "display.height": 633},
        {"display.width": 419, "display.height": 633, "display.dpi": 0},
        {"display.width": True, "display.height": 633},
        {"bogus.leaf": 1},
    ],
)
@_sync
async def test_configure_invalid_spec_is_typed_before_io(adb_module: Any, spec: dict[str, object]) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    with pytest.raises(InvalidArgumentError):
        await ctl.configure(spec)
    assert calls == []


@_sync
async def test_configure_timeout_is_typed_and_shared(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _display_responder(write_hang=True))
    with pytest.raises(OperationTimeoutError) as excinfo:
        await ctl.configure(
            {"display.width": 419, "display.height": 633, "display.dpi": 160},
            timeout=0.05,
        )
    assert excinfo.value.operation == "configure"
    # reads finished; the first write consumed the shared budget
    assert calls[-1] == [*SCOPED, "shell", "wm", "size", "419x633"]


@_sync
async def test_configure_unusable_wm_output_fails_closed(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _display_responder(size_out=b"garbage\n"))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.configure({"display.width": 419, "display.height": 633})
    assert excinfo.value.output_reason == "unusable_output"


@_sync
async def test_configure_rejects_non_mapping(adb_module: Any) -> None:
    ctl = _make(adb_module, [], _ok())
    with pytest.raises(InvalidArgumentError):
        await ctl.configure("display.width=419")  # type: ignore[arg-type]


@pytest.mark.skipif(os.name == "nt", reason="the fake adb shim is a POSIX executable")
def test_consumer_configure_path_needs_no_raw_shell_out(tmp_path: Any) -> None:
    from emuctl import connect_sync

    log = tmp_path / "adb.log"
    shim = tmp_path / "fake-adb"
    shim.write_text(
        f"""#!{sys.executable}
import sys

LOG = {str(log)!r}
argv = sys.argv[1:]
index = 0
while index < len(argv) and argv[index] in ("-H", "-P", "-s"):
    index += 2
args = argv[index:]
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
if args == ["shell", "wm", "size"]:
    print("Physical size: 1080x2400")
elif args == ["shell", "wm", "density"]:
    print("Physical density: 420")
elif args[:3] == ["shell", "wm", "size"] or args[:3] == ["shell", "wm", "density"]:
    pass
else:
    sys.stderr.write("unexpected adb args: " + " ".join(args) + "\\n")
    raise SystemExit(3)
""",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    config = EmuctlConfig(
        adb=AdbConfig(path=str(shim)),
        display=DisplayConfig(width=419, height=633, dpi=160),
    )
    facade = connect_sync("adb:127.0.0.1:5555", config=config)
    try:
        previous = facade.configure()  # exactly what the consumer adapter calls
    finally:
        facade.close()
    assert previous == DisplayState(1080, 2400, 420)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "shell wm size",
        "shell wm density",
        "shell wm size 419x633",
        "shell wm density 160",
    ]


@_sync
async def test_transport_degradation_raises_device_state_error(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stderr=b"adb: device offline", returncode=1))
    with pytest.raises(DeviceStateError) as excinfo:
        await ctl.screenshot()
    assert excinfo.value.state == "offline"
    assert excinfo.value.serial == SERIAL
    assert excinfo.value.error_code == "device_state"


@pytest.mark.parametrize(
    ("stderr", "state"),
    [
        (b"error: device unauthorized. check prompt", "unauthorized"),
        (b"adb: device 'emulator-5554' not found", "disconnected"),
        (b"adb: no devices/emulators found", "disconnected"),
        (b"adb: connection reset by peer", "disconnected"),
    ],
)
@_sync
async def test_transport_state_token_map(adb_module: Any, stderr: bytes, state: str) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stderr=stderr, returncode=1))
    with pytest.raises(DeviceStateError) as excinfo:
        await ctl.click(1, 2)
    assert excinfo.value.state == state


@_sync
async def test_non_transport_vendor_failure_stays_vendor_error(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(
        adb_module,
        calls,
        _ok(stderr=b"Failure [INSTALL_FAILED_ALREADY_EXISTS]", returncode=1),
    )
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.install_apk("dist/app.apk")
    assert excinfo.value.returncode == 1


@_sync
async def test_transport_loss_is_repaired_on_the_next_op(adb_module: Any) -> None:
    calls: list[list[str]] = []
    state = {"first": True}

    def respond(argv: list[str]) -> _FakeHandle:
        if "screencap" in argv:
            if state["first"]:
                state["first"] = False
                return _FakeHandle(stderr=b"adb: device 'emulator-5554' not found", returncode=1)
            return _FakeHandle(stdout=_png())
        if "connect" in argv:
            return _FakeHandle(stdout=b"connected to emulator-5554")
        return _FakeHandle()

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(DeviceStateError) as excinfo:
        await ctl.screenshot()
    assert excinfo.value.state == "disconnected"
    assert calls == [[*SCOPED, "exec-out", "screencap", "-p"]]  # no retry

    frame = await ctl.screenshot()
    assert frame.startswith(b"\x89PNG")
    assert calls[1] == [*PREFIX, "connect", SERIAL]  # server verb, never -s scoped
    assert calls[2] == [*SCOPED, "exec-out", "screencap", "-p"]


@_sync
async def test_reattach_uses_the_op_remaining_budget(adb_module: Any) -> None:
    calls: list[list[str]] = []
    state = {"first": True}

    def respond(argv: list[str]) -> _FakeHandle:
        if "screencap" in argv:
            if state["first"]:
                state["first"] = False
                return _FakeHandle(stderr=b"adb: device 'emulator-5554' not found", returncode=1)
            return _FakeHandle(stdout=_png())
        return _FakeHandle(stdout=b"connected")

    spy = _TimeoutSpy(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    ref = InstanceRef(backend=BackendId.ADB, key=SERIAL)
    ctl = adb_module.AdbController(ref, config=EmuctlConfig(), _executor=spy)
    with pytest.raises(DeviceStateError):
        await ctl.screenshot()
    await ctl.screenshot(timeout=0.05)
    assert spy.seen["reattach"] == pytest.approx(0.05)
    assert (spy.seen["reattach"] or 0) < 2.0


@_sync
async def test_install_apk_argv(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.install_apk("dist/app.apk")
    assert calls == [[*SCOPED, "install", "dist/app.apk"]]


@_sync
async def test_start_app_launch_plus_pidof_verify(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(
        adb_module,
        calls,
        _ok(stdout=b"1234\n"),  # pidof stdout: consumed, never read
    )
    await ctl.start_app("com.example.game")
    assert calls == [
        [*SCOPED, "shell", "am", "force-stop", "com.example.game"],
        [
            *SCOPED,
            "shell",
            "monkey",
            "-p",
            "com.example.game",
            "-c",
            "android.intent.category.LAUNCHER",
            "--pct-syskeys",
            "0",
            "1",
        ],
        [*SCOPED, "shell", "pidof", "com.example.game"],
    ]


@_sync
async def test_start_app_primary_success_never_tries_the_fallback(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"1234\n"))
    await ctl.start_app("com.example.game")
    monkeys = [argv for argv in calls if "monkey" in argv]
    assert len(monkeys) == 1
    assert "--pct-syskeys" in monkeys[0]
    assert all("am" not in argv or "force-stop" in argv for argv in calls)


@_sync
async def test_start_app_syskeys_failure_falls_back_to_plain_monkey(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        if "force-stop" in argv:
            return _FakeHandle()
        if "monkey" in argv and "--pct-syskeys" in argv:
            return _FakeHandle(stderr=b"monkey aborted", returncode=251)
        if "monkey" in argv:
            return _FakeHandle(stdout=b"Events injected: 1")
        return _FakeHandle(stdout=b"1234\n")  # pidof: spawned

    ctl = _make(adb_module, calls, respond)
    await ctl.start_app("com.example.game")

    assert calls[0][-3:] == ["am", "force-stop", "com.example.game"]
    assert calls[1] == [
        *SCOPED,
        "shell",
        "monkey",
        "-p",
        "com.example.game",
        "-c",
        "android.intent.category.LAUNCHER",
        "--pct-syskeys",
        "0",
        "1",
    ]
    assert calls[2] == [
        *SCOPED,
        "shell",
        "monkey",
        "-p",
        "com.example.game",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    ]
    assert calls[3] == [*SCOPED, "shell", "pidof", "com.example.game"]


@_sync
async def test_start_app_both_launch_mechanisms_fail_carries_both_rcs(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    seen = {"monkey": 0}

    def respond(argv: list[str]) -> _FakeHandle:
        if "force-stop" in argv:
            return _FakeHandle()
        if "monkey" in argv:
            seen["monkey"] += 1
            rc = 251 if seen["monkey"] == 1 else 97
            return _FakeHandle(stderr=b"monkey aborted", returncode=rc)
        return _FakeHandle(stderr=b"unexpected", returncode=1)

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.start_app("com.example.game")

    error = excinfo.value
    assert error.output_reason == "exit"
    assert error.returncode == 97  # the fallback's payload, not the first rc
    assert "monkey" in error.command
    assert error.stderr_excerpt == "monkey aborted"
    assert "251" in (error.detail or "")  # original launch context preserved
    assert "97" in (error.detail or "")
    assert sum(1 for argv in calls if "monkey" in argv) == 2
    assert all("pidof" not in argv for argv in calls)  # no verify after failure


@_sync
async def test_start_app_fallback_spends_the_one_shared_deadline(
    adb_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        if "force-stop" in argv:
            return _FakeHandle()
        if "monkey" in argv and "--pct-syskeys" in argv:
            return _FakeHandle(stderr=b"monkey aborted", returncode=251)
        if "monkey" in argv:
            return _FakeHandle(stdout=b"Events injected: 1")
        return _FakeHandle(stdout=b"1234\n")

    class _SteppingClock:
        def __init__(self) -> None:
            self.readings: list[float] = []
            self._now = 100.0

        def monotonic(self) -> float:
            self.readings.append(self._now)
            self._now += 0.5
            return self.readings[-1]

    clock = _SteppingClock()
    # Only the port's view is faked: waits.py/_proc.py keep the real clock,
    # so asyncio scheduling and executor deadlines stay untouched.
    monkeypatch.setattr("emuctl._adb.time", types.SimpleNamespace(monotonic=clock.monotonic))

    spy = _TimeoutSpy(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    ref = InstanceRef(backend=BackendId.ADB, key=SERIAL)
    ctl = adb_module.AdbController(ref, config=EmuctlConfig(), _executor=spy)
    await ctl.start_app("com.example.game", timeout=5.0)

    probes = spy.seen_all["start_app"]
    assert len(probes) == 4  # force-stop + primary monkey + fallback monkey + pidof
    assert probes[0] == 5.0  # force-stop carries the resolved op budget
    assert probes[1] == 4.5  # primary: 5.0 deadline - 0.5 s elapsed
    assert probes[2] == 4.0  # fallback: 5.0 deadline - 1.0 s elapsed
    assert probes[3] == 3.0  # pidof: 5.0 deadline - 2.0 s elapsed
    # and the general invariant the exact numbers instantiate
    assert all(budget is not None and budget <= 5.0 for budget in probes)
    assert all((probes[i] or 0) <= (probes[i - 1] or 0) for i in range(1, len(probes)))


@_sync
async def test_start_app_force_stop_failure_raises_before_launch(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        if "force-stop" in argv:
            return _FakeHandle(stderr=b"Error: package not found", returncode=1)
        return _FakeHandle(stdout=b"events injected")

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.start_app("com.example.game")
    assert excinfo.value.output_reason == "exit"
    assert calls == [[*SCOPED, "shell", "am", "force-stop", "com.example.game"]]
    assert all("monkey" not in argv for argv in calls)
    assert all("pidof" not in argv for argv in calls)


@_sync
async def test_start_app_launch_failure_falls_back_then_raises_before_verify(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        if "force-stop" in argv:
            return _FakeHandle()  # warm-device stop succeeds
        return _FakeHandle(stderr=b"no such package", returncode=1)

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.start_app("com.example.missing")
    assert excinfo.value.output_reason == "exit"
    assert calls[0][-3:] == ["am", "force-stop", "com.example.missing"]
    # force-stop + primary monkey + one plain-monkey fallback: no verify
    assert len(calls) == 3
    assert sum(1 for argv in calls if "monkey" in argv) == 2
    assert all("pidof" not in argv for argv in calls)


@_sync
async def test_start_app_deadline_expiry_is_typed_timeout_never_green_pass(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        if "force-stop" in argv:
            return _FakeHandle()
        if "monkey" in argv:
            return _FakeHandle(stdout=b"events injected")
        return _FakeHandle(stdout=b"", returncode=1)  # pidof: never spawns

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(OperationTimeoutError) as excinfo:
        await ctl.start_app("com.example.ghost", timeout=0.2)
    assert excinfo.value.operation == "start_app"
    verify = [c for c in calls if "pidof" in c]
    assert len(verify) > 1  # polled, not a single launch-and-hope probe


@_sync
async def test_start_app_exit_zero_without_process_fails_fast(
    adb_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("emuctl._adb._LAUNCH_CONFIRM_TIMEOUT_S", 0.1)
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        if "force-stop" in argv:
            return _FakeHandle()
        if "monkey" in argv:
            # A silent no-op like ``am start -p`` on some images: rc 0,
            # process never appears.
            return _FakeHandle(stdout=b"Events injected: 1")
        return _FakeHandle(stdout=b"", returncode=1)  # pidof: never spawns

    ctl = _make(adb_module, calls, respond)
    started = time.monotonic()
    with pytest.raises(OperationTimeoutError) as excinfo:
        await ctl.start_app("com.example.ghost", timeout=30.0)
    elapsed = time.monotonic() - started
    assert excinfo.value.operation == "start_app"
    assert "exited 0" in (excinfo.value.detail or "")
    assert "no process" in (excinfo.value.detail or "")
    assert elapsed < 2.0  # acceptance window, not the 30 s op budget
    assert sum(1 for argv in calls if "pidof" in argv) >= 1


@pytest.mark.parametrize(
    ("pm_out", "installed"),
    [
        (b"package:com.example.game\n", True),
        (b"package:com.example.game\r\n", True),  # adb text transport CRLF
        (b"", False),
        # pm applies its own substring filter: exact-line match decides.
        (b"package:com.example.game2\npackage:com.example.gameX\n", False),
        (b"package:com.example.game\npackage:com.example.game2\n", True),
    ],
)
@_sync
async def test_is_app_installed_exact_line_match(adb_module: Any, pm_out: bytes, installed: bool) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=pm_out))
    assert await ctl.is_app_installed("com.example.game") is installed
    assert calls == [[*SCOPED, "shell", "pm", "list", "packages", "com.example.game"]]


# ── serial scoping ──


@_sync
async def test_loopback_prefix_omits_H_so_a_cold_server_can_start(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    cfg = EmuctlConfig(adb=AdbConfig(port=5042))
    ctl = _make(adb_module, calls, _ok(), config=cfg)
    await ctl.click(1, 2)
    assert calls == [["adb", "-P", "5042", "-s", SERIAL, "shell", "input", "tap", "1", "2"]]


@_sync
async def test_genuinely_remote_host_keeps_H(adb_module: Any) -> None:
    calls: list[list[str]] = []
    cfg = EmuctlConfig(adb=AdbConfig(host="10.0.0.2", port=5037))
    ctl = _make(adb_module, calls, _ok(), config=cfg)
    await ctl.click(1, 2)
    assert calls == [["adb", "-H", "10.0.0.2", "-P", "5037", "-s", SERIAL, "shell", "input", "tap", "1", "2"]]


@_sync
async def test_device_verbs_carry_exactly_one_s_scope(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=_png()))
    await ctl.click(1, 2)
    await ctl.screenshot()
    for argv in calls:
        assert argv.count("-s") == 1
        assert argv[argv.index("-s") + 1] == SERIAL


@_sync
async def test_per_call_serial_recheck(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    ctl._serial = "has space"  # transport degraded after construction
    with pytest.raises(InvalidSerialError):
        await ctl.click(1, 2)
    assert calls == []


@_sync
async def test_is_reachable_ok(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"emuctl\n"))
    probe = await ctl.is_reachable()
    assert probe.ok is True and probe.reason == "ok"
    assert calls == [[*SCOPED, "shell", "echo", "emuctl"]]


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (b"adb: device offline", "offline"),
        (b"error: device unauthorized. check prompt", "unauthorized"),
        (b"adb: device 'emulator-5554' not found", "disconnected"),
        (b"error: no devices/emulators found", "disconnected"),
        (b"weird failure with no token", "disconnected"),
    ],
)
@_sync
async def test_is_reachable_failure_vocabulary(adb_module: Any, stderr: bytes, reason: str) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"", stderr=stderr, returncode=1))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (False, reason)


@_sync
async def test_list_instances_vendor_failure_propagates(adb_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(stderr=b"daemon dead", returncode=1)))  # type: ignore[arg-type]
    with pytest.raises(VendorCommandError):
        await adb_module.list_instances(EmuctlConfig(), _executor=executor)


# ── timeouts: group defaults, validation, cancellation ──


class _TimeoutSpy(ProcessExecutor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: dict[str, float | None] = {}
        self.seen_all: dict[str, list[float | None]] = {}

    async def run(  # type: ignore[override]
        self, argv: Sequence[str], *, operation: str, timeout: Any = None
    ) -> ProcessResult:
        self.seen[operation] = timeout
        self.seen_all.setdefault(operation, []).append(timeout)
        return await super().run(argv, operation=operation, timeout=timeout)


@_sync
async def test_invalid_timeout_rejected_before_io(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    with pytest.raises(InvalidArgumentError):
        await ctl.click(1, 2, timeout=0)
    assert calls == []


@_sync
async def test_cancellation_propagates_unmodified(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, lambda argv: _FakeHandle(hang=True))
    task = asyncio.ensure_future(ctl.click(1, 2))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── wiring: FEATURES parity, gates, close, connect ──


def test_features_declare_plain_adb_mechanics(adb_module: Any) -> None:
    assert (
        frozenset(
            {
                CapabilityFlag.SCREENSHOT,
                CapabilityFlag.INPUT,
                CapabilityFlag.APPS,
                CapabilityFlag.APK_INSTALL,
                CapabilityFlag.CONFIGURE,
            }
        )
        == adb_module.AdbController.FEATURES
    )


def test_list_instances_ignores_cold_start_daemon_banner(adb_module: Any) -> None:
    calls: list[list[str]] = []
    scripted = (
        b"* daemon not running; starting now at tcp:5037\n"
        b"* daemon started successfully\n"
        b"List of devices attached\n"
        b"emulator-5554\tdevice\n"
        b"ABCDEF123456\toffline\n"
    )
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, _ok(stdout=scripted)))  # type: ignore[arg-type]
    found = asyncio.run(adb_module.list_instances(EmuctlConfig(), _executor=executor))
    assert calls == [[*PREFIX, "devices"]]
    assert [str(r.ref) for r in found] == ["adb:emulator-5554", "adb:ABCDEF123456"]
    assert not any(r.serial.startswith("*") for r in found)


@_sync
async def test_is_reachable_after_close_reports_data(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"emuctl\n"))
    await ctl.aclose()
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (True, "ok")


@_sync
async def test_connect_builds_adb_adapter_without_io(adb_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from emuctl._discovery import connect

    # Zero executor spawns during construction: bind the real production
    # spawner with a recorder and prove connect() never reaches it (no
    # discovery, no probe — readiness begins at first call, not at wiring).
    spawn_calls: list[list[str]] = []

    async def _recording_default_spawn(argv: Sequence[str]) -> _FakeHandle:
        spawn_calls.append(list(argv))
        await asyncio.sleep(3600)
        return _FakeHandle()  # pragma: no cover — spies, never spawns

    monkeypatch.setattr("emuctl._proc._default_spawn", _recording_default_spawn)
    ctl = await connect("adb:emulator-5554", config=EmuctlConfig())
    try:
        assert isinstance(ctl, adb_module.AdbController)
        assert str(ctl.identity) == "adb:emulator-5554"
    finally:
        await ctl.aclose()
    assert spawn_calls == []  # construction + close: zero spawns, zero probes


@_sync
async def test_connect_accepts_configured_display_for_plain_adb() -> None:
    from emuctl._discovery import connect

    cfg = EmuctlConfig(display=DisplayConfig(width=800, height=600, dpi=320))
    ctl = await connect("adb:emulator-5554", config=cfg)
    try:
        assert ctl.has_feature(CapabilityFlag.CONFIGURE)
    finally:
        await ctl.aclose()


@_sync
async def test_missing_binary_maps_to_executable_not_found(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, FileNotFoundError("adb"))
    with pytest.raises(ExecutableNotFoundError) as excinfo:
        await ctl.click(1, 2)
    assert excinfo.value.tool == "adb"


@_sync
async def test_restored_boot_create_surface_gates_on_adb(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    for op in (
        lambda: ctl.create(CreateSpec(name="vm")),
        lambda: ctl.start(),
        lambda: ctl.stop(),
        lambda: ctl.restart(),
    ):
        with pytest.raises(FeatureNotSupportedError):
            await op()
    assert calls == []

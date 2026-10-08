"""§7 conformance (ADB, §1/§3/§4): exact argv, serial scoping, escaping,
screenshot validation, app verbs, probes, discovery, timeouts, wiring."""

import asyncio
import functools
import struct
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl.config.model import AdbConfig, DisplayConfig, EmuctlConfig
from emuctl.errors import (
    ExecutableNotFoundError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    InvalidSerialError,
    VendorCommandError,
)
from emuctl.port import CapabilityFlag
from emuctl.timing import (
    APP_TIMEOUT,
    INTERACTION_TIMEOUT,
    QUERY_TIMEOUT,
    SNAPSHOT_TIMEOUT,
)

SERIAL = "emulator-5554"
PREFIX = ["adb", "-H", "127.0.0.1", "-P", "5037"]
SCOPED = [*PREFIX, "-s", SERIAL]


def _sync(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    """Run an async test body to completion on a fresh loop (repo convention).

    The suite is plugin-free: async tests are plain coroutines driven here
    instead of via pytest-asyncio/anyio markers.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    """Leave no `emuctl.backends*` trace (see test_registry/test_discovery)."""
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
    """Import the adapter module lazily (never at collection time)."""
    import importlib

    return importlib.import_module("emuctl.backends.adb")


def _png(width: int = 800, height: int = 600) -> bytes:
    """Minimal bytes png_dimensions accepts (signature + IHDR; no CRC check)."""
    return (
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


class _FakeHandle:
    """Minimal child-process double for the ``_spawn`` seam."""

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


def _ok(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
) -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(stdout=stdout, stderr=stderr, returncode=returncode)

    return respond


def _make(adb_module: Any, calls: list[list[str]], respond: Any, **kwargs: Any) -> Any:
    """Build a controller bound to SERIAL over a scripted transport."""
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


def test_construction_honors_adb_config(adb_module: Any) -> None:
    calls: list[list[str]] = []
    cfg = EmuctlConfig(adb=AdbConfig(path="/x/adb", host="10.0.0.2", port=5042))
    ctl = _make(adb_module, calls, _ok(), config=cfg)
    assert ctl.adb_path == "/x/adb"
    assert ctl.adb_server_port == 5042
    assert calls == []


def test_construction_rejects_bad_config(adb_module: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        _make(adb_module, [], _ok(), config="nope")  # type: ignore[arg-type]


def test_construction_rejects_bad_serial(adb_module: Any) -> None:
    with pytest.raises(InvalidSerialError):
        _make(adb_module, [], _ok(), key="has space")


# ── exact argv shapes per mechanic ──


@_sync
async def test_click_argv(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.click(100, 200)
    assert calls == [[*SCOPED, "shell", "input", "tap", "100", "200"]]


@_sync
async def test_click_count_repeats_with_interval(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.click(1, 2, count=3, interval=0.01)
    assert calls == [[*SCOPED, "shell", "input", "tap", "1", "2"]] * 3


@_sync
async def test_swipe_argv(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.swipe(1, 2, 3, 4, duration_ms=500)
    assert calls == [[*SCOPED, "shell", "input", "swipe", "1", "2", "3", "4", "500"]]


@_sync
async def test_swipe_default_duration(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.swipe(1, 2, 3, 4)
    assert calls[0][-1] == "300"


@_sync
async def test_key_argv_str_and_int(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.key("KEYCODE_ENTER")
    await ctl.key(3)
    assert calls == [
        [*SCOPED, "shell", "input", "keyevent", "KEYCODE_ENTER"],
        [*SCOPED, "shell", "input", "keyevent", "3"],
    ]


@pytest.mark.parametrize(
    ("value", "token"),
    [
        ("hello", "hello"),
        ("a b", "a%sb"),
        ("a  b", "a%s%sb"),
        ("x;y", "x\\;y"),
        ('say "hi"', 'say%s\\"hi\\"'),
        ("it's", "it\\'s"),
        ("a&b|c", "a\\&b\\|c"),
        ("100%s", "100%s"),  # documented: %s types as space (adb behavior)
        ("$HOME", "\\$HOME"),
    ],
)
@_sync
async def test_text_escaping_vectors(adb_module: Any, value: str, token: str) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.text(value)
    assert calls == [[*SCOPED, "shell", "input", "text", token]]


@_sync
async def test_screenshot_argv_and_passthrough(adb_module: Any) -> None:
    calls: list[list[str]] = []
    frame = _png() + b"IDAT-noise-bytes"
    ctl = _make(adb_module, calls, _ok(stdout=frame))
    out = await ctl.screenshot()
    assert out == frame  # byte-identical: never transcoded
    assert calls == [[*SCOPED, "exec-out", "screencap", "-p"]]


@pytest.mark.parametrize("body", [b"not-a-png", b"", _png()[:10], b"\x89PNGjunk"])
@_sync
async def test_screenshot_corruption_maps_unusable_output(adb_module: Any, body: bytes) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=body))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.screenshot()
    assert excinfo.value.output_reason == "unusable_output"
    assert len(calls) == 1  # one attempt: no repair, no retry


@_sync
async def test_screenshot_nonzero_rc_maps_exit(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stderr=b"fail", returncode=1))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.screenshot()
    assert excinfo.value.output_reason == "exit"
    assert excinfo.value.returncode == 1


@_sync
async def test_install_apk_argv(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.install_apk("dist/app.apk")
    assert calls == [[*SCOPED, "install", "dist/app.apk"]]


@_sync
async def test_install_apk_failure_raises(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stderr=b"INSTALL_FAILED", returncode=1))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.install_apk("dist/app.apk")
    assert excinfo.value.output_reason == "exit"


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
        [
            *SCOPED,
            "shell",
            "monkey",
            "-p",
            "com.example.game",
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
        [*SCOPED, "shell", "pidof", "com.example.game"],
    ]


@_sync
async def test_start_app_launch_failure_raises_before_verify(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stderr=b"no such package", returncode=1))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.start_app("com.example.missing")
    assert excinfo.value.output_reason == "exit"
    assert len(calls) == 1  # launch failed: no verify attempt


@_sync
async def test_start_app_negative_verify_raises(adb_module: Any) -> None:
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        if "monkey" in argv:
            return _FakeHandle(stdout=b"events injected")
        return _FakeHandle(stdout=b"", returncode=1)  # pidof: no process

    ctl = _make(adb_module, calls, respond)
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.start_app("com.example.game")
    assert excinfo.value.output_reason == "exit"
    assert excinfo.value.command[-2:] == ("pidof", "com.example.game")


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
async def test_is_app_installed_exact_line_match(
    adb_module: Any, pm_out: bytes, installed: bool
) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=pm_out))
    assert await ctl.is_app_installed("com.example.game") is installed
    assert calls == [[*SCOPED, "shell", "pm", "list", "packages", "com.example.game"]]


# ── serial scoping ──


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
async def test_server_verbs_bypass_scoping(adb_module: Any) -> None:
    """`devices` (pinned exclusion) carries no `-s` — via the runner seam."""
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"List of devices attached\n"))
    await ctl._run_adb(["devices"], binary=False, operation="t", timeout=1.0, server_verb=True)
    assert calls == [[*PREFIX, "devices"]]


@_sync
async def test_server_verb_policing_rejects_non_server_heads(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    with pytest.raises(InvalidArgumentError):
        await ctl._run_adb(["shell"], binary=False, operation="t", timeout=1.0, server_verb=True)
    assert calls == []  # refused before any I/O


@_sync
async def test_per_call_serial_recheck(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    ctl._serial = "has space"  # transport degraded after construction
    with pytest.raises(InvalidSerialError):
        await ctl.click(1, 2)
    assert calls == []


@_sync
async def test_vendor_run_is_device_scoped(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"ok"))
    out = await ctl._vendor_run(["shell", "echo", "hi"])
    assert out == "ok"
    assert calls == [[*SCOPED, "shell", "echo", "hi"]]


# ── is_reachable: data only, stderr-vocabulary, never stdout ──


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
async def test_is_reachable_ok_ignores_misleading_stdout(
    adb_module: Any,
) -> None:
    """rc 0 decides ok even when stdout mentions failure words."""
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"offline unauthorized\n"))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (True, "ok")
    assert calls == [[*SCOPED, "shell", "echo", "emuctl"]]


@_sync
async def test_is_reachable_missing_binary_is_no_adb(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, FileNotFoundError("adb"))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (False, "no-adb")


@_sync
async def test_is_reachable_unexpected_error_is_data(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, RuntimeError("boom"))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (False, "disconnected")


@_sync
async def test_is_reachable_timeout_reason(adb_module: Any) -> None:
    async def _spawn(argv: Sequence[str]) -> _FakeHandle:
        await asyncio.sleep(5)
        return _FakeHandle()

    ref = InstanceRef(backend=BackendId.ADB, key=SERIAL)
    executor = ProcessExecutor(_spawn=_spawn)  # type: ignore[arg-type]
    ctl = adb_module.AdbController(ref, config=EmuctlConfig(), _executor=executor)
    probe = await ctl.is_reachable(timeout=0.05)
    assert (probe.ok, probe.reason) == (False, "timeout")


# ── discovery: adb devices enumeration ──


_DEVICES_FIXTURE = (
    b"List of devices attached\n"
    b"emulator-5554\tdevice\n"
    b"127.0.0.1:5555\tdevice product:vsys_x86_64 model:phone device:emulator\n"
    b"ABCDEF123456\toffline\n"
    b"ZYX987\tunauthorized\n"
    b"\n"
    b"has;semicolon\tdevice\n"  # invalid serial: skipped, never a ref
    b"lonelyline\n"  # unparseable: skipped
)


@_sync
async def test_list_instances_enumeration(adb_module: Any) -> None:
    calls: list[list[str]] = []
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, _ok(stdout=_DEVICES_FIXTURE)))  # type: ignore[arg-type]
    found = await adb_module.list_instances(EmuctlConfig(), _executor=executor)
    assert calls == [[*PREFIX, "devices"]]  # server verb: no -s
    assert [str(r.ref) for r in found] == [
        "adb:emulator-5554",
        "adb:127.0.0.1:5555",
        "adb:ABCDEF123456",
        "adb:ZYX987",
    ]
    assert [r.running for r in found] == [True, True, False, False]
    assert [r.serial for r in found] == [
        "emulator-5554",
        "127.0.0.1:5555",
        "ABCDEF123456",
        "ZYX987",
    ]
    assert [r.details["adb_state"] for r in found] == [
        "device",
        "device",
        "offline",
        "unauthorized",
    ]


@_sync
async def test_list_instances_empty_server(adb_module: Any) -> None:
    executor = ProcessExecutor(
        _spawn=_spawn_factory([], _ok(stdout=b"List of devices attached\n\n"))  # type: ignore[arg-type]
    )
    assert await adb_module.list_instances(EmuctlConfig(), _executor=executor) == ()


@_sync
async def test_list_instances_vendor_failure_propagates(adb_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(stderr=b"daemon dead", returncode=1)))  # type: ignore[arg-type]
    with pytest.raises(VendorCommandError):
        await adb_module.list_instances(EmuctlConfig(), _executor=executor)


@_sync
async def test_get_instance_found_and_absent(adb_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(stdout=_DEVICES_FIXTURE)))  # type: ignore[arg-type]
    cfg = EmuctlConfig()
    hit = await adb_module.get_instance(
        cfg, InstanceRef(backend=BackendId.ADB, key="ABCDEF123456"), _executor=executor
    )
    assert hit is not None and hit.running is False
    assert hit.details["adb_state"] == "offline"
    miss = await adb_module.get_instance(
        cfg, InstanceRef(backend=BackendId.ADB, key="ghost-0000"), _executor=executor
    )
    assert miss is None


@_sync
async def test_get_instance_wrong_backend_is_absent(adb_module: Any) -> None:
    calls: list[list[str]] = []
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, _ok(stdout=_DEVICES_FIXTURE)))  # type: ignore[arg-type]
    out = await adb_module.get_instance(
        EmuctlConfig(),
        InstanceRef(backend=BackendId.MEMU, key="pyclashbot-136"),
        _executor=executor,
    )
    assert out is None
    assert calls == []  # not ours: no probe at all


# ── timeouts: group defaults, validation, cancellation ──


class _TimeoutSpy(ProcessExecutor):
    """Records the resolved budget per operation, then runs the fake."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: dict[str, float | None] = {}

    async def run(  # type: ignore[override]
        self, argv: Sequence[str], *, operation: str, timeout: Any = None
    ) -> ProcessResult:
        self.seen[operation] = timeout
        return await super().run(argv, operation=operation, timeout=timeout)


@_sync
async def test_timeout_group_defaults(adb_module: Any) -> None:
    spy = _TimeoutSpy(_spawn=_spawn_factory([], _ok(stdout=_png())))  # type: ignore[arg-type]
    ref = InstanceRef(backend=BackendId.ADB, key=SERIAL)
    ctl = adb_module.AdbController(ref, config=EmuctlConfig(), _executor=spy)
    await ctl.click(1, 2)
    await ctl.screenshot()
    await ctl.is_app_installed("com.example.game")
    await ctl.install_apk("dist/app.apk")
    assert spy.seen["click"] == INTERACTION_TIMEOUT
    assert spy.seen["screenshot"] == SNAPSHOT_TIMEOUT
    assert spy.seen["is_app_installed"] == QUERY_TIMEOUT
    assert spy.seen["install_apk"] == APP_TIMEOUT


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
            }
        )
        == adb_module.AdbController.FEATURES
    )


@_sync
async def test_plain_adb_has_no_boot_create_configure(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        await ctl.restart()
    assert excinfo.value.feature == "boot"
    with pytest.raises(FeatureNotSupportedError):
        await ctl.start()
    with pytest.raises(FeatureNotSupportedError):
        await ctl.stop()
    with pytest.raises(FeatureNotSupportedError):
        await ctl.configure(None)
    assert calls == []  # gate-first: no transport touched


@_sync
async def test_closed_controller_guards_gated_ops(adb_module: Any) -> None:
    from emuctl.errors import ControllerClosedError

    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    await ctl.aclose()
    await ctl.aclose()  # idempotent
    with pytest.raises(ControllerClosedError):
        await ctl.click(1, 2)
    assert calls == []


@_sync
async def test_is_reachable_after_close_reports_data(adb_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok(stdout=b"emuctl\n"))
    await ctl.aclose()
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (True, "ok")


@_sync
async def test_connect_builds_adb_adapter_without_io(adb_module: Any) -> None:
    from emuctl._discovery import connect

    ctl = await connect("adb:emulator-5554", config=EmuctlConfig())
    try:
        assert isinstance(ctl, adb_module.AdbController)
        assert str(ctl.identity) == "adb:emulator-5554"
    finally:
        await ctl.aclose()


@_sync
async def test_connect_refuses_configured_display_for_plain_adb() -> None:
    from emuctl._discovery import connect

    cfg = EmuctlConfig(display=DisplayConfig(width=800, height=600))
    with pytest.raises(FeatureNotSupportedError):
        await connect("adb:emulator-5554", config=cfg)


@_sync
async def test_missing_binary_maps_to_executable_not_found(
    adb_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, FileNotFoundError("adb"))
    with pytest.raises(ExecutableNotFoundError) as excinfo:
        await ctl.click(1, 2)
    assert excinfo.value.tool == "adb"


@pytest.mark.parametrize("op", ["click", "key", "text", "swipe"])
@_sync
async def test_caller_misuse_is_invalid_argument(adb_module: Any, op: str) -> None:
    calls: list[list[str]] = []
    ctl = _make(adb_module, calls, _ok())
    with pytest.raises(InvalidArgumentError):
        if op == "click":
            await ctl.click(-1, 0)
        elif op == "key":
            await ctl.key("")
        elif op == "text":
            await ctl.text("line\nbreak")
        elif op == "swipe":
            await ctl.swipe(0, 0, 1, 1, duration_ms=-5)
    assert calls == []

"""§7 conformance (Google Play Games, §1/§3/§4): bundled-ADB resolution,
exact ADB argv, discovery rows, capability gates (no BOOT/CREATE/APK_INSTALL),
display configure via the shared ADB middle layer, and close/timeout/error
taxonomy.

All offline: every child replays through ``ProcessExecutor(_spawn=...)`` and
the Windows registry walk is a fake ``winreg`` module planted in
``sys.modules``. No real Google Play Games installation is touched.
"""

from __future__ import annotations

import asyncio
import functools
import struct
import sys
import types
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._proc import ProcessExecutor
from emuctl.config.model import AdbConfig, CreateSpec, DisplayConfig, EmuctlConfig, GooglePlayConfig
from emuctl.errors import (
    ConfigValidationError,
    ControllerClosedError,
    ExecutableNotFoundError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    InvalidSerialError,
    OperationTimeoutError,
    VendorCommandError,
)
from emuctl.port import CapabilityFlag, DisplayState

SERIAL = "localhost:6520"
ADB = "/opt/google-play/current/emulator/adb.exe"
PREFIX = [ADB, "-P", "5037"]
SCOPED = [*PREFIX, "-s", SERIAL]

DEVICES_PRESENT = b"List of devices attached\nlocalhost:6520\tdevice\n"
DEVICES_OFFLINE = b"List of devices attached\nlocalhost:6520\toffline\n"
DEVICES_ABSENT = b"List of devices attached\n"


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
def gp_module() -> Any:
    import importlib

    return importlib.import_module("emuctl.backends.google_play")


def _png(width: int = 419, height: int = 633) -> bytes:
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
    respond: Callable[[list[str]], _FakeHandle],
) -> Callable[[Sequence[str]], Awaitable[_FakeHandle]]:
    async def _spawn(argv: Sequence[str]) -> _FakeHandle:
        calls.append(list(argv))
        return respond(list(argv))

    return _spawn


def _ok(stdout: bytes = b"") -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(stdout=stdout)

    return respond


def _config(**kwargs: Any) -> EmuctlConfig:
    gp = kwargs.pop("google_play", GooglePlayConfig(adb_path=ADB))
    return EmuctlConfig(google_play=gp, **kwargs)


def _make(
    gp_module: Any,
    calls: list[list[str]],
    respond: Any,
    *,
    key: str = SERIAL,
    config: EmuctlConfig | None = None,
) -> Any:
    ref = InstanceRef(backend=BackendId.GOOGLE_PLAY, key=key)
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    return gp_module.GooglePlayController(
        ref,
        config=config if config is not None else _config(),
        _executor=executor,
    )


def _display_responder(
    *,
    size_out: bytes = b"Physical size: 1080x2400\n",
    density_out: bytes = b"Physical density: 420\n",
) -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        if argv[-3:] == ["shell", "wm", "size"]:
            return _FakeHandle(stdout=size_out)
        if argv[-3:] == ["shell", "wm", "density"]:
            return _FakeHandle(stdout=density_out)
        return _FakeHandle()

    return respond


# ── construction + declared capability set ──


def test_construction_binds_and_probes_nothing(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok())
    assert ctl.identity == InstanceRef(backend=BackendId.GOOGLE_PLAY, key=SERIAL)
    assert ctl.adb_path == ADB and ctl.adb_server_port == 5037
    assert calls == []


def test_features_are_honest(gp_module: Any) -> None:
    assert (
        frozenset(
            {
                CapabilityFlag.SCREENSHOT,
                CapabilityFlag.INPUT,
                CapabilityFlag.APPS,
                CapabilityFlag.CONFIGURE,
            }
        )
        == gp_module.GooglePlayController.FEATURES
    )


@pytest.mark.parametrize(
    "flag",
    [
        CapabilityFlag.BOOT,
        CapabilityFlag.CREATE,
        CapabilityFlag.APK_INSTALL,
    ],
)
def test_unsupported_capabilities_are_not_declared(gp_module: Any, flag: CapabilityFlag) -> None:
    assert flag not in gp_module.GooglePlayController.FEATURES


def test_construction_rejects_bad_key_and_config(gp_module: Any) -> None:
    with pytest.raises(InvalidSerialError):
        _make(gp_module, [], _ok(), key="not a serial")
    with pytest.raises(InvalidArgumentError):
        _make(gp_module, [], _ok(), config="nope")  # ty: ignore[invalid-argument-type]


# ── capability gates: every undeclared operation refuses before I/O ──


@_sync
async def test_boot_create_and_apk_install_are_typed_refusals(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok())
    for call in (
        lambda: ctl.start(),
        lambda: ctl.stop(),
        lambda: ctl.restart(),
        lambda: ctl.create(CreateSpec(name="new-vm")),
        lambda: ctl.install_apk("app.apk"),
    ):
        with pytest.raises(FeatureNotSupportedError):
            await call()
    assert calls == []


def test_deferred_surface_is_absent(gp_module: Any) -> None:
    ctl = _make(gp_module, [], _ok())
    for name in (
        "is_running",
        "screen_size",
        "run_vendor_cmd",
        "clone",
        "pause",
        "resume",
        "snapshot",
        "stop_app",
    ):
        assert not hasattr(ctl, name), name


# ── bundled-ADB resolution: explicit → system → registry → typed refusal ──


@_sync
async def test_explicit_adb_path_wins(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok(), config=_config())
    await ctl._vendor_run(["shell", "echo", "hi"])
    assert calls == [[*SCOPED, "shell", "echo", "hi"]]


@_sync
async def test_system_adb_path_is_the_fallback(gp_module: Any) -> None:
    calls: list[list[str]] = []
    cfg = EmuctlConfig(adb=AdbConfig(path="/usr/bin/adb"))
    ctl = _make(gp_module, calls, _ok(), config=cfg)
    assert ctl.adb_path == "/usr/bin/adb"
    await ctl._vendor_run(["shell", "echo", "hi"])
    assert calls == [["/usr/bin/adb", "-P", "5037", "-s", SERIAL, "shell", "echo", "hi"]]


@_sync
async def test_registry_install_location_resolves_bundled_adb(
    gp_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "GooglePlayGames"
    adb = root / "current" / "emulator" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.write_text("adb")

    class _FakeWinreg:
        HKEY_LOCAL_MACHINE = "HKLM"

        def ConnectRegistry(self, whom: str | None, hive: str) -> str:
            assert whom is None and hive == self.HKEY_LOCAL_MACHINE
            return "root"

        def OpenKey(self, handle: str, key: str) -> Any:
            del handle
            if key.endswith("GooglePlayGamesDeveloperEmulator"):
                raise FileNotFoundError(key)
            if key.endswith("GooglePlayGames"):
                return self
            raise FileNotFoundError(key)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def QueryValueEx(self, handle: Any, name: str) -> tuple[str, str]:
            assert name == "InstallLocation"
            return (str(root), "REG_SZ")

    module = types.ModuleType("winreg")
    module.__dict__.update(
        HKEY_LOCAL_MACHINE=_FakeWinreg.HKEY_LOCAL_MACHINE,
        ConnectRegistry=_FakeWinreg().ConnectRegistry,
        OpenKey=_FakeWinreg().OpenKey,
        QueryValueEx=_FakeWinreg().QueryValueEx,
    )
    monkeypatch.setitem(sys.modules, "winreg", module)
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok(), config=EmuctlConfig(google_play=GooglePlayConfig()))
    await ctl._vendor_run(["shell", "echo", "hi"])
    assert calls == [[str(adb), "-P", "5037", "-s", SERIAL, "shell", "echo", "hi"]]


@_sync
async def test_missing_adb_is_typed_refusal_before_io(
    gp_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force "no install": otherwise a real Google Play install on the test host
    # (its registry row) resolves a bundled adb and there is nothing to refuse.
    monkeypatch.setattr(gp_module, "_registry_install_root", lambda: None)
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok(), config=EmuctlConfig(google_play=GooglePlayConfig()))
    with pytest.raises(ExecutableNotFoundError) as excinfo:
        await ctl._vendor_run(["shell", "echo", "hi"])
    assert excinfo.value.tool == "adb"
    assert any("GooglePlayGames" in item for item in excinfo.value.searched)
    assert calls == []


# ── interaction over the bound serial ──


@_sync
async def test_screenshot_exact_argv_and_png(gp_module: Any) -> None:
    calls: list[list[str]] = []
    frame = _png() + b"IDAT"
    ctl = _make(gp_module, calls, _ok(frame))
    assert await ctl.screenshot() == frame
    assert calls == [[*SCOPED, "exec-out", "screencap", "-p"]]


@_sync
async def test_click_and_text_exact_argv(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok())
    await ctl.click(10, 20)
    await ctl.text("hello world")
    assert calls == [
        [*SCOPED, "shell", "input", "tap", "10", "20"],
        [*SCOPED, "shell", "input", "text", "hello%sworld"],
    ]


@_sync
async def test_apps_exact_argv(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok(b"package:com.example.game\n"))
    assert await ctl.is_app_installed("com.example.game") is True
    await ctl.start_app("com.example.game")
    assert calls == [
        [*SCOPED, "shell", "pm", "list", "packages", "com.example.game"],
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
async def test_configure_display_uses_shared_adb_geometry(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _display_responder())
    previous = await ctl.configure({"display.width": 419, "display.height": 633, "display.dpi": 160})
    assert previous == DisplayState(1080, 2400, 420)
    assert calls == [
        [*SCOPED, "shell", "wm", "size"],
        [*SCOPED, "shell", "wm", "density"],
        [*SCOPED, "shell", "wm", "size", "419x633"],
        [*SCOPED, "shell", "wm", "density", "160"],
    ]


@_sync
async def test_configure_none_reapplies_bound_display(gp_module: Any) -> None:
    calls: list[list[str]] = []
    cfg = EmuctlConfig(
        display=DisplayConfig(width=419, height=633),
        google_play=GooglePlayConfig(adb_path=ADB),
    )
    ctl = _make(gp_module, calls, _display_responder(), config=cfg)
    await ctl.configure(None)
    assert calls[-1] == [*SCOPED, "shell", "wm", "size", "419x633"]


@_sync
async def test_configure_foreign_leaf_is_typed(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok())
    with pytest.raises(InvalidArgumentError):
        await ctl.configure({"resources.cpus": 2})
    with pytest.raises(InvalidArgumentError):
        await ctl.configure({"display.fps": 40})
    assert calls == []


# ── discovery rows ──


@_sync
async def test_list_instances_present_device(gp_module: Any) -> None:
    calls: list[list[str]] = []
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, _ok(DEVICES_PRESENT)))  # type: ignore[arg-type]
    rows = await gp_module.list_instances(_config(), _executor=executor)
    assert calls == [[ADB, "-P", "5037", "devices"]]
    assert [row.ref.key for row in rows] == [SERIAL]
    assert rows[0].running is True
    assert rows[0].serial == SERIAL
    assert dict(rows[0].details) == {"adb_state": "device"}


@_sync
async def test_list_instances_stopped_device_is_a_row(gp_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(DEVICES_ABSENT)))  # type: ignore[arg-type]
    rows = await gp_module.list_instances(_config(), _executor=executor)
    assert [row.ref.key for row in rows] == [SERIAL]
    assert rows[0].running is False
    assert dict(rows[0].details) == {}


@_sync
async def test_list_instances_offline_state(gp_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(DEVICES_OFFLINE)))  # type: ignore[arg-type]
    rows = await gp_module.list_instances(_config(), _executor=executor)
    assert rows[0].running is False
    assert dict(rows[0].details) == {"adb_state": "offline"}


@_sync
async def test_list_instances_no_adb_is_honest_empty(
    gp_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force "no install" so a real Google Play install on the test host does not
    # resolve a bundled adb and produce an enumerated row.
    monkeypatch.setattr(gp_module, "_registry_install_root", lambda: None)
    assert await gp_module.list_instances(EmuctlConfig(google_play=GooglePlayConfig())) == ()


@_sync
async def test_list_instances_vendor_failure_propagates(gp_module: Any) -> None:
    executor = ProcessExecutor(  # type: ignore[arg-type]
        _spawn=_spawn_factory([], lambda argv: _FakeHandle(stderr=b"nope", returncode=1))
    )
    with pytest.raises(VendorCommandError):
        await gp_module.list_instances(_config(), _executor=executor)


@_sync
async def test_get_instance_found_absent_and_foreign(gp_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(DEVICES_PRESENT)))  # type: ignore[arg-type]
    cfg = _config()
    hit = await gp_module.get_instance(
        cfg, InstanceRef(backend=BackendId.GOOGLE_PLAY, key=SERIAL), _executor=executor
    )
    assert hit is not None and hit.running is True
    miss = await gp_module.get_instance(
        cfg, InstanceRef(backend=BackendId.GOOGLE_PLAY, key="localhost:6521"), _executor=executor
    )
    assert miss is None
    foreign = await gp_module.get_instance(
        cfg, InstanceRef(backend=BackendId.ADB, key=SERIAL), _executor=executor
    )
    assert foreign is None


@_sync
async def test_get_instance_rejects_non_ref(gp_module: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        await gp_module.get_instance(_config(), "google-play:localhost:6520")  # type: ignore[arg-type]


# ── probe, timeouts, close ──


@_sync
async def test_is_reachable_uses_scoped_echo(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok(b"emuctl\n"))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (True, "ok")
    assert calls == [[*SCOPED, "shell", "echo", "emuctl"]]


@_sync
async def test_timeout_and_cancellation(gp_module: Any) -> None:
    ctl = _make(gp_module, [], lambda argv: _FakeHandle(hang=True))
    with pytest.raises(OperationTimeoutError):
        await ctl.screenshot(timeout=0.05)
    task = asyncio.ensure_future(ctl.click(1, 2))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@_sync
async def test_closed_controller_guards_enabled_ops(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok())
    await ctl.aclose()
    await ctl.aclose()
    with pytest.raises(ControllerClosedError):
        await ctl.screenshot()
    with pytest.raises(ControllerClosedError):
        await ctl.configure(None)
    assert calls == []


@_sync
async def test_invalid_timeout_rejected_before_io(gp_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(gp_module, calls, _ok())
    with pytest.raises(InvalidArgumentError):
        await ctl.screenshot(timeout=0)
    assert calls == []


# ── config model ──


def test_google_play_config_defaults_and_validation() -> None:
    default = GooglePlayConfig()
    assert default.device_serial == "localhost:6520"
    with pytest.raises(ConfigValidationError):
        GooglePlayConfig(device_serial="not a serial")
    with pytest.raises(ConfigValidationError):
        GooglePlayConfig(device_serial="   ")
    with pytest.raises(ConfigValidationError):
        GooglePlayConfig(install_dir="   ")


def test_emuctl_config_rejects_bad_section() -> None:
    with pytest.raises(ConfigValidationError):
        EmuctlConfig(google_play="nope")  # ty: ignore[invalid-argument-type]


# ── surface: every public port member is either enabled or a typed gate ──


@_sync
async def test_public_surface_has_no_silent_noops(gp_module: Any) -> None:
    ctl = _make(gp_module, [], _ok(b"package:x\n" + _png()))
    gated = [
        lambda: ctl.start(),
        lambda: ctl.stop(),
        lambda: ctl.restart(),
        lambda: ctl.install_apk("app.apk"),
        lambda: ctl.create(CreateSpec(name="x")),
    ]
    for call in gated:
        with pytest.raises(FeatureNotSupportedError):
            await call()
    assert ctl.has_feature(CapabilityFlag.SCREENSHOT)
    assert ctl.has_feature(CapabilityFlag.INPUT)
    assert ctl.has_feature(CapabilityFlag.APPS)
    assert ctl.has_feature(CapabilityFlag.CONFIGURE)

"""§7 conformance (MEmu, §1/§3/§4): exact memuc argv per mechanic, the
``-n`` identity discipline, scoped kills, create's observed confirmation,
the §4 translation-table walk, transport derivation + attach ordering,
discovery rows, timeouts, cancellation, and the wiring gate.

All offline (macOS included): the vendor binary is fake-named and every
memuc/adb child replays through ``ProcessExecutor(_spawn=...)`` — the same
recorded-transcript tier as test_adb.py. The one Windows-only piece the
adapter itself touches (the ``winreg`` registry walk) is replayed through a
fake module planted in ``sys.modules``. memuc is never imported; the
``[memu]`` extra's ``pymemuc`` dep is a find_spec marker only.
"""

from __future__ import annotations

import asyncio
import functools
import struct
import sys
import time
import types
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl.config.model import (
    AdbConfig,
    CreateSpec,
    DisplayConfig,
    EmuctlConfig,
    MemuConfig,
    MemuSettings,
    ResourceConfig,
)
from emuctl.errors import (
    ConfigValidationError,
    ControllerClosedError,
    EmulatorNotReadyError,
    ExecutableNotFoundError,
    FeatureNotSupportedError,
    InstanceNotFoundError,
    InvalidArgumentError,
    OperationTimeoutError,
    VendorCommandError,
)
from emuctl.port import CapabilityFlag
from emuctl.timing import CONFIG_TIMEOUT, LIFECYCLE_TIMEOUT, PROVISION_TIMEOUT, QUERY_TIMEOUT

NAME = "pyclashbot-136"
MEMUC = "memuc-fake"

#: ``memuc listvms`` row shape (pymemuc/_manage.py:285-305):
#: index,title,top_level,running(0/1),pid.
ROWS_STOPPED = (
    b"read failed\n"  # memuc no-VMs noise: skipped, never a row
    b"0,MEmu 1,Default Android,0,0\n"
    b"1,pyclashbot-136,Default Medium Android,0,555\n"
    b"garbage without commas\n"  # unparseable: skipped
    b"2,broken,row,1\n"  # <5 columns: skipped
    b"3,no title,Default Android,1,notapid\n"  # unparsable pid: skipped
)
ROWS_RUNNING = b"0,MEmu 1,Default Android,0,0\n1,pyclashbot-136,Default Medium Android,1,4200\n"
ROWS_ABSENT = b"0,MEmu 1,Default Android,0,0\n"
ROWS_WITH_FRESH_3 = (
    b"0,MEmu 1,Default Android,0,0\n"
    b"1,pyclashbot-136,Default Medium Android,1,4200\n"
    b"3,MEmu New,Default Medium Android,0,0\n"
)


def _sync(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    """Run an async test body to completion on a fresh loop (repo convention)."""

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
def memu_module() -> Any:
    """Import the adapter module lazily (never at collection time)."""
    import importlib

    return importlib.import_module("emuctl.backends.memu")


def _png(width: int = 419, height: int = 633) -> bytes:
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


def _rows_responder(scenarios: list[tuple[int, bytes]]) -> Callable[[list[str]], _FakeHandle]:
    """Respond listvms rows per poll cycle (cycle 0 = first listvms call)."""
    counters = {"cycle": -1}

    def respond(argv: list[str]) -> _FakeHandle:
        if "listvms" in argv:
            counters["cycle"] += 1
            chosen = b""
            for start_cycle, rows in scenarios:
                if counters["cycle"] >= start_cycle:
                    chosen = rows
            return _FakeHandle(stdout=chosen)
        return _FakeHandle()

    return respond


def _transport_responder(
    *, rows: bytes = ROWS_RUNNING, stdout: bytes = b"", echo_rc: int = 0
) -> Callable[[list[str]], _FakeHandle]:
    """Rows for the listvms lookups; payload stdout for ADB device calls."""

    def respond(argv: list[str]) -> _FakeHandle:
        if "listvms" in argv:
            return _FakeHandle(stdout=rows)
        return _FakeHandle(stdout=stdout, returncode=echo_rc)

    return respond


def _config(**kwargs: Any) -> EmuctlConfig:
    """Built config: explicit memuc path (offline), default ADB server."""
    memu = kwargs.pop("memu", MemuConfig(memuc_path=MEMUC))
    return EmuctlConfig(adb=AdbConfig(), memu=memu, **kwargs)


def _make(
    memu_module: Any,
    calls: list[list[str]],
    respond: Any,
    *,
    key: str = NAME,
    config: EmuctlConfig | None = None,
) -> Any:
    """Build a controller bound to NAME over a scripted transport."""
    ref = InstanceRef(backend=BackendId.MEMU, key=key)
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    return memu_module.MemuController(
        ref, config=config if config is not None else _config(), _executor=executor
    )


# ── construction: cheap, binds config, never probes ──


def test_construction_binds_transport_and_probes_nothing(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    assert ctl.identity == InstanceRef(backend=BackendId.MEMU, key=NAME)
    assert ctl.adb_path is None and ctl.adb_server_port == 5037
    assert ctl.features  # declared set, no gate, no I/O
    assert calls == []  # no discovery, no boot, no probe at construction


def test_features_declare_the_full_v0_set(memu_module: Any) -> None:
    assert (
        frozenset(
            {
                CapabilityFlag.BOOT,
                CapabilityFlag.CREATE,
                CapabilityFlag.CONFIGURE,
                CapabilityFlag.SCREENSHOT,
                CapabilityFlag.INPUT,
                CapabilityFlag.APPS,
                CapabilityFlag.APK_INSTALL,
            }
        )
        == memu_module.MemuController.FEATURES
    )


def test_construction_rejects_bad_key_and_config(memu_module: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        _make(memu_module, [], _ok(), key="has\nnewline")
    with pytest.raises(InvalidArgumentError):
        _make(memu_module, [], _ok(), config="nope")  # ty: ignore[invalid-argument-type]


# ── §8 deferrals: not port members, no silent no-ops ──


def test_deferred_surface_is_absent(memu_module: Any) -> None:
    ctl = _make(memu_module, [], _ok())
    for name in (
        "is_running",  # §8: public is_running deferred
        "screen_size",  # §8: screen_size deferred
        "run_vendor_cmd",  # §8: public vendor passthrough deferred
        "clone",  # §8: clone family deferred (v0.1)
        "pause",
        "resume",
        "snapshot",
        "stop_app",  # §8: public stop_app deferred
    ):
        assert not hasattr(ctl, name), name


# ── exact memuc argv per mechanic; the -n identity discipline ──


@_sync
async def test_start_argv_headless(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    await ctl.start()
    assert calls == [[MEMUC, "-n", NAME, "start", "-b"]]  # vendor: start [-b]


@_sync
async def test_stop_argv_and_scope(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    await ctl.stop()
    assert calls == [[MEMUC, "-n", NAME, "stop"]]  # instance-scoped only


@_sync
async def test_stop_and_start_never_sweep(memu_module: Any) -> None:
    """§2: no stopall, no process-table walk — every argv selects -n NAME."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    await ctl.stop()
    await ctl.start()
    for argv in calls:
        assert argv[:2] == [MEMUC, "-n"]
        assert "stopall" not in argv


@_sync
async def test_stop_failure_raises_honestly(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok(stderr=b"already stopped", returncode=1))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.stop()
    assert excinfo.value.output_reason == "exit"
    assert len(calls) == 1


@_sync
async def test_is_running_reads_the_running_column(memu_module: Any) -> None:
    """Running comes from the listvms running COLUMN — memuc's
    isvmrunning words ("Running"/"Not Running") are never prose-parsed."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _rows_responder([(0, ROWS_RUNNING)]))
    assert await ctl._is_running(1.0) is True
    ctl = _make(memu_module, calls, _rows_responder([(0, ROWS_STOPPED)]))
    assert await ctl._is_running(1.0) is False
    for argv in calls:
        assert argv == [MEMUC, "listvms"]


@_sync
async def test_is_running_on_stale_ref_raises(memu_module: Any) -> None:
    """An absent-bound-name observation raises; never laundered False."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _rows_responder([(0, ROWS_ABSENT)]))
    with pytest.raises(InstanceNotFoundError) as excinfo:
        await ctl._is_running(1.0)
    assert excinfo.value.instance_ref == f"memu:{NAME}"


@_sync
async def test_is_app_installed_via_inherited_mechanics(memu_module: Any) -> None:
    """APPS/APK_INSTALL/SCREENSHOT/INPUT are the INHERITED middle-layer ADB
    mechanics over the lazily derived transport."""
    calls: list[list[str]] = []
    ctl = _make(
        memu_module,
        calls,
        _transport_responder(stdout=b"package:com.example.game\n"),
    )
    assert await ctl.is_app_installed("com.example.game") is True
    assert calls[0] == [MEMUC, "listvms"]  # lazy transport derivation
    assert calls[1] == [
        "adb",
        "-H",
        "127.0.0.1",
        "-P",
        "5037",
        "-s",
        "127.0.0.1:21513",  # index 1: 21503 + 10*1
        "shell",
        "pm",
        "list",
        "packages",
        "com.example.game",
    ]


@_sync
async def test_screenshot_sniffs_png_and_scopes(memu_module: Any) -> None:
    calls: list[list[str]] = []
    frame = _png() + b"IDAT"
    ctl = _make(memu_module, calls, _transport_responder(stdout=frame))
    assert await ctl.screenshot() == frame
    assert calls == [
        [MEMUC, "listvms"],
        [
            "adb",
            "-H",
            "127.0.0.1",
            "-P",
            "5037",
            "-s",
            "127.0.0.1:21513",
            "exec-out",
            "screencap",
            "-p",
        ],
    ]


@_sync
async def test_vendor_run_is_the_memuc_transport(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok(stdout=b"2"))
    out = await ctl._vendor_run(["-n", NAME, "getconfigex", "cpus"])
    assert out == "2"
    assert calls == [[MEMUC, "-n", NAME, "getconfigex", "cpus"]]


# ── memuc binary resolution: explicit path → registry → typed refusal ──


class _FakeWinreg:
    """Fake ``winreg`` module: replays the recorded registry walk."""

    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, locations: dict[str, str]) -> None:
        self._locations = locations
        self.opened: list[str] = []

    class _Key:
        def __init__(self, opener: _FakeWinreg, key: str) -> None:
            self._opener = opener
            self.key = key

        def __enter__(self) -> _FakeWinreg._Key:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    def ConnectRegistry(self, whom: str | None, hive: str) -> str:
        assert whom is None and hive == self.HKEY_LOCAL_MACHINE
        return "root"

    def OpenKey(self, root: str, key: str) -> _FakeWinreg._Key:
        del root
        self.opened.append(key)
        if key not in self._locations:
            raise FileNotFoundError(key)
        return _FakeWinreg._Key(self, key)

    def QueryValueEx(self, hkey: _FakeWinreg._Key, name: str) -> tuple[str, str]:
        assert name == "InstallLocation"
        return (self._locations[hkey.key], "REG_SZ")


@pytest.fixture
def fake_winreg_module() -> Iterator[list[Any]]:
    """Plants/evicts a fake ``winreg`` module per test (Windows-only piece)."""
    holder: list[_FakeWinreg] = []
    yield holder
    sys.modules.pop("winreg", None)


@_sync
async def test_registry_discovery_replaces_none_path(
    memu_module: Any, fake_winreg_module: Any
) -> None:
    fake = _FakeWinreg(
        {r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\MEmu": r"C:\MEmu"}
    )
    fake_winreg_module.append(fake)
    module = types.ModuleType("winreg")
    module.__dict__.update(
        HKEY_LOCAL_MACHINE=_FakeWinreg.HKEY_LOCAL_MACHINE,
        ConnectRegistry=fake.ConnectRegistry,
        OpenKey=fake.OpenKey,
        QueryValueEx=fake.QueryValueEx,
    )
    sys.modules["winreg"] = module
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok(), config=EmuctlConfig())
    await ctl.start()
    assert calls[0][0].endswith("memuc.exe")
    assert "Memu" in calls[0][0]
    assert fake.opened == [  # the first key misses; the WOW6432Node twin hits
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MEmu",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\MEmu",
    ]


@_sync
async def test_explicit_path_wins_over_registry(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    await ctl.stop()
    assert calls == [[MEMUC, "-n", NAME, "stop"]]


@_sync
async def test_missing_memuc_binary_is_typed_refusal(memu_module: Any) -> None:
    """No explicit path + no winreg (macOS dev box) = typed refusal, no I/O."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok(), config=EmuctlConfig())
    with pytest.raises(ExecutableNotFoundError) as excinfo:
        await ctl.start()
    assert excinfo.value.tool == "memuc"
    searched = excinfo.value.searched
    assert any("WOW6432Node" in path for path in searched)
    assert any("Uninstall" in path for path in searched)
    assert calls == []  # refusal before any I/O


# ── discovery rows ──


@_sync
async def test_list_instances_row_mapping(memu_module: Any) -> None:
    calls: list[list[str]] = []
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, _ok(stdout=ROWS_STOPPED)))  # type: ignore[arg-type]
    found = await memu_module.list_instances(_config(), _executor=executor)
    assert calls == [[MEMUC, "listvms"]]
    assert [str(r.ref) for r in found] == ["memu:MEmu 1", "memu:pyclashbot-136"]
    assert [r.running for r in found] == [False, False]
    assert [r.details["index"] for r in found] == ["0", "1"]
    assert [r.details["pid"] for r in found] == ["0", "555"]
    assert [r.serial for r in found] == [None, None]  # serial is observation


@_sync
async def test_list_instances_running_column(memu_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(stdout=ROWS_RUNNING)))  # type: ignore[arg-type]
    found = await memu_module.list_instances(_config(), _executor=executor)
    assert [r.running for r in found] == [False, True]


@_sync
async def test_list_instances_empty_is_no_rows(memu_module: Any) -> None:
    executor = ProcessExecutor(_spawn=_spawn_factory([], _ok(stdout=b"read failed\n")))  # type: ignore[arg-type]
    assert await memu_module.list_instances(_config(), _executor=executor) == ()


@_sync
async def test_list_instances_vendor_failure_propagates(memu_module: Any) -> None:
    executor = ProcessExecutor(
        _spawn=_spawn_factory([], _ok(stderr=b"no memuc", returncode=1))  # type: ignore[arg-type]
    )
    with pytest.raises(VendorCommandError):
        await memu_module.list_instances(_config(), _executor=executor)


@_sync
async def test_get_instance_found_absent_and_foreign(memu_module: Any) -> None:
    calls: list[list[str]] = []
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, _ok(stdout=ROWS_RUNNING)))  # type: ignore[arg-type]
    cfg = _config()
    hit = await memu_module.get_instance(
        cfg, InstanceRef(backend=BackendId.MEMU, key=NAME), _executor=executor
    )
    assert hit is not None and hit.running is True
    miss = await memu_module.get_instance(
        cfg, InstanceRef(backend=BackendId.MEMU, key="ghost"), _executor=executor
    )
    assert miss is None
    foreign = await memu_module.get_instance(
        cfg, InstanceRef(backend=BackendId.ADB, key=NAME), _executor=executor
    )
    assert foreign is None
    listvms_calls = [argv for argv in calls if "listvms" in argv]
    assert len(listvms_calls) == 2  # hit + miss probed; foreign never did


@_sync
async def test_get_instance_invalid_ref(memu_module: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        await memu_module.get_instance(_config(), "memu:x")  # type: ignore[arg-type]


# ── create: pre-check seam, vendor verb, bounded observed confirmation ──


def _create_responder() -> Callable[[list[str]], _FakeHandle]:
    """Rows lack the fresh VM until poll cycle 2; rename/create are rc 0."""
    rows = _rows_responder([(0, ROWS_STOPPED), (2, ROWS_WITH_FRESH_3)])

    def respond(argv: list[str]) -> _FakeHandle:
        if "create" in argv:
            return _FakeHandle(stdout=b"SUCCESS: creating\n")
        if "rename" in argv:
            return _FakeHandle(stdout=b"SUCCESS: renamed\n")
        return rows(argv)

    return respond


@_sync
async def test_create_full_flow_observes_without_stdout(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _create_responder())
    ref = await ctl.create(CreateSpec(name="new-vm"))
    memuc_calls = [argv for argv in calls if argv[0] == MEMUC]
    assert memuc_calls[0] == [MEMUC, "listvms"]  # pre-check through the seam
    assert [MEMUC, "create", "136"] in memuc_calls  # §4 default image pin
    assert any("listvms" in argv for argv in memuc_calls[2:-1])  # bounded poll
    assert memuc_calls[-1] == [MEMUC, "-i", "3", "rename", "new-vm"]
    assert str(ref) == "memu:new-vm"


@_sync
async def test_create_uses_requested_android_version(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _create_responder())
    await ctl.create(CreateSpec(name="new-vm", android_version="138"))
    assert [MEMUC, "create", "138"] in calls
    assert [MEMUC, "-i", "3", "rename", "new-vm"] in calls


@_sync
async def test_create_refuses_existing_name_before_any_vendor_verb(
    memu_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _rows_responder([(0, ROWS_RUNNING)]))
    with pytest.raises(InvalidArgumentError) as excinfo:
        await ctl.create(CreateSpec(name=NAME))
    assert excinfo.value.constraint == "already provisioned"
    assert [argv for argv in calls if argv[0] == MEMUC] == [[MEMUC, "listvms"]]


@_sync
async def test_create_vendor_failure_never_renames(memu_module: Any) -> None:
    calls: list[list[str]] = []
    rows = _rows_responder([(0, ROWS_STOPPED)])

    def respond(argv: list[str]) -> _FakeHandle:
        if argv[-1] == "136":
            return _FakeHandle(stderr=b"create failed", returncode=1)
        return rows(argv)

    ctl = _make(memu_module, calls, respond)
    with pytest.raises(VendorCommandError):
        await ctl.create(CreateSpec(name="new-vm"))
    assert not any("rename" in argv for argv in calls)


@_sync
async def test_create_polls_to_typed_timeout(memu_module: Any) -> None:
    """A fresh VM row never appearing exhausts the shared provision budget."""
    calls: list[list[str]] = []
    rows = _rows_responder([(0, ROWS_STOPPED)])

    def respond(argv: list[str]) -> _FakeHandle:
        if "create" in argv:
            return _FakeHandle()
        return rows(argv)

    ctl = _make(memu_module, calls, respond)
    with pytest.raises(OperationTimeoutError) as excinfo:
        await ctl.create(CreateSpec(name="new-vm"), timeout=0.3)
    assert excinfo.value.operation == "create"
    assert len([argv for argv in calls if "listvms" in argv]) > 2  # polled


@_sync
async def test_observed_boundary_raises_typed_deadline_error(memu_module: Any) -> None:
    """Positive-budget invariant at the _observed boundary (§2): an already
    exhausted deadline raises the TYPED deadline error — wait_until never
    sees a non-positive timeout (that confusion is InvalidArgumentError)."""
    ctl = _make(memu_module, [], _ok())
    start = time.monotonic()
    deadline = start - 0.5  # exhausted before the wait even begins
    with pytest.raises(OperationTimeoutError) as excinfo:
        await ctl._observed(
            lambda: ...,  # never awaited — the boundary fires first
            operation="create",
            start=start,
            deadline=deadline,
        )
    assert excinfo.value.operation == "create"


@_sync
async def test_get_instance_honors_the_passed_deadline(memu_module: Any) -> None:
    """§2 one-deadline: the pre-check/derivation seam honors the CALLER'S
    slice — a slow never-answering listvms is cut at the passed budget."""
    ctl = _make(memu_module, [], lambda argv: _FakeHandle(hang=True))
    ref = InstanceRef(backend=BackendId.MEMU, key=NAME)
    with pytest.raises(OperationTimeoutError) as excinfo:
        await ctl._get_instance(ref, 0.1)
    assert excinfo.value.operation == "get_instance"


# ── configure: translation table walk (§4) ──


@_sync
async def test_configure_renderer_encodings(memu_module: Any) -> None:
    calls: list[list[str]] = []
    for value in ("directx", "auto", "opengl"):
        ctl = _make(memu_module, calls, _ok())
        await ctl.configure({"display.renderer": value})
    assert calls == [
        [MEMUC, "-n", NAME, "setconfigex", "graphics_render_mode", "1"],
        [MEMUC, "-n", NAME, "setconfigex", "graphics_render_mode", "1"],
        [MEMUC, "-n", NAME, "setconfigex", "graphics_render_mode", "0"],
    ]


@_sync
async def test_configure_vulkan_is_typed_refusal(memu_module: Any) -> None:
    """VULKAN on MEmu: the §4 gate via a missing table entry (never silent)."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        await ctl.configure({"display.renderer": "vulkan"})
    assert excinfo.value.feature == "display.renderer"
    assert calls == []  # refused before any I/O


@_sync
async def test_configure_unknown_renderer_spelling_is_validation_error(
    memu_module: Any,
) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    with pytest.raises(ConfigValidationError) as excinfo:
        await ctl.configure({"display.renderer": "meteor"})
    assert excinfo.value.layer == "memu"
    assert calls == []


@_sync
async def test_configure_custom_resolution_absorption(memu_module: Any) -> None:
    """§4: is_customed_resolution=1 is absorbed when a custom-res leaf is
    present, emitted BEFORE its cohort. Working vendor spelling
    (is_customed_resolution per pymemuc/_types.py ConfigKeys; the docs CSV's
    is_custom_resolution diverges — noted in the module docstring)."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    await ctl.configure({"display.height": 633})
    assert calls == [
        [MEMUC, "-n", NAME, "setconfigex", "is_customed_resolution", "1"],
        [MEMUC, "-n", NAME, "setconfigex", "resolution_height", "633"],
    ]


@_sync
async def test_configure_full_bound_overlay_walk(memu_module: Any) -> None:
    """spec=None re-applies every non-None bound-config leaf (§1.8), in the
    translation table's strict order, with the custom-res flag first."""
    calls: list[list[str]] = []
    cfg = _config(
        resources=ResourceConfig(cpus=2, memory_mb=2048, cpu_cap_pct=50),
        display=DisplayConfig(width=419, height=633, dpi=160, fps=40, renderer="opengl"),
        memu=MemuConfig(
            memuc_path=MEMUC,
            settings=MemuSettings(
                hide_toolbar=True,
                turbo=False,
                audio=True,
                window_mode="custom",
                scale_percent=75,
            ),
        ),
    )
    ctl = _make(memu_module, calls, _ok(), config=cfg)
    await ctl.configure(None)
    assert calls == [
        [MEMUC, "-n", NAME, "setconfigex", "is_hide_toolbar", "1"],
        [MEMUC, "-n", NAME, "setconfigex", "turbo_mode", "0"],
        [MEMUC, "-n", NAME, "setconfigex", "enable_audio", "1"],
        [MEMUC, "-n", NAME, "setconfigex", "start_window_mode", "2"],
        [MEMUC, "-n", NAME, "setconfigex", "win_scaling_percent2", "75"],
        [MEMUC, "-n", NAME, "setconfigex", "cpus", "2"],
        [MEMUC, "-n", NAME, "setconfigex", "memory", "2048"],
        [MEMUC, "-n", NAME, "setconfigex", "cpucap", "50"],
        [MEMUC, "-n", NAME, "setconfigex", "is_customed_resolution", "1"],
        [MEMUC, "-n", NAME, "setconfigex", "resolution_width", "419"],
        [MEMUC, "-n", NAME, "setconfigex", "resolution_height", "633"],
        [MEMUC, "-n", NAME, "setconfigex", "vbox_dpi", "160"],
        [MEMUC, "-n", NAME, "setconfigex", "fps", "40"],
        [MEMUC, "-n", NAME, "setconfigex", "graphics_render_mode", "0"],
    ]


@_sync
async def test_configure_window_mode_spellings(memu_module: Any) -> None:
    calls: list[list[str]] = []
    for value in ("0", "last", "custom", 2):
        ctl = _make(memu_module, calls, _ok())
        await ctl.configure({"memu.settings.window_mode": value})
    assert calls == [
        [MEMUC, "-n", NAME, "setconfigex", "start_window_mode", "0"],
        [MEMUC, "-n", NAME, "setconfigex", "start_window_mode", "1"],
        [MEMUC, "-n", NAME, "setconfigex", "start_window_mode", "2"],
        [MEMUC, "-n", NAME, "setconfigex", "start_window_mode", "2"],
    ]


@_sync
async def test_configure_unknown_key_is_caller_misuse(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    with pytest.raises(InvalidArgumentError):
        await ctl.configure({"weather.tomorrow": "rain"})
    assert calls == []


@_sync
async def test_configure_foreign_leaf_is_the_typed_gate(memu_module: Any) -> None:
    """A canonical leaf the MEmu table cannot translate (a BlueStacks leaf)
    is the FeatureNotSupportedError gate — never silent-ignore (§4)."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        await ctl.configure({"bluestacks.settings.vsync": True})
    assert excinfo.value.feature == "configure"
    assert calls == []


@_sync
async def test_configure_strict_value_refusals(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"memu.settings.window_mode": "minimized"})
    # speculative alias spellings were cut: only vendor ints + the three
    # CSV-readable spellings (default/last/custom) remain
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"memu.settings.window_mode": "last-time"})
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"memu.settings.window_mode": "lasttime"})
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"resources.cpus": 0})
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"resources.cpu_cap_pct": 900})
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"resources.cpus": "2"})  # strict: int tokens only
    assert calls == []  # every refusal fires before any I/O


@_sync
async def test_configure_nothing_to_apply_is_no_io(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok(), config=_config())
    await ctl.configure(None)  # bare config: empty task list
    assert calls == []  # zero subprocesses (not even the binary probe)


@_sync
async def test_configure_shares_one_deadline(memu_module: Any) -> None:
    """§2: the config-group budget is ONE deadline distributed over every
    setconfigex — slices shrink, never reset to the full budget."""
    calls: list[list[str]] = []
    spy = _TimeoutSpy(_spawn=_spawn_factory(calls, _ok()))  # type: ignore[arg-type]
    ref = InstanceRef(backend=BackendId.MEMU, key=NAME)
    cfg = _config(
        resources=ResourceConfig(cpus=2, memory_mb=2048),
        display=DisplayConfig(width=419, height=633, dpi=160),
    )
    ctl = memu_module.MemuController(ref, config=cfg, _executor=spy)
    await ctl.configure(None)
    seen = spy.seen_all["configure"]
    assert len(seen) == 6  # cpus + memory + 3 display ints + the custom-res flag
    assert abs((seen[0] or 0.0) - CONFIG_TIMEOUT) < 0.1  # group constant slice
    assert all(b <= CONFIG_TIMEOUT for b in seen if b is not None)
    assert all((seen[i] or 0) <= (seen[i - 1] or 0) for i in range(1, len(seen)))


# ── transport reachability probe (probes exemption: data, never raises) ──


def _probe_responder(
    *, echo_rc: int = 0, stderr: bytes = b""
) -> Callable[[list[str]], _FakeHandle]:
    rows = _rows_responder([(0, ROWS_RUNNING)])

    def respond(argv: list[str]) -> _FakeHandle:
        if "echo" in argv:
            return _FakeHandle(stdout=b"emuctl\n", stderr=stderr, returncode=echo_rc)
        return rows(argv)

    return respond


@_sync
async def test_is_reachable_ok_via_rows_then_echo(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _probe_responder())
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (True, "ok")
    assert calls == [
        [MEMUC, "listvms"],
        [
            "adb",
            "-H",
            "127.0.0.1",
            "-P",
            "5037",
            "-s",
            "127.0.0.1:21513",
            "shell",
            "echo",
            "emuctl",
        ],
    ]


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (b"adb: device offline", "offline"),
        (b"error: device unauthorized", "unauthorized"),
        (b"cannot connect to 127.0.0.1:21513", "disconnected"),
    ],
)
@_sync
async def test_is_reachable_failure_vocabulary(
    memu_module: Any, stderr: bytes, reason: str
) -> None:
    ctl = _make(memu_module, [], _probe_responder(echo_rc=1, stderr=stderr))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (False, reason)


@_sync
async def test_is_reachable_on_stale_ref_is_data(memu_module: Any) -> None:
    ctl = _make(memu_module, [], _rows_responder([(0, ROWS_ABSENT)]))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (False, "disconnected")


@_sync
async def test_is_reachable_probe_is_the_promoted_core_one(memu_module: Any) -> None:
    """§6 promotion: MEmu does NOT override the probe — MemuController's
    is_reachable IS the core AdbController's function (its transport facts
    engage through the overridden _run_adb instead)."""
    import emuctl.port as port

    assert memu_module.MemuController.is_reachable is port.AdbController.is_reachable
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _probe_responder())
    await ctl.is_reachable()
    assert [argv for argv in calls if "echo" in argv][-1][-1] == "emuctl"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"adb: device offline", "offline"),
        (b"error: device unauthorized", "unauthorized"),
        (b"", "disconnected"),
    ],
)
@_sync
async def test_memu_probe_stderr_walk_matches_core_classifier(
    memu_module: Any, stderr: bytes, expected: str
) -> None:
    """The MEmu stderr → reason mapping is the CORE classifier's shape
    (promotion invariant); every emitted reason stays in the closed
    vocabulary."""
    ctl = _make(memu_module, [], _probe_responder(echo_rc=1, stderr=stderr))
    probe = await ctl.is_reachable()
    assert (probe.ok, probe.reason) == (False, expected)
    from emuctl.port import PROBE_REASONS

    assert probe.reason in PROBE_REASONS


@_sync
async def test_memu_probe_reason_walk_stays_closed(memu_module: Any) -> None:
    """Every reason the MEmu probe paths emit across scenarios is a member
    of port.PROBE_REASONS (same conformance walk as test_adb's)."""
    from emuctl.port import PROBE_REASONS

    scenarios: list[tuple[str, Callable[[list[str]], _FakeHandle] | None]] = [
        ("ok", _probe_responder()),
        ("offline", _probe_responder(echo_rc=1, stderr=b"adb: device offline")),
        ("unauthorized", _probe_responder(echo_rc=1, stderr=b"error: device unauthorized")),
    ]
    reasons: set[str] = set()
    for expected, respond in scenarios:
        assert respond is not None
        ctl = _make(memu_module, [], respond)
        probe = await ctl.is_reachable()
        reasons.add(probe.reason)
        assert probe.reason == expected
    # missing memuc binary (no explicit path + no winreg) → "no-adb" data
    ctl = _make(memu_module, [], _ok(), config=EmuctlConfig())
    probe = await ctl.is_reachable()
    reasons.add(probe.reason)
    assert probe.reason == "no-adb"
    ctl = _make(memu_module, [], _rows_responder([(0, ROWS_ABSENT)]))
    reasons.add((await ctl.is_reachable()).reason)  # stale ref: "disconnected"
    ctl = _make(memu_module, [], lambda argv: _FakeHandle(hang=True))
    reasons.add((await ctl.is_reachable(timeout=0.05)).reason)  # budget expiry
    assert {"ok", "disconnected", "timeout"} <= reasons
    assert reasons <= PROBE_REASONS


# ── restart: §1.6 wholesale override with vendor-ordered attach ──


def _restart_responder(
    rows: bytes = ROWS_RUNNING,
    *,
    fail: str | None = None,
) -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        if "listvms" in argv:
            return _FakeHandle(stdout=rows)
        if fail == "start" and "start" in argv:
            return _FakeHandle(stderr=b"boot failed", returncode=1)
        if fail == "connect" and "connect" in argv:
            return _FakeHandle(stderr=b"cannot connect", returncode=1)
        if "connect" in argv:
            return _FakeHandle(stdout=b"connected to 127.0.0.1:21513\n")
        if "screencap" in argv:
            return _FakeHandle(stdout=_png() + b"IDAT")
        return _FakeHandle()

    return respond


@_sync
async def test_restart_phase_argv_order(memu_module: Any) -> None:
    calls: list[list[str]] = []
    cfg = _config(memu=MemuConfig(memuc_path=MEMUC, settings=MemuSettings(hide_toolbar=True)))
    ctl = _make(memu_module, calls, _restart_responder(), config=cfg)
    await ctl.restart()
    memuc_calls = [argv for argv in calls if argv[0] == MEMUC]
    adb_calls = [argv for argv in calls if argv[0] == "adb"]
    # 1 stop-check (rows say running) → stop → configure → start (headless)
    assert memuc_calls[0] == [MEMUC, "listvms"]
    assert memuc_calls[1] == [MEMUC, "-n", NAME, "stop"]
    assert memuc_calls[2] == [MEMUC, "-n", NAME, "setconfigex", "is_hide_toolbar", "1"]
    assert memuc_calls[3] == [MEMUC, "-n", NAME, "start", "-b"]
    # 4 attach: transport derivation, then the ADB server registration
    assert memuc_calls[4] == [MEMUC, "listvms"]
    assert adb_calls[0] == [
        "adb",
        "-H",
        "127.0.0.1",
        "-P",
        "5037",
        "connect",
        "127.0.0.1:21513",
    ]
    # 5 settle: a properly scoped device screencap
    screencap = adb_calls[-1]
    assert screencap[-2:] == ["screencap", "-p"]
    assert screencap.count("-s") == 1
    assert screencap[screencap.index("-s") + 1] == "127.0.0.1:21513"


@_sync
async def test_restart_app_flow_spawn_verify(memu_module: Any) -> None:
    calls: list[list[str]] = []
    base = _restart_responder()

    def respond(argv: list[str]) -> _FakeHandle:
        if "pm" in argv:  # is_app_installed's terminal check
            return _FakeHandle(stdout=b"package:com.example.game\n")
        if "pidof" in argv:
            return _FakeHandle(stdout=b"1234\n")
        return base(argv)

    ctl = _make(memu_module, calls, respond)
    await ctl.restart(app="com.example.game")  # spawn-present terminal check
    assert [argv for argv in calls if "monkey" in argv]
    assert [argv for argv in calls if "pidof" in argv]


@_sync
async def test_restart_ready_false_is_menu_not_reached(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _restart_responder())
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — reason pinned below
        await ctl.restart(ready=lambda controller: False)
    assert getattr(excinfo.value, "reason", "") == "menu_not_reached"


@_sync
async def test_restart_ready_check_raise_maps_ready_check_failed(
    memu_module: Any,
) -> None:
    async def boom(controller: Any) -> bool:
        raise RuntimeError("no menu")

    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _restart_responder())
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — reason pinned below
        await ctl.restart(ready=boom)
    assert getattr(excinfo.value, "reason", "") == "ready_check_failed"


@_sync
async def test_restart_start_failure_maps_start_failed(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _restart_responder(fail="start"))
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        await ctl.restart()
    assert excinfo.value.reason == "start_failed"
    assert not any("connect" in argv for argv in calls[-1])  # never reached attach


@_sync
async def test_restart_connect_failure_maps_attach_failed(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _restart_responder(fail="connect"))
    with pytest.raises(EmulatorNotReadyError):
        await ctl.restart()
    assert not any("screencap" in argv for argv in calls)  # never reached settle


@_sync
async def test_restart_one_attempt_no_retries(memu_module: Any) -> None:
    """§1.6: one attempt, never internal retries — a failing settle raises
    EmulatorNotReadyError(settle_failed) on the first screenshot."""
    calls: list[list[str]] = []
    base = _restart_responder()

    def respond(argv: list[str]) -> _FakeHandle:
        if "screencap" in argv:
            return _FakeHandle(stdout=b"")  # settle fails: decodes empty
        return base(argv)

    ctl = _make(memu_module, calls, respond)
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        await ctl.restart()
    assert excinfo.value.reason == "settle_failed"
    assert len([argv for argv in calls if "screencap" in argv]) == 1


@_sync
async def test_restart_on_stale_ref_raises_instance_not_found(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _rows_responder([(0, ROWS_ABSENT)]))
    with pytest.raises(InstanceNotFoundError):
        await ctl.restart()
    assert calls == [[MEMUC, "listvms"]]  # the stop-check observation only


@_sync
async def test_restart_concurrent_aclose_propagates_controller_closed_raw(
    memu_module: Any,
) -> None:
    """The port pins ControllerClosedError RAW propagation (concurrent
    aclose mid-restart is a wiring/state error, never readiness data):
    flipping the aclose flag mid-flight surfaces it from a phase
    unmodified — never laundered into EmulatorNotReadyError."""
    calls: list[list[str]] = []
    holder: dict[str, Any] = {}
    base = _restart_responder()

    def respond(argv: list[str]) -> _FakeHandle:
        ctl = holder.get("ctl")
        if ctl is not None and not ctl._closed:
            ctl._closed = True  # aclose()'s first observable effect
        return base(argv)

    ctl = _make(memu_module, calls, respond)
    holder["ctl"] = ctl
    with pytest.raises(ControllerClosedError) as excinfo:
        await ctl.restart()
    assert not isinstance(excinfo.value, EmulatorNotReadyError)


@_sync
async def test_restart_skips_stop_when_not_running(memu_module: Any) -> None:
    calls: list[list[str]] = []
    cfg = _config(memu=MemuConfig(memuc_path=MEMUC, settings=MemuSettings(hide_toolbar=True)))
    ctl = _make(memu_module, calls, _restart_responder(rows=ROWS_STOPPED), config=cfg)
    await ctl.restart()
    stops = [
        argv for argv in calls if argv[0] == MEMUC and argv[1:2] == ["-n"] and argv[3:4] == ["stop"]
    ]
    assert stops == []  # rows said not running: no vendor stop verb at all
    # the headless boot DID happen exactly once
    assert len([argv for argv in calls if argv[0] == MEMUC and "start" in argv]) == 1


# ── timeouts: group defaults, deadline discipline, cancellation ──


class _TimeoutSpy(ProcessExecutor):
    """Records the resolved budget per operation, then runs the fake."""

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
async def test_timeout_group_defaults(memu_module: Any) -> None:
    calls: list[list[str]] = []
    spy = _TimeoutSpy(
        _spawn=_spawn_factory(calls, _rows_responder([(0, ROWS_RUNNING)]))  # type: ignore[arg-type]
    )
    ref = InstanceRef(backend=BackendId.MEMU, key=NAME)
    ctl = memu_module.MemuController(ref, config=_config(), _executor=spy)
    await ctl.start()
    await ctl.stop()
    await ctl.is_reachable()
    assert spy.seen["start"] == LIFECYCLE_TIMEOUT
    assert spy.seen["stop"] == LIFECYCLE_TIMEOUT
    assert spy.seen["get_instance"] == QUERY_TIMEOUT  # probe's rows lookup
    assert abs((spy.seen["is_reachable"] or 0.0) - QUERY_TIMEOUT) < 0.1


@_sync
async def test_create_carries_the_provision_budget(memu_module: Any) -> None:
    calls: list[list[str]] = []
    spy = _TimeoutSpy(
        _spawn=_spawn_factory(  # type: ignore[arg-type]
            calls, _rows_responder([(0, ROWS_STOPPED), (2, ROWS_WITH_FRESH_3)])
        )
    )
    ref = InstanceRef(backend=BackendId.MEMU, key=NAME)
    ctl = memu_module.MemuController(ref, config=_config(), _executor=spy)
    await ctl.create(CreateSpec(name="new-vm"))
    budgets = spy.seen_all["create"]
    assert bool(budgets)  # create + bounded observation, all under ONE budget
    assert all(b <= PROVISION_TIMEOUT for b in budgets if b is not None)
    assert abs((budgets[0] or 0.0) - PROVISION_TIMEOUT) < 0.1


@_sync
async def test_discovery_is_query_bounded(memu_module: Any) -> None:
    calls: list[list[str]] = []
    spy = _TimeoutSpy(_spawn=_spawn_factory(calls, _ok(stdout=ROWS_STOPPED)))  # type: ignore[arg-type]
    await memu_module.list_instances(_config(), _executor=spy)
    assert spy.seen["list_instances"] == QUERY_TIMEOUT


@_sync
async def test_invalid_timeout_rejected_before_io(memu_module: Any) -> None:
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _rows_responder([(0, ROWS_RUNNING)]))
    with pytest.raises(InvalidArgumentError):
        await ctl.start(timeout=0)
    assert calls == []


@_sync
async def test_cancellation_propagates_unmodified(memu_module: Any) -> None:
    """§2: Cancel = abandon; the caller's CancelledError is never caught,
    converted, or logged as an error."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, lambda argv: _FakeHandle(hang=True))
    task = asyncio.ensure_future(ctl.start())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@_sync
async def test_closed_controller_guards_gated_ops(memu_module: Any) -> None:
    """aclose() is idempotent and NEVER stops the emulator (transport-only)."""
    calls: list[list[str]] = []
    ctl = _make(memu_module, calls, _ok())
    await ctl.aclose()
    await ctl.aclose()  # idempotent
    with pytest.raises(ControllerClosedError):
        await ctl.start()
    with pytest.raises(ControllerClosedError):
        await ctl.click(1, 2)  # inherited mechanics guard on the same state
    assert calls == []

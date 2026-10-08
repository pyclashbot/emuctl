"""§7 conformance (BlueStacks 5, §1/§3/§4): exact Windows lifecycle argv,
the scoped-kill invariant, the bundled-HD-tool/registry walk, the conf-file
round trip + §4 renderer/custom-res translation, lazily-bound private-server
transport, discovery rows, scripted restart, and the family-isolation guard.

All offline (macOS dev box included): every Windows-only child replays
through ``ProcessExecutor(_spawn=...)`` (memu.py's recorded-transcript
tier); the ``winreg`` walk is replayed through a fake module planted in
``sys.modules``; conf/MIM fixtures live under ``tmp_path``. Vendor stdout
is consumed, never obeyed; tasklist CSV is the structured row tier (the
MEmu ``listvms`` precedent), and HD-Player/HD-Adb are fake-named via
``bluestacks.install_dir`` knobs — no real BlueStacks install is touched.
"""

from __future__ import annotations

import asyncio
import functools
import json
import struct
import sys
import types
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._platforms import Platform
from emuctl._proc import ProcessExecutor
from emuctl.config.model import (
    BlueStacksConfig,
    BlueStacksSettings,
    CreateSpec,
    DisplayConfig,
    EmuctlConfig,
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

KEY = "pyclashbot-136"
NAME_INTERNAL = "Tiramisu64"

#: ``bluestacks.conf`` fixture — real-look, two instances, one MIM-named.
CONF_TEXT = (
    'bst.custom_resolutions="418 x 633"\n'
    'bst.enable_adb_access="1"\n'
    'bst.instance.Tiramisu64.status.adb_port="5555"\n'
    'bst.instance.Tiramisu64.display_name="pyclashbot-136"\n'
    'bst.instance.Tiramisu64.graphics_renderer="dx"\n'
    'bst.instance.Tiramisu64.status.google_account_logins=""\n'
    'bst.instance.Pie64.status.adb_port="5565"\n'
)

#: MimMetaData fixture — the vendor's real-look Organization shape.
MIM_TEXT = json.dumps(
    {
        "Organization": [
            {
                "ID": 0,
                "Name": KEY,
                "IsFolder": False,
                "ParentFolder": -1,
                "IsOpen": False,
                "IsVisible": True,
                "InstanceName": NAME_INTERNAL,
            },
            {"ID": 1, "Name": "Pie64", "InstanceName": "Pie64"},
        ]
    }
)

#: tasklist /V /FO CSV output — header, a match (window title == KEY),
#: another HD-Player instance with a foreign title, and a malformed row.
TASKLIST_MATCH = (
    '"Image Name","PID","Session Name","Mem Usage","Status","User Name",'
    '"CPU Time","Window Title"\n'
    '"HD-Player.exe","4200","Console","123456 K","Running","user",'
    '"0:00:01","pyclashbot-136"\n'
    '"HD-Player.exe","4300","Console","999999 K","Running","user",'
    '"0:00:01","Some Other Instance"\n'
    '"broken,row\n'
)
TASKLIST_EMPTY = (
    '"Image Name","PID","Session Name","Mem Usage","Status","User Name","CPU Time","Window Title"\n'
)


def _sync(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    """Run an async test body on a fresh loop (repo convention)."""

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
def bt_module() -> Any:
    """Import the adapter module lazily (never at collection time)."""
    import importlib

    return importlib.import_module("emuctl.backends.bluestacks.bs5")


@pytest.fixture
def paths_module() -> Any:
    import importlib

    return importlib.import_module("emuctl.backends.bluestacks._paths")


def _png(width: int = 419, height: int = 633) -> bytes:
    """Minimal bytes png_dimensions accepts (signature + IHDR; no CRC)."""
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
        returncode: int | None = None,
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
        return self._returncode or 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return (self._stdout, self._stderr)


class _SpawnedHandle(_FakeHandle):
    """A detached GUI child: never exits within the test (rc stays None)."""


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
        head = Path(argv[0]).name.lower()
        if head == "hd-player.exe":
            return _SpawnedHandle()
        if head == "tasklist":
            return _FakeHandle(stdout=stdout, returncode=0)
        return _FakeHandle(stdout=stdout)

    return respond


class _FakeWinreg:
    """Fake ``winreg`` module: replays SOFTWARE\\BlueStacks_nxt values."""

    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.opened: list[tuple[str, str]] = []

    def ConnectRegistry(self, whom: str | None, hive: str) -> str:
        assert whom is None and hive == self.HKEY_LOCAL_MACHINE
        return "root"

    def OpenKey(self, root: str, key: str) -> _FakeWinreg:
        del root
        if key != r"SOFTWARE\BlueStacks_nxt":
            raise FileNotFoundError(key)
        self.opened.append((key, ""))
        return self

    def __enter__(self) -> _FakeWinreg:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def QueryValueEx(self, hkey: Any, name: str) -> tuple[str, str]:
        if name not in self._values:
            raise FileNotFoundError(name)
        self.opened.append((r"SOFTWARE\BlueStacks_nxt", name))
        return (self._values[name], "REG_SZ")


def _make_env(
    tmp_path: Path, *, with_mim: bool = False, conf_text: str = CONF_TEXT
) -> tuple[Path, Path, Path]:
    """Offline BlueStacks 5 store: InstallDir root (player + HD-Adb), conf,
    optional MIM metadata. Returns (root, conf_path, mim_path)."""
    root = tmp_path / "InstallDir"
    root.mkdir()
    (root / "HD-Player.exe").write_text("player")
    (root / "HD-Adb.exe").write_text("adb")
    conf = tmp_path / "bluestacks.conf"
    conf.write_text(conf_text, encoding="utf-8")
    mim = tmp_path / "Engine" / "UserData" / "MimMetaData.json"
    if with_mim:
        mim.parent.mkdir(parents=True)
        mim.write_text(MIM_TEXT, encoding="utf-8")
    return root, conf, mim


def _config(root: Path, conf: Path) -> EmuctlConfig:
    """Offline config: explicit knobs only (no winreg walk)."""
    return EmuctlConfig(bluestacks=BlueStacksConfig(install_dir=str(root), conf_path=str(conf)))


def _make(
    bt_module: Any,
    calls: list[list[str]],
    respond: Any,
    *,
    key: str = KEY,
    config: EmuctlConfig | None = None,
) -> Any:
    """Controller bound to KEY over a scripted transport."""
    ref = InstanceRef(backend=BackendId.BLUESTACKS, key=key)
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    return bt_module.BlueStacks5Controller(ref, config=config, _executor=executor)


# ── construction + declared set ──


def test_features_declare_boot_configure_and_inherited_adb(bt_module: Any) -> None:
    assert (
        frozenset(
            {
                CapabilityFlag.BOOT,
                CapabilityFlag.CONFIGURE,
                CapabilityFlag.SCREENSHOT,
                CapabilityFlag.INPUT,
                CapabilityFlag.APPS,
                CapabilityFlag.APK_INSTALL,
            }
        )
        == bt_module.BlueStacks5Controller.FEATURES
    )


def test_construction_binds_and_probes_nothing(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    assert ctl.identity == InstanceRef(backend=BackendId.BLUESTACKS, key=KEY)
    assert ctl.adb_path is None and ctl.adb_server_port == 5041  # §4 private server
    assert ctl._serial == "" and ctl._target is None and ctl._spawned is None
    assert calls == []  # no discovery, no boot, no probe at construction


def test_construction_rejects_bad_key_and_config(bt_module: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        _make(bt_module, [], _ok(), key="has\nnewline")
    with pytest.raises(InvalidArgumentError):
        _make(bt_module, [], _ok(), config="nope")  # ty: ignore[invalid-argument-type]


def test_deferred_surface_is_absent(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    for name in (
        "is_running",  # §8: public is_running deferred
        "screen_size",  # §8
        "run_vendor_cmd",  # §8: public vendor passthrough deferred
        "clone",  # §8: clone family deferred (v0.1)
        "pause",
        "resume",
        "snapshot",
        "stop_app",  # §8: public stop_app deferred
    ):
        assert not hasattr(ctl, name), name


def test_create_funnel_raises_typed_not_supported(bt_module: Any, tmp_path: Path) -> None:
    """No unattended BlueStacks provisioning verb exists: create() raises the
    derived FeatureNotSupportedError through the port funnel (never silent)."""
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(ctl.create(CreateSpec(name="new-vm")))
    assert excinfo.value.feature == "create"


# ── bundled-tool resolution: explicit knob → registry walk → typed refusal ──


def test_registry_walk_supplies_install_root(
    bt_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths_module: Any,
) -> None:
    """No explicit install_dir: the SOFTWARE\\BlueStacks_nxt InstallDir wins
    and callers pass tools through without a typed refusal."""
    root, conf, _mim = _make_env(tmp_path)
    fake = _FakeWinreg({"InstallDir": str(root), "DataDir": str(root / "Engine")})
    module = types.ModuleType("winreg")
    module.__dict__.update(
        HKEY_LOCAL_MACHINE=_FakeWinreg.HKEY_LOCAL_MACHINE,
        ConnectRegistry=fake.ConnectRegistry,
        OpenKey=fake.OpenKey,
        QueryValueEx=fake.QueryValueEx,
    )
    sys.modules["winreg"] = module  # replayed piece — evicted BELOW afterEach fixture
    try:
        monkeypatch.setattr(paths_module, "_platform", lambda: Platform.WINDOWS)
        calls: list[list[str]] = []
        cfg = EmuctlConfig(
            bluestacks=BlueStacksConfig(conf_path=str(conf))
        )  # no install_dir: registry resolution must do it
        ctl = _make(bt_module, calls, _ok(), config=cfg)
        asyncio.run(ctl._vendor_run(["shell", "echo", "x"]))
        assert Path(calls[0][0]) == root / "HD-Adb.exe"
        assert [name for _key, name in fake.opened if name] == ["InstallDir"]
    finally:
        sys.modules.pop("winreg", None)


def test_missing_player_binary_is_typed_refusal_before_io(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path)
    (root / "HD-Player.exe").unlink()
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    with pytest.raises(ExecutableNotFoundError) as excinfo:
        asyncio.run(ctl.start())
    assert excinfo.value.tool == "HD-Player.exe"
    assert any("install_dir" in item for item in excinfo.value.searched)
    assert any("BlueStacks_nxt" in item for item in excinfo.value.searched)
    assert calls == []  # refusal before any I/O


# ── BOOT mechanics: exact argv, detached launch, scoped kills ──


@_sync
async def test_start_argv_is_detached_launch(bt_module: Any, tmp_path: Path) -> None:
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    await ctl.start()
    assert calls == [[str(root / "HD-Player.exe"), "--instance", NAME_INTERNAL]]
    assert ctl._spawned is not None  # the handle is retained (GC pipe protection)


@_sync
async def test_start_argv_falls_back_to_conf_display_name(bt_module: Any, tmp_path: Path) -> None:
    """Key resolution without any MIM metadata: the conf's display_name maps
    to the internal name (the vendor's pinned fallback)."""
    root, conf, _mim = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    await ctl.start()
    assert calls == [[str(root / "HD-Player.exe"), "--instance", NAME_INTERNAL]]


@_sync
async def test_stop_argv_is_scope_pinned(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    await ctl.stop()
    # §2-sanctioned window-title-equal bound key + the tool's image filter.
    assert calls == [
        [
            "taskkill",
            "/FI",
            f"WINDOWTITLE eq {KEY}",
            "/IM",
            "HD-Player.exe",
            "/F",
        ]
    ]


@_sync
async def test_no_global_sweep_ever(bt_module: Any, tmp_path: Path) -> None:
    """The §2 invariant, argv-form: every kill call carries the bound-key
    discriminator (/FI WINDOWTITLE eq …) — a bare ``/IM`` sweep never exists."""
    root, conf, _mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    await ctl.stop()
    for argv in calls:
        if argv[0] == "taskkill":
            assert "/FI" in argv
            assert f"WINDOWTITLE eq {KEY}" in argv
            assert argv.index("/IM") > argv.index("/FI")
    joined = " ".join(calls[0])
    assert "stopall" not in joined  # the MEmu-style sweep is not a family tool


@_sync
async def test_stop_nonzero_rc_raises_honestly(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(stderr=b"process not found", returncode=128)

    ctl = _make(bt_module, calls, respond, config=_config(root, conf))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.stop()
    assert excinfo.value.output_reason == "exit"
    assert len(calls) == 1  # one bounded attempt, no internal retries (§2)


@_sync
async def test_stop_timeout_maps_typed(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(hang=True)

    ctl = _make(bt_module, calls, respond, config=_config(root, conf))
    with pytest.raises(OperationTimeoutError):
        await ctl.stop(timeout=0.05)


@_sync
async def test_is_running_adjudicates_window_title_rows(
    bt_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh controller (no owned handle): the structured tasklist rows
    decide; header/malformed rows skip; EXACT title match only."""
    root, conf, _mim = _make_env(tmp_path, with_mim=True)
    match = _make(bt_module, [], _ok(TASKLIST_MATCH.encode()), config=_config(root, conf))
    assert await match._is_running(None) is True
    empty = _make(
        bt_module,
        [],
        _ok(TASKLIST_EMPTY.encode()),
        key="Some Other Instance",
        config=_config(root, conf),
    )
    assert await empty._is_running(None) is False


@_sync
async def test_is_running_nonzero_rc_raises_not_false(bt_module: Any, tmp_path: Path) -> None:
    """A failed observation raises — it never masquerades as not running (§1.4)."""
    root, conf, _mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(stderr=b"access denied", returncode=5)

    ctl = _make(bt_module, calls, respond, config=_config(root, conf))
    with pytest.raises(VendorCommandError):
        await ctl._is_running(None)


@_sync
async def test_is_running_prefers_our_spawned_handle(bt_module: Any, tmp_path: Path) -> None:
    """After our detached start (rc stays None): the owned-handle truth —
    zero further subprocess I/O (the §2 our-spawned-PIDs case)."""
    root, conf, _mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    await ctl.start()
    assert await ctl._is_running(None) is True
    assert len(calls) == 1  # only the spawn — no tasklist walk


@_sync
async def test_stale_key_is_typed_instance_not_found(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), key="no-such-instance", config=_config(root, conf))
    with pytest.raises(InstanceNotFoundError):
        await ctl._vendor_run(["shell", "echo", "x"])
    assert calls == []


# ── transport: bundled HD-Adb over the private 5041 server ──


@_sync
async def test_vendor_run_prefix_and_serial_scope(bt_module: Any, tmp_path: Path) -> None:
    """HD-Adb binary + -H/-P private server + one -s per device verb; the
    serial derives from conf (status.adb_port), never a guessed port."""
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    out = await ctl._vendor_run(["shell", "echo", "hi"])
    assert out == ""
    assert calls == [
        [
            str(root / "HD-Adb.exe"),
            "-H",
            "127.0.0.1",
            "-P",
            "5041",
            "-s",
            "127.0.0.1:5555",
            "shell",
            "echo",
            "hi",
        ]
    ]


@_sync
async def test_server_verbs_never_scoped(bt_module: Any, tmp_path: Path) -> None:
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    await ctl._run_adb(  # the attach shape (restart's phase 4)
        ["connect", "127.0.0.1:5555"],
        binary=False,
        operation="restart",
        timeout=None,
        server_verb=True,
    )
    assert calls == [
        [str(root / "HD-Adb.exe"), "-H", "127.0.0.1", "-P", "5041", "connect", "127.0.0.1:5555"]
    ]


@_sync
async def test_missing_adb_port_is_typed_not_guessed(bt_module: Any, tmp_path: Path) -> None:
    """A conf without port keys must never produce a guessed serial: typed
    conf error at the first transport resolution."""
    root, conf, _mim = _make_env(
        tmp_path, conf_text='bst.other="1"\nbst.instance.Tiramisu64.display_name="pyclashbot-136"\n'
    )
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    with pytest.raises(ConfigValidationError) as excinfo:
        await ctl._vendor_run(["shell", "echo", "x"])
    assert excinfo.value.path == f"bst.instance.{NAME_INTERNAL}.adb_port"
    assert calls == []


# ── §4 translation: renderer tokens, AUTO pin, custom-res absorb (all
# assertions assert on the WRITTEN conf bytes — round-trip honesty) ──


ARENDERER = "bst.instance.Tiramisu64.graphics_renderer"


@_sync
async def test_configure_renderer_walk_with_autopin(bt_module: Any, tmp_path: Path) -> None:
    """AUTO → DIRECTX (§4 Windows pin); canonical spellings AND the exact
    vendor tokens (gl/dx/vlcn) encode; feats written round-trip verbatim."""
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    conf_text = conf.read_text(encoding="utf-8")

    await ctl.configure({"display.renderer": "auto"}, timeout=None)
    assert f'{ARENDERER}="dx"' in conf.read_text(encoding="utf-8")
    conf.write_text(conf_text, encoding="utf-8")  # reset between legs

    await ctl.configure({"display.renderer": "vulkan"}, timeout=None)
    assert f'{ARENDERER}="vlcn"' in conf.read_text(encoding="utf-8")
    conf.write_text(conf_text, encoding="utf-8")

    await ctl.configure({"display.renderer": "gl"}, timeout=None)
    assert f'{ARENDERER}="gl"' in conf.read_text(encoding="utf-8")


@_sync
async def test_configure_unknown_renderer_is_typed_before_write(
    bt_module: Any, tmp_path: Path
) -> None:
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    before = conf.read_text(encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"display.renderer": "direct3d"}, timeout=None)
    assert conf.read_text(encoding="utf-8") == before  # no partial write


@_sync
async def test_configure_custom_resolution_absorbed(bt_module: Any, tmp_path: Path) -> None:
    """§4 absorption: both width+height leaves collapse into the custom-res
    token list + custom_resolution_selected, with fb_* set to the leaves."""
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    await ctl.configure({"display.width": 860, "display.height": 633}, timeout=None)
    text = conf.read_text(encoding="utf-8")
    assert 'bst.instance.Tiramisu64.fb_width="860"' in text
    assert 'bst.instance.Tiramisu64.fb_height="633"' in text
    assert 'bst.instance.Tiramisu64.custom_resolution_selected="1"' in text
    assert 'bst.custom_resolutions="418 x 633; 860 x 633"' in text


@_sync
async def test_configure_single_dimension_absorbs_nothing(bt_module: Any, tmp_path: Path) -> None:
    """The absorb fires only with BOTH halves (a "W x H" token needs both;
    the missing half is never invented)."""
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    await ctl.configure({"display.width": 860}, timeout=None)
    text = conf.read_text(encoding="utf-8")
    assert 'bst.instance.Tiramisu64.fb_width="860"' in text
    assert "custom_resolution_selected" not in text
    assert text.count("bst.custom_resolutions") == 1  # the original entry only


@_sync
async def test_configure_unknown_and_foreign_keys(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    before = conf.read_text(encoding="utf-8")
    with pytest.raises(InvalidArgumentError):  # unknown head → caller misuse
        await ctl.configure({"bogus.leaf": 1}, timeout=None)
    with pytest.raises(FeatureNotSupportedError):  # known-canonical, not ours
        await ctl.configure({"resources.cpu_cap_pct": 50}, timeout=None)
    with pytest.raises(FeatureNotSupportedError):  # another section entirely
        await ctl.configure({"memu.settings.turbo": True}, timeout=None)
    assert conf.read_text(encoding="utf-8") == before


@_sync
async def test_configure_none_reapplies_bound_config(bt_module: Any, tmp_path: Path) -> None:
    """``spec=None``: every non-None leaf of the bound config (§1.8) — here
    bluestacks.settings.high_fps + a renderer."""
    root, conf, _mim = _make_env(tmp_path)
    cfg = EmuctlConfig(
        display=DisplayConfig(width=419, height=633),
        bluestacks=BlueStacksConfig(
            install_dir=str(root),
            conf_path=str(conf),
            settings=BlueStacksSettings(high_fps=True),
        ),
    )
    ctl = _make(bt_module, [], _ok(), config=cfg)
    await ctl.configure(None, timeout=None)
    text = conf.read_text(encoding="utf-8")
    assert 'bst.instance.Tiramisu64.enable_high_fps="1"' in text
    assert 'bst.instance.Tiramisu64.custom_resolution_selected="1"' in text


@_sync
async def test_configure_no_change_skips_write(bt_module: Any, tmp_path: Path) -> None:
    """A translated document equal to the current one writes NOTHING (no
    vendor churn)."""
    root, conf, _mim = _make_env(tmp_path)
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    # The renderer is already "dx" in the fixture — a no-change overlay.
    await ctl.configure({"display.renderer": "dx"}, timeout=None)
    assert conf.read_text(encoding="utf-8") == CONF_TEXT


@_sync
async def test_conf_round_trip_preserves_unknown_keys(bt_module: Any, tmp_path: Path) -> None:
    """Translate touches ONLY the leaves it owns; everything else survives
    byte-for-byte (lossless-on-known, loud-on-unknown)."""
    root, conf, _mim = _make_env(tmp_path, conf_text=CONF_TEXT + 'bst.weird.Key="no touch"\n')
    ctl = _make(bt_module, [], _ok(), config=_config(root, conf))
    await ctl.configure({"display.renderer": "vlcn"}, timeout=None)
    text = conf.read_text(encoding="utf-8")
    assert 'bst.weird.Key="no touch"' in text
    assert ARENDERER in text  # ...replaced in place, position preserved


@_sync
async def test_configure_missing_conf_is_typed_unreadable(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path)
    conf.unlink()
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), config=_config(root, conf))
    from emuctl.errors import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        await ctl.configure({"display.renderer": "dx"}, timeout=None)
    assert excinfo.value.reason == "unreadable"
    assert calls == []


# ── discovery rows (conf-FILE reads; offline; honest emptiness) ──


@_sync
async def test_list_instances_conf_rows(bt_module: Any, tmp_path: Path) -> None:
    """Key = the conf/MIM display name; internal + port ride details; rows
    are conf-truth only: running stays False (never state-parsed)."""
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    rows = await bt_module.list_instances(_config(root, conf))
    assert [row.ref.key for row in rows] == [KEY, "Pie64"]
    assert all(row.running is False for row in rows)
    assert all(row.serial is None for row in rows)
    assert dict(rows[0].details) == {"internal": NAME_INTERNAL, "adb_port": "5555"}
    assert dict(rows[1].details) == {"internal": "Pie64", "adb_port": "5565"}


@_sync
async def test_list_instances_missing_store_is_honest_empty(
    bt_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths_module: Any
) -> None:
    """No conf candidate (no registry, plain macOS/other host) → (): the
    honest vendor-missing row, never a raise, on every platform shape."""
    monkeypatch.setattr(paths_module, "_platform", lambda: Platform.WINDOWS)
    assert await bt_module.list_instances(EmuctlConfig()) == ()
    monkeypatch.setattr(paths_module, "_platform", lambda: Platform.LINUX)
    assert await bt_module.list_instances(EmuctlConfig()) == ()


@_sync
async def test_list_instances_missing_conf_file_is_empty(bt_module: Any, tmp_path: Path) -> None:
    """A resolvable store whose conf file is absent (cold/side-installed):
    enumerating = honest zero rows (configure, by contrast, raises)."""
    root, conf, _mim = _make_env(tmp_path)
    conf.unlink()
    assert await bt_module.list_instances(_config(root, conf)) == ()


@_sync
async def test_list_instances_malformed_conf_raises(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path, conf_text='good="1"\ngarbage line\n')
    with pytest.raises(ConfigValidationError):
        await bt_module.list_instances(_config(root, conf))
    # discover() isolates the probe failure into a row (root stays silent).


@_sync
async def test_get_instance_found_absent_foreign(bt_module: Any, tmp_path: Path) -> None:
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    cfg = _config(root, conf)
    found = await bt_module.get_instance(cfg, InstanceRef(backend=BackendId.BLUESTACKS, key=KEY))
    assert found is not None and found.ref.key == KEY
    absent = await bt_module.get_instance(
        cfg, InstanceRef(backend=BackendId.BLUESTACKS, key="nope")
    )
    assert absent is None
    foreign = await bt_module.get_instance(cfg, InstanceRef(backend=BackendId.MEMU, key=KEY))
    assert foreign is None


@_sync
async def test_get_instance_rejects_non_ref(bt_module: Any, tmp_path: Path) -> None:
    root, conf, _mim = _make_env(tmp_path)
    with pytest.raises(InvalidArgumentError):
        await bt_module.get_instance(_config(root, conf), "bluestacks:x")


# ── restart: the §1.6 wholesale override, scripted end-to-end ──


def _restart_responder(
    calls: list[list[str]], *, running_rows: bytes = TASKLIST_EMPTY.encode()
) -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name.lower()
        if head == "tasklist":
            return _FakeHandle(stdout=running_rows, returncode=0)
        if head == "hd-player.exe":
            return _SpawnedHandle()
        if head == "hd-adb.exe":
            if "screencap" in argv:
                return _FakeHandle(stdout=_png(), returncode=0)
            return _FakeHandle(returncode=0)
        return _FakeHandle()

    return respond


@_sync
async def test_restart_scripted_flow_order(bt_module: Any, tmp_path: Path) -> None:
    """Not-running → configure → detached start → hd-adb connect → settle
    screenshot; phases share the lifecycle budget; the connect is the
    private-server server-verb shape."""
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _restart_responder(calls), config=_config(root, conf))
    await ctl.restart(timeout=None)
    heads = [Path(a[0]).name.lower() for a in calls]
    assert heads == ["tasklist", "hd-player.exe", "hd-adb.exe", "hd-adb.exe", "hd-adb.exe"]
    assert "screencap" in calls[-1]
    assert "connect" in calls[2]
    assert ctl._serial == "127.0.0.1:5555"
    # the conf stayed byte-identical (the fixture already carries the bound
    # config's effective leaves — no-spin configure):
    assert conf.read_text(encoding="utf-8").startswith("bst.custom_resolutions")


@_sync
async def test_restart_stops_running_instance_scoped(bt_module: Any, tmp_path: Path) -> None:
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(
        bt_module,
        calls,
        _restart_responder(calls, running_rows=TASKLIST_MATCH.encode()),
        config=_config(root, conf),
    )
    await ctl.restart(timeout=None)
    heads = [Path(a[0]).name.lower() for a in calls]
    assert heads == [
        "tasklist",
        "taskkill",
        "hd-player.exe",
        "hd-adb.exe",
        "hd-adb.exe",
        "hd-adb.exe",
    ]
    assert f"WINDOWTITLE eq {KEY}" in calls[1]


@_sync
async def test_restart_with_app_and_ready(bt_module: Any, tmp_path: Path) -> None:
    """app launch + bounded pidof verify + the ready check all funnel the
    inherited ADB mechanics under the shared deadline."""
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name.lower()
        if head == "hd-player.exe":
            return _SpawnedHandle()
        if head == "hd-adb.exe":
            if "screencap" in argv:
                return _FakeHandle(stdout=_png(), returncode=0)
            if "pidof" in argv:
                return _FakeHandle(stdout=b"4242\n", returncode=0)
            return _FakeHandle(returncode=0)
        if head == "tasklist":
            return _FakeHandle(stdout=TASKLIST_EMPTY.encode(), returncode=0)
        return _FakeHandle()

    ctl = _make(bt_module, calls, respond, config=_config(root, conf))
    seen: list[bool] = []

    def ready(controller: Any) -> bool:
        seen.append(True)
        return True

    await ctl.restart(
        app="com.sky.dawn",
        ready=ready,
        timeout=None,  # type: ignore[arg-type]
    )
    tails = [" ".join(call[1:]) for call in calls if Path(call[0]).name.lower() == "hd-adb.exe"]
    assert any("monkey" in tail for tail in tails)
    assert any("pidof" in tail for tail in tails)  # bounded spawn-verify (§1)
    assert seen == [True]


@_sync
async def test_restart_ready_false_is_menu_not_reached(bt_module: Any, tmp_path: Path) -> None:
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _restart_responder(calls), config=_config(root, conf))
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        await ctl.restart(ready=lambda _ctl: False, timeout=None)  # type: ignore[misc,return-value]
    assert excinfo.value.reason == "menu_not_reached"


@_sync
async def test_restart_stale_identity_propagates_raw(bt_module: Any, tmp_path: Path) -> None:
    """A stale bound ref is IDENTITY failure (§3): it surfaces under its own
    name — never laundered into EmulatorNotReadyError."""
    root, conf, _mim = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(bt_module, calls, _ok(), key="no-such", config=_config(root, conf))
    with pytest.raises(InstanceNotFoundError):
        await ctl.restart(timeout=None)
    assert [a[0] for a in calls] == ["tasklist"]  # the observation is the only I/O


@_sync
async def test_closed_after_aclose_and_never_gets_stopped(bt_module: Any, tmp_path: Path) -> None:
    root, conf, mim = _make_env(tmp_path, with_mim=True)
    calls: list[list[str]] = []
    handle_holder: dict[str, _SpawnedHandle] = {}
    spawn_real = _spawn_factory(calls, _ok())

    async def spawn(argv: Sequence[str]) -> _FakeHandle:
        handle = await spawn_real(argv)
        if (
            isinstance(handle, _FakeHandle)
            and Path(argv[0]).name.lower() == "hd-player.exe"
            and isinstance(handle, _SpawnedHandle)
        ):
            handle_holder["proc"] = handle
        return handle

    executor = ProcessExecutor(_spawn=spawn)  # type: ignore[arg-type]
    ctl = bt_module.BlueStacks5Controller(
        InstanceRef(backend=BackendId.BLUESTACKS, key=KEY),
        config=_config(root, conf),
        _executor=executor,
    )
    await ctl.start()
    spawned = handle_holder["proc"]
    await ctl.aclose()
    assert ctl._spawned is None
    assert spawned.terminated is False and spawned.killed is False  # never stopped
    with pytest.raises(ControllerClosedError):
        await ctl.start()


# ── §7.10 family hygiene: no cross-family imports, conformance exports ──


def test_importing_bs5_imports_no_sibling_family(bt_module: Any) -> None:
    assert "emuctl.backends.memu" not in sys.modules
    for name in list(sys.modules):
        if (
            name.startswith("emuctl.backends.adb")
            or name.startswith("emuctl.backends.google_play")
            or name.startswith("emuctl.backends.avd")
        ):
            pytest.fail(f"family leakage: {name}")


def test_convention_members_exist(bt_module: Any) -> None:
    assert callable(bt_module.list_instances)
    assert callable(bt_module.get_instance)
    assert bt_module.BlueStacks5Controller.__name__ == "BlueStacks5Controller"

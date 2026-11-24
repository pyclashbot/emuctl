"""§7 conformance (BlueStacks Air, §1/§3/§4): the macOS tool resolution, the
cold-synthesized MIM metadata determinism, pgrep/pkill exit-status kill
discipline (scoped, no sweeps), VULKAN AUTO pin, one configured instance,
discovery honesty off-platform, and the family-isolation guard.

All offline: the app bundle is faked through ``bluestacks.install_dir``/
``conf_path`` (tmp_path); every child replays through
``ProcessExecutor(_spawn=...)``; no real BlueStacks.app is touched.
The vendor evidence (py-clash-bot bluestacks.py macOS branches) is cited
in the adapter's docstrings; the tests pin OUR reposed shapes.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRef
from emuctl._platforms import Platform
from emuctl._proc import ProcessExecutor
from emuctl.config.model import BlueStacksConfig, EmuctlConfig
from emuctl.errors import (
    ConfigError,
    ConfigValidationError,
    InstanceNotFoundError,
    InvalidArgumentError,
    VendorCommandError,
)
from emuctl.port import CapabilityFlag

KEY = "Tiramisu64"  # Air: the sole internal name (display name == internal cold)

CONF_TEXT = 'bst.instance.Tiramisu64.status.adb_port="5555"\n'


def _sync(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Any]:
    """Run an async test body on a fresh loop (repo convention)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _png(width: int = 419, height: int = 633) -> bytes:
    """Minimal bytes png_dimensions accepts (signature + IHDR; no CRC)."""
    import struct

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

    def finish(self, returncode: int = 0) -> None:
        """Simulate the child exiting on its own."""
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode or 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return (self._stdout, self._stderr)


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
def air_module() -> Any:
    import importlib

    return importlib.import_module("emuctl.backends.bluestacks.air")


@pytest.fixture
def paths_module() -> Any:
    import importlib

    return importlib.import_module("emuctl.backends.bluestacks._paths")


def _make_env(tmp_path: Path, *, conf_text: str = CONF_TEXT) -> tuple[Path, Path]:
    """Offline macOS bundle: Contents/MacOS tool dir + conf beside it."""
    tools = tmp_path / "Contents" / "MacOS"
    tools.mkdir(parents=True)
    (tools / "BlueStacks").write_text("player")
    (tools / "hd-adb").write_text("adb")
    conf = tmp_path / "bluestacks.conf"
    conf.write_text(conf_text, encoding="utf-8")
    return tools, conf


def _config(tools: Path, conf: Path) -> EmuctlConfig:
    return EmuctlConfig(bluestacks=BlueStacksConfig(install_dir=str(tools), conf_path=str(conf)))


def _spawn_factory(
    calls: list[list[str]],
    respond: Callable[[list[str]], _FakeHandle],
) -> Callable[[Sequence[str]], Awaitable[_FakeHandle]]:
    async def _spawn(argv: Sequence[str]) -> _FakeHandle:
        calls.append(list(argv))
        return respond(list(argv))

    return _spawn


def _make(
    air_module: Any,
    calls: list[list[str]],
    respond: Any,
    *,
    key: str = KEY,
    config: EmuctlConfig | None = None,
) -> Any:
    ref = InstanceRef(backend=BackendId.BLUESTACKS_AIR, key=key)
    executor = ProcessExecutor(_spawn=_spawn_factory(calls, respond))  # type: ignore[arg-type]
    return air_module.BlueStacksAirController(ref, config=config, _executor=executor)


def _discovery_executor(pgrep_rc: int = 1) -> ProcessExecutor:
    """Offline executor for the module-level discovery seams."""

    def respond(argv: list[str]) -> _FakeHandle:
        if Path(argv[0]).name == "pgrep":
            return _FakeHandle(returncode=pgrep_rc)
        return _FakeHandle()

    return ProcessExecutor(_spawn=_spawn_factory([], respond))  # type: ignore[arg-type]


class _SpawnedHandle(_FakeHandle):
    """A detached GUI child: rc stays None (alive)."""


def _air_responder(calls: list[list[str]], *, pgrep_rc: int = 1) -> Callable[[list[str]], _FakeHandle]:
    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name
        if head == "pgrep":
            return _FakeHandle(returncode=pgrep_rc)
        if head == "pkill":
            return _FakeHandle(returncode=0)
        if head == "BlueStacks":
            return _SpawnedHandle()
        if head == "hd-adb":
            if "screencap" in argv:
                return _FakeHandle(stdout=_png(), returncode=0)
            return _FakeHandle(returncode=0)
        return _FakeHandle()

    return respond


# ── construction + declared set ──


def test_features_declare_boot_configure_and_inherited_adb(air_module: Any) -> None:
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
        == air_module.BlueStacksAirController.FEATURES
    )


def test_construction_binds_and_probes_nothing(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    assert ctl.identity == InstanceRef(backend=BackendId.BLUESTACKS_AIR, key=KEY)
    assert ctl.adb_path is None and ctl.adb_server_port == 5041
    assert ctl._serial == "" and ctl._target is None and ctl._spawned is None
    assert calls == []


def test_construction_rejects_bad_key(air_module: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        _make(air_module, [], _air_responder([]), key="has\nnewline")


def test_deferred_surface_is_absent(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
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


# ── cold metadata synthesis: deterministic, never invented, never written ──


def test_cold_synthesis_is_deterministic_and_shape_honest(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    doc = air_module._conf.parse(conf.read_text(encoding="utf-8"))
    synth1 = air_module._meta.synthesize(doc)
    synth2 = air_module._meta.synthesize(doc)
    assert synth1 == synth2  # deterministic
    entry = synth1.entries[0]
    assert (entry.name, entry.internal, entry.is_open) == (KEY, KEY, False)
    # No vendor-store write ever happens (present file untouched; absent stays absent):
    meta_path = air_module._paths.mim_meta_path(BlueStacksConfig(install_dir=str(tools), conf_path=str(conf)))
    assert meta_path is not None and not meta_path.exists()
    conf.write_text(CONF_TEXT, encoding="utf-8")  # byte-identical after the walk


def test_present_mim_metadata_is_used_once_published(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    meta_path = air_module._paths.mim_meta_path(BlueStacksConfig(install_dir=str(tools), conf_path=str(conf)))
    assert meta_path is not None
    meta_path.parent.mkdir(parents=True)
    # A MIM Name differing from the internal name: display wins for the key.
    meta_path.write_text(
        '{"Organization": [{"Name": "Air Main", "InstanceName": "Tiramisu64"}]}',
        encoding="utf-8",
    )
    rows = asyncio.run(air_module.list_instances(_config(tools, conf), _executor=_discovery_executor()))
    assert [row.ref.key for row in rows] == ["Air Main"]


def test_malformed_mim_metadata_is_typed(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    meta_path = air_module._paths.mim_meta_path(BlueStacksConfig(install_dir=str(tools), conf_path=str(conf)))
    assert meta_path is not None
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        asyncio.run(air_module.list_instances(_config(tools, conf), _executor=_discovery_executor()))


# ── BOOT mechanics: detached launch + exit-status pgrep/pkill ──


@_sync
async def test_start_argv_is_detached_launch(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    await ctl.start()
    assert calls == [[str(tools / "BlueStacks"), "--instance", KEY]]


@_sync
async def test_start_stale_identity_raises_typed_zero_io(air_module: Any, tmp_path: Path) -> None:
    """§3's stale-ref rule in _start: a bound key that maps to NO instance
    raises the typed ``InstanceNotFoundError`` BEFORE any I/O — the vendor's
    launch-the-default-VM fallback is banned (identity rules)."""
    tools, conf = _make_env(tmp_path, conf_text='bst.other="1"\n')
    calls: list[list[str]] = []
    ctl = _make(
        air_module,
        calls,
        _air_responder(calls),
        key="no-such",
        config=_config(tools, conf),
    )
    with pytest.raises(InstanceNotFoundError):
        await ctl.start()
    assert calls == []  # zero I/O
    assert ctl._spawned is None


@_sync
@pytest.mark.parametrize(
    ("leaf", "value"),
    [("resources.cpus", 0), ("resources.memory_mb", 0), ("display.width", -1)],
)
async def test_configure_out_of_range_leaf_refused_before_io(
    air_module: Any, tmp_path: Path, leaf: str, value: int
) -> None:
    """§4 strict typed primitives (the memu ``_TABLE_INTS`` bounded-table
    precedent, shared family table): an out-of-range overlay leaf is a typed
    ``ConfigValidationError`` and NOTHING is written."""
    tools, conf = _make_env(tmp_path)
    before = conf.read_text(encoding="utf-8")
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    with pytest.raises(ConfigValidationError):
        await ctl.configure({leaf: value}, timeout=None)
    assert conf.read_text(encoding="utf-8") == before


@_sync
async def test_stop_argv_is_the_strict_bound_key_pattern(air_module: Any, tmp_path: Path) -> None:
    """§2's internal-instance-name discriminator: the pkill pattern carries
    the FULL launch argv shape (escaped) — never the vendor's broad
    `pkill -f BlueStacks` sweep."""
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    await ctl.stop()
    pkill = next(call for call in calls if call[0] == "pkill")
    assert pkill[1] == "-f"
    pattern = pkill[2]
    assert "\\ \\-\\-instance\\ Tiramisu64" in pattern  # the --instance arg, escaped
    assert KEY in pattern
    assert pattern.startswith("/Applications/") or "BlueStacks" in pattern
    assert "BlueStacks$" in pattern  # the bare app launch (Finder) alternative


@_sync
async def test_pgrep_pattern_scopes_to_the_player_and_instance(air_module: Any, tmp_path: Path) -> None:
    """Bare app launches and our ``--instance`` launch both count; a
    *different* instance, a sibling binary, or the vendor's bare
    ``BlueStacks`` substring sweep never does."""
    import re

    tools, conf = _make_env(tmp_path)
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    player = str(tools / "BlueStacks")
    pattern = ctl._pgrep_pattern(KEY)

    def matches(cmdline: str) -> bool:
        return re.search(pattern, cmdline) is not None

    assert matches(player)
    assert matches(f"{player} --instance {KEY}")
    assert matches(f"{player} --instance {KEY} --restarted")
    assert not matches(f"{player} --instance Tiramisu642")
    assert not matches(f"{player} --instance Pie64")
    assert not matches("/Applications/BlueStacks.app/Contents/Resources/BlueStacksAI/BlueStacksAI")
    assert not matches("/Applications/BlueStacks.app/Contents/MacOS/HD-Player --instance Tiramisu64")


@_sync
async def test_stop_and_running_finalize_never_sweep(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    await ctl._is_running(None)
    await ctl.stop()
    for argv in calls:
        if argv[0] in ("pgrep", "pkill"):
            assert "\\ \\-\\-instance\\ Tiramisu64" in argv[2]  # bound-key scoped


@_sync
async def test_stop_nonzero_rc_raises_honestly(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(returncode=1)  # pkill found nothing

    ctl = _make(air_module, calls, respond, config=_config(tools, conf))
    with pytest.raises(VendorCommandError) as excinfo:
        await ctl.stop()
    assert excinfo.value.output_reason == "exit"
    assert len(calls) == 1  # one bounded attempt, no internal retries


@_sync
async def test_stop_waits_for_the_player_to_exit(
    air_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signalled player can still be exiting; stop observes until gone so
    configure-while-stopped cannot race the vendor's conf rewrite."""
    monkeypatch.setattr(air_module, "_STOP_POLL_S", 0.0)
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    states = iter([0, 0, 1])  # running, running, gone

    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name
        if head == "pkill":
            return _FakeHandle(returncode=0)
        if head == "pgrep":
            return _FakeHandle(returncode=next(states, 1))
        return _FakeHandle()

    ctl = _make(air_module, calls, respond, config=_config(tools, conf))
    await ctl.stop(timeout=5)
    assert [Path(a[0]).name for a in calls] == ["pkill", "pgrep", "pgrep", "pgrep"]


@_sync
async def test_stop_timeout_is_operation_timeout(
    air_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from emuctl.errors import OperationTimeoutError

    monkeypatch.setattr(air_module, "_STOP_POLL_S", 0.0)
    tools, conf = _make_env(tmp_path)

    def respond(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(returncode=0)  # never exits

    ctl = _make(air_module, [], respond, config=_config(tools, conf))
    with pytest.raises(OperationTimeoutError):
        await ctl.stop(timeout=0.05)


@_sync
async def test_is_running_exit_status_only(air_module: Any, tmp_path: Path) -> None:
    """stdout is NEVER parsed: pgrep rc 0 → True, rc 1 → False, other → raise."""
    tools, conf = _make_env(tmp_path)
    match = _make(air_module, [], _air_responder([], pgrep_rc=0), config=_config(tools, conf))
    assert await match._is_running(None) is True
    empty = _make(air_module, [], _air_responder([], pgrep_rc=1), config=_config(tools, conf))
    assert await empty._is_running(None) is False

    def broken(argv: list[str]) -> _FakeHandle:
        return _FakeHandle(returncode=3)

    boom = _make(air_module, [], broken, config=_config(tools, conf))
    with pytest.raises(VendorCommandError):
        await boom._is_running(None)


@_sync
async def test_is_running_prefers_our_spawned_handle(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    await ctl.start()
    assert await ctl._is_running(None) is True
    assert len(calls) == 1  # the spawn only — no pgrep


@_sync
async def test_is_running_reobserves_after_our_spawn_exits(air_module: Any, tmp_path: Path) -> None:
    """A dead spawn handle is not proof of stopped (§1.4): the scoped pgrep
    observation decides (single-instance handover keeps the app alive)."""
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls, pgrep_rc=0), config=_config(tools, conf))
    await ctl.start()
    assert isinstance(ctl._spawned, _FakeHandle)
    ctl._spawned.finish()  # our child exited; another player may remain
    assert await ctl._is_running(None) is True
    assert [Path(a[0]).name for a in calls] == ["BlueStacks", "pgrep"]


@_sync
async def test_stale_key_is_typed_instance_not_found(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(
        air_module,
        calls,
        _air_responder(calls),
        key="no-such",
        config=_config(tools, conf),
    )
    with pytest.raises(InstanceNotFoundError):
        await ctl._vendor_run(["shell", "echo", "x"])


# ── transport ──


@_sync
async def test_vendor_run_prefix_and_serial_scope(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    await ctl._vendor_run(["shell", "echo", "hi"])
    assert calls == [
        [
            str(tools / "hd-adb"),
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
async def test_missing_bundle_is_typed_refusal_before_io(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    (tools / "hd-adb").unlink()
    calls: list[list[str]] = []
    cfg = _config(tools, conf)
    ctl = _make(air_module, calls, _air_responder(calls), config=cfg)
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — typed below
        await ctl._vendor_run(["shell", "echo", "x"])
    err = excinfo.value
    assert type(err).__name__ == "ExecutableNotFoundError"
    searched = getattr(err, "searched", [])
    assert any("BlueStacks.app" in item for item in searched)


# ── TRANSLATION (§4): VULKAN AUTO pin + shared family table ──


@_sync
async def test_configure_auto_resolves_vulkan(air_module: Any, tmp_path: Path) -> None:
    """§4: AUTO → VULKAN on the Air row (the vendor's macOS default, too)."""
    tools, conf = _make_env(tmp_path)
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    await ctl.configure({"display.renderer": "auto"}, timeout=None)
    assert 'bst.instance.Tiramisu64.graphics_renderer="vlcn"' in conf.read_text(encoding="utf-8")


@_sync
async def test_configure_unknown_renderer_is_typed(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    before = conf.read_text(encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        await ctl.configure({"display.renderer": "metal"}, timeout=None)
    assert conf.read_text(encoding="utf-8") == before


@_sync
async def test_configure_foreign_key_is_gated(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — typed below
        await ctl.configure({"bogus.leaf": 1}, timeout=None)
    assert type(excinfo.value).__name__ == "InvalidArgumentError"


@_sync
@pytest.mark.parametrize(
    "leaf",
    ["resources.cpu_cap_pct", "adb.port", "bluestacks.install_dir", "memu.vm_name"],
)
async def test_configure_canonical_untranslatable_deploys_the_gate(
    air_module: Any, tmp_path: Path, leaf: str
) -> None:
    """§4: canonical leaves this table cannot translate are a typed gate,
    never caller misuse and never a silent drop."""
    from emuctl.errors import FeatureNotSupportedError

    tools, conf = _make_env(tmp_path)
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    before = conf.read_text(encoding="utf-8")
    with pytest.raises(FeatureNotSupportedError):
        await ctl.configure({leaf: 1}, timeout=None)
    assert conf.read_text(encoding="utf-8") == before


@_sync
async def test_configure_unknown_leaf_under_known_head_is_caller_misuse(
    air_module: Any, tmp_path: Path
) -> None:
    tools, conf = _make_env(tmp_path)
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    with pytest.raises(InvalidArgumentError):
        await ctl.configure({"display.bogus": 1}, timeout=None)


@_sync
async def test_missing_conf_raises_unreadable(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    conf.unlink()
    ctl = _make(air_module, [], _air_responder([]), config=_config(tools, conf))
    with pytest.raises(ConfigError) as excinfo:
        await ctl.configure({"display.renderer": "auto"}, timeout=None)
    assert excinfo.value.reason == "unreadable"


# ── discovery (one configured instance; honest off-platform) ──


@_sync
async def test_list_instances_single_row(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    stopped = await air_module.list_instances(_config(tools, conf), _executor=_discovery_executor(pgrep_rc=1))
    assert [row.ref.key for row in stopped] == [KEY]
    assert stopped[0].running is False
    assert dict(stopped[0].details) == {"internal": KEY, "adb_port": "5555"}

    running = await air_module.list_instances(_config(tools, conf), _executor=_discovery_executor(pgrep_rc=0))
    assert running[0].running is True


@_sync
async def test_list_instances_missing_player_is_stopped_not_a_raise(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    (tools / "BlueStacks").unlink()
    rows = await air_module.list_instances(_config(tools, conf), _executor=_discovery_executor(pgrep_rc=0))
    assert rows[0].running is False  # nothing can be running without the player


@_sync
async def test_list_instances_off_mac_is_honest_empty(
    air_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths_module: Any,
) -> None:
    """Windows-host shape (a winreg-less resolution port on the Air row):
    no macOS candidates on pretend-Windows and pretend-Linux → honest ()"""
    monkeypatch.setattr(paths_module, "_platform", lambda: Platform.WINDOWS)
    assert await air_module.list_instances(EmuctlConfig()) == ()
    monkeypatch.setattr(paths_module, "_platform", lambda: Platform.LINUX)
    assert await air_module.list_instances(EmuctlConfig()) == ()


@_sync
async def test_get_instance_found_absent_foreign(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    cfg = _config(tools, conf)
    executor = _discovery_executor()
    found = await air_module.get_instance(
        cfg, InstanceRef(backend=BackendId.BLUESTACKS_AIR, key=KEY), _executor=executor
    )
    assert found is not None and found.ref.key == KEY
    assert (
        await air_module.get_instance(
            cfg, InstanceRef(backend=BackendId.BLUESTACKS_AIR, key="nope"), _executor=executor
        )
        is None
    )
    assert (
        await air_module.get_instance(cfg, InstanceRef(backend=BackendId.MEMU, key=KEY), _executor=executor)
        is None
    )


# ── restart: the §1.6 wholesale override, scripted end-to-end ──


@_sync
async def test_restart_scripted_phase_order(air_module: Any, tmp_path: Path) -> None:
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    await ctl.restart(timeout=None)
    heads = [Path(a[0]).name for a in calls]
    # pgrep (not running) → configure (file write, no subprocess) → detached
    # start → attach probe (connect + echo) → settle screencap → terminal screencap.
    assert heads == ["pgrep", "BlueStacks", "hd-adb", "hd-adb", "hd-adb", "hd-adb"]
    assert "connect" in calls[2]
    assert "echo" in calls[3]
    assert ctl._serial == "127.0.0.1:5555"


@_sync
async def test_restart_running_instance_stops_scoped(
    air_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(air_module, "_STOP_POLL_S", 0.0)
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    stopped = {"value": False}

    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name
        if head == "pgrep":
            return _FakeHandle(returncode=0 if not stopped["value"] else 1)
        if head == "pkill":
            stopped["value"] = True
            return _FakeHandle(returncode=0)
        if head == "BlueStacks":
            return _SpawnedHandle()
        if head == "hd-adb":
            if "screencap" in argv:
                return _FakeHandle(stdout=_png(), returncode=0)
            return _FakeHandle(returncode=0)
        return _FakeHandle()

    ctl = _make(air_module, calls, respond, config=_config(tools, conf))
    await ctl.restart(timeout=None)
    heads = [Path(a[0]).name for a in calls]
    assert heads == ["pgrep", "pkill", "pgrep", "BlueStacks", "hd-adb", "hd-adb", "hd-adb", "hd-adb"]
    assert "\\ \\-\\-instance\\ Tiramisu64" in calls[1][2]


@_sync
async def test_restart_waits_for_cold_boot_transport(
    air_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detached spawn returns before the guest answers ADB; restart must
    poll the transport (bounded) instead of one-shotting the screenshot."""
    monkeypatch.setattr(air_module, "_BOOT_POLL_S", 0.0)
    monkeypatch.setattr(air_module, "_PROBE_TIMEOUT_S", 1.0)
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    echoes = {"n": 0}

    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name
        if head == "BlueStacks":
            return _SpawnedHandle()
        if head == "pgrep":
            return _FakeHandle(returncode=1)
        if head == "pkill":
            return _FakeHandle(returncode=0)
        if head == "hd-adb":
            if "screencap" in argv:
                return _FakeHandle(stdout=_png(), returncode=0)
            if "echo" in argv:
                echoes["n"] += 1
                if echoes["n"] < 3:
                    return _FakeHandle(stderr=b"device not found", returncode=1)
            return _FakeHandle(returncode=0)
        return _FakeHandle()

    ctl = _make(air_module, calls, respond, config=_config(tools, conf))
    await ctl.restart(timeout=5)
    assert echoes["n"] == 3  # two not-yet-up probes, then ready


@_sync
async def test_restart_settle_retries_until_frame_decodes(
    air_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADB can answer while the framebuffer is still coming up: the settle
    phase retries until a screenshot decodes (§1.6)."""
    monkeypatch.setattr(air_module, "_BOOT_POLL_S", 0.0)
    monkeypatch.setattr(air_module, "_PROBE_TIMEOUT_S", 1.0)
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    screencaps = {"n": 0}

    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name
        if head == "BlueStacks":
            return _SpawnedHandle()
        if head == "pgrep":
            return _FakeHandle(returncode=1)
        if head == "pkill":
            return _FakeHandle(returncode=0)
        if head == "hd-adb":
            if "screencap" in argv:
                screencaps["n"] += 1
                if screencaps["n"] == 1:
                    return _FakeHandle(stderr=b"device offline", returncode=255)
                return _FakeHandle(stdout=_png(), returncode=0)
            return _FakeHandle(returncode=0)
        return _FakeHandle()

    ctl = _make(air_module, calls, respond, config=_config(tools, conf))
    await ctl.restart(timeout=5)
    assert screencaps["n"] == 3  # failed settle probe, decoded settle, terminal


@_sync
async def test_restart_ready_false_is_menu_not_reached(air_module: Any, tmp_path: Path) -> None:
    from emuctl.errors import EmulatorNotReadyError

    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    ctl = _make(air_module, calls, _air_responder(calls), config=_config(tools, conf))
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        await ctl.restart(ready=lambda _ctl: False, timeout=None)  # type: ignore[misc,return-value]
    assert excinfo.value.reason == "menu_not_reached"


@_sync
async def test_restart_with_app_and_ready(air_module: Any, tmp_path: Path) -> None:
    """Air's app-path leg (mirror of bs5's): ``restart(app=..., ready=...)``
    funnels start_app's launch + bounded spawn-verify under the shared
    deadline through hd-adb."""
    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> _FakeHandle:
        head = Path(argv[0]).name
        if head == "BlueStacks":
            return _SpawnedHandle()
        if head == "pgrep":
            return _FakeHandle(returncode=1)
        if head == "hd-adb":
            if "screencap" in argv:
                return _FakeHandle(stdout=_png(), returncode=0)
            if "pidof" in argv:
                return _FakeHandle(stdout=b"4242\n", returncode=0)
            return _FakeHandle(returncode=0)
        return _FakeHandle()

    ctl = _make(air_module, calls, respond, config=_config(tools, conf))

    def ready(_controller: Any) -> bool:
        return True

    await ctl.restart(app="com.sky.dawn", ready=ready, timeout=None)  # type: ignore[misc,return-value]
    tails = [" ".join(call[1:]) for call in calls if Path(call[0]).name == "hd-adb"]
    assert any("monkey" in tail for tail in tails)
    assert any("pidof" in tail for tail in tails)  # bounded spawn-verify (§1)
    assert "connect" in calls[2]


@_sync
async def test_closed_after_aclose(air_module: Any, tmp_path: Path) -> None:
    from emuctl.errors import ControllerClosedError

    tools, conf = _make_env(tmp_path)
    calls: list[list[str]] = []
    holder: dict[str, _SpawnedHandle] = {}
    base = _spawn_factory(calls, _air_responder(calls))

    async def spawn(argv: Sequence[str]) -> _FakeHandle:
        handle = await base(argv)
        if Path(argv[0]).name == "BlueStacks" and isinstance(handle, _SpawnedHandle):
            holder["proc"] = handle
        return handle

    ctl = air_module.BlueStacksAirController(
        InstanceRef(backend=BackendId.BLUESTACKS_AIR, key=KEY),
        config=_config(tools, conf),
        _executor=ProcessExecutor(_spawn=spawn),  # type: ignore[arg-type]
    )
    await ctl.start()
    spawned = holder["proc"]
    await ctl.aclose()
    assert ctl._spawned is None
    assert spawned.terminated is False and spawned.killed is False  # never stopped
    with pytest.raises(ControllerClosedError):
        await ctl.start()


# ── §7.10 family hygiene ──


def test_importing_air_imports_no_sibling_family(air_module: Any) -> None:
    assert "emuctl.backends.memu" not in sys.modules
    for name in sys.modules:
        if name.startswith(("emuctl.backends.adb", "emuctl.backends.google_play", "emuctl.backends.avd")):
            pytest.fail(f"family leakage: {name}")


def test_convention_members_exist(air_module: Any) -> None:
    assert callable(air_module.list_instances)
    assert callable(air_module.get_instance)
    assert air_module.BlueStacksAirController.__name__ == "BlueStacksAirController"

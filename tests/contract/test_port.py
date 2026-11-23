"""§7.6 conformance: port wiring gate, capability gates, restart/create (fake-backed)."""

import asyncio
import logging
import os
import struct
import time
from collections.abc import Sequence

import pytest

from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl.config.model import ConfigSpec, CreateSpec
from emuctl.errors import (
    ControllerClosedError,
    DeviceStateError,
    EmulatorNotReadyError,
    FeatureNotSupportedError,
    InvalidArgumentError,
    OperationTimeoutError,
    VendorCommandError,
)
from emuctl.port import (
    MECHANICS,
    PROBE_REASONS,
    AsyncEmulatorController,
    CapabilityFlag,
    DisplayState,
    ProbeResult,
    ReadyCheck,
)
from emuctl.timing import (
    APP_TIMEOUT,
    CONFIG_TIMEOUT,
    DEFAULT,
    INTERACTION_TIMEOUT,
    LIFECYCLE_TIMEOUT,
    PROVISION_TIMEOUT,
    QUERY_TIMEOUT,
    SNAPSHOT_TIMEOUT,
    TimeoutParam,
)

ALL_FLAGS = frozenset(
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


def _ref(key: str = "bound-vm") -> InstanceRef:
    return InstanceRef(backend=BackendId.ADB, key=key)


def _png(width: int = 800, height: int = 600) -> bytes:
    """Minimal bytes png_dimensions accepts (signature + IHDR; no CRC check)."""
    return (
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def _vendor_error(op: str = "op") -> VendorCommandError:
    return VendorCommandError(
        command=[op], returncode=1, output_reason="exit", stderr_excerpt="boom"
    )


class FullFake(AsyncEmulatorController):
    """Scripted port fake: records (mechanic, timeout); honors fail/delays."""

    FEATURES = ALL_FLAGS

    def __init__(
        self,
        ref: InstanceRef,
        *,
        running: bool = False,
        frame: bytes | None = None,
        installed: bool = True,
        existing: InstanceRecord | None = None,
        fail: dict[str, BaseException] | None = None,
        delays: dict[str, float] | None = None,
        attach_fail: BaseException | None = None,
    ) -> None:
        super().__init__(ref)
        self.calls: list[tuple[str, object]] = []
        self.configure_specs: list[ConfigSpec | None] = []
        self._running = running
        self._frame = _png() if frame is None else frame
        self._installed = installed
        self._existing = existing
        self._fail = dict(fail or {})
        self._delays = dict(delays or {})
        self._attach_fail = attach_fail

    async def _act(self, name: str, timeout: float | None) -> None:
        self.calls.append((name, timeout))
        if name in self._delays:
            await asyncio.sleep(self._delays[name])
        if name in self._fail:
            raise self._fail[name]

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        self.calls.append(("_vendor_run", tuple(argv)))
        return b""

    async def _is_running(self, timeout: float | None) -> bool:
        await self._act("_is_running", timeout)
        return self._running

    async def _start(self, timeout: float | None) -> None:
        await self._act("_start", timeout)

    async def _stop(self, timeout: float | None) -> None:
        await self._act("_stop", timeout)

    async def _create(self, spec: CreateSpec, timeout: float | None) -> InstanceRef:
        await self._act("_create", timeout)
        return InstanceRef(backend=self._ref.backend, key=spec.name)

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:
        self.configure_specs.append(spec)
        await self._act("_configure", timeout)
        return DisplayState(None, None, None)

    async def _screenshot(self, timeout: float | None) -> bytes:
        await self._act("_screenshot", timeout)
        return self._frame

    async def _click(
        self, x: int, y: int, *, count: int, interval: float, timeout: float | None
    ) -> None:
        await self._act("_click", timeout)

    async def _swipe(
        self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int, timeout: float | None
    ) -> None:
        await self._act("_swipe", timeout)

    async def _key(self, code: str | int, timeout: float | None) -> None:
        await self._act("_key", timeout)

    async def _text(self, value: str, timeout: float | None) -> None:
        await self._act("_text", timeout)

    async def _start_app(self, package: str, timeout: float | None) -> None:
        await self._act("_start_app", timeout)

    async def _is_app_installed(self, package: str, timeout: float | None) -> bool:
        await self._act("_is_app_installed", timeout)
        return self._installed

    async def _install_apk(self, apk_path: str | os.PathLike[str], timeout: float | None) -> None:
        await self._act("_install_apk", timeout)

    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> InstanceRecord | None:
        self.calls.append(("_get_instance", str(ref)))
        return self._existing

    def _attach(self) -> None:
        self.calls.append(("_attach", None))
        if self._attach_fail is not None:
            raise self._attach_fail
        return None


class BootOnlyFake(AsyncEmulatorController):
    """Minimal fake: BOOT only."""

    FEATURES = frozenset({CapabilityFlag.BOOT})

    def __init__(self, ref: InstanceRef, *, running: bool = False) -> None:
        super().__init__(ref)
        self.calls: list[tuple[str, object]] = []
        self._running = running

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        return b""

    async def _is_running(self, timeout: float | None) -> bool:
        self.calls.append(("_is_running", timeout))
        return self._running

    async def _start(self, timeout: float | None) -> None:
        self.calls.append(("_start", timeout))

    async def _stop(self, timeout: float | None) -> None:
        self.calls.append(("_stop", timeout))


class BareFake(AsyncEmulatorController):
    """No FEATURES (inherits empty); only the abstract seam implemented."""

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        return b""


class BootConfigureFake(AsyncEmulatorController):
    """BOOT + CONFIGURE, no SCREENSHOT: restart reaches the settle gate."""

    FEATURES = frozenset({CapabilityFlag.BOOT, CapabilityFlag.CONFIGURE})

    def __init__(self, ref: InstanceRef, *, running: bool = False) -> None:
        super().__init__(ref)
        self.calls: list[tuple[str, object]] = []
        self._running = running

    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
        return b""

    async def _is_running(self, timeout: float | None) -> bool:
        self.calls.append(("_is_running", timeout))
        return self._running

    async def _start(self, timeout: float | None) -> None:
        self.calls.append(("_start", timeout))

    async def _stop(self, timeout: float | None) -> None:
        self.calls.append(("_stop", timeout))

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:
        self.calls.append(("_configure", timeout))
        return DisplayState(None, None, None)


class BootConfigureScreenshotFake(BootConfigureFake):
    """BOOT + CONFIGURE + SCREENSHOT, no APPS: restart reaches the app gate."""

    FEATURES = frozenset({CapabilityFlag.BOOT, CapabilityFlag.CONFIGURE, CapabilityFlag.SCREENSHOT})

    async def _screenshot(self, timeout: float | None) -> bytes:
        self.calls.append(("_screenshot", timeout))
        return _png()


class GateOrderFake(BootOnlyFake):
    """Spy: records _require calls against mechanic execution order."""

    def __init__(self, ref: InstanceRef) -> None:
        super().__init__(ref)
        self.events: list[object] = []

    def _require(self, flag: CapabilityFlag) -> None:
        self.events.append(("require", flag))
        super()._require(flag)

    async def _start(self, timeout: float | None) -> None:
        self.events.append("start")
        await super()._start(timeout)


def _names(fake: FullFake | BootOnlyFake) -> list[str]:
    return [name for name, _ in fake.calls]


# ── flags / mechanics table ──


def test_capability_flags_pinned() -> None:
    assert {flag.value for flag in CapabilityFlag} == {
        "boot",
        "create",
        "configure",
        "screenshot",
        "input",
        "apps",
        "apk_install",
    }
    assert len(CapabilityFlag) == 7
    assert all(isinstance(flag.value, str) for flag in CapabilityFlag)


def test_mechanics_table_pinned() -> None:
    assert set(MECHANICS) == set(CapabilityFlag)
    assert MECHANICS[CapabilityFlag.BOOT] == ("_start", "_stop", "_is_running")
    assert MECHANICS[CapabilityFlag.CREATE] == ("_create",)
    assert MECHANICS[CapabilityFlag.CONFIGURE] == ("_configure",)
    assert MECHANICS[CapabilityFlag.SCREENSHOT] == ("_screenshot",)
    assert MECHANICS[CapabilityFlag.INPUT] == ("_click", "_swipe", "_key", "_text")
    assert MECHANICS[CapabilityFlag.APPS] == ("_start_app", "_is_app_installed")
    assert MECHANICS[CapabilityFlag.APK_INSTALL] == ("_install_apk",)
    table_names = {name for names in MECHANICS.values() for name in names}
    assert "_attach" not in table_names  # concrete hook, not a mechanic


def test_mechanics_exist_on_base() -> None:
    for names in MECHANICS.values():
        for name in names:
            assert callable(getattr(AsyncEmulatorController, name))


# ── ProbeResult ──


def test_probe_result_ok_discipline() -> None:
    probe = ProbeResult(ok=False, reason="no-adb")
    assert probe.ok is False
    assert probe.reason == "no-adb"
    assert bool(probe) is True  # 2-tuple trap: never truthy-check, check .ok
    assert ProbeResult(ok=True, reason="ok").ok is True


def test_probe_reason_vocabulary_pinned() -> None:
    assert isinstance(PROBE_REASONS, frozenset)
    assert "ok" in PROBE_REASONS
    assert "no-adb" in PROBE_REASONS
    # additive-only, but never speculative: "not-found" was cut until a
    # is_reachable/probe producer emits it.
    assert "not-found" not in PROBE_REASONS


def test_is_reachable_default_reports_unknown_without_io() -> None:
    fake = FullFake(_ref())
    assert asyncio.run(fake.is_reachable()) == ProbeResult(ok=False, reason="unknown")
    assert fake.calls == []


# ── wiring gate ──


def test_valid_subclass_accepted() -> None:
    assert isinstance(BootOnlyFake(_ref()), AsyncEmulatorController)


def test_missing_mechanics_fail_at_definition() -> None:
    with pytest.raises(TypeError) as excinfo:

        class MissingMechanics(AsyncEmulatorController):
            FEATURES = frozenset({CapabilityFlag.BOOT})

            async def _vendor_run(
                self, argv: Sequence[str], *, binary: bool = False
            ) -> bytes | str:
                return b""

            async def _start(self, timeout: float | None) -> None: ...

    message = str(excinfo.value)
    assert "_start" in message and "_stop" in message and "_is_running" in message


def test_stub_body_counts_as_missing() -> None:
    with pytest.raises(TypeError) as excinfo:

        class StubInput(AsyncEmulatorController):
            FEATURES = frozenset({CapabilityFlag.INPUT})

            async def _vendor_run(
                self, argv: Sequence[str], *, binary: bool = False
            ) -> bytes | str:
                return b""

            async def _click(
                self, x: int, y: int, *, count: int, interval: float, timeout: float | None
            ) -> None: ...

    assert "_click" in str(excinfo.value)


_UNWIRED_INPUT = NotImplementedError("unwired input")


def test_raise_not_implemented_counts_as_stub() -> None:
    with pytest.raises(TypeError) as excinfo:

        class NieInput(AsyncEmulatorController):
            FEATURES = frozenset({CapabilityFlag.INPUT})

            async def _vendor_run(
                self, argv: Sequence[str], *, binary: bool = False
            ) -> bytes | str:
                return b""

            async def _click(
                self, x: int, y: int, *, count: int, interval: float, timeout: float | None
            ) -> None:
                raise NotImplementedError("no click yet")

            async def _swipe(
                self,
                x1: int,
                y1: int,
                x2: int,
                y2: int,
                *,
                duration_ms: int,
                timeout: float | None,
            ) -> None:
                raise NotImplementedError

            async def _key(self, code: str | int, timeout: float | None) -> None:
                raise _UNWIRED_INPUT

            async def _text(self, value: str, timeout: float | None) -> None:
                raise NotImplementedError(f"no text for {value!r}")

    message = str(excinfo.value)
    assert "_click" in message
    assert "_swipe" in message
    assert "_key" in message
    assert "_text" in message


def test_one_line_delegation_counts_as_concrete() -> None:
    class DelegatingBoot(AsyncEmulatorController):
        FEATURES = frozenset({CapabilityFlag.BOOT})

        async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> bytes | str:
            return b""

        async def _start(self, timeout: float | None) -> None:
            await self._vendor_run(["start"])

        async def _stop(self, timeout: float | None) -> None:
            await self._vendor_run(["stop"])

        async def _is_running(self, timeout: float | None) -> bool:
            await self._vendor_run(["is-running"])
            return False

    assert isinstance(DelegatingBoot(_ref()), AsyncEmulatorController)


def test_create_without_get_instance_override_fails_at_definition() -> None:
    with pytest.raises(TypeError) as excinfo:

        class CreateWithoutSeam(AsyncEmulatorController):
            FEATURES = frozenset({CapabilityFlag.CREATE})

            async def _vendor_run(
                self, argv: Sequence[str], *, binary: bool = False
            ) -> bytes | str:
                return b""

            async def _create(self, spec: CreateSpec, timeout: float | None) -> InstanceRef:
                return InstanceRef(backend=self._ref.backend, key=spec.name)

    assert "_get_instance" in str(excinfo.value)


def test_bad_features_types_fail_at_definition() -> None:
    with pytest.raises(TypeError):
        type("StringFeatures", (AsyncEmulatorController,), {"FEATURES": {"boot"}})
    with pytest.raises(TypeError):
        type("ListFeatures", (AsyncEmulatorController,), {"FEATURES": [CapabilityFlag.BOOT]})


def test_concrete_inherited_mechanics_satisfy_gate() -> None:
    class Child(BootOnlyFake):
        pass

    assert isinstance(Child(_ref()), BootOnlyFake)


def test_vendor_run_is_abstract() -> None:
    class NoVendor(AsyncEmulatorController):
        pass

    with pytest.raises(TypeError):
        NoVendor(_ref())  # ty: ignore[call-non-callable]


# ── FEATURES / _require ──


def test_features_declared_io_free() -> None:
    fake = FullFake(_ref())
    assert fake.features == ALL_FLAGS
    assert isinstance(fake.features, frozenset)
    assert fake.has_feature(CapabilityFlag.BOOT) is True
    assert fake.calls == []  # no I/O
    slim = BootOnlyFake(_ref())
    assert slim.has_feature(CapabilityFlag.SCREENSHOT) is False
    assert BareFake(_ref()).features == frozenset()


def test_require_raises_typed_refusal() -> None:
    fake = BootOnlyFake(_ref())
    fake._require(CapabilityFlag.BOOT)  # declared: silent pass
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        fake._require(CapabilityFlag.SCREENSHOT)
    assert excinfo.value.feature == "screenshot"


def test_gate_first_ordering() -> None:
    fake = GateOrderFake(_ref())
    asyncio.run(fake.start())
    assert fake.events == [("require", CapabilityFlag.BOOT), "start"]


def test_gated_default_never_reaches_mechanic_when_undeclared() -> None:
    fake = BootOnlyFake(_ref())
    with pytest.raises(FeatureNotSupportedError):
        asyncio.run(fake.screenshot())
    assert fake.calls == []


# ── construction / close ──


def test_construction_binds_identity_without_io() -> None:
    ref = _ref()
    fake = FullFake(ref)
    assert fake.identity == ref
    assert fake.calls == []


def test_construction_rejects_non_ref() -> None:
    with pytest.raises(InvalidArgumentError):
        BootOnlyFake("adb:127.0.0.1:5555")  # ty: ignore[invalid-argument-type]


def test_construction_rejects_unknown_adapter_kwargs() -> None:
    class KwargsFake(BootOnlyFake):
        """Adapter-shaped: consumes known knobs, forwards the rest to the base."""

        def __init__(
            self, ref: InstanceRef, *, known_knob: str = "default", **kwargs: object
        ) -> None:
            self.known_knob = known_knob
            AsyncEmulatorController.__init__(self, ref, **kwargs)
            self.calls: list[tuple[str, object]] = []
            self._running = False

    with pytest.raises(InvalidArgumentError) as excinfo:
        KwargsFake(_ref(), adb_pth=5037)  # typo'd kwarg reaches the base
    assert excinfo.value.parameter == "adb_pth"
    assert KwargsFake(_ref(), known_knob="v").known_knob == "v"  # consumed knobs pass


def test_close_idempotent_and_guards_ops() -> None:
    ref = _ref()
    fake = BootOnlyFake(ref)
    asyncio.run(fake.aclose())
    asyncio.run(fake.aclose())
    asyncio.run(fake.close())
    with pytest.raises(ControllerClosedError):
        asyncio.run(fake.start())
    assert fake.identity == ref  # declared attrs survive close
    assert fake.has_feature(CapabilityFlag.BOOT) is True
    assert asyncio.run(fake.is_reachable()) == ProbeResult(ok=False, reason="unknown")


def test_no_del_finalizer() -> None:
    assert not hasattr(AsyncEmulatorController, "__del__")


# ── timeout groups (§2 table) ──


def test_timeout_groups_resolve_to_pinned_constants() -> None:
    fake = FullFake(_ref())
    asyncio.run(fake.screenshot())
    assert fake.calls[-1] == ("_screenshot", SNAPSHOT_TIMEOUT)
    asyncio.run(fake.click(1, 2))
    assert fake.calls[-1] == ("_click", INTERACTION_TIMEOUT)
    asyncio.run(fake.start())
    assert fake.calls[-1] == ("_start", LIFECYCLE_TIMEOUT)
    asyncio.run(fake.configure(None))
    assert fake.calls[-1] == ("_configure", CONFIG_TIMEOUT)
    asyncio.run(fake.start_app("com.example"))
    assert fake.calls[-1] == ("_start_app", APP_TIMEOUT)
    asyncio.run(fake.is_app_installed("com.example"))
    assert fake.calls[-1] == ("_is_app_installed", QUERY_TIMEOUT)
    asyncio.run(fake.create(CreateSpec(name="n")))
    # §2 one-deadline: _create receives the REMAINING provision slice (the
    # pre-check ran first) — ≤ the pinned constant, ≈ full at fake I/O speed
    create_slice = fake.calls[-1][1]
    assert fake.calls[-1][0] == "_create"
    assert isinstance(create_slice, float)
    # Windows timer granularity (~15.6 ms default tick) can read the
    # pre-check's elapsed as zero, leaving the slice at the deadline
    # float's own ulp — sometimes an ulp ABOVE the constant
    assert 0 < create_slice <= PROVISION_TIMEOUT + 1e-9
    assert create_slice > PROVISION_TIMEOUT - 0.1  # pre-check was ~free
    asyncio.run(fake.install_apk("game.apk"))
    assert fake.calls[-1] == ("_install_apk", APP_TIMEOUT)


def test_timeout_none_is_unbounded_passthrough() -> None:
    fake = FullFake(_ref())
    asyncio.run(fake.start(timeout=None))
    assert fake.calls[-1] == ("_start", None)


def test_timeout_rejected_before_io() -> None:
    fake = FullFake(_ref())
    with pytest.raises(InvalidArgumentError):
        asyncio.run(fake.start(timeout=0))
    assert fake.calls == []


def test_interaction_ops_delegate_to_mechanics() -> None:
    fake = FullFake(_ref())
    asyncio.run(fake.click(10, 20, count=2, interval=0.1))
    asyncio.run(fake.swipe(0, 0, 5, 5))
    asyncio.run(fake.key("KEYCODE_HOME"))
    asyncio.run(fake.text("hi"))
    asyncio.run(fake.install_apk("game.apk"))
    asyncio.run(fake.start_app("com.example"))
    assert asyncio.run(fake.is_app_installed("com.example")) is True
    for op in (
        "_click",
        "_swipe",
        "_key",
        "_text",
        "_install_apk",
        "_start_app",
        "_is_app_installed",
    ):
        assert op in _names(fake)


def test_configure_overlay_reaches_mechanic() -> None:
    fake = FullFake(_ref())
    overlay: ConfigSpec = {"display.width": 800}
    asyncio.run(fake.configure(overlay))
    assert fake.configure_specs == [overlay]
    assert fake.calls[-1] == ("_configure", CONFIG_TIMEOUT)


# ── restart() ──


def test_restart_phase_order_app_and_ready() -> None:
    seen: list[object] = []

    def ready(controller: AsyncEmulatorController) -> bool:
        seen.append(controller)
        return True

    fake = FullFake(_ref(), running=True)
    assert asyncio.run(fake.restart(app="com.example", ready=ready)) is None
    assert _names(fake) == [
        "_is_running",
        "_stop",
        "_configure",
        "_start",
        "_attach",
        "_screenshot",
        "_start_app",
    ]
    assert fake.configure_specs == [None]  # bound config re-applied while stopped
    assert seen == [fake]


def test_restart_skips_stop_when_not_running() -> None:
    fake = FullFake(_ref(), running=False)
    asyncio.run(fake.restart())
    assert _names(fake) == [
        "_is_running",
        "_configure",
        "_start",
        "_attach",
        "_screenshot",  # settle
        "_screenshot",  # terminal screenshot-decodes
    ]


def test_restart_terminal_spawn_present_when_app_without_ready() -> None:
    fake = FullFake(_ref(), running=False)
    asyncio.run(fake.restart(app="com.example"))
    assert _names(fake) == [
        "_is_running",
        "_configure",
        "_start",
        "_attach",
        "_screenshot",
        "_start_app",
        "_is_app_installed",
    ]


def test_restart_without_configure_capability_surfaces_gate() -> None:
    fake = BootOnlyFake(_ref(), running=False)
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(fake.restart())
    assert excinfo.value.feature == "configure"
    assert _names(fake) == ["_is_running"]  # stop skipped (not running); configure gate fires


def test_restart_without_screenshot_capability_surfaces_gate() -> None:
    fake = BootConfigureFake(_ref(), running=False)
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(fake.restart())
    assert excinfo.value.feature == "screenshot"
    assert [name for name, _ in fake.calls] == ["_is_running", "_configure", "_start"]


def test_restart_app_without_apps_capability_surfaces_gate_raw() -> None:
    # A strictly BOOT-only fake fails earlier at the configure gate; this fake
    # clears configure + settle so the APPS gate itself fires — raw, never
    # wrapped in EmulatorNotReadyError.
    fake = BootConfigureScreenshotFake(_ref(), running=False)
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(fake.restart(app="com.example"))
    assert excinfo.value.feature == "apps"


def test_restart_without_boot_gates_first() -> None:
    fake = BareFake(_ref())
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(fake.restart())
    assert excinfo.value.feature == "boot"


def test_restart_is_one_attempt_with_typed_failure() -> None:
    marker = _vendor_error("_start")
    fake = FullFake(_ref(), running=False, fail={"_start": marker})
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart())
    assert excinfo.value.reason == "start_failed"
    assert excinfo.value.cause is marker
    assert sum(1 for name, _ in fake.calls if name == "_start") == 1


@pytest.mark.parametrize(
    ("op", "reason", "app"),
    [
        ("_stop", "stop_failed", None),
        ("_configure", "configure_failed", None),
        ("_attach", "attach_failed", None),
        ("_start_app", "app_start_failed", "com.example"),
    ],
)
def test_restart_phase_failures_map_to_not_ready(op: str, reason: str, app: str | None) -> None:
    marker = _vendor_error(op)
    if op == "_attach":
        fake = FullFake(_ref(), running=True, attach_fail=marker)
    else:
        fake = FullFake(_ref(), running=True, fail={op: marker})
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart(app=app))
    assert excinfo.value.reason == reason
    assert excinfo.value.cause is marker


def test_restart_mid_phase_device_state_is_not_ready() -> None:
    marker = DeviceStateError(serial="emulator-5554", state="offline")
    fake = FullFake(_ref(), running=True, fail={"_stop": marker})
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart())
    assert excinfo.value.reason == "stop_failed"
    assert excinfo.value.cause is marker


def test_restart_mid_phase_operation_timeout_maps_to_timeout() -> None:
    marker = OperationTimeoutError(operation="restart", deadline=1.0, elapsed=1.0)
    fake = FullFake(_ref(), running=False, fail={"_start": marker})
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart())
    assert excinfo.value.reason == "timeout"
    assert excinfo.value.cause is marker


def test_restart_timeout_none_threads_none_into_every_phase() -> None:
    fake = FullFake(_ref(), running=True)
    assert asyncio.run(fake.restart(timeout=None)) is None
    assert _names(fake) == [
        "_is_running",
        "_stop",
        "_configure",
        "_start",
        "_attach",
        "_screenshot",  # settle
        "_screenshot",  # terminal screenshot-decodes
    ]
    assert fake.calls != []
    assert all(budget is None for _, budget in fake.calls)  # no deadline conversions


def test_restart_settle_rejects_undecodable_frame() -> None:
    fake = FullFake(_ref(), running=False, frame=b"not-a-png")
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart())
    assert excinfo.value.reason == "settle_failed"


def test_restart_terminal_absent_app_is_not_ready() -> None:
    fake = FullFake(_ref(), running=False, installed=False)
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart(app="com.example"))
    assert excinfo.value.reason == "app_not_installed"
    assert excinfo.value.cause is None


def test_restart_ready_false_is_menu_not_reached() -> None:
    fake = FullFake(_ref(), running=False)
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart(ready=lambda controller: False))
    assert excinfo.value.reason == "menu_not_reached"


def test_restart_ready_raise_is_ready_check_failed() -> None:
    marker = ValueError("menu probe blew up")

    def ready(controller: AsyncEmulatorController) -> bool:
        raise marker

    fake = FullFake(_ref(), running=False)
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart(ready=ready))
    assert excinfo.value.reason == "ready_check_failed"
    assert excinfo.value.cause is marker


def test_restart_async_ready_awaited() -> None:
    async def ready(controller: AsyncEmulatorController) -> bool:
        await asyncio.sleep(0.01)
        return True

    fake = FullFake(_ref(), running=False)
    assert asyncio.run(fake.restart(ready=ready)) is None


def test_restart_shared_deadline_enforced() -> None:
    fake = FullFake(_ref(), running=False, delays={"_is_running": 5.0})
    before = time.monotonic()
    with pytest.raises(EmulatorNotReadyError) as excinfo:
        asyncio.run(fake.restart(timeout=0.2))
    elapsed = time.monotonic() - before
    assert excinfo.value.reason == "timeout"
    assert isinstance(excinfo.value.cause, OperationTimeoutError)
    assert elapsed < 4.0


def test_restart_phases_share_one_shrinking_deadline() -> None:
    fake = FullFake(_ref(), running=True, delays={"_stop": 0.15})
    asyncio.run(fake.restart(timeout=1.0))
    slices = {name: budget for name, budget in fake.calls if isinstance(budget, float)}
    assert slices["_is_running"] == pytest.approx(1.0, abs=0.05)
    assert all(budget <= 1.0 for budget in slices.values())
    # Each slice is the remaining-budget arithmetic (deadline − measured now).
    # Windows timer granularity (≈15.6 ms default tick) can read a
    # sub-granularity slice as costing zero, so the measured slice equals the
    # previous one exactly — assert the shrink weakly, with an epsilon, not a
    # strict wall-clock `<`.
    assert slices["_stop"] <= slices["_is_running"] + 1e-6
    assert slices["_start"] <= slices["_stop"] - 0.05 + 1e-6


def test_restart_cancellation_propagates_unmodified() -> None:
    async def main() -> None:
        fake = FullFake(_ref(), running=False, delays={"_is_running": 30.0})
        task = asyncio.ensure_future(fake.restart(timeout=30.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_restart_rejects_non_callable_ready_before_io() -> None:
    fake = FullFake(_ref())
    with pytest.raises(InvalidArgumentError):
        asyncio.run(fake.restart(ready="menu"))  # ty: ignore[invalid-argument-type]
    assert fake.calls == []


def test_restart_on_closed_controller_is_closed_not_not_ready() -> None:
    fake = BootOnlyFake(_ref())
    asyncio.run(fake.aclose())
    with pytest.raises(ControllerClosedError):
        asyncio.run(fake.restart())


def test_restart_wholesale_override_allowed() -> None:
    class CustomRestart(BootOnlyFake):
        async def restart(
            self,
            *,
            app: str | None = None,
            ready: ReadyCheck | None = None,
            timeout: TimeoutParam = DEFAULT,
        ) -> None:
            self._require(CapabilityFlag.BOOT)
            return None

    assert asyncio.run(CustomRestart(_ref()).restart()) is None


# ── create() ──


def test_create_ignores_bound_identity_and_returns_new_ref() -> None:
    fake = FullFake(_ref("bound-vm"))
    new_ref = asyncio.run(fake.create(CreateSpec(name="fresh-vm")))
    assert new_ref == InstanceRef(backend=BackendId.ADB, key="fresh-vm")
    assert new_ref != fake.identity
    create_slice = next(v for name, v in fake.calls if name == "_create")
    # remaining slice of the ONE shared provision budget (§2), not a fresh one
    assert isinstance(create_slice, float)
    # Windows timer granularity (~15.6 ms default tick) can read the
    # pre-check's elapsed as zero, leaving the slice at the deadline
    # float's own ulp — sometimes an ulp ABOVE the constant
    assert 0 < create_slice <= PROVISION_TIMEOUT + 1e-9
    assert create_slice > PROVISION_TIMEOUT - 0.1


def test_create_precheck_refuses_existing_name() -> None:
    existing = InstanceRecord(
        ref=InstanceRef(backend=BackendId.ADB, key="taken"),
        running=False,
        serial=None,
        details={},
    )
    fake = FullFake(_ref(), existing=existing)
    with pytest.raises(InvalidArgumentError) as excinfo:
        asyncio.run(fake.create(CreateSpec(name="taken")))
    assert excinfo.value.parameter == "spec.name"
    assert excinfo.value.constraint == "already provisioned"
    assert all(name != "_create" for name, _ in fake.calls)


def test_create_without_capability_gates() -> None:
    fake = BareFake(_ref())
    with pytest.raises(FeatureNotSupportedError) as excinfo:
        asyncio.run(fake.create(CreateSpec(name="x")))
    assert excinfo.value.feature == "create"


def test_create_rejects_non_spec() -> None:
    fake = FullFake(_ref())
    with pytest.raises(InvalidArgumentError):
        asyncio.run(fake.create({"name": "x"}))  # ty: ignore[invalid-argument-type]
    assert fake.calls == []


# ── logging discipline ──


def test_port_logs_debug_only(caplog: pytest.LogCaptureFixture) -> None:
    fake = FullFake(_ref(), running=False)
    with caplog.at_level(logging.DEBUG, logger="emuctl.port"):
        asyncio.run(fake.restart())
    port_records = [record for record in caplog.records if record.name == "emuctl.port"]
    assert port_records != []
    assert all(record.levelno == logging.DEBUG for record in port_records)

"""§1/§3/§4/§6 — Google Play Games on PC backend adapter (Windows; ``[google-play]`` extra).

A concrete :class:`emuctl.port.AdbController` subclass (§6 sharing tiers: all
ADB-speaking adapters inherit the ONE middle layer) binding the bundled
Google Play ADB transport. Vendor evidence is py-clash-bot's
``pyclashbot/emulators/google_play.py`` (file:line cited throughout); where
that controller's shape is contract-BANNED (``shell=True`` strings,
image-wide ``taskkill /IM`` sweeps, stdout-prose control flow, sleeps and
unbounded retries), the same fact is reposed in the ``_proc.py`` executor's
argv-only, bounded terms.

Vendor-pinned facts (from the evidence file):

- Install root = registry
  ``SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GooglePlayGamesDeveloperEmulator``
  (then the ``GooglePlayGames`` twin) → ``InstallLocation``
  (google_play.py:234-271); ``Bootstrapper.exe`` sits at the root, the ADB
  binary at ``current/emulator/adb.exe`` and the service XML at
  ``current/service/Service.exe.config`` (google_play.py:52-80).
- The device is addressed over ADB at the fixed ``localhost:6520`` serial
  (``DEFAULT_DEVICE_SERIAL``, google_play.py:24-25); the bundled ADB speaks
  the standard local server protocol.
- ADB interaction (screenshot / input / app launch / app listing) is the
  shared ADB middle layer; ``install_apk`` is explicitly unimplemented in
  the vendor adapter (google_play.py:432-437) and the CONTRACT pins
  APK_INSTALL off for Google Play.

Interpretation decisions (CONTRACT wins; these fill its silences):

- **No BOOT / CREATE / APK_INSTALL.** The vendor's only shutdown path is a
  list of image-wide ``taskkill /f /im`` sweeps over ``crosvm.exe``,
  ``Service.exe``, ``client.exe``, ``gpu_check.exe``, ``adbproxy.exe`` and
  ``adb.exe`` (google_play.py:474-499). §2 bans sweeps without a
  bound-instance discriminator (``taskkill /IM …``-style), and no
  per-instance stop verb exists in the evidence. Provisioning likewise has
  no unattended vendor verb (``create``/``configure`` raise
  ``NotImplementedError``, google_play.py:281-292). Declaring BOOT/CREATE
  would therefore promise operations this adapter cannot perform
  contract-compliantly; their gates derive a typed
  ``FeatureNotSupportedError`` instead. ``restart()`` stays unavailable
  until a per-instance stop surface is evidenced.
- **CONFIGURE is display geometry only.** The shared ADB middle layer owns
  ``wm size`` / ``wm density`` (the §4 ADB translation); the vendor's
  ``EmulatorGpuGuestAngle`` XML renderer settings are NOT config leaves and
  require a stopped emulator, which this adapter cannot honestly provide.
  A backend-specific renderer table is therefore not invented.
- The bound ref key IS the ADB serial (identity, §3); a serial-shaped key
  is validated by the registry row's ``validate_serial``. The
  ``google_play.device_serial`` knob only feeds discovery; it never
  overrides an identity the caller bound (the MEmu ``vm_name`` rule).
- ``list_instances`` enumerates the configured serial as a plausibly
  present row: the ADB ``devices`` table supplies the observed state when
  the bundled/system ADB is resolvable, and the row stays
  ``running=False`` when the emulator is stopped. No vendor process probe
  and no stdout prose are consulted for control flow.
- Windows gating truth is the §3 registry row ALONE; path resolution
  returns honest Nones off-Windows, typed refusals fire at use time.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from emuctl._adb import _server_host_args, _stderr_excerpt
from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl._serials import is_valid_serial, validate_serial
from emuctl.config.model import EmuctlConfig, GooglePlayConfig
from emuctl.errors import (
    ExecutableNotFoundError,
    InvalidArgumentError,
    VendorCommandError,
)
from emuctl.log import sanitize
from emuctl.port import AdbController as _AdbMiddleLayer
from emuctl.port import CapabilityFlag
from emuctl.timing import (
    DEFAULT,
    QUERY_TIMEOUT,
    TimeoutParam,
    resolve_timeout,
)

__all__ = ["GooglePlayController", "list_instances", "get_instance"]

logger = logging.getLogger(__name__)

#: Vendor registry keys carrying ``InstallLocation`` (in order; google_play.py:248-251).
_REGISTRY_KEYS: Final[tuple[str, ...]] = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\GooglePlayGamesDeveloperEmulator",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\GooglePlayGames",
)

#: Bundled ADB location under the install root (google_play.py:71-74).
_ADB_REL: Final[tuple[str, ...]] = ("current", "emulator", "adb.exe")

#: Vendor default device serial (google_play.py:24-25).
_DEFAULT_SERIAL: Final[str] = "localhost:6520"

_DEVICES_HEADER: Final[str] = "List of devices attached"
_BANNER_MARKER: Final[str] = "*"


def _require_config(config: EmuctlConfig | None) -> EmuctlConfig:
    """Default programmatic consumers to a bare config (files are sugar)."""
    if config is None:
        return EmuctlConfig()
    if not isinstance(config, EmuctlConfig):
        raise InvalidArgumentError(
            parameter="config",
            constraint="must be an EmuctlConfig or None",
        )
    return config


def _gp_config(cfg: EmuctlConfig) -> GooglePlayConfig:
    return cfg.google_play if cfg.google_play is not None else GooglePlayConfig()


def _registry_install_root() -> str | None:
    """Probe the vendor uninstall keys for ``InstallLocation`` (no raises).

    ``winreg`` is Windows-only stdlib — on other hosts this is an honest
    no-candidate, never a raise (google_play.py:234-271 mirrored without its
    broad exception swallowing beyond the OSError surface winreg raises).
    """
    try:
        winreg: Any = importlib.import_module("winreg")  # noqa: PLC0415 — Windows-only stdlib
    except ImportError:
        return None
    try:
        root = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
    except OSError:
        return None
    for key in _REGISTRY_KEYS:
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, "InstallLocation")
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _install_root(gp: GooglePlayConfig) -> Path | None:
    """Explicit ``google-play.install_dir`` → registry → honest None."""
    if gp.install_dir:
        return Path(gp.install_dir)
    location = _registry_install_root()
    return Path(location) if location is not None else None


def _adb_path(cfg: EmuctlConfig) -> str | None:
    """ADB binary: explicit Google Play path → system ``adb.path`` → bundle."""
    gp = _gp_config(cfg)
    if gp.adb_path:
        return gp.adb_path
    if cfg.adb.path:
        return cfg.adb.path
    root = _install_root(gp)
    if root is None:
        return None
    candidate = root.joinpath(*_ADB_REL)
    return str(candidate) if candidate.is_file() else None


def _searched_adb() -> list[str]:
    searched = [
        "google-play.adb_path (explicit)",
        "adb.path (system selector)",
    ]
    searched.extend(f"registry {key}\\InstallLocation\\current\\emulator\\adb.exe" for key in _REGISTRY_KEYS)
    return searched


def _discovery_argv(cfg: EmuctlConfig, adb: str) -> list[str]:
    return [adb, *_server_host_args(cfg.adb.host), "-P", str(cfg.adb.port), "devices"]


def _device_states(stdout: bytes) -> dict[str, str]:
    """Parse the standard ``adb devices`` table (machine-readable, not prose).

    Banner/header/blank lines are skipped; rows whose serial fails the §3
    serial allowlist are skipped, never laundered.
    """
    text = stdout.decode("utf-8", errors="replace")
    states: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_BANNER_MARKER) or stripped == _DEVICES_HEADER:
            continue
        parts = stripped.split()
        if len(parts) < 2 or not is_valid_serial(parts[0]):
            continue
        states[parts[0]] = parts[1]
    return states


async def list_instances(
    config: EmuctlConfig, *, _executor: ProcessExecutor | None = None
) -> tuple[InstanceRecord, ...]:
    """Enumerate the configured Google Play device as one plausibly-present row.

    Read-only, bounded (query budget). No resolvable ADB binary on this host
    (no bundle, no registry, no explicit path) is an honest empty
    enumeration — never a raise. The ADB ``devices`` table supplies the
    observed state when available; a stopped emulator is a ``running=False``
    row, never a fabricated serial.
    """
    cfg = _require_config(config)
    serial = _gp_config(cfg).device_serial
    adb = _adb_path(cfg)
    if adb is None:
        return ()
    executor = _executor if _executor is not None else ProcessExecutor()
    argv = _discovery_argv(cfg, adb)
    logger.debug("google-play op=list_instances argv=%s", [sanitize(a) for a in argv])
    result: ProcessResult = await executor.run(argv, operation="list_instances", timeout=QUERY_TIMEOUT)
    if result.returncode != 0:
        raise VendorCommandError(
            command=argv,
            returncode=result.returncode,
            output_reason="exit",
            stderr_excerpt=_stderr_excerpt(result.stderr),
        )
    states = _device_states(result.stdout)
    state = states.get(serial)
    details = {"adb_state": state} if state is not None else {}
    return (
        InstanceRecord(
            ref=InstanceRef(backend=BackendId.GOOGLE_PLAY, key=serial),
            running=state == "device",
            serial=serial,
            details=details,
        ),
    )


async def get_instance(
    config: EmuctlConfig, ref: InstanceRef, *, _executor: ProcessExecutor | None = None
) -> InstanceRecord | None:
    """The record for ``ref`` or ``None`` when absent (never raises it).

    Non-Google-Play refs are not ours → ``None`` (no cross-backend fan-out).
    """
    if not isinstance(ref, InstanceRef):
        raise InvalidArgumentError(
            parameter="ref",
            constraint="must be an InstanceRef",
        )
    if ref.backend is not BackendId.GOOGLE_PLAY:
        return None
    for record in await list_instances(config, _executor=_executor):
        if record.ref.key == ref.key:
            return record
    return None


class GooglePlayController(_AdbMiddleLayer):
    """The concrete Google Play Games adapter (Windows).

    Declares SCREENSHOT / INPUT / APPS (inherited ADB mechanics) plus
    CONFIGURE (inherited ADB display geometry). BOOT / CREATE / APK_INSTALL
    are deliberately absent: the evidence exposes no contract-compliant
    per-instance stop, provisioning verb, or APK install path (see the
    module docstring); their gates raise the derived
    ``FeatureNotSupportedError``.
    """

    FEATURES: frozenset[CapabilityFlag] = frozenset(
        {
            CapabilityFlag.SCREENSHOT,
            CapabilityFlag.INPUT,
            CapabilityFlag.APPS,
            CapabilityFlag.CONFIGURE,
        }
    )

    def __init__(
        self,
        ref: InstanceRef,
        *,
        config: EmuctlConfig | None = None,
        _executor: ProcessExecutor | None = None,
    ) -> None:
        """Bind the ref + resolved config. No discovery, no boot, no probe.

        ``_executor`` is the §6 injection seam (recorded-fixture tier). The
        bundled ADB path resolves lazily on the first ADB call; the bound
        ref key is THE transport serial.
        """
        super().__init__(ref)
        cfg = _require_config(config)
        validate_serial(ref.key)
        self._config = cfg
        gp = _gp_config(cfg)
        self._gp = gp
        self.adb_path: str | None = gp.adb_path or cfg.adb.path
        self.adb_server_port: int | None = cfg.adb.port
        self._adb_host: str = cfg.adb.host
        self._serial: str = ref.key
        self._executor = _executor if _executor is not None else ProcessExecutor()
        logger.debug("bind google-play ref=%s", sanitize(str(ref)))

    def _resolve_adb(self) -> str:
        if self.adb_path:
            return self.adb_path
        resolved = _adb_path(self._config)
        if resolved is None:
            raise ExecutableNotFoundError(
                tool="adb",
                searched=_searched_adb(),
                detail=(
                    "the Google Play Games emulator's bundled adb was not found "
                    "(set google-play.adb_path, google-play.install_dir, or adb.path)"
                ),
            )
        self.adb_path = resolved
        return resolved

    async def _run_adb(  # type: ignore[override]
        self,
        tail: Sequence[str],
        *,
        binary: bool,
        operation: str,
        timeout: float | None,
        server_verb: bool = False,
    ) -> bytes | str:
        """Middle-layer runner with the bundled-ADB lazy resolution.

        Scoping, the per-call serial re-check, and cancel discipline are
        inherited unchanged; the ``-P`` prefix always carries the resolved
        server port and ``-H`` appears only for a non-loopback ``adb.host``.
        """
        self._resolve_adb()
        return await super()._run_adb(
            tail,
            binary=binary,
            operation=operation,
            timeout=timeout,
            server_verb=server_verb,
        )

    async def _vendor_run(
        self,
        argv: Sequence[str],
        *,
        binary: bool = False,
        timeout: TimeoutParam = DEFAULT,
    ) -> bytes | str:
        """Ungated internal seam: the bundled ADB transport."""
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="vendor_run")
        return await self._run_adb(argv, binary=binary, operation="vendor_run", timeout=budget)

    async def aclose(self) -> None:
        """Idempotent; releases transport only (never stops the emulator)."""
        await self._executor.close()
        await super().aclose()

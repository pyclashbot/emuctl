"""§1/§3/§4/§6 — MEmu backend adapter (Windows; ``[memu]`` extra).

The concrete :class:`MemuController` subclasses the CORE-side
:class:`emuctl.port.AdbController` middle layer (§6 sharing tiers: "no
second middle layer for MEmu's memuc transport"): SCREENSHOT / INPUT /
APPS / APK_INSTALL are inherited ADB mechanics over the resolved
``127.0.0.1:<port>`` transport, while this module adds the memuc-specific
BOOT / CREATE / CONFIGURE mechanics on top. The ``[memu]`` extra's
``pymemuc`` dependency is a find_spec marker only — the vendored wrapper
is never imported here: its subprocess handling predates §2 (unbounded by
default), its logging configures stream handlers as an import side
effect (§7.4 bans that), and ``stopall``-style surfaces are contract-
banned global sweeps.

memuc argv discipline (vendor evidence, pymemuc source):

- ``memuc [-i <index> | -n <name>] <verb>`` — selector BEFORE the verb
  (pymemuc/_control.py:42-45 start, :60-64 stop; pymemuc/_manage.py:338-341
  getconfigex, :372-377 setconfigex).
- ``start [-b]`` (headless boot; pymemuc/_control.py:49-50), ``stop``
  (instance-scoped: ``-n <name>`` is the ONLY sanctioned kill surface —
  pymemuc's ``stopall`` (pymemuc/_control.py:96) and py-clash-bot's
  ``taskkill``-style psutil sweep are contract-BANNED global sweeps).
- ``create <version>`` (pymemuc/_manage.py:22-40); v0 pins the default
  image ``"136"`` (Android 13; §4 CreateSpec — the working py-clash-bot
  spelling, ``ANDROID_VERSION = "136"``, above pymemuc's stale
  ``"76"/"96"`` Literal). A fresh VM gets a vendor auto-name, so
  ``_create`` renames it to ``spec.name`` via ``-i <index> rename
  <name>`` (pymemuc/_manage.py:160-195) before returning the
  ``memu:<name>`` ref — ``-i`` is the bootstrap handle for a
  not-yet-stable-name VM; every later op binds ``-n`` (identity is the
  registry row's pinned rule: the MEmu VM NAME, referential across
  stop/start).
- ``listvms`` rows ``index,title,top_level,running(0/1),pid``,
  comma-separated (pymemuc/_manage.py:285-305): the sanctioned
  machine-readable surface. Rows are parsed for ENUMERATION only (§3);
  ``running`` is read from the running column; unparseable lines
  (including memuc's ``read failed`` no-VMs noise) are skipped, never
  laundered. Titles containing commas break the vendor's own row shape
  (a documented CLI caveat, not a parser choice).
- ``memuc`` exposes NO machine-readable ADB-port surface — pymemuc's
  ``get_adb_connection`` resolves ports only by parsing ``connected to
  …`` stdout prose (pymemuc/_command.py:662-696), which §1.9 bans — so
  the transport serial follows the MEmu ADB-ports convention: instance
  ``i`` listens on ``127.0.0.1:21503 + 10*i``. DOCUMENTED-INTERPRETATION
  status, stated honestly: 21503 as the first-instance default is pinned
  as THIS MODULE's interpretation — no vendored citation exists in
  pymemuc's source or docs, and live MEmu Windows evidence is still
  pending; stride 10 is the multi-instance convention. The constant stays
  single-sited and every wrong-port failure is typed (VendorCommandError
  via the probe/runner), never silent.

Interpretation decisions (CONTRACT wins; these fill its silences):

- ``restart()`` is a §1.6-sanctioned WHOLESALE override: the port's
  one-attempt phase chain (stop-if-running → configure-while-stopped →
  start → connect-attach → settle → [app] → terminal) with the vendor
  ordering inserted as an extra attach step. The port's ``_attach`` hook is
  a SYNCHRONOUS no-op (never awaited) and is deliberately NOT invoked here —
  the ``adb connect 127.0.0.1:<port>`` server registration is inherently
  async I/O, so the override's awaited ``attach_failed``-mapped phase
  replaces it entirely. Plain ``start()`` promises boot only, never ADB
  readiness (restart() is THE verified boot primitive; contract §3
  boundary).
- ``_is_running`` observes the running COLUMN of ``listvms`` rows (bound
  name → row); a bound name absent from the rows is a stale ref ⇒
  ``InstanceNotFoundError`` (§3 identity lifetime), never a laundered
  False (§1.4: a failed observation raises, it never becomes data).
- ``get_instance`` (the discovery seam) returns a row-keyed record; the
  controller's ``_get_instance`` pre-check/transport-derivation seam
  funnels through the SAME ``listvms`` row fetch — and honors the
  CALLER'S deadline (the provision budget on create()'s pre-check, the
  op slice on lazy transport derivation; a fresh query-group hard-code
  would violate §2's one-deadline rule), unlike the module-level
  ``get_instance`` discovery function, which stays query-bounded (§3).
- The transport probe (``is_reachable``) is INHERITED from the core-side
  middle layer (§6 promotion tier): the echo probe, the probe word, and
  the stderr-failure classifier live once in
  ``emuctl.port.AdbController``. MEmu's behavior survives through its
  ``_run_adb`` lazy-resolution override: a stale bound ref surfaces as
  the identity error during lazy resolution inside ``_run_adb`` and maps
  through the shared catch-all to "disconnected" (data, never a raise).
- Creation success is observed, never parsed: after ``memuc create``
  (returncode gate only — memuc's ``SUCCESS`` prose is consumed, never
  obeyed), a bounded ``wait_until`` poll of the ``listvms`` rows waits
  for a row with a NEW index (the fresh VM), then renames it; create()
  resolves the provision budget ONCE at the port entry (pre-check
  included) and threads the REMAINING slice into ``_create`` (§2: one
  deadline per call, distributed internally; the deadline is never
  re-established with a fresh clock after the pre-check).
- ``configure()`` translation table (§4): vendor keys
  ``graphics_render_mode`` (int 0=OpenGL / 1=DirectX),
  ``resolution_width`` / ``resolution_height`` / ``vbox_dpi`` with
  ``is_customed_resolution=1`` emitted FIRST when a custom-resolution
  leaf is present (absorption), ``cpus`` / ``memory`` / ``cpucap``,
  ``is_hide_toolbar`` / ``turbo_mode`` / ``enable_audio`` (``"1"``/
  ``"0"``), ``start_window_mode`` (int enum 0/1/2), ``win_scaling_percent2``.
  AUTO → DIRECTX (=1) via the core ``resolve_renderer`` pin. The WORKING
  vendor spelling is ``is_customed_resolution`` (pymemuc/_types.py
  ConfigKeys; py-clash-bot MEMU_CONFIGURATION) — pymemuc's docs CSV
  spells it ``is_custom_resolution``; the working code spelling wins.
  ``setconfigex`` values are emitted as separate argv tokens
  (pymemuc/_manage.py:372-381 splits multi-token values on whitespace;
  every v0 value is a single token). Unknown canonical key →
  InvalidArgumentError (caller misuse); canonical-but-untranslatable
  leaf (e.g. a BlueStacks leaf) → FeatureNotSupportedError via the
  missing table entry; unknown renderer value → ConfigValidationError
  (VULKAN specifically: table-missing ⇒ FeatureNotSupportedError, per
  the §4 gate wording). Success = returncode, never stdout.
- FEATURE set = the full v0 flag set: memuc BOOT/CREATE/CONFIGURE plus
  the inherited ADB mechanics. §8 deferrals (public ``is_running``,
  ``screen_size``, ``run_vendor_cmd``, the clone family, snapshots) are
  not port members and simply do not exist here — no silent no-ops.
- Windows gating truth is the §3 registry row ALONE: construction does
  nothing platform-specific (§3 resolves platform mismatch in
  ``resolve_backend`` BEFORE import). ``memuc.exe`` resolves at first
  invocation: explicit ``memu.memuc_path`` wins; the still-None fallback
  probes the MEmu uninstall registry keys
  (``SOFTWARE\\…\\Uninstall\\MEmu`` + the WOW6432Node twin →
  ``InstallLocation`` → ``Memu\\memuc.exe``, pymemuc/_memuc.py:44-63) and
  raises ExecutableNotFoundError when neither yields — discovery is an
  honest no-candidate on hosts without ``winreg`` (macOS), never a raise.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, NamedTuple, TypeVar

from emuctl._adb import _stderr_excerpt
from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl._proc import _validate_argv as _require_argv
from emuctl.backends._registry import _validate_vm_name
from emuctl.config.model import (
    ConfigSpec,
    CreateSpec,
    EmuctlConfig,
    RendererMode,
    resolve_renderer,
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
from emuctl.log import sanitize
from emuctl.png import png_dimensions
from emuctl.port import AdbController as _AdbMiddleLayer
from emuctl.port import CapabilityFlag, DisplayState, ReadyCheck
from emuctl.timing import (
    DEFAULT,
    LIFECYCLE_TIMEOUT,
    QUERY_TIMEOUT,
    TimeoutParam,
    resolve_timeout,
)
from emuctl.waits import wait_until

__all__ = ["MemuController", "list_instances", "get_instance"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ── vendor pins ──

#: Pinned v0 default image (§4: None version ⇒ MEmu "136" / Android 13).
_DEFAULT_ANDROID_VERSION: Final[str] = "136"

#: ``memuc start``'s headless boot flag (pymemuc/_control.py:49-50).
_HEADLESS_FLAG: Final[str] = "-b"

#: MEmu uninstall registry keys probed for ``InstallLocation`` (in order).
_REGISTRY_KEYS: Final[tuple[str, ...]] = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MEmu",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\MEmu",
)

#: MEmu ADB-port convention constants (see module docstring).
_ADB_PORT_BASE: Final[int] = 21503
_ADB_PORT_STRIDE: Final[int] = 10

# ── translation table (§4; all ranges mirror the config model) ──

#: The renderer's MEmu encodings: vendor ints (1=DirectX, 0=OpenGL).
_RENDERER_ENCODINGS: Final[Mapping[RendererMode, str]] = {
    RendererMode.OPENGL: "0",
    RendererMode.DIRECTX: "1",
}

#: Custom-resolution leaves whose presence implies ``is_customed_resolution``.
_CUSTOM_RES_KEYS: Final[frozenset[str]] = frozenset({"display.width", "display.height", "display.dpi"})

#: Int leaves → (vendor key, min, max|None).
_TABLE_INTS: Final[Mapping[str, tuple[str, int, int | None]]] = {
    "display.width": ("resolution_width", 1, None),
    "display.height": ("resolution_height", 1, None),
    "display.dpi": ("vbox_dpi", 1, None),
    "display.fps": ("fps", 1, None),
    "resources.cpus": ("cpus", 1, None),
    "resources.memory_mb": ("memory", 1, None),
    "resources.cpu_cap_pct": ("cpucap", 1, 100),
    "memu.settings.scale_percent": ("win_scaling_percent2", 1, 100),
}

#: Bool leaves → vendor key (``"1"``/``"0"`` encodings).
_TABLE_BOOLS: Final[Mapping[str, str]] = {
    "memu.settings.hide_toolbar": "is_hide_toolbar",
    "memu.settings.turbo": "turbo_mode",
    "memu.settings.audio": "enable_audio",
}

#: Window-mode spellings → ``start_window_mode`` int enum tokens
#: (vendor CSV: 2=Custom, 1=Last time, 0=Default). Vendor ints kept +
#: exactly the three readable spellings the CSV names — no speculative
#: aliases.
_WINDOW_MODES: Final[Mapping[str, str]] = {
    "0": "0",
    "1": "1",
    "2": "2",
    "default": "0",
    "last": "1",
    "custom": "2",
}

#: The canonical leaf vocabulary MEmu can translate (§4 table extent).
_MEMU_LEAVES: Final[frozenset[str]] = frozenset(
    {"display.renderer", "memu.settings.window_mode", *_TABLE_INTS, *_TABLE_BOOLS}
)

#: Namespaces of canonical leaves that belong to OTHER model sections —
#: known-canonical leaves the MEmu table cannot translate (the typed gate
#: refusal), as opposed to unknown keys (caller misuse).
_FOREIGN_SECTIONS: Final[frozenset[str]] = frozenset(
    {
        "adb",
        "bluestacks",
        "google-play",
        "avd",
        "log",
        "memu",  # memu.* below the leaves translated here
    }
)


def _sh(value: object) -> str | int | float | bool | None:
    """A value's error-payload form (typed primitives only)."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _fail(path: str, value: object, expected: str) -> ConfigValidationError:
    """Translate-time validation error pinned to the MEmu config layer."""
    return ConfigValidationError(layer="memu", path=path, value=_sh(value), expected=expected)


def _registry_memuc_path() -> str | None:
    """Still-None vendor-binary discovery (§4 rule 6): probe the MEmu
    uninstall registry keys for ``InstallLocation`` and return the
    bundled ``Memu\\memuc.exe`` under it, or None. ``winreg`` is
    Windows-only — on other hosts this is an honest no-candidate, never
    a raise (pymemuc/_memuc.py:44-63 is the vendor shape mirrored here).
    """
    try:
        winreg: Any = importlib.import_module("winreg")  # noqa: PLC0415 — Windows-only stdlib
    except ImportError:
        return None
    for key in _REGISTRY_KEYS:
        try:
            root = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
        except OSError:
            return None
        try:
            with winreg.OpenKey(root, key) as hkey:
                location, _kind = winreg.QueryValueEx(hkey, "InstallLocation")
        except OSError:
            continue
        if isinstance(location, str) and location.strip():
            return str(Path(location.strip()) / "Memu" / "memuc.exe")
    return None


# ── translation (config spec → vendor setconfigex entries) ──


def _translate_entries(
    entries: Sequence[tuple[str, object]],
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Canonical (leaf, value) pairs → ordered (leaf, vendor_key, tokens).

    Strict typed translation (§4): unknown renderer spellings reject with
    ConfigValidationError; leaves absent from the MEmu table reject
    through FeatureNotSupportedError; the custom-resolution absorption
    emits ``is_customed_resolution=1`` before the first custom-res leaf;
    values are argv TOKENS (pymemuc/_manage.py:372-381 splits setconfigex
    values into separate argv tokens). Pure.
    """
    translated: list[tuple[str, str, tuple[str, ...]]] = []
    custom_res_fired = False
    for key, value in entries:
        if value is None:
            continue  # a None overlay entry targets nothing (leave as-is)
        if key == "display.renderer":
            encoding = _renderer_encoding(key, value)
            translated.append((key, "graphics_render_mode", (encoding,)))
            continue
        if key == "memu.settings.window_mode":
            translated.append((key, "start_window_mode", (_window_mode(key, value),)))
            continue
        if key in _TABLE_BOOLS:
            if not isinstance(value, bool):
                raise _fail(key, _sh(value), "a bool")
            translated.append((key, _TABLE_BOOLS[key], ("1" if value else "0",)))
            continue
        if key in _TABLE_INTS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise _fail(key, _sh(value), "an int")
            vendor_key, minimum, maximum = _TABLE_INTS[key]
            if value < minimum or (maximum is not None and value > maximum):
                expected = f"an int in {minimum}..{maximum}" if maximum is not None else f"an int >= {minimum}"
                raise _fail(key, value, expected)
            if key in _CUSTOM_RES_KEYS and not custom_res_fired:
                # §4: the custom-resolution flag is IMPLIED by any custom-res
                # leaf and must precede its cohort (absorption, not config).
                custom_res_fired = True
                translated.append(("is_customed_resolution", "is_customed_resolution", ("1",)))
            translated.append((key, vendor_key, (str(value),)))
            continue
        raise _untranslatable(key)
    return translated


def _renderer_encoding(key: str, value: object) -> str:
    """Resolve a renderer entry through the MEmu table (strict).

    AUTO resolves via the core pin (MEmu → DIRECTX); an unknown spelling
    is a ConfigValidationError; a known mode with no MEmu entry (VULKAN)
    is the typed gate refusal (§4 "same gate via a missing table entry").
    """
    mode = value
    if not isinstance(mode, RendererMode) and isinstance(mode, str):
        try:
            resolved = RendererMode(mode.strip().lower())
        except ValueError:
            raise _fail(key, _sh(value), "one of auto, opengl, directx, vulkan") from None
        mode = resolved
    if not isinstance(mode, RendererMode):
        raise _fail(key, _sh(value), "one of auto, opengl, directx, vulkan")
    resolved = resolve_renderer(mode, BackendId.MEMU) or mode
    encoding = _RENDERER_ENCODINGS.get(resolved)
    if encoding is None:
        raise FeatureNotSupportedError(
            feature=key,
            detail=(
                f"renderer '{resolved.value}' has no entry in the MEmu vendor "
                "encoding table (graphics_render_mode 0=OpenGL, 1=DirectX)"
            ),
        )
    return encoding


def _window_mode(key: str, value: object) -> str:
    """Window-mode spelling → vendor int token (strict)."""
    spelling: str | None
    if isinstance(value, bool):
        spelling = None
    elif isinstance(value, int):
        spelling = str(value)
    elif isinstance(value, str):
        spelling = value.strip().lower()
    else:
        spelling = None
    token = _WINDOW_MODES.get(spelling) if spelling is not None else None
    if token is None:
        raise _fail(key, _sh(value), "one of 0/1/2 or default/last/custom")
    return token


def _untranslatable(key: str) -> FeatureNotSupportedError:
    """Known-canonical leaf with no MEmu table entry → the typed gate."""
    return FeatureNotSupportedError(
        feature="configure",
        detail=f"canonical leaf '{key}' has no entry in the MEmu vendor translation table",
    )


def _overlay_entries(
    spec: Mapping[str, str | int | float | bool | None],
) -> list[tuple[str, object]]:
    """A populated ConfigSpec overlay → ordered (leaf, value) entries.

    Refusals fire here, before any I/O: a key whose head names another
    model section (or a memu.* leaf outside this table) is
    canonical-but-untranslatable → FeatureNotSupportedError; anything
    else the table lacks is an unknown canonical key → InvalidArgumentError.
    """
    entries: list[tuple[str, object]] = []
    for key, value in spec.items():
        if not isinstance(key, str):
            raise InvalidArgumentError(
                parameter="spec",
                constraint="ConfigSpec keys are canonical config leaf names",
            )
        if key not in _MEMU_LEAVES:
            head = key.partition(".")[0]
            if head in _FOREIGN_SECTIONS:
                raise _untranslatable(key)
            raise InvalidArgumentError(
                parameter=key,
                constraint="unknown canonical config leaf (ConfigSpec vocabulary)",
            )
        entries.append((key, value))
    return entries


def _bound_entries(cfg: EmuctlConfig) -> list[tuple[str, object]]:
    """``spec=None`` source: every non-None leaf of the bound config
    (§1.8 — "re-applies every non-None section of the bound config").
    Ordered memu.settings → resources → display for determinism.
    """
    entries: list[tuple[str, object]] = []
    memu = cfg.memu
    if memu is not None:
        settings = memu.settings
        for name in ("hide_toolbar", "turbo", "audio", "window_mode", "scale_percent"):
            value = getattr(settings, name)
            if value is not None:
                entries.append((f"memu.settings.{name}", value))
    for name in ("cpus", "memory_mb", "cpu_cap_pct"):
        value = getattr(cfg.resources, name)
        if value is not None:
            entries.append((f"resources.{name}", value))
    for name in ("width", "height", "dpi", "fps", "renderer"):
        value = getattr(cfg.display, name)
        if value is not None:
            entries.append((f"display.{name}", value))
    return entries


# ── discovery (BackendDiscovery convention; mirrors emuctl.backends.adb) ──


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


def _memuc_argv(cfg: EmuctlConfig, tail: Sequence[str]) -> list[str]:
    """Full memuc argv from the resolved config (pure).

    The explicit ``memu.memuc_path`` wins; the still-None fallback is the
    documented PATH spelling (same shape as the plain-ADB module's ``adb``
    PATH fallback — registry discovery is the adapter-enriched path).
    """
    memuc_path = cfg.memu.memuc_path if cfg.memu is not None else None
    return [memuc_path if memuc_path else "memuc", *_require_argv(tail)]


class _VmRow(NamedTuple):
    """One parsed ``memuc listvms`` row (vendor 5-column shape)."""

    index: int
    title: str
    running: bool
    pid: int


def _vm_rows(stdout: bytes) -> list[_VmRow]:
    """Parse ``listvms`` stdout into rows (pure; enumeration only).

    Row shape: ``index,title,top_level,running,pid``. Unparseable lines —
    memuc's ``read failed`` no-VMs noise, blank lines, rows a comma-title
    already broke — are skipped, never laundered into refs (§3).
    """
    text = stdout.decode("utf-8", errors="replace")
    rows: list[_VmRow] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.strip().split(",")]
        if len(parts) < 5 or not parts[1]:
            continue
        try:
            index = int(parts[0])
            pid = int(parts[4])
        except ValueError:
            continue
        rows.append(_VmRow(index=index, title=parts[1], running=parts[3] == "1", pid=pid))
    return rows


async def list_instances(
    config: EmuctlConfig, *, _executor: ProcessExecutor | None = None
) -> tuple[InstanceRecord, ...]:
    """Enumerate plausibly-present MEmu VMs via ``memuc listvms`` (§3).

    Read-only, bounded (query budget); refusals from the naming rule
    (control characters) fire at InstanceRef construction. Vendor failures
    propagate (``discover()`` isolates them into rows).
    """
    cfg = _require_config(config)
    executor = _executor if _executor is not None else ProcessExecutor()
    argv = _memuc_argv(cfg, ["listvms"])
    logger.debug("memu op=list_instances argv=%s", [sanitize(a) for a in argv])
    result: ProcessResult = await executor.run(argv, operation="list_instances", timeout=QUERY_TIMEOUT)
    if result.returncode != 0:
        raise VendorCommandError(
            command=argv,
            returncode=result.returncode,
            output_reason="exit",
            stderr_excerpt=_stderr_excerpt(result.stderr),
        )
    records: list[InstanceRecord] = []
    for row in _vm_rows(result.stdout):
        records.append(
            InstanceRecord(
                ref=InstanceRef(backend=BackendId.MEMU, key=row.title),
                running=row.running,
                serial=None,
                details={"index": str(row.index), "pid": str(row.pid)},
            )
        )
    logger.debug("memu list_instances rows=%d", len(records))
    return tuple(records)


async def get_instance(
    config: EmuctlConfig, ref: InstanceRef, *, _executor: ProcessExecutor | None = None
) -> InstanceRecord | None:
    """Return the record for ``ref`` or ``None`` when absent (name-keyed).

    Non-memu refs are not ours → ``None`` (no cross-backend fan-out).
    """
    if not isinstance(ref, InstanceRef):
        raise InvalidArgumentError(
            parameter="ref",
            constraint="must be an InstanceRef",
        )
    if ref.backend is not BackendId.MEMU:
        return None
    for record in await list_instances(config, _executor=_executor):
        if record.ref.key == ref.key:
            return record
    return None


def _adb_serial(index: int) -> str:
    """Transport serial from a VM index (MEmu ADB-ports convention)."""
    return f"127.0.0.1:{_ADB_PORT_BASE + index * _ADB_PORT_STRIDE}"


# ── the concrete adapter ──


class MemuController(_AdbMiddleLayer):
    """The concrete MEmu adapter (§1 AdbController subclass).

    Declares the full v0 set: memuc BOOT/CREATE/CONFIGURE mechanics over
    the ``-n <name>`` selector plus the inherited ADB interaction
    mechanics over the derived ``127.0.0.1:<port>`` transport.
    Construction binds ref + config only (no discovery, no boot, no
    probes; never raises EmulatorNotReadyError); the transport serial
    resolves lazily on the first ADB device invocation (one bounded
    ``listvms`` lookup — construction stays I/O-free).
    """

    FEATURES: frozenset[CapabilityFlag] = frozenset(
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

    def __init__(
        self,
        ref: InstanceRef,
        *,
        config: EmuctlConfig | None = None,
        _executor: ProcessExecutor | None = None,
    ) -> None:
        """Bind the ref + resolved config. No discovery, no boot, no probe.

        ``config`` is the ``connect()`` convention (selection already ran
        the registry + capability gates); the bound ref's key is THE
        ``-n`` selector — the ``memu.vm_name`` path knob never overrides
        an identity the caller bound. ``_executor`` is the single private
        injection seam (§6): tests pass ``ProcessExecutor(_spawn=...)``;
        production uses the real argv-only spawner.
        """
        super().__init__(ref)
        cfg = _require_config(config)
        _validate_vm_name(ref.key)  # the §3 spec row's key rule, bound here too
        self._config = cfg
        self.adb_path = cfg.adb.path
        self.adb_server_port = cfg.adb.port
        self._adb_host = cfg.adb.host
        #: Transport serial: "" = unresolved (derived lazily from rows).
        self._serial = ""
        self._executor = _executor if _executor is not None else ProcessExecutor()
        logger.debug("bind memu ref=%s", sanitize(str(ref)))

    # ── memuc transport helpers ──

    def _resolve_memuc_path(self) -> str:
        """Vendor binary: explicit path → registry discovery → typed refusal."""
        memu = self._config.memu
        if memu is not None and memu.memuc_path:
            return memu.memuc_path
        discovered = _registry_memuc_path()
        if discovered is not None:
            return discovered
        raise ExecutableNotFoundError(
            tool="memuc",
            searched=(
                [f"{key}\\InstallLocation\\Memu\\memuc.exe" for key in _REGISTRY_KEYS]
                + ["memu.memuc_path (explicit)"]
            ),
            detail=(
                "memuc.exe not found: set memu.memuc_path or install MEmu "
                "(the Windows uninstall registry keys locate it automatically)"
            ),
        )

    async def _memuc_run(self, args: Sequence[str], *, operation: str, timeout: float | None) -> str:
        """One bounded argv-only memuc invocation (the transport seam).

        Every memuc interaction funnels here and through the ``_proc.py``
        executor (§6: executor is the single choke point; argv-only,
        never shell). Nonzero rc → ``VendorCommandError(output_reason=
        "exit")``; stdout text is returned for callers to verify, never
        obeyed. ``CancelledError`` propagates unmodified (the executor
        owns abandonment with bounded scoped cleanup).
        """
        argv = _require_argv([self._resolve_memuc_path(), *args])
        logger.debug("memuc op=%s argv=%s", sanitize(operation), [sanitize(a) for a in argv])
        try:
            result: ProcessResult = await self._executor.run(argv, operation=operation, timeout=timeout)
        except OSError as exc:
            raise ExecutableNotFoundError(
                tool=argv[0],
                searched=[argv[0]],
                detail=f"could not execute the memuc binary: {type(exc).__name__}",
            ) from exc
        if result.returncode != 0:
            raise VendorCommandError(
                command=argv,
                returncode=result.returncode,
                output_reason="exit",
                stderr_excerpt=_stderr_excerpt(result.stderr),
            )
        return result.stdout.decode("utf-8", errors="replace")

    async def _rows(self, operation: str, timeout: float | None) -> list[_VmRow]:
        """Bounded ``listvms`` row fetch (the one machine-readable surface)."""
        text = await self._memuc_run(["listvms"], operation=operation, timeout=timeout)
        return _vm_rows(text.encode("utf-8", errors="replace"))

    @staticmethod
    def _headroom(deadline: float | None, start: float, operation: str) -> float | None:
        """Remaining slice of the one op-level deadline (None = unbounded).

        An exhausted deadline raises OperationTimeoutError before any
        further I/O (§2: expiry never degrades to a value).
        """
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            raise OperationTimeoutError(
                operation=operation,
                deadline=deadline,
                elapsed=time.monotonic() - start,
            )
        return left

    async def _observed(
        self,
        predicate: Callable[[], Awaitable[bool]],
        *,
        operation: str,
        start: float,
        deadline: float | None,
    ) -> None:
        """Bounded state observation through ``wait_until`` (the ONE
        sanctioned internal loop). Explicit-unbounded budgets degrade to
        a single probe — polling requires a finite budget shape, and no
        second unsanctioned loop is invented to fake one.
        """
        if deadline is None:
            if not await predicate():
                raise OperationTimeoutError(
                    operation=operation,
                    deadline=None,
                    elapsed=time.monotonic() - start,
                )
            return
        # Positive-budget invariant at the boundary (§2): a non-positive
        # remainder is expiry NOW — the typed deadline error — never a
        # non-positive timeout handed to wait_until (which would confuse
        # the confusion with an InvalidArgumentError the caller can't act on).
        left = deadline - time.monotonic()
        if left <= 0:
            raise OperationTimeoutError(
                operation=operation,
                deadline=deadline,
                elapsed=time.monotonic() - start,
            )
        await wait_until(predicate, operation=operation, timeout=left)

    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> InstanceRecord | None:
        """create()'s pre-check seam + lazy-transport lookup, row-keyed.

        Honors the CALLER'S deadline via the same bounded ``listvms`` row
        fetch (§2's one-deadline rule): the provision budget on create()'s
        pre-check, the op slice on lazy transport derivation, the restart
        attach remainder — never a fresh query-group hard-code.
        """
        if not isinstance(ref, InstanceRef):
            raise InvalidArgumentError(
                parameter="ref",
                constraint="must be an InstanceRef",
            )
        if ref.backend is not BackendId.MEMU:
            return None
        for row in await self._rows("get_instance", timeout):
            if row.title == ref.key:
                return InstanceRecord(
                    ref=InstanceRef(backend=BackendId.MEMU, key=row.title),
                    running=row.running,
                    serial=None,
                    details={"index": str(row.index), "pid": str(row.pid)},
                )
        return None

    async def _ensure_transport(self, timeout: float | None) -> None:
        """Lazily derive the ADB transport serial (one bounded rows lookup).

        The serial is an OBSERVATION, never identity: a bound ref with no
        ``listvms`` row is a stale ref ⇒ InstanceNotFoundError (§3
        identity lifetime). The derivation is pure digits, so the middle
        layer's per-call serial re-check always accepts it.
        """
        if self._serial:
            return
        record = await self._get_instance(self._ref, timeout)
        if record is None:
            raise InstanceNotFoundError(instance_ref=str(self._ref))
        index = int(record.details["index"])
        self._serial = _adb_serial(index)
        logger.debug("memu transport serial=%s", sanitize(self._serial))

    async def _run_adb(  # type: ignore[override]
        self,
        tail: Sequence[str],
        *,
        binary: bool,
        operation: str,
        timeout: float | None,
        server_verb: bool = False,
    ) -> bytes | str:
        """Middle-layer runner with MEmu transport lazy resolution.

        Device verbs auto-resolve the transport first (one bounded rows
        lookup on the first invocation); server verbs never do (they
        don't need a device). Scoping, the per-call serial re-check, and
        cancel discipline are inherited unchanged.
        """
        if not server_verb and not self._serial:
            await self._ensure_transport(timeout)
        return await super()._run_adb(
            tail,
            binary=binary,
            operation=operation,
            timeout=timeout,
            server_verb=server_verb,
        )

    # ── transport seam (ungated internal) ──

    async def _vendor_run(
        self,
        argv: Sequence[str],
        *,
        binary: bool = False,
        timeout: TimeoutParam = DEFAULT,
    ) -> bytes | str:
        """Ungated internal seam: the memuc transport.

        ``argv`` is the memuc tail (the caller owns the ``-n``/``-i``
        selector discipline); the vendor binary path is resolved here.
        Nonzero rc → VendorCommandError("exit"). Query-group budget when
        the caller passes no explicit one.
        """
        args = _require_argv(argv)
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="vendor_run")
        text = await self._memuc_run(args, operation="vendor_run", timeout=budget)
        return text.encode("utf-8", errors="replace") if binary else text

    # ── BOOT mechanics (memuc; ``-n`` scoped; the ONLY kill surface) ──

    async def _start(self, timeout: float | None) -> None:
        """``memuc -n <key> start -b`` — headless boot.

        The VM is driven over ADB, so memuc's headless flag is the
        defensible boot shape (the vendored wrapper exposes ``-b`` for
        exactly this). Returncode gate only — memuc's SUCCESS prose is
        never obeyed.
        """
        await self._memuc_run(
            ["-n", self._ref.key, "start", _HEADLESS_FLAG],
            operation="start",
            timeout=timeout,
        )

    async def _stop(self, timeout: float | None) -> None:
        """``memuc -n <key> stop`` — the instance-scoped kill surface.

        Bounded and scoped: never ``stopall`` (a contract-BANNED global
        sweep), never a process-table walk. Stopping an already-stopped
        VM is the vendor's failure, raised honestly (§1.4).

        Accepted interpretation — rc-only success gate, residual risk
        documented: the CONTRACT pins no stdout cross-check and no
        post-stop running-column observation for standalone ``stop()``
        (§1.4 sanctions "vendor failure at a nonzero rc raised honestly";
        there is no pinned cross-check surface), so success of the kill
        is provable ONLY as the vendor's rc 0 for this call. Historically
        rc-gated stops have been insufficient evidence (pymemuc's
        ``_control.py:52/:88`` stop paths gate on rc alone, and memuc is
        known to return rc 0 on an already-stopped VM before it fails —
        the reviewers' cited asymmetry); this module therefore treats
        "rc 0 but the VM still runs" as an UNSURFACED residual risk of
        the vendor interface, accepted rather than silently laundered:
        restart()'s funnel re-observes via ``_is_running`` (verbatim
        phase 1), so a failed kill inside restart() is re-detected there
        and mapped to "stop_failed"; a standalone ``stop()`` caller who
        needs certainty re-checks ``_is_running`` policy-side (§8's
        public is_running is the named follow-up surface). No
        stopall-style cross-check workaround is invented to presets.
        """
        await self._memuc_run(
            ["-n", self._ref.key, "stop"],
            operation="stop",
            timeout=timeout,
        )

    async def _is_running(self, timeout: float | None) -> bool:
        """Running observation: the bound name's row running-column value.

        A bound name with no row is a stale ref ⇒ InstanceNotFoundError —
        never a laundered False (§1.4).
        """
        record = await self._get_instance(self._ref, timeout)
        if record is None:
            raise InstanceNotFoundError(instance_ref=str(self._ref))
        return record.running

    # ── CREATE mechanic (provision group; one shared budget) ──

    async def _create(self, spec: CreateSpec, timeout: float | None) -> InstanceRef:
        """``memuc create <version>`` → bounded fresh-row observation →
        ``memuc -i <index> rename <name>``.

        ``timeout`` is the REMAINING slice of create()'s ONE shared
        provision deadline — the port's ``create()`` resolves the budget
        once (including the pre-check's elapsed time) and threads the
        remainder here, so the wall-clock cost of create() provably never
        exceeds the caller's provision budget (§2; mirror of
        ``configure()``'s ``_headroom`` slices).

        Never parses vendor stdout for control flow: the create returncode
        gate and the bounded ``listvms`` observation (a row with a NEW
        index — the fresh placeholder VM: ``wait_until`` on the shrinking
        provision remainder) decide. The placeholder is renamed to
        ``spec.name`` so the returned ``memu:<name>`` ref is truthful. An
        absent fresh row after the bounded confirmation raises
        OperationTimeoutError — nothing masquerades as success (§1.4/§2).
        """
        start = time.monotonic()
        deadline = None if timeout is None else start + float(timeout)

        def slice_left() -> float | None:
            return self._headroom(deadline, start, "create")

        def fresh_row(rows: Sequence[_VmRow]) -> _VmRow | None:
            for row in rows:
                if row.index not in before:
                    return row
            return None

        before: set[int] = {row.index for row in await self._rows("create", slice_left())}
        version = spec.android_version if spec.android_version else _DEFAULT_ANDROID_VERSION
        await self._memuc_run(["create", version], operation="create", timeout=slice_left())

        async def _appeared() -> bool:
            return fresh_row(await self._rows("create", slice_left())) is not None

        await self._observed(_appeared, operation="create", start=start, deadline=deadline)
        row = fresh_row(await self._rows("create", slice_left()))
        if row is None:  # pragma: no cover — observed() guarantees presence
            raise VendorCommandError(
                command=["memuc", "listvms"],
                returncode=0,
                output_reason="unusable_output",
                stderr_excerpt="",
                detail="the created VM row vanished before rename",
            )
        await self._memuc_run(
            ["-i", str(row.index), "rename", spec.name],
            operation="create",
            timeout=slice_left(),
        )
        return InstanceRef(backend=BackendId.MEMU, key=spec.name)

    # ── CONFIGURE mechanic (config group; one shared budget) ──

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:
        """``memuc -n <key> setconfigex <vendor-key> <tokens…>`` per entry.

        ``spec=None`` re-applies every non-None leaf of the bound config
        (§1.8); a populated overlay translates strictly (§4: unknown
        spelling → ConfigValidationError, missing table entry → the typed
        gate, unknown key → InvalidArgumentError — all BEFORE any I/O).
        All entries share the ONE config-group deadline: each setconfigex
        runs on the shrinking remaining slice. An empty task list does no
        I/O at all.

        Returns ``DisplayState(None, None, None)``: memuc exposes no
        machine-readable readback for the previous geometry (pymemuc reads
        values one key at a time from ``getconfigex`` stdout, and the
        vendor's prose is not a control-flow surface here). The adapter
        reports the honest "no readback" shape rather than fabricating a
        previous state.
        """
        entries = _translate_entries(_bound_entries(self._config) if spec is None else _overlay_entries(spec))
        if not entries:
            return DisplayState(None, None, None)
        self._resolve_memuc_path()  # fail-fast on the binary before any set
        start = time.monotonic()
        deadline = None if timeout is None else start + float(timeout)
        for leaf, vendor_key, tokens in entries:
            left = self._headroom(deadline, start, "configure")
            await self._memuc_run(
                ["-n", self._ref.key, "setconfigex", vendor_key, *tokens],
                operation="configure",
                timeout=left,
            )
            logger.debug(
                "memu configure leaf=%s vendor=%s tokens=%s",
                sanitize(leaf),
                sanitize(vendor_key),
                [sanitize(token) for token in tokens],
            )
        return DisplayState(None, None, None)

    # ── BOOT primitive: §1.6 wholesale override (vendor ordering) ──

    async def restart(
        self,
        *,
        app: str | None = None,
        ready: ReadyCheck | None = None,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        """ONE attempt: None or EmulatorNotReadyError — never False, never
        idempotent (§1.6).

        The port's phase chain with the vendor ordering inserted: stop-
        if-running → configure-while-stopped → start → CONNECT (derive
        the transport serial from a bounded listvms lookup, then
        ``adb connect 127.0.0.1:<port>`` — the server registration the
        inherited mechanics need before a screenshot can decode) →
        settle (first screenshot decodes non-empty) → [app: launch +
        bounded spawn-verify] → terminal check (ready(...), else
        spawn-present-if-app, else screenshot-decodes). All phases share
        the ONE remaining deadline; the override inherits no default
        body and re-invokes the BOOT gate at its head.

        Reason vocabulary (additive, port pins): "timeout" |
        "stop_failed" | "configure_failed" | "start_failed" |
        "attach_failed" | "settle_failed" | "app_start_failed" |
        "app_not_installed" | "menu_not_reached" | "ready_check_failed".
        Raw-propagation additions (documented reason vocabulary of THIS
        override, additive to the port's): ``InstanceNotFoundError``
        propagates raw from any phase — a stale bound ref is IDENTITY
        failure (§3), not readiness, so it must surface under its own
        name, never laundered into EmulatorNotReadyError.

        TRACKED PAIR with emuctl.port.AsyncEmulatorController.restart()'s
        phase() re-raise list (§6 sharing tiers — keep BOTH files in sync
        when editing either list): the port propagates
        FeatureNotSupported/ControllerClosed/InvalidArgument raw; THIS
        copy additionally keeps InstanceNotFoundError raw as reasoned
        above, and restores ControllerClosedError raw propagation so a
        concurrent ``aclose()`` mid-restart surfaces under contract terms
        (the port pins it; a wiring/state error is never readiness data).
        """
        self._require(CapabilityFlag.BOOT)
        self._ensure_open()
        if ready is not None and not callable(ready):
            raise InvalidArgumentError(
                parameter="ready",
                constraint="must be a callable ReadyCheck or None",
            )
        budget = resolve_timeout(timeout, LIFECYCLE_TIMEOUT, operation="restart")
        logger.debug(
            "restart ref=%s app=%s",
            sanitize(str(self._ref)),
            sanitize(str(app)),
        )
        start = time.monotonic()
        deadline = None if budget is None else start + budget

        def remaining(operation: str) -> float | None:
            if deadline is None:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                raise EmulatorNotReadyError(
                    reason="timeout",
                    cause=OperationTimeoutError(
                        operation=operation,
                        deadline=deadline,
                        elapsed=time.monotonic() - start,
                    ),
                )
            return left

        async def phase(
            operation: str,
            reason: str,
            call: Callable[[float | None], Coroutine[Any, Any, T]],
        ) -> T:
            """Run one phase under the shared remainder (§1.6 mapping)."""
            fresh = remaining(operation)
            try:
                if fresh is None:
                    return await call(None)
                return await asyncio.wait_for(call(fresh), fresh)
            except asyncio.CancelledError:
                raise
            except EmulatorNotReadyError:
                raise
            except OperationTimeoutError as exc:
                raise EmulatorNotReadyError(reason="timeout", cause=exc) from exc
            except TimeoutError as exc:
                raise EmulatorNotReadyError(
                    reason="timeout",
                    cause=OperationTimeoutError(
                        operation=operation,
                        deadline=deadline,
                        elapsed=time.monotonic() - start,
                    ),
                ) from exc
            # TRACKED PAIR with emuctl.port.AsyncEmulatorController.restart()'
            # s phase() re-raise list (§6 sharing tiers — keep both in sync;
            # this copy adds InstanceNotFoundError raw, see the docstring).
            except (
                FeatureNotSupportedError,
                ControllerClosedError,
                InvalidArgumentError,
                InstanceNotFoundError,
            ):
                raise
            except Exception as exc:
                raise EmulatorNotReadyError(reason=reason, cause=exc) from exc

        async def attach(budget_left: float | None) -> None:
            """The vendor-ordered connect phase.

            Deliberately does NOT invoke the port's ``_attach`` sync no-op
            hook (it is never awaited and would be dead here — the async
            connect work this adapter needs is exactly what replaces it;
            see the class docstring).
            """
            record = await self._get_instance(self._ref, budget_left)
            if record is None:
                raise InstanceNotFoundError(instance_ref=str(self._ref))
            serial = _adb_serial(int(record.details["index"]))
            self._serial = serial
            await self._run_adb(
                ["connect", serial],
                binary=False,
                operation="restart",
                timeout=budget_left,
                server_verb=True,
            )
            logger.debug("memu restart connected serial=%s", sanitize(serial))

        # 1. stop-if-running
        if await phase("restart", "stop_failed", self._is_running):
            await phase("restart", "stop_failed", lambda left: self.stop(timeout=left))
        # 2. configure-while-stopped (unconditional funnel)
        await phase("restart", "configure_failed", lambda left: self.configure(None, timeout=left))
        # 3. start
        await phase("restart", "start_failed", lambda left: self.start(timeout=left))
        # 4. attach: transport derivation + server registration
        await phase("restart", "attach_failed", attach)
        # 5. settle: first screenshot decodes non-empty (public funnel)
        png_dimensions(await phase("restart", "settle_failed", lambda left: self.screenshot(timeout=left)))
        # 6. app: launch + bounded spawn-verify
        if app is not None:
            await phase(
                "restart",
                "app_start_failed",
                lambda left: self.start_app(app, timeout=left),
            )
        # 7. terminal check
        if ready is not None:
            await self._eval_ready(ready, start, deadline, remaining)
        elif app is not None:
            present = await phase(
                "restart",
                "app_not_installed",
                lambda left: self.is_app_installed(app, timeout=left),
            )
            if not present:
                raise EmulatorNotReadyError(reason="app_not_installed", cause=None)
        else:
            png_dimensions(await phase("restart", "settle_failed", lambda left: self.screenshot(timeout=left)))
        logger.debug("restart ready ref=%s", sanitize(str(self._ref)))

    # ── transport probe · INHERITED ──
    # is_reachable is the core-side AdbController shared probe (§6 promotion
    # tier): the echo probe, the probe word, and the stderr classifier live
    # once in emuctl.port. MEmu's transport facts engage through the
    # overridden _run_adb above — lazy serial resolution inside the runner
    # (stale bound ref → the identity error → the shared catch-all maps it
    # to "disconnected" as probe DATA; missing memuc binary → "no-adb").

    # ── lifecycle close ──

    async def aclose(self) -> None:
        """Idempotent; releases transport only (never stops the emulator)."""
        await self._executor.close()
        await super().aclose()

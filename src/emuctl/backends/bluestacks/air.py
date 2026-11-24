"""§1/§3/§4/§6 — BlueStacks Air backend adapter (macOS Apple-Silicon;
``[bluestacks]`` extra, same family package per §6 — the layout rule that
triggers packaging day one).

A concrete :class:`emuctl.port.AdbController` subclass mirroring bs5.py on
the macOS shapes the §3 registry row pins: ``/Applications/BlueStacks.app``
with the bundled ``hd-adb``, ONE configured instance, cold-synthesized MIM
metadata (the vendor's own cold path — py-clash-bot synthesizes
MimMetaData for BlueStacks Air too, bluestacks.py:82-88, :288-307), and
renderer AUTO → VULKAN (§4's macOS pin; the vendor's normalize default —
"Default to Vulkan on macOS", bluestacks.py:248-249 — is the EVIDENCE for
that pin).

Vendor-pinned facts (py-clash-bot ``pyclashbot/emulators/bluestacks.py``):

- macOS tool dir: ``/Applications/BlueStacks.app/Contents/MacOS`` holding
  the ``BlueStacks`` binary + bundled ``hd-adb`` (:68-73, :107-114); the
  conf store: ``/Users/Shared/Library/Application Support/BlueStacks``
  (``bluestacks.conf`` + ``Engine/UserData/MimMetaData.json``, :127-134).
- macOS launch: direct execution of the player with ``--instance
  <internal>`` (:561-566); no memuc-like CLI exists on this platform.
- macOS stop in the vendor code is ``pkill -f "BlueStacks.*<internal>"``
  (:578-583) and run-state observation is ``pgrep -fl BlueStacks`` with
  stdout substring matching (:516-529) — both reposed here: the §2 kill
  rule sanctions the INTERNAL-INSTANCE-NAME discriminator, but our
  patterns are STRICTER than the vendor's: the complete player path, as
  either our detached ``--instance <internal>`` launch or a bare macOS app
  launch (Air boots its single configured instance), regex-escaped and
  anchored — never the vendor's ``BlueStacks`` substring sweep. The
  OBSERVATION keys on pgrep's EXIT STATUS only (0 = a process whose
  command line matches the bound-key pattern / 1 = none) — stdout is
  never parsed.

Interpretation decisions (CONTRACT wins; these fill its silences):

- ONE configured instance: the Air row's semantics. Enumeration reports
  the instances the conf actually names (usually exactly one) — an
  honestly empty ``()`` when the app bundle or conf is absent on this
  host, typed refusals for malformed content. ``bluestacks.instance_name``
  is a selection knob, never an identity override (memu.py's vm_name
  rule, restated): the bound key is THE identity.
- Kill scope (§2, quoted): *"… our-spawned PIDs, or for vendor-owned
  processes the bound key as discriminator (window-title-equal bound key,
  internal instance name, per-VM vendor stop addressed by identity)."*
  The stop is ``pkill -f`` (argv-only, through the executor) with a
  bound-key discriminator pattern; the CONTRACT permits this shape, and
  it is narrower than the vendor's. No ``pgrep``/``pkill`` call ever
  carries a pattern that matches without the bound internal name.
- ``restart()`` is the §1.6 wholesale override (the shared family shape
  with bs5.py/memu.py; §6 sharing tiers keep the three phase() copies
  TRACKED PAIRS, sync'd by comment).
- Renderer: ``display.renderer`` AUTO → VULKAN (§4 macOS pin) resolved in
  ``_configure`` exactly like bs5 resolves to DirectX; the translator
  table is the SHARED family one (:mod:`emuctl.backends.bluestacks._conf`).
- The launch is a DETACHED spawn through the executor's
  ``spawn_detached`` member (same reasoning as bs5._start — the GUI child
  outlives the call), behind a strict identity gate: a bound key mapping
  to no instance raises the typed ``InstanceNotFoundError`` before I/O
  (the vendor's launch-the-default-VM fallback is banned, §3 stale-ref
  rule).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Coroutine, Sequence
from types import MappingProxyType
from typing import Any, Final, TypeVar

from emuctl._adb import _stderr_excerpt
from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult, _ProcessHandle
from emuctl._proc import _validate_argv as _require_argv
from emuctl.backends._registry import _validate_vm_name
from emuctl.backends.bluestacks import _conf, _meta, _paths
from emuctl.config.model import (
    BlueStacksConfig,
    ConfigSpec,
    EmuctlConfig,
    RendererMode,
    resolve_renderer,
)
from emuctl.errors import (
    ConfigError,
    ConfigValidationError,
    ControllerClosedError,
    DeviceStateError,
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

__all__ = ["BlueStacksAirController", "list_instances", "get_instance"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

_PLAYER_NAME: Final[str] = "BlueStacks"
_HD_ADB_NAME: Final[str] = "hd-adb"

#: Air's §3 platform: macOS. Off-macOS resolution yields honest Nones (no
#: candidates) instead of platform branches here (the registry row gates).

#: pgrep/pkill exit statuses (POSIX): 1 = no process matched.
_NO_MATCH_RC: Final[int] = 1

#: Cold-boot readiness polling (§2's bounded-wait rule): the player returns
#: from the detached spawn immediately, while the guest and its private
#: hd-adb server need tens of seconds before a transport connect can
#: succeed. Each probe is short so the shared restart deadline stays in
#: charge; the poll gap keeps the probe rate sane.
_BOOT_POLL_S: Final[float] = 0.5
_PROBE_TIMEOUT_S: Final[float] = 5.0

#: Bounded internal budget for an explicitly unbounded restart (timeout=None):
#: the guest boot must still give up eventually — never an unbounded loop.
_BOOT_READY_TIMEOUT_S: Final[float] = LIFECYCLE_TIMEOUT

#: Stop waits for the signalled player to actually exit (the vendor's own
#: restart polls this): a still-exiting app can rewrite bluestacks.conf after
#: our configure-while-stopped write and hold the single-instance lock.
_STOP_POLL_S: Final[float] = 0.25


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


def _bs_config(cfg: EmuctlConfig) -> BlueStacksConfig:
    return cfg.bluestacks if cfg.bluestacks is not None else BlueStacksConfig()


def _process_pattern(player: str, internal: str) -> str:
    """The scoped bound-key process pattern (module-level so discovery can
    observe run state without constructing a controller).

    Both accepted spellings are anchored to the complete player path — our
    detached ``--instance <internal>`` launch, and a bare macOS app launch
    (Air boots its single configured instance). POSIX ERE: pgrep/pkill call
    regcomp, so no non-capturing groups or ``\\s``.
    """
    escaped_player = re.escape(player)
    launch = re.escape(f"{player} --instance {internal}")
    return f"({launch}( |$)|{escaped_player}$)"


async def _observe_running(executor: ProcessExecutor, player: str, internal: str) -> bool:
    """Run-state observation for one instance: pgrep exit status only."""
    argv = ["pgrep", "-f", _process_pattern(player, internal)]
    logger.debug("bluestacks-air op=list_instances argv=%s", [sanitize(a) for a in argv])
    result = await executor.run(argv, operation="list_instances", timeout=QUERY_TIMEOUT)
    if result.returncode == 0:
        return True
    if result.returncode == _NO_MATCH_RC:
        return False
    raise VendorCommandError(
        command=argv,
        returncode=result.returncode,
        output_reason="exit",
        stderr_excerpt=_stderr_excerpt(result.stderr),
    )


# ── discovery (BackendDiscovery convention; enumerate the ONE configured
# instance from the conf file — a machine-readable file, never stdout) ──


async def list_instances(
    config: EmuctlConfig, *, _executor: ProcessExecutor | None = None
) -> tuple[InstanceRecord, ...]:
    """Enumerate Air's configured instance(s) from the conf store.

    Read-only and bounded. Identity and transport facts come from the conf
    file; run state is a scoped ``pgrep`` exit-status observation (never a
    conf read, never stdout parsing). Cold installs with no MIM metadata get
    IN-MEMORY synthesis (deterministic; see _meta) — never a disk write into
    the vendor's store. No app bundle or conf on this host → honest ``()``.
    """
    cfg = _require_config(config)
    bs = _bs_config(cfg)
    store_path = _paths.conf_path(bs)
    if store_path is None or not store_path.is_file():
        return ()
    doc = _conf.parse(store_path.read_text(encoding="utf-8", errors="replace"))
    meta_path = _paths.mim_meta_path(bs)
    meta = (
        _meta.parse(meta_path.read_text(encoding="utf-8", errors="replace"))
        if meta_path is not None and meta_path.is_file()
        else _meta.synthesize(doc)
    )
    executor = _executor if _executor is not None else ProcessExecutor()
    player = _paths.tool_path(bs, _PLAYER_NAME)
    records: list[InstanceRecord] = []
    for internal in _conf.instance_internals(doc):
        try:
            port: int | None = _conf.instance_adb_port(doc, internal)
        except ConfigValidationError:
            port = None  # port keys absent for this instance: no serial truth here
        display = _conf.display_name(doc, internal) or internal
        named = next((e.name for e in meta.entries if e.internal == internal), None)
        details = {"internal": internal, **({"adb_port": str(port)} if port else {})}
        running = player is not None and await _observe_running(executor, str(player), internal)
        records.append(
            InstanceRecord(
                ref=InstanceRef(backend=BackendId.BLUESTACKS_AIR, key=named or display),
                running=running,
                serial=None,
                details=MappingProxyType(details),
            )
        )
    logger.debug("bluestacks-air list_instances rows=%d", len(records))
    return tuple(records)


async def get_instance(
    config: EmuctlConfig, ref: InstanceRef, *, _executor: ProcessExecutor | None = None
) -> InstanceRecord | None:
    """The record for ``ref`` or ``None`` when absent (never raises it)."""
    if not isinstance(ref, InstanceRef):
        raise InvalidArgumentError(
            parameter="ref",
            constraint="must be an InstanceRef",
        )
    if ref.backend is not BackendId.BLUESTACKS_AIR:
        return None
    for record in await list_instances(config, _executor=_executor):
        if record.ref.key == ref.key:
            return record
    return None


class BlueStacksAirController(_AdbMiddleLayer):
    """The concrete BlueStacks Air adapter (macOS Apple-Silicon).

    Declares BOOT + CONFIGURE plus the inherited ADB mechanics (SCREENSHOT /
    INPUT / APPS / APK_INSTALL). No CREATE — the family has no unattended
    provisioning verb on either platform. Construction binds ref + config
    only; per-family lazies (player path, serial, internal name) resolve
    on first use.
    """

    FEATURES: frozenset[CapabilityFlag] = frozenset(
        {
            CapabilityFlag.BOOT,
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
        """Bind the ref + resolved config (memu.py construction convention).

        ``bluestacks.adb_server_port`` is the private HD-Adb SERVER port
        (5041 §4 pin); the adb host stays the system ``adb.host``.
        """
        super().__init__(ref)
        cfg = _require_config(config)
        _validate_vm_name(ref.key)  # the §3 spec row's key rule, bound here too
        self._config = cfg
        self._bs: BlueStacksConfig = _bs_config(cfg)
        self.adb_path: str | None = None  # bundled hd-adb, resolved lazily
        self.adb_server_port: int | None = self._bs.adb_server_port
        self._adb_host: str = cfg.adb.host
        self._serial: str = ""
        self._target: tuple[str, int] | None = None  # (internal, adb port)
        self._spawned: _ProcessHandle | None = None  # retained, untracked
        self._executor = _executor if _executor is not None else ProcessExecutor()
        logger.debug("bind bluestacks-air ref=%s", sanitize(str(ref)))

    # ── bundle resolution ──

    def _tool(self, name: str) -> str:
        """Bundled macOS tool (player or hd-adb) or a typed refusal naming
        the searched locations (cold-start bundled hd-adb resolution per
        the §3 row: /Applications/BlueStacks.app + bundled hd-adb)."""
        candidate = _paths.tool_path(self._bs, name)
        if candidate is not None:
            return str(candidate)
        raise ExecutableNotFoundError(
            tool=name,
            searched=[
                "bluestacks.install_dir (explicit)",
                "/Applications/BlueStacks.app/Contents/MacOS",
            ],
            detail=f"{name} was not found in the BlueStacks app bundle (set bluestacks.install_dir)",
        )

    def _store(self) -> tuple[_conf.ConfDocument, Any]:
        """(conf, meta) for op use; missing/unreadable conf = typed ConfigError
        (an observation failure never becomes negative data, §1.4)."""
        path = _paths.conf_path(self._bs)
        if path is None or not path.is_file():
            raise ConfigError(
                layer="bluestacks",
                path=str(path) if path is not None else "bluestacks.conf",
                reason="unreadable",
                value=None,
            )
        doc = _conf.parse(path.read_text(encoding="utf-8", errors="replace"))
        meta_path = _paths.mim_meta_path(self._bs)
        if meta_path is not None and meta_path.is_file():
            return doc, _meta.parse(meta_path.read_text(encoding="utf-8", errors="replace"))
        return doc, _meta.synthesize(doc)  # cold: in-memory synthesis

    def _target_and_port(self) -> tuple[str, int]:
        """(internal, adb port) for the BOUND key, cached (shared lookup):
        MIM metadata Name → internal, conf ``display_name`` fallback; a
        miss is a stale ref ⇒ InstanceNotFoundError; a missing/nonsensical
        port is the typed conf error (never a guessed serial)."""
        if self._target is not None:
            return self._target
        doc, meta = self._store()
        internal = _meta.internal_for(meta, self._ref.key) or _conf.internal_by_display(doc, self._ref.key)
        if internal is None:
            raise InstanceNotFoundError(instance_ref=str(self._ref))
        port = _conf.instance_adb_port(doc, internal)
        self._target = (internal, port)
        logger.debug(
            "bluestacks-air target internal=%s port=%s",
            sanitize(internal),
            sanitize(str(port)),
        )
        return self._target

    # ── transport ──

    async def _run_adb(  # type: ignore[override]
        self,
        tail: Sequence[str],
        *,
        binary: bool,
        operation: str,
        timeout: float | None,
        server_verb: bool = False,
    ) -> bytes | str:
        """Middle-layer runner with the bundled ``hd-adb`` lazy resolution
        (device verbs resolve the serial out of the conf store first; the
        ``-P`` prefix always carries the private server port). Scoping,
        re-check and cancel discipline are inherited."""
        if self.adb_path is None:
            self.adb_path = self._tool(_HD_ADB_NAME)
        if not server_verb and not self._serial:
            record = await self._get_instance(self._ref, timeout)
            if record is None:
                raise InstanceNotFoundError(instance_ref=str(self._ref))
            self._serial = f"127.0.0.1:{record.details['adb_port']}"
            logger.debug("bluestacks-air transport serial=%s", sanitize(self._serial))
        return await super()._run_adb(
            tail,
            binary=binary,
            operation=operation,
            timeout=timeout,
            server_verb=server_verb,
        )

    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> InstanceRecord | None:
        """The transport-derivation seam keyed on the (single) configured
        instance; a stale key → ``None``, store failures raise (§1.4)."""
        if not isinstance(ref, InstanceRef):
            raise InvalidArgumentError(
                parameter="ref",
                constraint="must be an InstanceRef",
            )
        if ref.backend is not BackendId.BLUESTACKS_AIR:
            return None
        try:
            internal, port = (
                self._target_and_port() if ref.key == self._ref.key else (self._lookup_key(ref.key))
            )
        except InstanceNotFoundError:
            return None
        return InstanceRecord(
            ref=ref,
            running=False,
            serial=None,
            details=MappingProxyType({"internal": internal, "adb_port": str(port)}),
        )

    def _lookup_key(self, key: str) -> tuple[str, int]:
        doc, meta = self._store()
        internal = _meta.internal_for(meta, key) or _conf.internal_by_display(doc, key)
        if internal is None:
            raise InstanceNotFoundError(instance_ref=f"bluestacks-air:{key}")
        return internal, _conf.instance_adb_port(doc, internal)

    async def _vendor_run(
        self,
        argv: Sequence[str],
        *,
        binary: bool = False,
        timeout: TimeoutParam = DEFAULT,
    ) -> bytes | str:
        """Ungated internal seam: the bundled hd-adb transport."""
        args = _require_argv(argv)
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="vendor_run")
        return await self._run_adb(args, binary=binary, operation="vendor_run", timeout=budget)

    # ── scoped process tools (pgrep/pkill tiers; the ROOT single-site
    # equality of the family: every pattern carries the bound internal
    # name — a keyless "BlueStacks" pattern is the vendor's banned sweep) ──

    def _launch_argv(self, internal: str) -> list[str]:
        """``<player> --instance <internal>`` — the vendor's macOS launch
        argv (bluestacks.py:561-566). ``internal`` always arrives resolved
        (``_start`` raises before I/O when the bound key maps to nothing);
        the no-flag plain launch only serves a hypothetical empty internal,
        never an invented fallback launch."""
        argv = [self._tool(_PLAYER_NAME)]
        if internal:
            argv += ["--instance", internal]
        return argv

    def _pgrep_pattern(self, internal: str) -> str:
        """The scoped bound-key discriminator (full command line).

        Two accepted spellings, both anchored to the complete player path
        (never the vendor's bare ``BlueStacks`` substring sweep, :581):

        - our detached launch: ``<player> --instance <internal>``, escaped;
        - a normal macOS launch: ``<player>`` with no argv — Air boots its
          single configured instance, so that process IS the bound instance
          (the §3 row's one-instance semantics). This keeps run-state and
          stop honest when the user opened the app from Finder first."""
        return _process_pattern(self._tool(_PLAYER_NAME), internal)

    async def _tool_run(self, argv: list[str], *, operation: str, timeout: float | None) -> ProcessResult:
        args = _require_argv(argv)
        logger.debug("bluestacks-air op=%s argv=%s", sanitize(operation), [sanitize(a) for a in args])
        try:
            return await self._executor.run(args, operation=operation, timeout=timeout)
        except OSError as exc:
            raise ExecutableNotFoundError(
                tool=args[0],
                searched=[args[0]],
                detail=f"could not execute the tool binary: {type(exc).__name__}",
            ) from exc

    async def _transport_ready(self) -> bool:
        """One bounded cold-boot probe for :func:`emuctl.waits.wait_until`.

        Attempts the private-server ``connect`` and then the shared
        ``is_reachable`` data probe (transport/exit-status truth only — no
        vendor stdout parsing). Both failures and not-yet-up states are
        negative data here; the deadline in ``wait_until`` owns the budget.
        """
        await asyncio.sleep(_BOOT_POLL_S)
        try:
            await self._run_adb(
                ["connect", self._serial],
                binary=False,
                operation="restart",
                timeout=_PROBE_TIMEOUT_S,
                server_verb=True,
            )
        except (DeviceStateError, VendorCommandError, OperationTimeoutError):
            return False
        except TimeoutError:
            return False
        return (await self.is_reachable(timeout=_PROBE_TIMEOUT_S)).ok

    async def _stable_frame(self) -> bool:
        """One bounded settle probe: a screenshot that decodes (§1.6)."""
        await asyncio.sleep(_BOOT_POLL_S)
        try:
            png_dimensions(await self.screenshot(timeout=_PROBE_TIMEOUT_S))
        except (DeviceStateError, VendorCommandError, OperationTimeoutError):
            return False
        except TimeoutError:
            return False
        return True

    # ── BOOT mechanics ──

    async def _start(self, timeout: float | None) -> None:
        """DETACHED launch (see bs5._start's reasoning): the GUI child
        outlives the call; spawned through the executor's
        ``spawn_detached`` member, argv-only, never awaited.

        Identity first (§3 stale-ref rule, like every other phase): a bound
        key that maps to no instance raises the typed
        ``InstanceNotFoundError`` BEFORE any I/O — the vendor's
        fallback-to-default-VM launch is banned (provisioning-as-identity
        laundering, §Identity)."""
        del timeout  # the spawn seam is immediate; boot is not await-based
        internal, _port = self._target_and_port()
        argv = _require_argv(self._launch_argv(internal))
        logger.debug("bluestacks-air start argv=%s", [sanitize(a) for a in argv])
        self._spawned = await self._executor.spawn_detached(argv, operation="bluestacks-air start")
        return None

    async def _stop(self, timeout: float | None) -> None:
        """Scoped kill: ``pkill -f`` over the bound-key pattern (the
        §2 "internal instance name" discriminator; see the module
        docstring). rc 0 = matched and signalled; ANY other rc raises
        honestly (the §1.4 already-stopped rule mirrors memu's ``-n stop``).

        Then wait (bounded, shared deadline) until the player is gone: a
        still-exiting app can rewrite ``bluestacks.conf`` after our
        configure-while-stopped write and hold the single-instance lock."""
        internal, _port = self._target_and_port()
        start = time.monotonic()
        deadline = None if timeout is None else start + timeout
        argv = ["pkill", "-f", self._pgrep_pattern(internal)]
        result = await self._tool_run(argv, operation="stop", timeout=timeout)
        if result.returncode != 0:
            raise VendorCommandError(
                command=argv,
                returncode=result.returncode,
                output_reason="exit",
                stderr_excerpt=_stderr_excerpt(result.stderr),
            )
        if deadline is None:
            budget = LIFECYCLE_TIMEOUT
        else:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise OperationTimeoutError(
                    operation="stop",
                    deadline=deadline,
                    elapsed=time.monotonic() - start,
                )
        await wait_until(self._exited, operation="stop", timeout=budget)

    async def _exited(self) -> bool:
        """One bounded stop probe: has the signalled player exited?"""
        await asyncio.sleep(_STOP_POLL_S)
        return not await self._is_running(_PROBE_TIMEOUT_S)

    async def _is_running(self, timeout: float | None) -> bool:
        """Run-state observation: OWN handle first (``returncode is None`` =
        alive, the §2 our-PIDs truth); a dead/absent handle falls through to
        ``pgrep -f`` over the scoped bound-key pattern keyed on EXIT STATUS
        ONLY (0 → running, 1 → stopped — stdout is never parsed). Any other
        rc raises the failed observation (§1.4)."""
        if self._spawned is not None and self._spawned.returncode is None:
            return True
        # Our spawn exited (or never existed): re-observe through the scoped
        # pattern instead of inferring "stopped" from the handle (§1.4).
        try:
            internal, _port = self._target_and_port()
        except InstanceNotFoundError:
            return False  # no mapped instance: there is nothing of ours to observe
        argv = ["pgrep", "-f", self._pgrep_pattern(internal)]
        result = await self._tool_run(argv, operation="is_running", timeout=timeout)
        if result.returncode == 0:
            return True
        if result.returncode == _NO_MATCH_RC:
            return False
        raise VendorCommandError(
            command=argv,
            returncode=result.returncode,
            output_reason="exit",
            stderr_excerpt=_stderr_excerpt(result.stderr),
        )

    # ── CONFIGURE mechanic (§4; the SHARED family translation) ──

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:
        """Translate the overlay (or bound-config leaves when ``spec=None``)
        through the family table and write ``bluestacks.conf`` scoped to
        the bound instance. Renderer AUTO → VULKAN here (§4 macOS pin)."""
        entries: list[tuple[str, object]] = (
            _conf.overlay_entries(spec) if spec is not None else _conf.bound_entries(self._config)
        )
        translated = [
            (key, self._resolve_auto(key, value) if key == "display.renderer" else value)
            for key, value in entries
            if value is not None
        ]
        if not translated:
            return DisplayState(None, None, None)
        internal, _port = self._target_and_port()
        doc, _meta_view = self._store()
        new_doc = _conf.translate(doc, internal, translated)
        if new_doc == doc:
            return DisplayState(None, None, None)
        path = _paths.conf_path(self._bs)
        assert path is not None  # _store() would have raised otherwise
        logger.debug(
            "bluestacks-air configure internal=%s leaves=%s",
            sanitize(internal),
            [sanitize(k) for k, _v in translated],
        )
        path.write_text(_conf.serialize(new_doc), encoding="utf-8")
        # The vendor store exposes no machine-readable previous-geometry readback.
        return DisplayState(None, None, None)

    @staticmethod
    def _resolve_auto(key: str, value: object) -> RendererMode:
        """AUTO → §4's macOS pin (Air → VULKAN); concrete passes."""
        mode = _conf.renderer_mode(key, value)
        resolved = resolve_renderer(mode, BackendId.BLUESTACKS_AIR) or mode
        if resolved is RendererMode.AUTO:  # core pin covers Air; belt for drift
            resolved = RendererMode.VULKAN  # pragma: no cover
        return resolved

    # ── BOOT primitive: §1.6 wholesale override (family shape) ──

    async def restart(
        self,
        *,
        app: str | None = None,
        ready: ReadyCheck | None = None,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        """ONE attempt: None or EmulatorNotReadyError — never False (§1.6).

        Family phase chain (TRACKED PAIRS with the port's, memu's and
        bs5's phase() re-raise lists — keep them in sync): stop-if-running
        → configure-while-stopped → start (detached launch) → attach
        (hd-adb connect against the private 5041 server) → settle → [app]
        → terminal. All phases share the ONE remaining deadline;
        InstanceNotFoundError propagates raw (identity failure)."""
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
            # TRACKED PAIR with the port's, memu's and bs5's phase() lists.
            except (
                FeatureNotSupportedError,
                ControllerClosedError,
                InvalidArgumentError,
                InstanceNotFoundError,
            ):
                raise
            except Exception as exc:
                raise EmulatorNotReadyError(reason=reason, cause=exc) from exc

        def boot_budget(left: float | None) -> float:
            """The bounded wait for cold-boot readiness under the shared
            deadline; an explicitly unbounded restart still has to give up
            (§2's bounded-wait rule)."""
            return left if left is not None else _BOOT_READY_TIMEOUT_S

        async def attach(budget_left: float | None) -> None:
            """The private-server connect phase: wait (bounded) for the
            spawned guest to accept the connect against the private server,
            then register the transport."""
            record = await self._get_instance(self._ref, budget_left)
            if record is None:
                raise InstanceNotFoundError(instance_ref=str(self._ref))
            serial = f"127.0.0.1:{record.details['adb_port']}"
            self._serial = serial
            await wait_until(
                self._transport_ready,
                operation="restart",
                timeout=boot_budget(budget_left),
            )
            logger.debug("bluestacks-air restart connected serial=%s", sanitize(serial))

        async def settle(budget_left: float | None) -> None:
            """Settle: wait (bounded) for the first screenshot that decodes —
            ADB may answer while the guest framebuffer is still coming up."""
            await wait_until(
                self._stable_frame,
                operation="restart",
                timeout=boot_budget(budget_left),
            )

        # 1. stop-if-running
        if await phase("restart", "stop_failed", self._is_running):
            await phase("restart", "stop_failed", lambda left: self.stop(timeout=left))
        # 2. configure-while-stopped (unconditional funnel)
        await phase("restart", "configure_failed", lambda left: self.configure(None, timeout=left))
        # 3. start (detached launch)
        await phase("restart", "start_failed", lambda left: self.start(timeout=left))
        # 4. attach: private-server registration
        await phase("restart", "attach_failed", attach)
        # 5. settle: first screenshot decodes non-empty (public funnel)
        await phase("restart", "settle_failed", settle)
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
            # Same bounded settle probe: ADB can flap offline for a moment
            # right after boot, and the terminal check is still readiness.
            await phase("restart", "settle_failed", settle)
        logger.debug("restart ready ref=%s", sanitize(str(self._ref)))

    # ── lifecycle close ──

    async def aclose(self) -> None:
        """Idempotent; releases transport + the untracked spawn handle ONLY
        (never stops the emulator)."""
        await self._executor.close()
        self._spawned = None
        await super().aclose()

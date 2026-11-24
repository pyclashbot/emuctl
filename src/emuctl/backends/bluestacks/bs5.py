"""§1/§3/§4/§6 — BlueStacks 5 backend adapter (Windows; ``[bluestacks]`` extra).

A concrete :class:`emuctl.port.AdbController` subclass (§6 sharing tiers:
all ADB-speaking adapters inherit the ONE middle layer — there is no second)
binding the private-server transport facts onto the Windows HD lifecycle.
Vendor evidence is py-clash-bot's ``pyclashbot/emulators/bluestacks.py``
(file:line cited throughout); where that controller's shape is
contract-BANNED (``shell=True`` strings, image-wide kills, stdout-prose
control flow, sleeps/retries), the same fact is reposed in the ``_proc.py``
executor's argv-only, bounded, scoped terms. The evidence file is FACTS,
never a template.

Vendor-pinned facts (from the evidence file):

- Windows install root = registry ``SOFTWARE\\BlueStacks_nxt -> InstallDir``
  holding ``HD-Player.exe`` + ``HD-Adb.exe`` (bluestacks.py:104-125); the
  conf store resolves from ``DataDir`` (bluestacks.py:127-143).
- Instances launch as ``HD-Player.exe --instance <internal>``
  (bluestacks.py:556-574); the internal name rides the Multi-Instance
  Manager metadata (display ``Name`` ↔ ``InstanceName``, :210-218) with the
  conf's ``display_name`` as the pinned fallback (:205-208); the
  per-instance ADB PORT rides ``bst.instance.<i>.status.adb_port``
  (:220-239). The PRIVATE ADB SERVER, by contrast, is the family-wide
  port 5041 (``BlueStacksConfig.adb_server_port`` §4; class pin :32).
- Run state + stop are judged per-window: tasklist CSV over
  ``HD-Player.exe`` adjudicated by EXACT window title == the bound key
  (:531-554 — the vendor's "parsed CSV from tasklist" precedent: a
  structured machine surface, the same tier as MEmu's ``listvms`` rows);
  stop is ``taskkill /FI "WINDOWTITLE eq <key>" /IM HD-Player.exe /F``
  (:587-593). The vendor's image-wide ``taskkill /IM`` calls (:313-317)
  and every ``shell=True`` string are contract-banned shapes never ported
  here.

Interpretation decisions (CONTRACT wins; these fill its silences):

- Kill scope (§2, quoted): *"Kill scope = the bound instance's
  discriminator: our-spawned PIDs, or for vendor-owned processes the bound
  key as discriminator (window-title-equal bound key, internal instance
  name, per-VM vendor stop addressed by identity). Sweeps without a
  bound-instance discriminator (``taskkill /IM MEmuHeadless.exe``-style)
  are banned."* The stop above keeps its image-name filter only as the
  vendor tool's drill-down; the discriminator is the window title equal
  to the bound key — a §2-sanctioned vendor-owned-process shape. The
  CONTRACT wins over the ticket's stricter "PIDs only" gloss: no
  machine-readable PID surface exists for a foreign HD-Player without
  banned stdout parsing, and the contract expressly names this
  discriminator.
- ``tasklist`` CSV is the sanctioned structured row surface (memu.py
  precedent); rows adjudicate window-title == bound key EXACTLY; header
  and malformed rows are skipped, never laundered. Nonzero rc raises the
  failed observation — it never becomes negative data (§1.4).
- ``restart()`` is the §1.6 wholesale override (memu.py's structured
  twin): stop-if-running → configure-while-stopped (conf writes must land
  while the instance is AT REST — the vendor enforces the same ordering,
  bluestacks.py:396-411) → start → attach (derive the serial from the
  store, then ``hd-adb connect`` against the private 5041 server) →
  settle → [app] → terminal. InstanceNotFoundError propagates raw
  (identity, not readiness — memu.py's reasoned addition).
- ``_start`` is a DETACHED spawn through the executor's ``spawn_detached``
  member (the child outlives the call — an awaited ``communicate()`` would
  block until the emulator exits and then reap it; a GUI launch must not go
  through ``ProcessExecutor.run``). The handle is retained UNTRACKED on
  ``self`` (protection from GC pipe decay); ``aclose()`` releases it
  without terminating — close() never stops the emulator (§1).
- ``_is_running`` is our spawn handle first (``returncode is None`` =
  alive — the §2 "our-spawned PIDs" truth, zero subprocess I/O); a
  foreign/unowned launch falls through to the scoped ``tasklist`` rows.
- No CREATE: provisioning has no unattended vendor verb (the vendor
  evidence refuses to create unattended too, bluestacks.py:386-394), so
  ``create()`` raises the derived FeatureNotSupportedError through the
  port funnel — honest, never a silent no-op.
- ``configure`` (§4) writes ``bluestacks.conf`` through the family
  translation table (:mod:`emuctl.backends.bluestacks._conf`), always
  scoped to the bound instance's internal name. ``bst.enable_adb_access``
  and friends are NOT silently forced (see _conf's module docstring).
- Windows gating truth is the §3 registry row ALONE; path resolution in
  :mod:`._paths` returns honest Nones off-Windows, typed refusals fire at
  use-time.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from types import MappingProxyType
from typing import Any, Final, NamedTuple, TypeVar

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

__all__ = ["BlueStacks5Controller", "list_instances", "get_instance"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The image-name drill-down filters (the VENDOR TOOL's scope, not the kill
#: discriminator — that is the bound key; see the module docstring's §2 quote).
_PLAYER_EXE: Final[str] = "HD-Player.exe"
_HD_ADB_EXE: Final[str] = "HD-Adb.exe"
#: The WINDOW-TITLE column check is case-sensitive on the title, exact (§2's
#: "window-title-equal bound key"); the image name is case-folded.
_HEADER_IMAGE: Final[str] = "image name"


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
    """The bound backend subconfig, defaulted (§4: private server 5041)."""
    return cfg.bluestacks if cfg.bluestacks is not None else BlueStacksConfig()


# ── store access (the sanctioned conf-FILE reads — machine-readable files,
# never vendor stdout) ──


def _conf_document(
    cfg: BlueStacksConfig,
) -> tuple[_conf.ConfDocument, Any]:
    """(conf doc, MIM meta) for the host platform's store.

    Raises ``ConfigError(reason="unreadable")`` when conf store resolution
    yields no candidate on this host, or the file itself is missing (an op
    that needs the conf truth cannot silently proceed); malformed content
    raises typed (never laundered). Metadata: parsed when present, cold
    synthesized in memory otherwise (see _meta).
    """
    path = _paths.conf_path(cfg)
    if path is None or not path.is_file():
        raise ConfigError(
            layer="bluestacks",
            path=str(path) if path is not None else "bluestacks.conf",
            reason="unreadable",
            value=None,
        )
    doc = _conf.parse(path.read_text(encoding="utf-8", errors="replace"))
    meta_path = _paths.mim_meta_path(cfg)
    if meta_path is not None and meta_path.is_file():
        return doc, _meta.parse(meta_path.read_text(encoding="utf-8", errors="replace"))
    return doc, _meta.synthesize(doc)  # cold: in-memory synthesis (§ _meta)


def _instance_record(backend: BackendId, key: str, internal: str, port: int | None) -> InstanceRecord:
    """One enumeration/pre-check row: transport serial stays an OBSERVATION
    (None until a controller resolves it) per §3's identity rule."""
    details = {"internal": internal, **({"adb_port": str(port)} if port else {})}
    return InstanceRecord(
        ref=InstanceRef(backend=backend, key=key),
        running=False,
        serial=None,
        details=MappingProxyType(details),
    )


def _lookup(doc: _conf.ConfDocument, meta: Any, key: str) -> tuple[str, int]:
    """Key → (internal, adb port): MIM metadata first, then the conf's
    ``display_name`` (vendor shape); a miss raises the typed stale-ref
    error; a missing/nonsensical port raises the typed conf error."""
    internal = _meta.internal_for(meta, key) or _conf.internal_by_display(doc, key)
    if internal is None:
        raise InstanceNotFoundError(instance_ref=f"bluestacks:{key}")
    return internal, _conf.instance_adb_port(doc, internal)


# ── discovery (BackendDiscovery convention; enumerate = honest rows, a
# missing store candidate or file = no rows, malformed = raised) ──


async def list_instances(config: EmuctlConfig) -> tuple[InstanceRecord, ...]:
    """Enumerate plausibly-present BlueStacks 5 instances from the conf store.

    Read-only, bounded, OFFLINE: run state is never parsed from a conf
    file, so rows carry ``running=False`` (§3: discovery answers "what
    plausibly exists"; readiness begins at construction). No conf store
    candidate on this host (e.g. no Windows registry) is an HONEST empty
    enumeration — never a raise; store errors propagate (``discover()``
    isolates them into rows).
    """
    cfg = _require_config(config)
    store_path = _paths.conf_path(_bs_config(cfg))
    if store_path is None or not store_path.is_file():
        return ()
    doc = _conf.parse(store_path.read_text(encoding="utf-8", errors="replace"))
    meta_path = _paths.mim_meta_path(_bs_config(cfg))
    meta = (
        _meta.parse(meta_path.read_text(encoding="utf-8", errors="replace"))
        if meta_path is not None and meta_path.is_file()
        else _meta.synthesize(doc)
    )
    records: list[InstanceRecord] = []
    for internal in _conf.instance_internals(doc):
        try:
            port: int | None = _conf.instance_adb_port(doc, internal)
        except ConfigValidationError:
            port = None  # port keys absent for this instance: no serial truth here
        display = _conf.display_name(doc, internal) or internal
        mixin = meta
        named = next((e.name for e in mixin.entries if e.internal == internal), None)
        records.append(_instance_record(BackendId.BLUESTACKS, named or display, internal, port))
    logger.debug("bluestacks list_instances rows=%d", len(records))
    return tuple(records)


async def get_instance(config: EmuctlConfig, ref: InstanceRef) -> InstanceRecord | None:
    """The record for ``ref`` or ``None`` when absent (never raises it).

    Non-bluestacks refs are not ours → ``None`` (no cross-backend fan-out).
    """
    if not isinstance(ref, InstanceRef):
        raise InvalidArgumentError(
            parameter="ref",
            constraint="must be an InstanceRef",
        )
    if ref.backend is not BackendId.BLUESTACKS:
        return None
    for record in await list_instances(config):
        if record.ref.key == ref.key:
            return record
    return None


# ── the concrete adapter ──


class _TaskRow(NamedTuple):
    """One adjudicated tasklist CSV row (image name + window title)."""

    image: str
    title: str


class BlueStacks5Controller(_AdbMiddleLayer):
    """The concrete BlueStacks 5 adapter (Windows; §1 AdbController subclass).

    Declares BOOT + CONFIGURE plus the inherited ADB mechanics (SCREENSHOT /
    INPUT / APPS / APK_INSTALL). No CREATE (see module docstring).
    Construction binds ref + config only — no discovery, no boot, no probe,
    never raises EmulatorNotReadyError; the internal name, the bundled tool
    paths and the transport serial all resolve lazily on first use.
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

        ``_executor`` is the §6 injection seam (recorded-fixture tier). The
        private server port comes from ``bluestacks.adb_server_port`` (§4:
        the backend's table wins for its own backend — 5041, never the §1
        system-server shape); the adb host stays the system ``adb.host``.
        """
        super().__init__(ref)
        cfg = _require_config(config)
        _validate_vm_name(ref.key)  # the §3 spec row's key rule, bound here too
        self._config = cfg
        self._bs: BlueStacksConfig = _bs_config(cfg)
        self.adb_path: str | None = None  # bundled HD-Adb, resolved lazily
        self.adb_server_port: int | None = self._bs.adb_server_port
        self._adb_host: str = cfg.adb.host
        self._serial: str = ""
        self._target: tuple[str, int] | None = None  # (internal, adb port)
        self._spawned: _ProcessHandle | None = None  # retained, untracked
        self._executor = _executor if _executor is not None else ProcessExecutor()
        logger.debug("bind bluestacks ref=%s", sanitize(str(ref)))

    # ── bundle resolution (typed refusals name every searched location) ──

    def _hd_adb_path(self) -> str:
        """Bundled HD-Adb: explicit ``install_dir`` → registry → typed refusal."""
        candidate = _paths.tool_path(self._bs, _HD_ADB_EXE)
        if candidate is not None:
            return str(candidate)
        raise ExecutableNotFoundError(
            tool=_HD_ADB_EXE,
            searched=[
                "bluestacks.install_dir (explicit)",
                r"registry SOFTWARE\BlueStacks_nxt\InstallDir",
            ],
            detail="the bundled HD-Adb.exe was not found (set bluestacks.install_dir)",
        )

    def _player_path(self) -> str:
        """Bundled HD-Player: explicit ``install_dir`` → registry → refusal."""
        candidate = _paths.tool_path(self._bs, _PLAYER_EXE)
        if candidate is not None:
            return str(candidate)
        raise ExecutableNotFoundError(
            tool=_PLAYER_EXE,
            searched=[
                "bluestacks.install_dir (explicit)",
                r"registry SOFTWARE\BlueStacks_nxt\InstallDir",
            ],
            detail="HD-Player.exe was not found (set bluestacks.install_dir)",
        )

    def _target_and_port(self) -> tuple[str, int]:
        """(internal, adb port) for the BOUND key, cached. See ``_lookup``."""
        if self._target is not None:
            return self._target
        doc, meta = _conf_document(self._bs)
        internal, port = _lookup(doc, meta, self._ref.key)
        self._target = (internal, port)
        logger.debug(
            "bluestacks target internal=%s port=%s",
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
        """Middle-layer runner with the bundled-binary lazy resolution.

        Device verbs auto-resolve the transport serial first (a bounded
        conf-store lookup within the caller's slice); server verbs never do.
        Scoping, the per-call serial re-check and cancel discipline are
        inherited unchanged; the ``-P`` prefix always carries the private
        server port (5041 §4 pin) and ``-H`` the system ``adb.host``.
        """
        if self.adb_path is None:
            self.adb_path = self._hd_adb_path()
        if not server_verb and not self._serial:
            await self._ensure_serial(timeout)
        return await super()._run_adb(
            tail,
            binary=binary,
            operation=operation,
            timeout=timeout,
            server_verb=server_verb,
        )

    async def _ensure_serial(self, timeout: float | None) -> None:
        """Derive the transport serial (``127.0.0.1:<instance port>``) from
        the store: a bound key mapping to nothing is a stale ref ⇒ typed
        InstanceNotFoundError (§3 identity lifetime), never a laundered
        connect against a guessed port. Local file reads are immediate;
        the one op deadline remains the adb runner's (the runner is the
        caller that funnels here)."""
        if self._serial:
            return
        record = await self._get_instance(self._ref, timeout)
        if record is None:
            raise InstanceNotFoundError(instance_ref=str(self._ref))
        self._serial = f"127.0.0.1:{record.details['adb_port']}"
        logger.debug("bluestacks transport serial=%s", sanitize(self._serial))

    async def _get_instance(self, ref: InstanceRef, timeout: float | None) -> InstanceRecord | None:
        """The transport-derivation seam keyed on the conf store (records
        carry ``running=False`` — the store is not run-state truth). A
        stale key maps to ``None``; store-unreadable/missing conf raises
        honestly (the observer failed — never negative data, §1.4)."""
        if not isinstance(ref, InstanceRef):
            raise InvalidArgumentError(
                parameter="ref",
                constraint="must be an InstanceRef",
            )
        if ref.backend is not BackendId.BLUESTACKS:
            return None
        doc, meta = _conf_document(self._bs)
        try:
            internal, port = _lookup(doc, meta, ref.key)
        except InstanceNotFoundError:
            return None
        return _instance_record(ref.backend, ref.key, internal, port)

    async def _vendor_run(
        self,
        argv: Sequence[str],
        *,
        binary: bool = False,
        timeout: TimeoutParam = DEFAULT,
    ) -> bytes | str:
        """Ungated internal seam: the bundled HD-Adb transport (device tails
        auto-scoped with the bound serial; server verbs pass through)."""
        args = _require_argv(argv)
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="vendor_run")
        return await self._run_adb(args, binary=binary, operation="vendor_run", timeout=budget)

    # ── scoped process tools (tasklist/taskkill tiers — argv-only) ──

    def _taskkill_argv(self) -> list[str]:
        """The §2-sanctioned stop: window-title-equal bound key + the vendor
        tool's image drill-down (see module docstring)."""
        return [
            "taskkill",
            "/FI",
            f"WINDOWTITLE eq {self._ref.key}",
            "/IM",
            _PLAYER_EXE,
            "/F",
        ]

    def _tasklist_argv(self) -> list[str]:
        """The structured run-state surface: CSV rows, image drill-down."""
        return [
            "tasklist",
            "/V",
            "/FI",
            f"IMAGENAME eq {_PLAYER_EXE}",
            "/FO",
            "CSV",
        ]

    async def _tool_run(self, argv: list[str], *, operation: str, timeout: float | None) -> ProcessResult:
        """One bounded argv-only scoped-tool run through the executor."""
        args = _require_argv(argv)
        logger.debug("bluestacks op=%s argv=%s", sanitize(operation), [sanitize(a) for a in args])
        try:
            return await self._executor.run(args, operation=operation, timeout=timeout)
        except OSError as exc:
            raise ExecutableNotFoundError(
                tool=args[0],
                searched=[args[0]],
                detail=f"could not execute the tool binary: {type(exc).__name__}",
            ) from exc

    @staticmethod
    def _task_rows(stdout: bytes) -> list[_TaskRow]:
        """Parse tasklist CSV output into adjudicable rows (pure; the
        structured tier, memu.py's listvms precedent). Header + malformed
        rows are skipped, never laundered."""
        text = stdout.decode("utf-8", errors="replace")
        rows: list[_TaskRow] = []
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            image = row[0].strip()
            if not image or image.lower() == _HEADER_IMAGE:
                continue
            rows.append(_TaskRow(image=image.lower(), title=row[-1].strip()))
        return rows

    # ── BOOT mechanics ──

    async def _start(self, timeout: float | None) -> None:
        """DETACHED launch: ``HD-Player.exe --instance <internal>``.

        Spawned through the executor's ``_spawn`` seam and never awaited
        (the child must outlive the call — see the module docstring).
        Argv-only: the vendor's shell-string launch, bluestacks.py:569-573,
        is never ported.
        """
        del timeout  # the spawn seam is immediate; the boot promise is not await-based
        internal, _port = self._target_and_port()
        argv = _require_argv([self._player_path(), "--instance", internal])
        logger.debug("bluestacks start argv=%s", [sanitize(a) for a in argv])
        self._spawned = await self._executor.spawn_detached(argv, operation="bluestacks start")
        return None

    async def _stop(self, timeout: float | None) -> None:
        """Scoped kill: the window-title-equal bound-key taskkill (§2 quote
        in the module docstring). Nonzero rc raises honestly — no
        already-stopped laundering, no sweep, no stdout consultation."""
        argv = self._taskkill_argv()
        result = await self._tool_run(argv, operation="stop", timeout=timeout)
        if result.returncode != 0:
            raise VendorCommandError(
                command=argv,
                returncode=result.returncode,
                output_reason="exit",
                stderr_excerpt=_stderr_excerpt(result.stderr),
            )

    async def _is_running(self, timeout: float | None) -> bool:
        """Run-state observation: OWN handle first, then scoped tasklist rows
        (window title EXACTLY the bound key). Nonzero rc raises (a failed
        observation is never negative data, §1.4)."""
        if self._spawned is not None:
            return self._spawned.returncode is None
        argv = self._tasklist_argv()
        result = await self._tool_run(argv, operation="is_running", timeout=timeout)
        if result.returncode != 0:
            raise VendorCommandError(
                command=argv,
                returncode=result.returncode,
                output_reason="exit",
                stderr_excerpt=_stderr_excerpt(result.stderr),
            )
        return any(
            row.image == _PLAYER_EXE.lower() and row.title == self._ref.key
            for row in self._task_rows(result.stdout)
        )

    # ── CONFIGURE mechanic (§4; instance-scoped conf writes) ──

    async def _configure(self, spec: ConfigSpec | None, timeout: float | None) -> DisplayState:
        """Translate the overlay (or the bound config's non-None leaves when
        ``spec=None``) through the family table and write ``bluestacks.conf``
        scoped to the bound instance's internal name.

        Renderer AUTO resolves HERE through the core §4 platform pin
        (BlueStacks/Windows → DIRECTX) before the family token table sees
        it. Classification and value errors fire BEFORE any I/O; no-change
        translates skip the write entirely (no vendor churn).
        """
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
        doc, _meta_view = _conf_document(self._bs)
        new_doc = _conf.translate(doc, internal, translated)
        if new_doc == doc:
            return DisplayState(None, None, None)
        store_path = _paths.conf_path(self._bs)
        assert store_path is not None  # _conf_document() would have raised otherwise
        logger.debug(
            "bluestacks configure internal=%s leaves=%s",
            sanitize(internal),
            [sanitize(k) for k, _v in translated],
        )
        store_path.write_text(_conf.serialize(new_doc), encoding="utf-8")
        # The vendor store exposes no machine-readable previous-geometry readback.
        return DisplayState(None, None, None)

    @staticmethod
    def _resolve_auto(key: str, value: object) -> RendererMode:
        """AUTO → §4's Windows pin (BlueStacks → DIRECTX); concrete passes."""
        mode = _conf.renderer_mode(key, value)
        resolved = resolve_renderer(mode, BackendId.BLUESTACKS) or mode
        if resolved is RendererMode.AUTO:  # core pin covers BlueStacks; belt for drift
            resolved = RendererMode.DIRECTX  # pragma: no cover
        return resolved

    # ── BOOT primitive: §1.6 wholesale override (vendor ordering) ──

    async def restart(
        self,
        *,
        app: str | None = None,
        ready: ReadyCheck | None = None,
        timeout: TimeoutParam = DEFAULT,
    ) -> None:
        """ONE attempt: None or EmulatorNotReadyError — never False (§1.6).

        The port's phase chain with the vendor ordering inserted: stop-
        if-running → configure-while-stopped (writes must land before the
        player reads its conf) → start (detached launch) → attach (private-
        server connect: derive the serial from the store, then
        ``hd-adb connect``) → settle (first screenshot decodes) → [app]
        → terminal check. All phases share the ONE remaining deadline; the
        override inherits no default body and re-invokes the BOOT gate at
        its head. InstanceNotFoundError propagates raw (identity failure —
        memu.py's reasoned addition, TRACKED PAIR with the port's phase()
        re-raise list)."""
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
            # TRACKED PAIR with the port's and memu's phase() re-raise lists
            # (§6 sharing tiers — keep all three files in sync when editing).
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
            """The private-server connect phase (the vendor's kill-server +
            connect rotation, bluestacks.py:497-514, reposed as ONE bounded
            argv registration: no resets, no stdout consultation)."""
            record = await self._get_instance(self._ref, budget_left)
            if record is None:
                raise InstanceNotFoundError(instance_ref=str(self._ref))
            serial = f"127.0.0.1:{record.details['adb_port']}"
            self._serial = serial
            await self._run_adb(
                ["connect", serial],
                binary=False,
                operation="restart",
                timeout=budget_left,
                server_verb=True,
            )
            logger.debug("bluestacks restart connected serial=%s", sanitize(serial))

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

    # ── lifecycle close ──

    async def aclose(self) -> None:
        """Idempotent; releases transport + the untracked spawn handle ONLY
        (never stops the emulator — the GUI child keeps its own life)."""
        await self._executor.close()
        self._spawned = None
        await super().aclose()

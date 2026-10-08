"""§1/§3/§6 — Plain-ADB backend: the thin concrete adapter + BackendDiscovery.

The ONE ADB middle layer lives CORE-side (:class:`emuctl.port.AdbController`,
§6 sharing tiers: "core, port-adjacent — all ADB-speaking adapters inherit; no
second middle layer"), carrying the interaction primitives, the scoping
machinery, and the mechanics once over raw ADB. THIS module is the thin
concrete plain-ADB adapter: a subclass binding the transport construction
(§1: ``config.adb`` is the ONLY system-server selector), the declared
FEATURES set, the concrete ``_vendor_run`` through the ``_proc.py`` executor, and the
module-level ``BackendDiscovery`` seam (discovery is backend-specific; it
stays here).

The registry pins this module's ``adapter_attr`` to ``"AdbController"``, so
this class doubles as the plain-ADB concrete adapter; future ADB-speaking
adapters (e.g. BlueStacks Air's bundled hd-adb) inherit the core middle layer
directly and fill in their own lifecycle mechanics.

Interpretation decisions (CONTRACT wins; these fill its silences):

- Construction is cheap: no discovery, no boot, no probe, never raises
  ``EmulatorNotReadyError``. ``aclose()`` releases the transport only (sweeps
  executor stragglers; never stops anything).
- ``_vendor_run`` is always device-scoped (auto ``-s``; the scoping argv
  machinery is inherited core-side). Module-level discovery (``adb devices``)
  bypasses it through the runner with ``server_verb=True``.
- ``is_reachable()`` is INHERITED from the core-side middle layer (§6's
  promotion tier): the shared device-scoped ``shell echo`` probe — rc 0 →
  "ok"; expiry → "timeout"; missing binary → "no-adb"; nonzero rc → the
  shared stderr-token scan (unauthorized/offline/disconnected — stderr,
  never stdout); anything else → "disconnected". stdout is never parsed for
  the decision. ``CancelledError`` propagates unmodified.
- Discovery enumeration (``adb devices``) is the sanctioned stdout-reading
  probe: banner lines (``* daemon …`` cold-start/started lines) and the
  ``List of devices attached`` header are skipped position-independently;
  rows become ``InstanceRecord``\\ s with ``running=True`` only for the exact
  state word ``device``; every other state (offline/unauthorized/…) is
  ``running=False`` with the raw word preserved in ``details["adb_state"]``.
  Unparseable lines and non-allowlist serials are skipped, never laundered
  into refs.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl._proc import ProcessExecutor, ProcessResult
from emuctl._serials import is_valid_serial, validate_serial
from emuctl.config.model import EmuctlConfig
from emuctl.errors import (
    InvalidArgumentError,
    VendorCommandError,
)
from emuctl.log import sanitize
from emuctl.port import AdbController as _AdbMiddleLayer
from emuctl.port import CapabilityFlag, ProbeResult, _stderr_excerpt
from emuctl.timing import (
    DEFAULT,
    QUERY_TIMEOUT,
    TimeoutParam,
    resolve_timeout,
)

__all__ = ["AdbController", "list_instances", "get_instance"]

logger = logging.getLogger(__name__)

#: Cold-start daemon banner marker (position-skip; never a discovery row).
_BANNER_MARKER: Final[str] = "*"

_DEVICES_HEADER: Final[str] = "List of devices attached"


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


class AdbController(_AdbMiddleLayer):
    """The plain-ADB concrete adapter over the core-side middle layer.

    Declares exactly what plain ADB supports: SCREENSHOT / INPUT / APPS /
    APK_INSTALL. No BOOT / CREATE / CONFIGURE — ``restart()``/``start()``/
    ``stop()``/``create()``/``configure()`` raise the derived
    ``FeatureNotSupportedError`` through the port funnel (§1 invariant 3,
    never a silent skip); ``_attach`` inherits the port no-op. All ADB
    mechanics and scoping are inherited from the core middle layer; only the
    transport construction and the concrete ``_vendor_run`` are bound here.
    """

    FEATURES: frozenset[CapabilityFlag] = frozenset(
        {
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

        ``config`` is the ``connect()`` convention (selection already ran the
        registry + capability gates). ``_executor`` is the single private
        injection seam (transport-constructor-level per §6): tests pass
        ``ProcessExecutor(_spawn=...)``; production uses the real spawner.
        """
        super().__init__(ref)
        cfg = _require_config(config)
        validate_serial(ref.key)  # fail fast; per-call re-checks follow
        self._config = cfg
        self.adb_path: str | None = cfg.adb.path
        self.adb_server_port: int | None = cfg.adb.port
        self._adb_host: str = cfg.adb.host
        self._serial: str = ref.key
        self._executor = _executor if _executor is not None else ProcessExecutor()
        logger.debug("bind adb ref=%s", sanitize(str(ref)))

    async def _vendor_run(
        self,
        argv: Sequence[str],
        *,
        binary: bool = False,
        timeout: TimeoutParam = DEFAULT,
    ) -> bytes | str:
        """Ungated internal seam: always device-scoped (auto ``-s``).

        Server verbs never flow through here — module-level discovery uses
        the runner with ``server_verb=True`` instead. ``timeout`` resolves
        against the query group (mechanics carry their own op-group budgets
        and call the runner directly).
        """
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="vendor_run")
        return await self._run_adb(argv, binary=binary, operation="vendor_run", timeout=budget)

    async def is_reachable(self, *, timeout: TimeoutParam = DEFAULT) -> ProbeResult:
        """Probe as data — never raises on observation failure (probes exemption).

        INHERITED from the core-side middle layer (§6 promotion tier): the
        shared device-scoped ``shell echo`` probe, unchanged for plain ADB.
        This explicit super() delegation is the conformance anchor — the
        private probe copy (probe word + stderr classifier + catch-all
        mapping) now lives once in ``emuctl.port.AdbController``.
        """
        return await super().is_reachable(timeout=timeout)

    async def aclose(self) -> None:
        """Idempotent; releases transport only (never stops the emulator)."""
        await self._executor.close()
        await super().aclose()


def _discovery_argv(cfg: EmuctlConfig) -> list[str]:
    """Server-verb argv for ``adb devices`` from the resolved config (pure)."""
    binary = cfg.adb.path if cfg.adb.path else "adb"
    return [binary, "-H", cfg.adb.host, "-P", str(cfg.adb.port), "devices"]


def _device_rows(stdout: bytes) -> list[tuple[str, str]]:
    """Parse ``adb devices`` stdout into (serial, state) rows (pure).

    Banner/header lines are skipped position-independently (a cold server
    prints ``* daemon …`` lines BEFORE the header, so a naive
    ``lines[1:]`` clip cannot be relied on); unparseable and
    non-allowlist-serial lines are dropped, never laundered.
    """
    text = stdout.decode("utf-8", errors="replace")
    body = [
        line
        for line in text.splitlines()
        if line.strip()
        and not line.strip().startswith(_BANNER_MARKER)
        and line.strip() != _DEVICES_HEADER
    ]
    rows: list[tuple[str, str]] = []
    for line in body:
        parts = line.split()
        if len(parts) < 2 or not is_valid_serial(parts[0]):
            continue
        rows.append((parts[0], parts[1]))
    return rows


async def list_instances(
    config: EmuctlConfig, *, _executor: ProcessExecutor | None = None
) -> tuple[InstanceRecord, ...]:
    """Enumerate plausibly-present devices via ``adb devices`` (read-only, bounded).

    The sanctioned stdout-reading probe (§3): cold ``adb devices`` may start
    the local ADB server (documented exception). Vendor/process failures
    propagate (``discover()`` isolates them into ``BackendStatus`` rows).
    """
    cfg = _require_config(config)
    executor = _executor if _executor is not None else ProcessExecutor()
    argv = _discovery_argv(cfg)
    logger.debug("adb op=list_instances argv=%s", [sanitize(a) for a in argv])
    result: ProcessResult = await executor.run(
        argv, operation="list_instances", timeout=QUERY_TIMEOUT
    )
    if result.returncode != 0:
        raise VendorCommandError(
            command=argv,
            returncode=result.returncode,
            output_reason="exit",
            stderr_excerpt=_stderr_excerpt(result.stderr),
        )
    records: list[InstanceRecord] = []
    for serial, state in _device_rows(result.stdout):
        records.append(
            InstanceRecord(
                ref=InstanceRef(backend=BackendId.ADB, key=serial),
                running=state == "device",
                serial=serial,
                details={"adb_state": state},
            )
        )
    logger.debug("adb list_instances rows=%d", len(records))
    return tuple(records)


async def get_instance(
    config: EmuctlConfig, ref: InstanceRef, *, _executor: ProcessExecutor | None = None
) -> InstanceRecord | None:
    """Return the record for ``ref`` or ``None`` when absent (never raises it).

    Non-ADB refs are not ours → ``None`` (no cross-backend fan-out).
    """
    if not isinstance(ref, InstanceRef):
        raise InvalidArgumentError(
            parameter="ref",
            constraint="must be an InstanceRef",
        )
    if ref.backend is not BackendId.ADB:
        return None
    for record in await list_instances(config, _executor=_executor):
        if record.ref.key == ref.key:
            return record
    return None

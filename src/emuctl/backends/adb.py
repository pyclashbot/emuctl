from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from emuctl._adb import _server_host_args, _stderr_excerpt
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
from emuctl.port import CapabilityFlag, ProbeResult
from emuctl.timing import (
    DEFAULT,
    QUERY_TIMEOUT,
    TimeoutParam,
    resolve_timeout,
)

__all__ = ["AdbController", "list_instances", "get_instance"]

logger = logging.getLogger(__name__)

_BANNER_MARKER: Final[str] = "*"

_DEVICES_HEADER: Final[str] = "List of devices attached"


def _require_config(config: EmuctlConfig | None) -> EmuctlConfig:
    if config is None:
        return EmuctlConfig()
    if not isinstance(config, EmuctlConfig):
        raise InvalidArgumentError(
            parameter="config",
            constraint="must be an EmuctlConfig or None",
        )
    return config


class AdbController(_AdbMiddleLayer):
    FEATURES: frozenset[CapabilityFlag] = frozenset(
        {
            CapabilityFlag.SCREENSHOT,
            CapabilityFlag.INPUT,
            CapabilityFlag.APPS,
            CapabilityFlag.APK_INSTALL,
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
        super().__init__(ref)
        cfg = _require_config(config)
        validate_serial(ref.key)
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
        budget = resolve_timeout(timeout, QUERY_TIMEOUT, operation="vendor_run")
        return await self._run_adb(argv, binary=binary, operation="vendor_run", timeout=budget)

    async def is_reachable(self, *, timeout: TimeoutParam = DEFAULT) -> ProbeResult:
        return await super().is_reachable(timeout=timeout)

    async def aclose(self) -> None:
        await self._executor.close()
        await super().aclose()


def _discovery_argv(cfg: EmuctlConfig) -> list[str]:
    binary = cfg.adb.path if cfg.adb.path else "adb"
    return [binary, *_server_host_args(cfg.adb.host), "-P", str(cfg.adb.port), "devices"]


def _device_rows(stdout: bytes) -> list[tuple[str, str]]:
    text = stdout.decode("utf-8", errors="replace")
    body = [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(_BANNER_MARKER) and line.strip() != _DEVICES_HEADER
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
    cfg = _require_config(config)
    executor = _executor if _executor is not None else ProcessExecutor()
    argv = _discovery_argv(cfg)
    logger.debug("adb op=list_instances argv=%s", [sanitize(a) for a in argv])
    result: ProcessResult = await executor.run(argv, operation="list_instances", timeout=QUERY_TIMEOUT)
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

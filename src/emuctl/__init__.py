"""emuctl — vendor-agnostic Android emulator controller library.

The public surface (CONTRACT §5's "the one public surface" plus the §3/§4
data types):

- Construction: :func:`connect` (async; the sync twin ``connect_sync`` is
  §5-derived, owned by the sync PR — deliberately NOT exported here).
- Enumeration: :func:`discover` is the ONLY enumeration path (§1 invariant 11,
  §3 Boundary) — the registry/selection machinery stays internal.
- Identity (§3): ``BackendId`` / ``InstanceRef`` / ``InstanceRecord`` and the
  platform truth ``Platform`` / ``current_platform()``.
- Failures (§2): the full error taxonomy + ``resolve_error_code``.
- Config (§4): the model classes + :func:`resolve_config` /
  ``ConfigWarning`` / :func:`ensure_config_supported`.
- Port pins (§1): ``CapabilityFlag``, ``ReadyCheck``, ``ProbeResult`` and the
  port ABCs (consumers type against :class:`AsyncEmulatorController`).
- Timing (§2): the ``DEFAULT`` sentinel + group constants.
- Serial validation (§3): :func:`is_valid_serial` / :func:`validate_serial` —
  the public security control.
- Screenshot data (§4): :func:`png_dimensions`.

Import discipline (§6 lazy package rule / §1 invariant 11): importing this
package loads NO ``emuctl.backends.*`` modules — adapters are imported
lazily, only inside :func:`connect` / :func:`discover`, exclusively through
the registry's ``load_adapter``. No I/O, no handlers, no stdout at import
(§2 logging discipline; consumers attach handlers to the ``"emuctl"``
logger).
"""

import logging

_package_logger = logging.getLogger(__name__)
if not any(isinstance(h, logging.NullHandler) for h in _package_logger.handlers):
    _package_logger.addHandler(logging.NullHandler())

from emuctl._discovery import (  # noqa: E402
    BackendStatus,
    DiscoveryReport,
    connect,
    discover,
)
from emuctl._identification import BackendId, InstanceRecord, InstanceRef  # noqa: E402
from emuctl._platforms import Platform, current_platform  # noqa: E402
from emuctl._serials import is_valid_serial, validate_serial  # noqa: E402
from emuctl.config import (  # noqa: E402
    SCHEMA_VERSION,
    AdbConfig,
    BlueStacksConfig,
    BlueStacksSettings,
    ConfigSpec,
    ConfigWarning,
    CreateSpec,
    DisplayConfig,
    EmuctlConfig,
    LogConfig,
    MemuConfig,
    MemuSettings,
    RendererMode,
    ResolvedConfig,
    ResourceConfig,
    resolve_config,
)
from emuctl.config.model import ensure_config_supported  # noqa: E402
from emuctl.errors import (  # noqa: E402
    AmbiguousInstanceError,
    BackendNotAvailableError,
    BackendNotFoundError,
    ConfigError,
    ConfigValidationError,
    ControllerClosedError,
    DeviceStateError,
    EmuctlError,
    EmulatorNotReadyError,
    ExecutableNotFoundError,
    ExtrasMissingError,
    FeatureNotSupportedError,
    InstanceNotFoundError,
    InvalidArgumentError,
    InvalidSerialError,
    OperationTimeoutError,
    SyncCallInsideEventLoopError,
    VendorCommandError,
    resolve_error_code,
)
from emuctl.png import png_dimensions  # noqa: E402
from emuctl.port import (  # noqa: E402
    AdbController,
    AsyncEmulatorController,
    CapabilityFlag,
    ProbeResult,
    ReadyCheck,
)
from emuctl.timing import (  # noqa: E402
    APP_TIMEOUT,
    CONFIG_TIMEOUT,
    DEFAULT,
    INTERACTION_TIMEOUT,
    LIFECYCLE_TIMEOUT,
    PROVISION_TIMEOUT,
    QUERY_TIMEOUT,
    SNAPSHOT_TIMEOUT,
)

__all__: list[str] = [
    "APP_TIMEOUT",
    "AdbConfig",
    "AdbController",
    "AmbiguousInstanceError",
    "AsyncEmulatorController",
    "BackendId",
    "BackendNotAvailableError",
    "BackendNotFoundError",
    "BackendStatus",
    "BlueStacksConfig",
    "BlueStacksSettings",
    "CONFIG_TIMEOUT",
    "CapabilityFlag",
    "ConfigError",
    "ConfigSpec",
    "ConfigValidationError",
    "ConfigWarning",
    "ControllerClosedError",
    "CreateSpec",
    "DEFAULT",
    "DeviceStateError",
    "DiscoveryReport",
    "DisplayConfig",
    "EmuctlConfig",
    "EmuctlError",
    "EmulatorNotReadyError",
    "ExecutableNotFoundError",
    "ExtrasMissingError",
    "FeatureNotSupportedError",
    "InstanceNotFoundError",
    "InstanceRecord",
    "InstanceRef",
    "INTERACTION_TIMEOUT",
    "InvalidArgumentError",
    "InvalidSerialError",
    "LIFECYCLE_TIMEOUT",
    "LogConfig",
    "MemuConfig",
    "MemuSettings",
    "OperationTimeoutError",
    "PROVISION_TIMEOUT",
    "Platform",
    "QUERY_TIMEOUT",
    "ProbeResult",
    "ReadyCheck",
    "RendererMode",
    "ResolvedConfig",
    "ResourceConfig",
    "SCHEMA_VERSION",
    "SNAPSHOT_TIMEOUT",
    "SyncCallInsideEventLoopError",
    "VendorCommandError",
    "connect",
    "current_platform",
    "discover",
    "ensure_config_supported",
    "is_valid_serial",
    "png_dimensions",
    "resolve_config",
    "resolve_error_code",
    "validate_serial",
]

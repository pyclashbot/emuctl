"""§3 — Serial validation: is_valid_serial/validate_serial + the ADB server-verb exclusion list."""

from __future__ import annotations

import re
from typing import Final

from emuctl.errors import InvalidSerialError
from emuctl.log import sanitize

__all__ = ["ADB_SERVER_VERBS", "is_valid_serial", "validate_serial"]

#: Pinned ADB server-verb exclusion list (§1 AdbController): device commands
#: get automatic `-s <serial>` scoping, but these server verbs never do.
#: SINGLE SITE — never sniffed from argv, never redefined elsewhere. The
#: contract pins this exact set: connect, disconnect, devices, start-server,
#: kill-server, version.
ADB_SERVER_VERBS: Final[frozenset[str]] = frozenset(
    {"connect", "disconnect", "devices", "start-server", "kill-server", "version"}
)

#: Reject-by-default allowlist: alphanumerics plus the separators real serial
#: shapes need (`.`, `_`, `-` for USB/emulator serials; `:` for host:port and
#: the documented `transport_id:N` form). Everything else — whitespace,
#: shell metacharacters, controls — is rejected. New shapes require security
#: review + pinned tests; strictness relaxes only deliberately, never by
#: accident. No case folding: `ABC` and `abc` are distinct identities.
_SERIAL_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:-]+")

#: Generous upper bound (real serials are tens of chars); unbounded strings
#: have no business crossing a transport boundary.
_MAX_SERIAL_LENGTH: Final[int] = 256


def _rejection_reason(s: str) -> str | None:
    """Return None when `s` is acceptable, else a static reason token."""
    if not s:
        return "must be non-empty (never laundered into auto)"
    if len(s) > _MAX_SERIAL_LENGTH:
        return f"must be at most {_MAX_SERIAL_LENGTH} characters"
    if _SERIAL_RE.fullmatch(s) is None:
        return "must match allowlist [A-Za-z0-9._:-] with no whitespace"
    return None


def is_valid_serial(s: object) -> bool:
    """Allowlist check for a transport serial: True iff acceptable.

    Pure and zero-I/O. Rejects by default: non-strings, empty/whitespace
    (never stripped — surrounding whitespace invalidates rather than being
    laundered), and shell-flavored strings. `transport_id:N` matches the
    allowlist and is allowed. No case folding is applied.
    """
    if not isinstance(s, str):
        return False
    return _rejection_reason(s) is None


def validate_serial(s: object) -> None:
    """Enforce :func:`is_valid_serial`, raising `InvalidSerialError` otherwise.

    Pure and zero-I/O. The `serial` payload carries the offending value (or
    its sanitized repr for non-strings); `detail` names the static reason.
    """
    if isinstance(s, str) and _rejection_reason(s) is None:
        return None
    reason = _rejection_reason(s) if isinstance(s, str) else "must be a string"
    shown = s if isinstance(s, str) else sanitize(repr(s))
    raise InvalidSerialError(serial=shown, detail=reason)

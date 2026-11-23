from __future__ import annotations

import re
from typing import Final

from emuctl.errors import InvalidSerialError
from emuctl.log import sanitize

__all__ = ["ADB_SERVER_VERBS", "is_valid_serial", "validate_serial"]

ADB_SERVER_VERBS: Final[frozenset[str]] = frozenset(
    {"connect", "disconnect", "devices", "start-server", "kill-server", "version"}
)

_SERIAL_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:-]+")

_MAX_SERIAL_LENGTH: Final[int] = 256


def _rejection_reason(s: str) -> str | None:
    if not s:
        return "must be non-empty (never laundered into auto)"
    if len(s) > _MAX_SERIAL_LENGTH:
        return f"must be at most {_MAX_SERIAL_LENGTH} characters"
    if _SERIAL_RE.fullmatch(s) is None:
        return "must match allowlist [A-Za-z0-9._:-] with no whitespace"
    return None


def is_valid_serial(s: object) -> bool:
    if not isinstance(s, str):
        return False
    return _rejection_reason(s) is None


def validate_serial(s: object) -> None:
    if isinstance(s, str) and _rejection_reason(s) is None:
        return None
    reason = _rejection_reason(s) if isinstance(s, str) else "must be a string"
    shown = s if isinstance(s, str) else sanitize(repr(s))
    raise InvalidSerialError(serial=shown, detail=reason)

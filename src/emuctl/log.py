"""§2 — Logging discipline: NullHandler-only loggers and sanitize()."""

from __future__ import annotations

import logging
import re

__all__ = ["sanitize"]

logger = logging.getLogger(__name__)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize(text: str, *, max_length: int = 200) -> str:
    """Strip CR/LF/control characters and truncate excerpts to `max_length`.

    Dynamic strings (vendor output, free-text detail) pass through here before
    logging or error rendering; counts + ≤200-char excerpts at DEBUG, never
    bodies/binary.
    """
    if not isinstance(text, str):
        text = str(text)
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    if len(cleaned) > max_length:
        return cleaned[: max(max_length - 3, 0)] + "..."
    return cleaned

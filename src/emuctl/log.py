from __future__ import annotations

import re

__all__ = ["sanitize"]

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize(text: str, *, max_length: int = 200) -> str:
    if not isinstance(text, str):
        text = str(text)
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    if len(cleaned) > max_length:
        return cleaned[: max(max_length - 3, 0)] + "..."
    return cleaned

"""§4 — Screenshot data: png_dimensions(data) IHDR reader (stdlib-only)."""

from __future__ import annotations

__all__ = ["png_dimensions"]

_PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))
_IHDR_TYPE = b"IHDR"
_IHDR_DATA_LENGTH = 13
_MIN_LENGTH = 8 + 4 + 4 + _IHDR_DATA_LENGTH  # signature + length + type + IHDR data


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read ``(width, height)`` from a PNG's IHDR chunk without decoding.

    Stdlib-only: validates the 8-byte PNG signature, then requires the first
    chunk to be a 13-byte IHDR and returns its big-endian width/height.
    Trailing bytes (IDAT/IEND/vendor noise) are ignored — dimensions come
    from IHDR alone, so appended noise still decodes. No decoding, no
    repair, no base64, no retry; the CRC is not verified (that would be
    decoding — corruption surfacing elsewhere is the adapter's mapping).

    Raises :class:`ValueError` on malformed input (bad signature, truncated
    input, non-IHDR first chunk, bad IHDR length, zero dimensions) and
    :class:`TypeError` for non-bytes input. Per §4 S2 the adapter layer maps
    this ``ValueError`` to ``VendorCommandError(output_reason="unusable_output")``.
    """
    if not isinstance(data, bytes):
        raise TypeError(f"png_dimensions requires bytes, got {type(data).__name__}")
    if len(data) < len(_PNG_SIGNATURE) or data[:8] != _PNG_SIGNATURE:
        raise ValueError("not a PNG: bad signature")
    if len(data) < _MIN_LENGTH:
        raise ValueError(f"truncated PNG: {len(data)} bytes, need at least {_MIN_LENGTH}")
    length = int.from_bytes(data[8:12], "big")
    chunk_type = data[12:16]
    if chunk_type != _IHDR_TYPE:
        raise ValueError("PNG missing IHDR as first chunk")
    if length != _IHDR_DATA_LENGTH:
        raise ValueError(f"bad IHDR length: {length}")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width == 0 or height == 0:
        raise ValueError("PNG IHDR has zero dimension")
    return (width, height)

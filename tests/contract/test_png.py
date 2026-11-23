import struct
from typing import Any

import pytest

from emuctl.png import png_dimensions

_SIG = bytes((137, 80, 78, 71, 13, 10, 26, 10))


def _ihdr_png(width: int, height: int, *, extra: bytes = b"") -> bytes:
    body = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return _SIG + struct.pack(">I", 13) + b"IHDR" + body + b"\x00\x00\x00\x00" + extra


def test_valid_png_returns_big_endian_dimensions() -> None:
    assert png_dimensions(_ihdr_png(1, 1)) == (1, 1)
    assert png_dimensions(_ihdr_png(419, 633)) == (419, 633)
    assert png_dimensions(_ihdr_png(0x1234, 0x5678)) == (0x1234, 0x5678)


def test_appended_noise_still_decodes_from_ihdr() -> None:
    assert png_dimensions(_ihdr_png(320, 240, extra=b"NOISE" + bytes(128))) == (320, 240)


def test_bytes_like_non_bytes_rejected() -> None:
    raw = _ihdr_png(7, 9)
    for bad in (bytearray(raw), memoryview(raw)):
        with pytest.raises(TypeError):
            png_dimensions(bad)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\x89PNG",
        _SIG,
        _SIG + b"\x00\x00",  # partial chunk header
        _SIG + struct.pack(">I", 13) + b"IHDR" + b"\x00\x01",  # truncated IHDR data
        b"definitely not a png file, just text........",
    ],
    ids=["empty", "short", "sig-only", "partial-header", "truncated-ihdr", "text"],
)
def test_truncated_or_bad_signature_raises_value_error(blob: bytes) -> None:
    with pytest.raises(ValueError):
        png_dimensions(blob)


def test_non_ihdr_first_chunk_raises_value_error() -> None:
    blob = _SIG + struct.pack(">I", 4) + b"IDAT" + b"\x00" * 25
    with pytest.raises(ValueError):
        png_dimensions(blob)


def test_bad_ihdr_length_raises_value_error() -> None:
    blob = _SIG + struct.pack(">I", 5) + b"IHDR" + b"\x00" * 21
    with pytest.raises(ValueError):
        png_dimensions(blob)


@pytest.mark.parametrize("dims", [(0, 10), (10, 0), (0, 0)])
def test_zero_dimension_raises_value_error(dims: tuple[int, int]) -> None:
    with pytest.raises(ValueError):
        png_dimensions(_ihdr_png(*dims))


@pytest.mark.parametrize("bad", ["not bytes", None, 123, ["bytes"]])
def test_non_bytes_raises_type_error(bad: Any) -> None:
    with pytest.raises(TypeError):
        png_dimensions(bad)

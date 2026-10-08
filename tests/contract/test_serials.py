"""§7 conformance (serial security control, §3): allowlist validation + server-verb list."""

from typing import Any

import pytest

from emuctl._serials import ADB_SERVER_VERBS, is_valid_serial, validate_serial
from emuctl.errors import InvalidSerialError


@pytest.mark.parametrize(
    "serial",
    [
        "emulator-5554",
        "127.0.0.1:5555",
        "ABCDEF123456",
        "transport_id:5",
        "transport_id:123",
        "a",
        "0",
        "1A-2_b.3:c",
    ],
)
def test_valid_serials_accepted(serial: str) -> None:
    assert is_valid_serial(serial) is True
    assert validate_serial(serial) is None


def test_no_case_folding_both_cases_are_distinct_but_valid() -> None:
    assert is_valid_serial("ABC") is True
    assert is_valid_serial("abc") is True
    assert validate_serial("ABC") is None
    assert validate_serial("abc") is None


@pytest.mark.parametrize(
    "serial",
    [
        "",
        "   ",
        "\t",
        "\n",
        "a b",
        " adb:1",
        "adb:1 ",
        "a\nb",
        "a\rb",
        "a\x00b",
        "a;b",
        "a|b",
        "a&b",
        "a$b",
        "a`b",
        "$(a)",
        "${a}",
        "a>b",
        "a<b",
        "a*b",
        "a?b",
        "a'b",
        'a"b',
        "a(b)",
        "a{b}",
        "a[b]",
        "a#b",
        "a~b",
        "a/b",
        "a=b",
        "a%b",
        "a^b",
        "a,b",
        "a\\b",
        "a;b rm -rf /",
        "x" * 300,
    ],
)
def test_banned_shapes_rejected(serial: str) -> None:
    assert is_valid_serial(serial) is False
    with pytest.raises(InvalidSerialError) as excinfo:
        validate_serial(serial)
    assert excinfo.value.serial == serial
    assert excinfo.value.error_code == "invalid_serial"


@pytest.mark.parametrize("bad", [None, 123, 5.5, True, b"adb:1", ["adb:1"], ("adb:1",), {"s"}])
def test_non_string_shapes_rejected(bad: Any) -> None:
    assert is_valid_serial(bad) is False
    with pytest.raises(InvalidSerialError):
        validate_serial(bad)


def test_server_verb_exclusion_list_pinned_and_frozen() -> None:
    assert (
        frozenset({"connect", "disconnect", "devices", "start-server", "kill-server", "version"})
        == ADB_SERVER_VERBS
    )
    assert isinstance(ADB_SERVER_VERBS, frozenset)


def test_public_serial_api_bound_at_package_root() -> None:
    import emuctl

    assert emuctl.is_valid_serial is is_valid_serial
    assert emuctl.validate_serial is validate_serial
    assert "is_valid_serial" in emuctl.__all__
    assert "validate_serial" in emuctl.__all__

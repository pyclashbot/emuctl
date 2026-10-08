"""§7 conformance (identity, §3): backend ids, refs, records, no backend imports."""

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl.errors import BackendNotFoundError, InvalidArgumentError

SRC = Path(__file__).resolve().parents[2] / "src" / "emuctl"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("adb", BackendId.ADB),
        ("ADB", BackendId.ADB),
        ("Adb", BackendId.ADB),
        ("memu", BackendId.MEMU),
        ("MEMU", BackendId.MEMU),
        ("Memu", BackendId.MEMU),
        ("bluestacks", BackendId.BLUESTACKS),
        ("BlueStacks", BackendId.BLUESTACKS),
        ("BLUESTACKS", BackendId.BLUESTACKS),
        ("bluestacks-air", BackendId.BLUESTACKS_AIR),
        ("BLUESTACKS_AIR", BackendId.BLUESTACKS_AIR),
        ("bluestacks.air", BackendId.BLUESTACKS_AIR),
        ("Bluestacks_Air", BackendId.BLUESTACKS_AIR),
        ("bluestacks/air", BackendId.BLUESTACKS_AIR),
        ("google-play", BackendId.GOOGLE_PLAY),
        ("GOOGLE_PLAY", BackendId.GOOGLE_PLAY),
        ("Google.Play", BackendId.GOOGLE_PLAY),
        ("avd", BackendId.AVD),
        ("AVD", BackendId.AVD),
    ],
)
def test_backend_id_normalization(raw: str, expected: BackendId) -> None:
    assert BackendId.from_string(raw) is expected


@pytest.mark.parametrize("raw", ["", "   ", "foo", "adb2", "emu", "memu2", "blue-stack"])
def test_backend_id_unknown_raises(raw: str) -> None:
    with pytest.raises(BackendNotFoundError) as excinfo:
        BackendId.from_string(raw)
    assert excinfo.value.backend_id == raw
    assert excinfo.value.error_code == "backend_not_found"


def test_backend_id_non_string_raises() -> None:
    with pytest.raises(BackendNotFoundError):
        BackendId.from_string(None)  # ty: ignore[invalid-argument-type]


def test_backend_id_members_pinned() -> None:
    assert {m.value for m in BackendId} == {
        "adb",
        "memu",
        "bluestacks",
        "google-play",
        "avd",
        "bluestacks-air",
    }


def test_instance_ref_canonical_round_trip() -> None:
    ref = InstanceRef(backend=BackendId.ADB, key="127.0.0.1:5555")
    assert str(ref) == "adb:127.0.0.1:5555"
    assert InstanceRef.parse(str(ref)) == ref
    assert InstanceRef.parse("memu:pyclashbot-136") == InstanceRef(
        backend=BackendId.MEMU, key="pyclashbot-136"
    )
    assert InstanceRef.parse("bluestacks-air:conf") == InstanceRef(
        backend=BackendId.BLUESTACKS_AIR, key="conf"
    )


@pytest.mark.parametrize("bare", ["emulator-5554", "myvm", "ABCDEF123456"])
def test_bare_targets_rejected_with_prefix_hint(bare: str) -> None:
    with pytest.raises(InvalidArgumentError) as excinfo:
        InstanceRef.parse(bare)
    assert excinfo.value.parameter == "target"
    assert "backend:key" in excinfo.value.constraint
    assert "adb:127.0.0.1:5555" in excinfo.value.constraint
    assert excinfo.value.error_code == "invalid_argument"


def test_host_port_without_prefix_rejected_with_prefix_hint() -> None:
    # §3: bare host:port is a non-canonical shape — InvalidArgumentError with
    # the canonical-prefix hint, never BackendNotFoundError or shape inference.
    with pytest.raises(InvalidArgumentError) as excinfo:
        InstanceRef.parse("127.0.0.1:5555")
    assert excinfo.value.parameter == "target"
    assert "backend:key" in excinfo.value.constraint
    assert "adb:127.0.0.1:5555" in excinfo.value.constraint
    assert excinfo.value.error_code == "invalid_argument"


def test_transport_id_serial_without_prefix_rejected_with_prefix_hint() -> None:
    # Same §3 bare-shape rule for the documented `transport_id:N` serial shape:
    # no shape inference — uniformly InvalidArgumentError with the hint.
    with pytest.raises(InvalidArgumentError) as excinfo:
        InstanceRef.parse("transport_id:5")
    assert excinfo.value.parameter == "target"
    assert "backend:key" in excinfo.value.constraint
    assert excinfo.value.error_code == "invalid_argument"


def test_unknown_backend_prefix_rejected_with_prefix_hint() -> None:
    # Parse failures are uniformly InvalidArgumentError; BackendNotFoundError
    # remains the domain of BackendId.from_string + registry selection.
    with pytest.raises(InvalidArgumentError) as excinfo:
        InstanceRef.parse("foo:bar")
    assert excinfo.value.parameter == "target"
    assert "backend:key" in excinfo.value.constraint
    assert excinfo.value.error_code == "invalid_argument"


@pytest.mark.parametrize("bad", ["adb:", ":myvm", ":", ""])
def test_empty_backend_or_key_rejected(bad: str) -> None:
    with pytest.raises(InvalidArgumentError):
        InstanceRef.parse(bad)


def test_identity_is_backend_plus_key_only() -> None:
    adb = InstanceRef(backend=BackendId.ADB, key="shared")
    bs = InstanceRef(backend=BackendId.BLUESTACKS, key="shared")
    assert adb != bs
    assert hash(adb) != hash(bs)
    assert len({adb, bs, InstanceRef(backend=BackendId.ADB, key="shared")}) == 2
    assert InstanceRef(backend=BackendId.ADB, key="a") != InstanceRef(
        backend=BackendId.ADB, key="b"
    )


def test_key_rules_strip_and_reject_blank() -> None:
    assert InstanceRef(backend=BackendId.MEMU, key="  myvm  ").key == "myvm"
    for bad in ["", "   ", "\t", 123, None]:
        with pytest.raises(InvalidArgumentError) as excinfo:
            InstanceRef(backend=BackendId.MEMU, key=bad)  # ty: ignore[invalid-argument-type]
        assert excinfo.value.parameter == "key"


def test_backend_string_coerced_unknown_rejected() -> None:
    assert InstanceRef(backend="adb", key="x").backend is BackendId.ADB  # ty: ignore[invalid-argument-type]
    with pytest.raises(BackendNotFoundError):
        InstanceRef(backend="nope", key="x")  # ty: ignore[invalid-argument-type]
    with pytest.raises(InvalidArgumentError):
        InstanceRef(backend=123, key="x")  # ty: ignore[invalid-argument-type]


def test_refs_are_frozen() -> None:
    ref = InstanceRef(backend=BackendId.ADB, key="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.key = "y"  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.backend = BackendId.MEMU  # ty: ignore[invalid-assignment]


def test_instance_record_observation_shape() -> None:
    ref = InstanceRef(backend=BackendId.ADB, key="127.0.0.1:5555")
    record = InstanceRecord(ref=ref, running=True, serial="127.0.0.1:5555", details={"a": "b"})
    assert record.ref == ref
    assert record.details == {"a": "b"}
    assert InstanceRecord(ref=ref, running=False, serial=None, details={}).serial is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.running = False  # ty: ignore[invalid-assignment]


def test_instance_record_details_sealed() -> None:
    details = {"label": "one"}
    record = InstanceRecord(
        ref=InstanceRef(backend=BackendId.ADB, key="x"),
        running=True,
        serial=None,
        details=details,
    )
    details["label"] = "mutated-outside"
    assert record.details == {"label": "one"}
    with pytest.raises(TypeError):
        record.details["label"] = "mutated-inside"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ref": "adb:x", "running": True, "serial": None, "details": {}},
        {
            "ref": InstanceRef(backend=BackendId.ADB, key="x"),
            "running": 1,
            "serial": None,
            "details": {},
        },
        {
            "ref": InstanceRef(backend=BackendId.ADB, key="x"),
            "running": True,
            "serial": "",
            "details": {},
        },
        {
            "ref": InstanceRef(backend=BackendId.ADB, key="x"),
            "running": True,
            "serial": None,
            "details": {"a": 1},
        },
    ],
)
def test_instance_record_rejects_bad_shapes(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentError):
        InstanceRecord(**kwargs)


def test_identification_touches_no_backend_modules() -> None:
    source = (SRC / "_identification.py").read_text(encoding="utf-8")
    assert "backends" not in source
    leaked = sorted(m for m in sys.modules if m.startswith("emuctl.backends"))
    assert leaked == []

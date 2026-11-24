"""Explicit instance selection: never take ``discover()[0]``."""

from __future__ import annotations

import pytest

from emuctl._discovery import BackendStatus, DiscoveryReport, select_instance
from emuctl._identification import BackendId, InstanceRecord, InstanceRef
from emuctl.errors import AmbiguousInstanceError, InstanceNotFoundError, InvalidArgumentError


def _record(serial: str, backend: BackendId = BackendId.ADB, *, running: bool = True) -> InstanceRecord:
    return InstanceRecord(
        ref=InstanceRef(backend=backend, key=serial),
        running=running,
        serial=serial,
        details={},
    )


@pytest.fixture
def records() -> tuple[InstanceRecord, ...]:
    return (
        _record("emulator-5554"),
        _record("emulator-5556"),
        _record("emulator-5558"),
    )


def test_select_by_serial_returns_the_matching_record_not_the_first(
    records: tuple[InstanceRecord, ...],
) -> None:
    picked = select_instance(records, serial="emulator-5556")
    assert picked.ref == InstanceRef(BackendId.ADB, "emulator-5556")
    assert picked.serial == "emulator-5556"


def test_select_by_ref_object_and_canonical_string(records: tuple[InstanceRecord, ...]) -> None:
    ref = InstanceRef(BackendId.ADB, "emulator-5558")
    assert select_instance(records, ref=ref).ref == ref
    assert select_instance(records, ref="adb:emulator-5558").ref == ref


def test_select_flattens_a_discovery_report(records: tuple[InstanceRecord, ...]) -> None:
    report = DiscoveryReport(
        backends=(
            BackendStatus(
                backend=BackendId.ADB,
                available=True,
                reason="ok",
                instances=records,
            ),
        )
    )
    assert select_instance(report, serial="emulator-5554").ref.key == "emulator-5554"


def test_select_serial_matches_key_when_record_has_no_transport_serial() -> None:
    record = InstanceRecord(
        ref=InstanceRef(BackendId.MEMU, "bot-1"),
        running=False,
        serial=None,
        details={},
    )
    assert select_instance([record], serial="bot-1").ref == record.ref


def test_select_missing_is_typed_not_a_silent_none(records: tuple[InstanceRecord, ...]) -> None:
    with pytest.raises(InstanceNotFoundError) as caught:
        select_instance(records, serial="emulator-9999")
    assert caught.value.instance_ref == "emulator-9999"

    with pytest.raises(InstanceNotFoundError):
        select_instance(records, ref=InstanceRef(BackendId.ADB, "emulator-9999"))


def test_select_duplicate_serial_is_ambiguous() -> None:
    duplicate = (
        _record("emulator-5554", BackendId.ADB),
        _record("emulator-5554", BackendId.ADB),
    )
    with pytest.raises(AmbiguousInstanceError) as caught:
        select_instance(duplicate, serial="emulator-5554")
    assert caught.value.selector == "emulator-5554"
    assert caught.value.matched == ("adb:emulator-5554", "adb:emulator-5554")


def test_select_requires_exactly_one_selector(records: tuple[InstanceRecord, ...]) -> None:
    with pytest.raises(InvalidArgumentError):
        select_instance(records)
    with pytest.raises(InvalidArgumentError):
        select_instance(records, serial="emulator-5554", ref=InstanceRef(BackendId.ADB, "emulator-5554"))
    with pytest.raises(InvalidArgumentError):
        select_instance(records, serial="  ")


def test_select_rejects_non_records_and_non_canonical_refs(records: tuple[InstanceRecord, ...]) -> None:
    with pytest.raises(InvalidArgumentError):
        select_instance([object()], serial="emulator-5554")  # ty: ignore[invalid-argument-type]
    with pytest.raises(InvalidArgumentError):
        select_instance(records, ref="emulator-5554")  # bare serial: canonical form required

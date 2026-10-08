"""§7.6 — "extras map to concrete specs": the installed distribution's
PEP 685 metadata is parsed declaratively (importlib.metadata — the metadata
of the in-repo install) and pinned:

- every extra a spec-table row names (``[memu]``, ``[bluestacks]``) exists;
- ``[all]`` is a dependency superset of ``{memu, bluestacks}`` (extras with
  empty dep lists need no deps of their own);
- the vendored deps are declared requirements of their extras — importable
  MARKERS, never ambient installs asserted: no test here runs ``find_spec``
  or imports ``pymemuc``. Selection's runtime dep probing is pinned
  hermetically in test_registry.py instead.

§10 interpretation (anyio floor): the sync facade's portal/race semantics
are written against anyio ≥ 4.10; the base dependency line is pinned here
declaratively alongside the extras.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator

import pytest

#: Base (markerless) requirements, pinned declaratively (§5).
ANYIO_SPEC_RE = re.compile(r"^anyio>=4\.10(?:[^\d.]|$)")

EXTRAS_ROW = re.compile(r"^([^;]+?)\s*;\s*extra\s*==\s*[\"']([^\"']+)[\"']\s*$")


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    """Leave no ``emuctl.backends*`` trace after each test (same eviction
    shape as test_registry/test_discovery — the registry touch here must not
    plant a module leak a later no-backend-imports invariant observes)."""
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n.startswith("emuctl.backends")]:
        if name not in before:
            sys.modules.pop(name)


def _requirements_by_extra() -> tuple[list[str], dict[str, list[str]]]:
    import importlib.metadata

    raw = importlib.metadata.metadata("emuctl")
    declared = {extra.lower() for extra in raw.get_all("Provides-Extra") or []}
    reqs = importlib.metadata.requires("emuctl") or []
    by_extra: dict[str, list[str]] = {}
    for row in reqs:
        matched = EXTRAS_ROW.match(row.strip())
        if matched is None:
            continue
        by_extra.setdefault(matched.group(2), []).append(matched.group(1).strip())
    return sorted(declared), by_extra


def test_provides_extra_lists_every_expected_extra() -> None:
    declared, by_extra = _requirements_by_extra()
    for extra in ("all", "bluestacks", "cli", "google-play", "memu", "avd"):
        assert extra in declared, f"'{extra}' is not declared as a Provides-Extra"
    # the named extras are the marker namespace: all-rows reference extras
    # verbatim (PEP 685 lowercase), so no extra is decoupled from these names
    assert by_extra, "no per-extra requirement rows found"
    declared_names = set(declared)
    assert set(by_extra) <= declared_names


def test_spec_table_extras_are_declared_extras() -> None:
    """§7.6: every extra a spec-table row names exists as an extra. The rows
    are read through :func:`load`-free registry access (get_registry) — the
    registry's ``extra`` fields are the only consumer-facing extra names."""
    from emuctl.backends._registry import get_registry

    declared, _ = _requirements_by_extra()
    named = {
        spec.extra
        for spec in get_registry().values()
        if spec is not None and spec.extra is not None
    }
    assert named == {"memu", "bluestacks"}
    assert named <= set(declared)


def test_all_extra_is_a_dependency_superset() -> None:
    """``[all] ⊇ {memu, bluestacks}`` dependency-wise: every requirement row of
    the implemented-row extras also appears on ``all``. ``bluestacks`` carries
    no vendored dep at v0 (empty list — nothing to cover), so ``[all]`` need
    only cover ``[memu]``'s pymemuc row (plus click, pinned separately)."""
    _declared, by_extra = _requirements_by_extra()
    assert by_extra.get("bluestacks", []) == []
    assert set(by_extra.get("memu", [])) <= set(by_extra.get("all", []))


def test_cli_extra_requires_click() -> None:
    _declared, by_extra = _requirements_by_extra()
    assert any(row.startswith("click>=") for row in by_extra.get("cli", []))
    assert any(row.startswith("click>=") for row in by_extra.get("all", []))


def test_base_dependency_is_the_pinned_anyio_floor() -> None:
    """§10(d): the anyio floor (>=4.10) is a base (markerless) dependency."""
    import importlib.metadata

    unmarked = [
        row.strip() for row in importlib.metadata.requires("emuctl") or [] if ";" not in row
    ]
    row = [item for item in unmarked if item.startswith("anyio")]
    assert len(row) == 1
    assert ANYIO_SPEC_RE.match(row[0]) is not None, row[0]

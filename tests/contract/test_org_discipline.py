"""§7.10 organization backstops — AST/source walks, additive to the
import-linter contracts (defense in depth; §6's enforcement list names both
tracks: lint-imports in CI + "§7's AST-walk is the backstop").

(a) ``subprocess`` imports live ONLY in ``emuctl/_proc.py`` (and tests/**,
    mirroring ruff TID251's per-file-ignores shape);
(b) dynamic import calls live ONLY in ``emuctl/backends/_registry.py``
    (``load_adapter`` is the ONLY ADAPTER importer — §6 rule 3), with the
    sanctioned exception of the two Windows-only ``winreg`` stdlib probes
    (platform-conditional stdlib, not adapter imports);
(c) every spec-table row's module exists at its pinned path (or the row is
    an intentional spec-None not-implemented row);
(d) every implemented spec row's ``adapter_attr`` resolves on its module
    (read from test_registry's CONFORMANCE_ROWS — never duplicated here).

Scope note: (b) is enforced over ``src/`` only — the conformance tests use
``importlib.import_module``/``__import__`` against test doubles and the
stdlib, which is exactly the shape TID251's ``tests/**`` exemption models
(a) after; test-side dynamic loading is not part of the shipped organization.
"""

from __future__ import annotations

import ast
import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from emuctl._identification import BackendId

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"

PROC_MODULE = "emuctl/_proc.py"
REGISTRY_MODULE = "emuctl/backends/_registry.py"


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Iterator[None]:
    """Leave no ``emuctl.backends*`` trace after each test (same eviction shape
    as test_registry/test_discovery — §7.10's organization walk must not plant
    a module leak that a later no-backend-imports invariant can observe)."""
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n.startswith("emuctl.backends")]:
        if name not in before:
            sys.modules.pop(name)


def _root_module_names(tree: ast.Module) -> list[str]:
    """Root package names for every ``import``/``from`` statement."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    return names


def _iter_source_files() -> list[Path]:
    return sorted(
        [
            *SRC_ROOT.rglob("*.py"),
            *TESTS_ROOT.rglob("*.py"),
        ]
    )


# ── (a) subprocess backstop (mirrors ruff TID251) ──


def test_subprocess_is_imported_only_in__proc() -> None:
    violations: list[str] = []
    for path in _iter_source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == f"src/{PROC_MODULE}" or rel.startswith("tests/"):
            continue  # the sanctioned site + the TID251 tests/** exemption shape
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "subprocess" in _root_module_names(tree):
            violations.append(rel)
    assert violations == [], f"subprocess imported outside emuctl/_proc.py: {violations}"


# ── (b) dynamic-import backstop (§6 rule 3 / §7.10) ──


def test_dynamic_imports_occur_only_in_the_registry() -> None:
    """`import_module(...)`/`__import__(...)` machinery stays exclusively in
    the registry module (`load_adapter` is the ONLY ADAPTER importer, §6 rule
    3), plus the two Windows-only stdlib probes (`winreg`) that selection-side
    path discovery needs without importing a platform-incompatible stdlib
    module on macOS/Linux — a documented, narrower sanction than the adapter
    rule, never a backend import."""
    violations: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == f"src/{REGISTRY_MODULE}":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            args = node.args
            literal = args[0].value if args and isinstance(args[0], ast.Constant) else None
            if literal == "winreg":  # platform-conditional stdlib probe, sanctioned
                continue
            called = node.func
            name = ""
            if isinstance(called, ast.Name):
                name = called.id
            elif isinstance(called, ast.Attribute):
                name = called.attr
            if name in {"import_module", "__import__"}:
                violations.append(f"{rel}:{name}")
    assert violations == [], f"dynamic import outside backends/_registry.py: {violations}"


# ── (c)/(d) spec-table walks (read test_registry's single walk table —
# never duplicated here) ──


def _load_registry_module() -> tuple[Any, dict[BackendId, bool]]:
    """The live registry module + test_registry's CONFORMANCE_ROWS table.

    The registry is loaded through the installed package (its layout is what
    we verify); CONFORMANCE_ROWS are read from test_registry's AST — the
    single walk table, never duplicated here. No ``sys.modules`` pollution:
    the test-module import path that test_registry itself would trigger is
    avoided on purpose.
    """
    registry: Any = importlib.import_module("emuctl.backends._registry")
    source = (Path(__file__).parent / "test_registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="test_registry.py")
    rows: list[tuple[BackendId, bool]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "CONFORMANCE_ROWS" for t in node.targets)
        ):
            continue
        # Each row renders as `(BackendId.X, <bool>)`; ast.literal_eval cannot
        # evaluate the Attribute, so the backend ids are re-bound here from
        # the AST — never a duplicate table.
        assert isinstance(node.value, ast.List)
        for entry in node.value.elts:
            assert isinstance(entry, ast.Tuple)
            first, second = entry.elts
            assert isinstance(first, ast.Attribute)
            assert isinstance(first.value, ast.Name) and first.value.id == "BackendId"
            assert isinstance(second, ast.Constant) and isinstance(second.value, bool)
            rows.append((BackendId.from_string(first.attr.replace("_", "-")), second.value))
    assert rows, "CONFORMANCE_ROWS not found in test_registry.py"
    return registry, dict(rows)


def test_every_spec_row_module_exists_at_its_pinned_path() -> None:
    """§3: only the spec table names import-facing paths — each implemented
    row's module must exist at that literal path; spec-None rows are the
    sanctioned not-implemented rows."""
    registry, _rows = _load_registry_module()
    for backend, spec in registry.get_registry().items():
        if spec is None:
            continue  # GOOGLE_PLAY/AVD: intentional not-implemented rows
        rel_path = spec.module.replace(".", "/") + ".py"
        assert (REPO_ROOT / "src" / rel_path).is_file(), (
            f"{backend.value} pins '{spec.module}' but src/{rel_path} is missing"
        )


def test_implemented_rows_resolve_their_adapter_attr() -> None:
    """For every row the registry conformance walk marks implemented=True,
    `spec.adapter_attr` resolves on that module as a port subclass (the §7.10
    walk itself) — read from test_registry's table, never duplicated."""
    registry, rows = _load_registry_module()
    from emuctl.port import AsyncEmulatorController

    for backend, implemented in rows.items():
        if not implemented:
            continue
        spec = registry.get_registry()[backend]
        assert spec is not None, f"implemented row {backend.value} has spec None"
        module = importlib.import_module(spec.module)
        adapter = getattr(module, spec.adapter_attr)
        assert isinstance(adapter, type)
        assert issubclass(adapter, AsyncEmulatorController)


def test_unimplemented_rows_are_spec_none() -> None:
    """The flip-side: a row the conformance walk has NOT flipped implemented
    is a spec-None row today (ADB/MEMU/BS5/AIR are flipped; GOOGLE_PLAY/AVD
    are honest not-implemented)."""
    registry, rows = _load_registry_module()
    for backend, implemented in rows.items():
        spec = registry.get_registry()[backend]
        if implemented:
            assert spec is not None
        else:
            assert spec is None, f"{backend.value} is not-implemented but has a spec row"

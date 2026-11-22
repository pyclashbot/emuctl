"""Layout-drift detector: the CONTRACT §6 tree exists in src/emuctl
(organization lock) — and ONLY that tree: any stray file/shipped directory in
the package that the contract's layout does not name is drift in the other
direction."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "emuctl"

# §6 module layout sketch, plus the package __init__ files and shipped data.
EXPECTED: tuple[str, ...] = (
    "__init__.py",
    "port.py",
    "errors.py",
    "timing.py",
    "waits.py",
    "png.py",
    "log.py",
    "_proc.py",
    "_serials.py",
    "_identification.py",
    "_discovery.py",
    "_platforms.py",
    "_sync.py",
    "py.typed",
    "resources/.gitkeep",
    "config/__init__.py",
    "config/model.py",
    # §5's CLI lives in-tree behind the [cli] extra; `_exitmap.py` is named by §2/§5 itself.
    "cli/__init__.py",
    "cli/_exitmap.py",
    "cli/_options.py",
    "cli/_render.py",
    "cli/_commands.py",
    "cli/_device.py",
    "backends/__init__.py",
    "backends/_registry.py",
    "backends/adb.py",
    "backends/memu.py",
    "backends/bluestacks/__init__.py",
    "backends/bluestacks/bs5.py",
    "backends/bluestacks/air.py",
    "backends/bluestacks/_conf.py",
    "backends/bluestacks/_meta.py",
    "backends/bluestacks/_paths.py",
    "backends/google_play/__init__.py",
    "backends/avd/__init__.py",
)


def test_layout_tree_matches_contract() -> None:
    missing = [name for name in EXPECTED if not (ROOT / name).exists()]
    assert missing == [], f"layout drifted from CONTRACT §6; missing: {missing}"


def _actual_tree() -> set[str]:
    """The files the shipped package tree actually contains (no __pycache__)."""
    actual: set[str] = set()
    for item in sorted(ROOT.rglob("*")):
        rel = item.relative_to(ROOT).as_posix()
        if "__pycache__" in rel.split("/"):
            continue
        if item.is_file():
            actual.add(rel)
    return actual


def test_layout_tree_is_exactly_expected() -> None:
    """§7.10 layout completeness, both directions: every expected file
    exists AND the shipped package tree names no file beyond the contract's
    layout (generated ``__pycache__`` directories tolerated)."""
    expected_files = set(EXPECTED)
    # every parent directory an expected file lives under must exist
    expected_dirs = {str(Path(name).parent) for name in expected_files if "/" in name}
    missing_dirs = sorted(name for name in expected_dirs if not (ROOT / name).is_dir())
    assert missing_dirs == [], missing_dirs
    actual = _actual_tree()
    stray = sorted(actual - expected_files)
    missing_files = sorted(expected_files - actual)
    assert stray == [], f"files beyond CONTRACT §6's named layout: {stray}"
    assert missing_files == [], f"expected files absent: {missing_files}"

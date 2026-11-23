"""Layout-drift detector: the CONTRACT §6 tree exists in src/emuctl (organization lock)."""

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
    "py.typed",
    "resources/.gitkeep",
    "config/__init__.py",
    "config/model.py",
    "backends/__init__.py",
    "backends/_registry.py",
    "backends/adb.py",
)


def test_layout_tree_matches_contract() -> None:
    missing = [name for name in EXPECTED if not (ROOT / name).exists()]
    assert missing == [], f"layout drifted from CONTRACT §6; missing: {missing}"

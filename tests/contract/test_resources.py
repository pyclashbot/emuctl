"""§7 conformance (resources rule, §4): bundled files never use __file__/cwd paths."""

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "emuctl"


def test_no_file_arithmetic_in_core_sources() -> None:
    """Shipped files are addressed via importlib.resources, never __file__ math."""
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if "__file__" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_cwd_relative_resource_access() -> None:
    """No os.getcwd()/getcwd() in shipped sources (resolve_config takes cwd as input)."""
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if "os.getcwd" in path.read_text(encoding="utf-8")
        or "os.getcwdb" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_resources_package_exists() -> None:
    assert (SRC / "resources").is_dir()

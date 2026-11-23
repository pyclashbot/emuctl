from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "emuctl"


def test_no_file_arithmetic_in_core_sources() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if "__file__" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_cwd_relative_resource_access() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if "os.getcwd" in path.read_text(encoding="utf-8") or "os.getcwdb" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_resources_package_exists() -> None:
    assert (SRC / "resources").is_dir()

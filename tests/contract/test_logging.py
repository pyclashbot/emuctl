"""§7.4 conformance: logger silence + sanitize discipline."""

import importlib
import logging
import sys

from emuctl.log import sanitize


def test_sanitize_strips_controls_and_truncates() -> None:
    assert sanitize("a\nb\rc\x00d\x1be\x7ff") == "abcdef"
    long_text = "x" * 500
    assert sanitize(long_text) == "x" * 197 + "..."
    assert len(sanitize(long_text)) <= 200
    assert sanitize("ok") == "ok"
    assert sanitize("") == ""


def test_import_is_silent_and_null_handler_idempotent(capsys) -> None:  # type: ignore[no-untyped-def]
    root = logging.getLogger()
    root_before = list(root.handlers)
    saved_modules = dict(sys.modules)
    stale = [name for name in sys.modules if name == "emuctl" or name.startswith("emuctl.")]
    for name in stale:
        del sys.modules[name]
    try:
        import pkgutil

        import emuctl  # noqa: F401

        submodule_names = sorted(
            module.name for module in pkgutil.walk_packages(emuctl.__path__, prefix="emuctl.")
        )
        assert len(submodule_names) > 5
        for dotted in submodule_names:
            importlib.import_module(dotted)

        out, err = capsys.readouterr()
    finally:
        for name in [n for n in list(sys.modules) if n == "emuctl" or n.startswith("emuctl.")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
    assert out == "" and err == ""
    assert list(root.handlers) == root_before
    package_logger = logging.getLogger("emuctl")
    nulls = [h for h in package_logger.handlers if isinstance(h, logging.NullHandler)]
    assert len(package_logger.handlers) == 1 and len(nulls) == 1
    # re-import / reload still leaves exactly one NullHandler
    restored = importlib.import_module("emuctl")
    importlib.reload(restored)
    importlib.reload(restored)
    package_logger = logging.getLogger("emuctl")
    assert len(package_logger.handlers) == 1
    assert isinstance(package_logger.handlers[0], logging.NullHandler)

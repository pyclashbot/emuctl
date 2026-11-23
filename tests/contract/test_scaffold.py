import logging

import emuctl  # noqa: F401


def test_import_is_logger_silent() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    import sys

    sys.modules.pop("emuctl", None)

    after = list(root.handlers)
    added = [h for h in after if h not in before]
    assert added == [], f"importing emuctl must not add handlers to the root logger, added {added!r}"

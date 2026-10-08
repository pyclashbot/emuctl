"""emuctl — vendor-agnostic Android emulator controller (CONTRACT: Identity; surfaces in §5)."""

import logging

_package_logger = logging.getLogger(__name__)
if not any(isinstance(h, logging.NullHandler) for h in _package_logger.handlers):
    _package_logger.addHandler(logging.NullHandler())

__all__: list[str] = []

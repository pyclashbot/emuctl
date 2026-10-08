"""§6 — Backend lazy package: PEP 562 __getattr__ resolves adapter modules via the spec table.

TODO: implement once SpecTable exists (backends/_registry.py); raises until then.
"""

from typing import Any


def __getattr__(name: str) -> Any:
    """PEP 562: lazily expose adapters through the spec table."""
    raise NotImplementedError  # TODO: resolve via backends/_registry.SpecTable

"""§2/§5 — THE single exit-number site (CONTRACT §2: "Exit numbers live in one
site, ``emuctl/cli/_exitmap.py``; the comments above pin the mapping. Unmapped
codes exit 9. Exit 130 = SIGINT; exit 141 = SIGPIPE on raw-bytes stdout — both
documented beside the table, no code strings").

The pinned per-class comments of §2, consolidated:

===============  ============================================
exit             ``error_code``
===============  ============================================
2 usage          ``invalid_argument`` · ``invalid_serial`` · ``backend_not_found``
                 · ``sync_call_inside_loop``
3 environment    ``extras_missing`` · ``executable_not_found`` · ``config``
                 · ``config_validation``
4 not found      ``instance_not_found``
5 unsupported    ``feature_not_supported`` · ``backend_not_available``
6 state           ``not_ready`` · ``controller_closed`` · ``ambiguous_target``
7 deadline       ``timeout``
8 transport      ``vendor_command_failed`` · ``device_state``
1 general        ``emuctl_error`` (the root class)
9 internal       any code without a table entry (and every non-taxonomy
                 exception — a raw ``OSError``/``ModuleNotFoundError`` reaching
                 the surface is a broken install, per §3's ``load_adapter``
                 note: "surfaces the raw ModuleNotFoundError unmapped (CLI
                 exit 9: unmapped = broken install)")
130 SIGINT       KeyboardInterrupt — no code string, never a table entry
141 SIGPIPE      raw-bytes stdout write to a closed pipe — no code string
===============  ============================================

§2 also pins the drift test: "A CI drift test walks the class tree and asserts
every ``error_code`` has a table entry — no reverse-map artifact to maintain."
That test lives in ``tests/contract/test_cli.py`` (every code in this table is
reachable from ``EmuctlError`` and vice versa).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from emuctl.errors import EmuctlError, resolve_error_code

__all__ = [
    "ERROR_EXITS",
    "EXIT_BROKEN_PIPE",
    "EXIT_GENERAL",
    "EXIT_OK",
    "EXIT_SIGINT",
    "EXIT_UNMAPPED",
    "error_code",
    "exit_for",
]

#: Success (§5: exit 0).
EXIT_OK: Final[int] = 0

#: General failure — the root ``emuctl_error`` code.
EXIT_GENERAL: Final[int] = 1

#: §5: "unmapped ⇒ 9" (internal / broken install).
EXIT_UNMAPPED: Final[int] = 9

#: SIGINT — documented beside the table, never a code string (§2).
EXIT_SIGINT: Final[int] = 130

#: SIGPIPE on raw-bytes stdout — documented beside 130, never a code string (§2).
EXIT_BROKEN_PIPE: Final[int] = 141

#: The pinned error_code → exit mapping. Read-only (frozen at import).
ERROR_EXITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        # ── exit 2 · usage / caller misuse ──
        "invalid_argument": 2,
        "invalid_serial": 2,
        "backend_not_found": 2,
        "sync_call_inside_loop": 2,
        # ── exit 3 · environment (missing extra, missing binary, config) ──
        "extras_missing": 3,
        "executable_not_found": 3,
        "config": 3,
        "config_validation": 3,
        # ── exit 4 · the addressed thing is gone ──
        "instance_not_found": 4,
        # ── exit 5 · capability / backend selection ──
        "feature_not_supported": 5,
        "backend_not_available": 5,
        # ── exit 6 · state ──
        "not_ready": 6,
        "controller_closed": 6,
        "ambiguous_target": 6,
        # ── exit 7 · deadline ──
        "timeout": 7,
        # ── exit 8 · transport ──
        "vendor_command_failed": 8,
        "device_state": 8,
        # ── exit 1 · general (the root code) ──
        "emuctl_error": 1,
    }
)


def error_code(exc: BaseException) -> str:
    """The JSON error object's ``code``: §2's ``resolve_error_code``.

    A non-taxonomy exception resolves to the root fallback code
    (``"emuctl_error"``) — the same string ``resolve_error_code`` pins — while
    its EXIT is the unmapped 9 (see :func:`exit_for`). ``error_code`` is only
    the render-side projection of that one function; it adds no vocabulary.
    """
    return resolve_error_code(exc)


def exit_for(exc: BaseException) -> int:
    """Map one exception to its exit number (the ONLY such function).

    Taxonomy exceptions resolve through :data:`ERROR_EXITS` (a code with no
    entry ⇒ 9). Non-taxonomy exceptions are unmapped by construction ⇒ 9,
    which is §3's documented "broken install" exit for a raw
    ``ModuleNotFoundError`` and the honest answer for anything else that
    escapes the library.
    """
    if not isinstance(exc, EmuctlError):
        return EXIT_UNMAPPED
    return ERROR_EXITS.get(resolve_error_code(exc), EXIT_UNMAPPED)

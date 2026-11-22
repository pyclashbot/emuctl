"""§5 — The optional Click CLI (``emuctl[cli]``): the console entry points.

CONTRACT §5, quote-checked:

- "**CLI (``emuctl[cli]``).** Console script in the core distribution (pip
  ignores extras on entry points — pip#9726): a lazy-click stub exits 3 with
  'install ``emuctl[cli]``'; CLI code is in-tree behind the extra; framework
  is click."
- "**Entry:** ``main()`` refuses to run inside an event loop (exit 2, suggests
  ``await emuctl.cli.amain(argv)``); ``asyncio.run(amain())`` — one loop per
  invocation; KeyboardInterrupt ⇒ 130 always, no traceback; SIGTERM untrapped
  (OS-kill semantics preserved)."

Two entry points, one loop each:

- :func:`main` — the synchronous console script (``emuctl = "emuctl.cli:main"``
  in ``[project.scripts]``). Refuses inside a running loop, installs the
  §5-mandated SIGPIPE default, runs ONE ``asyncio.run(amain(argv))`` and maps
  ``KeyboardInterrupt`` to 130.
- :func:`amain` — the async twin, the target of the conformance tests. It
  drives the click tree with no event loop of its own and returns an exit
  number; it is stateless per invocation (no module-level state is mutated
  across calls).

The lazy rule: importing THIS package imports no click and no backend/vendor
code. ``click`` is imported inside :func:`main`/:func:`amain` (and by the
command modules only once click has proven present), so
``import emuctl.cli`` works without the ``[cli]`` extra and calling
``main``/``amain`` without it exits 3 with the install hint instead of raising
``ModuleNotFoundError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Sequence
from typing import Final

from emuctl.cli._exitmap import EXIT_SIGINT
from emuctl.cli._exitmap import exit_for as _exit_for
from emuctl.cli._render import emit_error
from emuctl.errors import ExtrasMissingError, SyncCallInsideEventLoopError

__all__ = ["amain", "main"]

#: §5/§4: the extra that carries the CLI framework.
_CLI_EXTRA: Final[str] = "cli"

#: The §5 loop-refusal hatch, named once and reused by the rendered message
#: and the exit mapping.
_LOOP_DETAIL: Final[str] = (
    "the sync console entry cannot run inside a running event loop; "
    "use `await emuctl.cli.amain(argv)` instead"
)


def _require_click() -> None:
    """The lazy-click gate (§5): absent click ⇒ exit 3 with the install hint.

    Fires BEFORE any command module (or backend) is imported, so a core
    install without the extra never loads vendor code on the way to its
    refusal.
    """
    try:
        import click  # noqa: F401  (the presence probe; the tree imports it itself)
    except ImportError as exc:
        raise ExtrasMissingError(
            extra=_CLI_EXTRA,
            detail=(
                "the emuctl CLI needs click; install it with "
                "\"pip install 'emuctl[cli]'\" (or uv add 'emuctl[cli]')"
            ),
        ) from exc


def _restore_sigpipe() -> None:
    """§5's pipe discipline: "restore SIGPIPE default, exit 141, no traceback".

    Restoring the OS default lets a raw-bytes write to a closed pipe terminate
    the process with the signal (shell exit 141) instead of a Python-level
    traceback. Called only from :func:`main` — the real console entry — never
    from :func:`amain`, whose in-process tests must not be killed by it. Absent
    on Windows (no ``SIGPIPE``) ⇒ no-op.
    """
    with_sigpipe = getattr(signal, "SIGPIPE", None)
    if with_sigpipe is None:  # pragma: no cover - Windows
        return
    # pragma: no cover - a non-main-thread caller cannot install handlers
    with contextlib.suppress(OSError, ValueError):
        signal.signal(with_sigpipe, signal.SIG_DFL)


async def amain(argv: Sequence[str] | None = None) -> int:
    """The async twin of :func:`main` (§5): drive one CLI invocation.

    ``argv`` is the argument vector WITHOUT the program name (``None`` reads
    ``sys.argv[1:]``). Returns the exit number; every handled failure renders
    the pinned error object on stderr and leaves stdout empty. Stateless per
    invocation — no loop is created here (the caller owns it) and no module
    state is carried between calls.

    A missing click extra is handled HERE, not by the caller: the refusal
    renders like every other failure (exit 3 + the install hint) instead of
    escaping as ``ModuleNotFoundError``.
    """
    try:
        _require_click()
    except ExtrasMissingError as exc:
        emit_error(exc)
        return _exit_for(exc)
    from emuctl.cli._commands import run

    return await run(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """The synchronous console entry (``[project.scripts] emuctl = "emuctl.cli:main"``).

    Returns the process exit number (the console-script wrapper passes it to
    ``sys.exit``). Refusals, never tracebacks:

    - click missing ⇒ 3 + the ``emuctl[cli]`` install hint (no vendor import);
    - called inside a running event loop ⇒ 2 + the
      ``await emuctl.cli.amain(argv)`` hatch (§5);
    - ``KeyboardInterrupt`` ⇒ 130, no traceback (§5);
    - a closed stdout pipe on the raw-bytes path ⇒ 141, silent (§5).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # §5: "main() refuses to run inside an event loop (exit 2, suggests
        # await emuctl.cli.amain(argv))". The escape hatch rides in the
        # message: §2's per-class suggestion template (rendered by the CLI)
        # names the `.aio` hatch and must not be ctor-baked per call.
        emit_error(SyncCallInsideEventLoopError(detail=_LOOP_DETAIL))
        # The pinned sync_call_inside_loop mapping (§2) — exit 2.
        return _exit_for(SyncCallInsideEventLoopError(detail=_LOOP_DETAIL))

    _restore_sigpipe()
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        # §5: "KeyboardInterrupt ⇒ 130 always, no traceback".
        return EXIT_SIGINT

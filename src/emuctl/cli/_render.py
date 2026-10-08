"""§5 — The CLI's output discipline: the stdout/stderr split, the JSON
documents, the error object, and pipe discipline.

CONTRACT §5, quote-checked: "**Output contract:** stdout = results only — one
JSON doc with ``"schema_version": 1``; ``list`` is a bare array; raw PNG bytes
for ``screenshot --stdout`` (the ONLY raw-bytes path); stdout on error is
empty. stderr = human text, logs, error objects (JSON even without ``--json``):
``{"error": {"code", "exit", "message", "suggestion"}}``." Plus: "**Pipe
discipline:** ``screenshot --stdout`` closes cleanly on ``BrokenPipeError``
(restore SIGPIPE default, exit 141, no traceback, no stderr noise); reading raw
bytes from a slow sink is never misreported as a timeout."

§7 obligation 9 ("Suggestion rendering: per-class templates interpolate payload
keys at the CLI render site; no ctor-baked suggestions") is implemented here
and nowhere else — :func:`render_suggestion` is the render site §2 names.

Interpretation decisions (CONTRACT §5 wins; these fill its silences):

- Field order is deterministic BY CONSTRUCTION: every document is built as a
  literal dict in the pinned/spelled order (``schema_version`` first; the error
  object in §5's ``code, exit, message, suggestion`` order) and serialized
  without ``sort_keys``, so ``json.dumps`` preserves it. Arrays are sorted by
  an explicit key (locale-invariant codepoint order — never ``locale``-keyed).
- No ANSI color is emitted in ANY mode: §5 requires "non-TTY ⇒ no color" and
  the agent-determinism rule is served trivially by never coloring (§5's
  "additive-only output evolution" leaves no room for a color knob at v0).
- A broken pipe on ANY stdout write (text or raw) exits 141 silently; the
  raw-bytes path is the only one §5 names, and closing cleanly on the text
  paths too is the same discipline, not an extra feature.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from emuctl.cli._exitmap import error_code, exit_for
from emuctl.errors import EmuctlError
from emuctl.log import sanitize

__all__ = [
    "PipeClosed",
    "document",
    "emit_error",
    "emit_line",
    "emit_note",
    "emit_raw",
    "emit_result",
    "error_object",
    "json_safe",
    "render_suggestion",
    "shown",
    "silence_broken_pipe",
]

#: §5's output-contract anchor: every pinned JSON document carries it.
#: Deliberately NOT §4's ``config.SCHEMA_VERSION`` (the config-FILE schema
#: major): the CLI's output contract and the config file format are separate
#: evolution surfaces that merely both start at 1.
SCHEMA_VERSION: Final[int] = 1


def document(**fields: Any) -> dict[str, Any]:
    """A pinned result document: the §5 anchor first, then ``fields`` in order.

    Field order is deterministic BY CONSTRUCTION (§5's "deterministic JSON
    field order"): the anchor leads and every caller passes its remaining
    fields as keywords in the pinned order.
    """
    return {"schema_version": SCHEMA_VERSION, **fields}


class PipeClosed(Exception):
    """Internal control-flow signal: the stdout sink closed mid-write.

    Raised only by the emitters below; :func:`emuctl.cli.amain` translates it
    to :data:`emuctl.cli._exitmap.EXIT_BROKEN_PIPE` (141) with no traceback
    and no stderr noise (§5's pipe discipline).
    """


def silence_broken_pipe() -> bool:
    """Point the real ``sys.stdout`` file descriptor at ``os.devnull`` after a
    broken-pipe write (the canonical Python recipe, §5's silent 141).

    Why: Windows has no SIGPIPE, so the broken pipe surfaces as an ordinary
    ``BrokenPipeError`` — and then the interpreter's shutdown flush of the
    still-buffered stdout re-raises it, turning the pinned exit 141 into the
    default 120 ("Exception ignored"). Redirecting the underlying descriptor
    makes every stray teardown flush land in the null device. POSIX is
    unaffected: with SIGPIPE restored to ``SIG_DFL`` the process is already
    dead at the signal before this helper ever runs, and in-process callers
    (mock streams without a ``fileno``) simply fall through.

    Returns whether a REAL process-level fd was silenced. Callers use this to
    decide the hard-shutdown path: only a real fd can produce a teardown flush
    that rewrites the exit code, so only a real fd justifies abandoning
    interpreter shutdown (see :func:`emuctl.cli._commands.run`). CliRunner /
    fake streams have no ``fileno`` and never reach the interpreter's own
    flush, so they yield ``False`` and in-process control keeps flowing.
    """
    silenced = False
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            fileno = stream.fileno()
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, fileno)
            os.close(devnull)
            silenced = True
    return silenced


def _stdout() -> Any:
    return sys.stdout


def _write_out(text: str) -> None:
    """Write one text chunk to stdout; a closed sink raises :class:`PipeClosed`."""
    try:
        _stdout().write(text)
        _stdout().flush()
    except BrokenPipeError as exc:  # §5 pipe discipline
        raise PipeClosed from exc
    except ValueError as exc:  # closed file object (I/O on closed file)
        raise PipeClosed from exc


def _write_err(text: str) -> None:
    """Write one text chunk to stderr (never fatal, never a result channel)."""
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except (BrokenPipeError, OSError, ValueError):  # pragma: no cover - sink gone
        pass


def _dumps(document: object) -> str:
    """Serialize one document deterministically (ASCII, insertion order)."""
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def json_safe(value: object) -> object:
    """Project a typed value into the JSON-safe payload vocabulary.

    Enums (e.g. ``RendererMode``) render by value; sequences become lists;
    everything already in ``str|int|float|bool|None`` passes through. Mirrors
    §3's ``to_dict()`` honesty rule: never an object, never a repr.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    enum_value = getattr(value, "value", None)
    if enum_value is not None and isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return sanitize(str(value))


def shown(value: object) -> str:
    """Human rendering of one observed value (never a Python repr).

    The ONE value-projection site for every human channel (the ``list`` table,
    the probe state line, the verb echoes): ``None`` reads ``unknown`` (a
    missing observation, not an invention — §3's honesty rule), a bool reads
    ``yes``/``no`` (association with the RUNNING column's pinned vocabulary),
    and everything else is ``str()``. Never colored, never a repr.
    """
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render_suggestion(exc: BaseException) -> str:
    """§7.9's render site: interpolate the CLASS-level suggestion template with
    the exception's own payload values.

    The template is never ctor-baked (§2); a payload key the template names
    but the instance lacks yields an empty string rather than a crash (the
    §7.1 tree test already pins template keys ⊆ payload, so this is the
    defensive branch only). Non-taxonomy exceptions have no template.
    """
    if not isinstance(exc, EmuctlError):
        return ""
    template = type(exc).suggestion
    if not template:
        return ""
    try:
        return sanitize(template.format_map(_PayloadValues(exc)))
    except (KeyError, IndexError, ValueError):
        return ""


class _PayloadValues(Mapping[str, Any]):
    """The payload view a suggestion template formats against (§7.9)."""

    def __init__(self, exc: EmuctlError) -> None:
        self._exc = exc
        self._keys: Sequence[str] = tuple(type(exc).payload)

    def __getitem__(self, key: str) -> Any:
        if key not in self._keys:
            raise KeyError(key)
        return getattr(self._exc, key, "")

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


def error_object(exc: BaseException) -> dict[str, Any]:
    """Build §5's pinned error document (field order = the pinned order).

    ``{"error": {"code", "exit", "message", "suggestion"}}`` — rendered even
    without ``--json``, always on stderr, with stdout left empty.
    """
    return {
        "error": {
            "code": error_code(exc),
            "exit": exit_for(exc),
            "message": sanitize(str(exc)),
            "suggestion": render_suggestion(exc),
        }
    }


def emit_error(exc: BaseException) -> None:
    """Render one error object to stderr (the ONLY error channel)."""
    _write_err(_dumps(error_object(exc)) + "\n")


def emit_result(document: object) -> None:
    """Emit one JSON result document to stdout (§5: results only)."""
    _write_out(_dumps(document) + "\n")


def emit_line(text: str) -> None:
    """Emit one human-mode result line to stdout."""
    _write_out(text + "\n")


def emit_note(text: str) -> None:
    """Emit one human-mode note to stderr (config warnings, hints)."""
    _write_err(text + "\n")


def emit_raw(data: bytes) -> None:
    """Emit RAW bytes to stdout — the only raw-bytes path (§5: screenshot
    ``--stdout``). No wrapper, no base64, no trailing newline.

    A closed sink raises :class:`PipeClosed` (→ exit 141, silent).
    """
    stream = getattr(sys.stdout, "buffer", None)
    with contextlib.suppress(BrokenPipeError, OSError, ValueError):
        # Flush any pending text-layer bytes first so ordering is preserved.
        sys.stdout.flush()
    try:
        # When a real process-level fd exists, write via the UNBUFFERED
        # ``os.write`` syscall: a dead pipe fails with ``BrokenPipeError``/
        # ``OSError`` IMMEDIATELY here (deterministically, incl. Windows,
        # where a buffered write can succeed into the pipe buffer and the
        # death only surfaces at shutdown flush — forcing exit 120).
        io = getattr(sys.stdout, "buffer", _stdout())
        fd = -1
        try:
            fd = io.fileno()
        except (AttributeError, OSError, ValueError):
            fd = -1
        if fd >= 0:
            view = memoryview(data)
            os.write(fd, view[:0])  # canary: raises immediately on a dead pipe
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.write(fd, b"")  # no-op write still errors if the pipe died now
        else:
            if stream is None:  # pragma: no cover - text-only capture surfaces
                stream = _stdout()
            stream.write(data)
            stream.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise PipeClosed from exc

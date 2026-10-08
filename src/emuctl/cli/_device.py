"""§5 — The instance-driving verbs: ``screenshot``, ``click``, ``swipe``,
``key``, ``text``, ``app start|installed``, ``start``/``stop``/``restart``,
``configure``.

Target grammar, quote-checked from §5: "``TARGET`` is positional = canonical
``backend:key`` ref. No bare serials, no inference, no ``--backend`` flag: a
malformed shape ⇒ exit 2 telling the caller to prefix; well-formed but absent ⇒
exit 4." Input rules: "``key TARGET KEYCODE  # ^KEYCODE_[A-Z_]+$ | bare digits
⇒ keyevent; anything else ⇒ usage exit 2 (no silent text)``" and
"``text TARGET VALUE  # input text — digits/PINs expressible here``".

Interpretation decisions (CONTRACT §5 wins; these fill its silences):

- The target is parsed by §3's ``InstanceRef.parse`` (I/O-free): the grammar
  and the "tell the caller to prefix" hint live ONCE in the identity layer,
  never re-spelled here.
- The ``key`` grammar is enforced in the CLI (not the port) because §5 pins it
  as a usage refusal with "no silent text". The refusal is a typed
  ``InvalidArgumentError``, so it renders as the pinned JSON error object with
  exit 2 rather than click's bare usage text.
- ``text`` VALUE passes through VERBATIM — §1/§4 put escaping in the shared
  ADB mechanic and §5 keeps stdin/prompting out; the CLI neither quotes,
  strips, nor echoes the value back (a typed PIN never reappears in output).
- ``--timeout`` exists only on the verbs whose §5 lines spell it
  (``start``/``stop``/``restart``); there is no global deadline knob (§5: "v0
  defaults are the agent-friendly ones").
- ``screenshot -o FILE`` reports the captured geometry through §4's core
  ``png_dimensions`` helper (the consumer-facing shape reader), never a
  second PNG decode.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import click

from emuctl._identification import InstanceRef
from emuctl.cli._exitmap import EXIT_OK
from emuctl.cli._options import Options
from emuctl.cli._render import document, emit_line, emit_raw, emit_result, json_safe
from emuctl.errors import InvalidArgumentError
from emuctl.png import png_dimensions
from emuctl.timing import TimeoutParam

__all__ = ["KEYCODE_RE", "register"]

#: §5's pinned key grammar: ``^KEYCODE_[A-Z_]+$`` or bare digits.
KEYCODE_RE: Final[re.Pattern[str]] = re.compile(r"^(?:KEYCODE_[A-Z_]+|\d+)$")


def _keycode_or_refuse(token: str) -> str:
    """§5: anything that is not a keyevent symbol or bare digits is a usage
    refusal (exit 2) — never a silent ``text`` fallback."""
    if KEYCODE_RE.match(token) is None:
        raise InvalidArgumentError(
            parameter="KEYCODE",
            constraint="must match ^KEYCODE_[A-Z_]+$ (e.g. KEYCODE_HOME) or be bare digits",
            detail=f"got {token!r}; `text` types free-form values",
        )
    return token


def _result(verb: str, target: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The uniform result envelope: ``schema_version`` first, then the pinned
    echo fields (§5 pins the anchor; the per-verb body is this module's
    additive-only evolution surface)."""
    return document(command=verb, target=target, result=payload)


def _human(verb: str, target: str, payload: Mapping[str, Any]) -> str:
    """The human-mode echo of one verb (never colored, never a secret)."""
    details = " ".join(f"{key}={_shown(value)}" for key, value in payload.items())
    return f"{verb} {target}" + (f" {details}" if details else "")


def _shown(value: object) -> str:
    return "unknown" if value is None else str(value)


def _write_file(path: str, data: bytes) -> None:
    """§5: "-o FILE overwrites an existing file — documented, not gated"."""
    Path(path).write_bytes(data)


# ── screenshot (-o FILE | --stdout) ──


@click.command(name="screenshot")
@click.argument("target", metavar="TARGET")
@click.option(
    "-o",
    "--output",
    "path",
    metavar="FILE",
    default=None,
    type=click.Path(dir_okay=False, path_type=str),
    help="Write the PNG bytes to FILE (overwrites an existing file; never gated).",
)
@click.option(
    "--stdout",
    "to_stdout",
    is_flag=True,
    default=False,
    help="Write the RAW PNG bytes to stdout (the only raw-bytes path).",
)
@click.pass_obj
def screenshot_cmd(options: Options, target: str, path: str | None, to_stdout: bool) -> Any:
    """Capture the framebuffer as PNG bytes.

    EXACTLY ONE of -o FILE / --stdout is required; ``--json --stdout`` is a
    usage refusal (a raw-bytes path cannot carry a JSON wrapper).
    """
    if path is None and not to_stdout:
        raise click.UsageError(
            "screenshot requires EXACTLY ONE of -o FILE / --stdout (neither was given)"
        )
    if path is not None and to_stdout:
        raise click.UsageError(
            "screenshot requires EXACTLY ONE of -o FILE / --stdout (both were given)"
        )
    if to_stdout and options.as_json:
        raise click.UsageError("--json cannot be combined with --stdout (the raw-bytes path)")
    return _screenshot(options, target, path, to_stdout)


async def _screenshot(options: Options, target: str, path: str | None, to_stdout: bool) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            frame = await controller.screenshot()
        finally:
            await controller.aclose()
    if to_stdout:
        # §5: raw PNG bytes, the ONLY raw-bytes path — no wrapper, no base64.
        emit_raw(frame)
        return EXIT_OK
    try:
        width, height = png_dimensions(frame)
    except (ValueError, TypeError):  # pragma: no cover - the port already sniffed
        width = height = 0
    if path is not None:
        _write_file(path, frame)
    payload = {"path": path, "bytes": len(frame), "width": width, "height": height}
    if options.as_json:
        emit_result(_result("screenshot", str(ref), payload))
    else:
        emit_line(_human("screenshot", str(ref), payload))
    return EXIT_OK


# ── click / swipe / key / text ──


@click.command(name="click")
@click.argument("target", metavar="TARGET")
@click.argument("x", type=int)
@click.argument("y", type=int)
@click.option(
    "--count", type=click.IntRange(min=1), default=1, show_default=True, help="Taps to send."
)
@click.option(
    "--interval",
    type=float,
    default=0.0,
    show_default=True,
    help="Seconds between taps (the whole burst shares one deadline).",
)
@click.pass_obj
def click_cmd(options: Options, target: str, x: int, y: int, count: int, interval: float) -> Any:
    """Tap at absolute device pixels X Y (§4 INV-C1: never re-scaled)."""
    return _click(options, target, x, y, count, interval)


async def _click(options: Options, target: str, x: int, y: int, count: int, interval: float) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            await controller.click(x, y, count=count, interval=interval)
        finally:
            await controller.aclose()
    payload = {"x": x, "y": y, "count": count, "interval": interval}
    if options.as_json:
        emit_result(_result("click", str(ref), payload))
    else:
        emit_line(_human("click", str(ref), payload))
    return EXIT_OK


@click.command(name="swipe")
@click.argument("target", metavar="TARGET")
@click.argument("x1", type=int)
@click.argument("y1", type=int)
@click.argument("x2", type=int)
@click.argument("y2", type=int)
@click.option(
    "--duration-ms",
    type=click.IntRange(min=0),
    default=300,
    show_default=True,
    help="Swipe duration in milliseconds.",
)
@click.pass_obj
def swipe_cmd(
    options: Options, target: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int
) -> Any:
    """Swipe between absolute device pixels X1 Y1 X2 Y2."""
    return _swipe(options, target, x1, y1, x2, y2, duration_ms)


async def _swipe(
    options: Options, target: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int
) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            await controller.swipe(x1, y1, x2, y2, duration_ms=duration_ms)
        finally:
            await controller.aclose()
    payload = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms}
    if options.as_json:
        emit_result(_result("swipe", str(ref), payload))
    else:
        emit_line(_human("swipe", str(ref), payload))
    return EXIT_OK


@click.command(name="key")
@click.argument("target", metavar="TARGET")
@click.argument("keycode", metavar="KEYCODE")
@click.pass_obj
def key_cmd(options: Options, target: str, keycode: str) -> Any:
    """Send a keyevent: KEYCODE_HOME style, or bare digits for a numeric code."""
    return _key(options, target, keycode)


async def _key(options: Options, target: str, keycode: str) -> int:
    from emuctl import connect

    token = _keycode_or_refuse(keycode)
    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            await controller.key(token)
        finally:
            await controller.aclose()
    payload = {"keycode": token}
    if options.as_json:
        emit_result(_result("key", str(ref), payload))
    else:
        emit_line(_human("key", str(ref), payload))
    return EXIT_OK


@click.command(name="text")
@click.argument("target", metavar="TARGET")
@click.argument("value", metavar="VALUE")
@click.pass_obj
def text_cmd(options: Options, target: str, value: str) -> Any:
    """Type VALUE verbatim (escaping is the ADB mechanic's job; digits/PINs work)."""
    return _text(options, target, value)


async def _text(options: Options, target: str, value: str) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            await controller.text(value)
        finally:
            await controller.aclose()
    # The value is deliberately NOT echoed back (a PIN is a secret): length only.
    payload = {"chars": len(value)}
    if options.as_json:
        emit_result(_result("text", str(ref), payload))
    else:
        emit_line(_human("text", str(ref), payload))
    return EXIT_OK


# ── app start|installed ──


@click.group(name="app")
def app_group() -> None:
    """App verbs: launch a package, or ask whether it is installed."""


@app_group.command(name="start")
@click.argument("target", metavar="TARGET")
@click.argument("package", metavar="PACKAGE")
@click.pass_obj
def app_start_cmd(options: Options, target: str, package: str) -> Any:
    """Launch PACKAGE on TARGET (bounded spawn-verify, no stdout parsing)."""
    return _app_start(options, target, package)


async def _app_start(options: Options, target: str, package: str) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            await controller.start_app(package)
        finally:
            await controller.aclose()
    payload = {"package": package, "started": True}
    if options.as_json:
        emit_result(_result("app start", str(ref), payload))
    else:
        emit_line(_human("app start", str(ref), payload))
    return EXIT_OK


@app_group.command(name="installed")
@click.argument("target", metavar="TARGET")
@click.argument("package", metavar="PACKAGE")
@click.pass_obj
def app_installed_cmd(options: Options, target: str, package: str) -> Any:
    """Report whether PACKAGE is installed (a failed observation raises)."""
    return _app_installed(options, target, package)


async def _app_installed(options: Options, target: str, package: str) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            installed = await controller.is_app_installed(package)
        finally:
            await controller.aclose()
    payload = {"package": package, "installed": bool(installed)}
    if options.as_json:
        emit_result(_result("app installed", str(ref), payload))
    else:
        emit_line(_human("app installed", str(ref), payload))
    return EXIT_OK


# ── start / stop / restart ──


@click.command(name="start")
@click.argument("target", metavar="TARGET")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=None,
    metavar="S",
    help="Seconds for this op (default: §2's lifecycle group budget).",
)
@click.pass_obj
def start_cmd(options: Options, target: str, timeout_s: float | None) -> Any:
    """Boot the bound instance (gated BOOT)."""
    return _lifecycle(options, "start", target, None, timeout_s)


@click.command(name="stop")
@click.argument("target", metavar="TARGET")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=None,
    metavar="S",
    help="Seconds for this op (default: §2's lifecycle group budget).",
)
@click.pass_obj
def stop_cmd(options: Options, target: str, timeout_s: float | None) -> Any:
    """Stop the bound instance (instance-scoped kills only)."""
    return _lifecycle(options, "stop", target, None, timeout_s)


@click.command(name="restart")
@click.argument("target", metavar="TARGET")
@click.option("--app", "app", default=None, metavar="PKG", help="Launch PKG once ready.")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=None,
    metavar="S",
    help="Seconds for the whole restart (default: §2's lifecycle group budget).",
)
@click.pass_obj
def restart_cmd(options: Options, target: str, app: str | None, timeout_s: float | None) -> Any:
    """THE boot primitive (backend-default readiness; a ReadyCheck is not CLI-expressible)."""
    return _lifecycle(options, "restart", target, app, timeout_s)


async def _lifecycle(
    options: Options, verb: str, target: str, app: str | None, timeout_s: float | None
) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    timeout: TimeoutParam = timeout_s
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            if verb == "start":
                await controller.start(timeout=timeout)
            elif verb == "stop":
                await controller.stop(timeout=timeout)
            else:
                await controller.restart(app=app, timeout=timeout)
        finally:
            await controller.aclose()
    payload: dict[str, Any] = {}
    if app is not None:
        payload["app"] = app
    if timeout_s is not None:
        payload["timeout"] = timeout_s
    if options.as_json:
        emit_result(_result(verb, str(ref), payload))
    else:
        emit_line(_human(verb, str(ref), payload))
    return EXIT_OK


# ── configure TARGET set KEY VALUE ──


@click.command(name="configure")
@click.argument("target", metavar="TARGET")
@click.argument("action", metavar="set")
@click.argument("key", metavar="KEY")
@click.argument("value", metavar="VALUE")
@click.pass_obj
def configure_cmd(options: Options, target: str, action: str, key: str, value: str) -> Any:
    """Apply a one-key ConfigSpec overlay: ``configure TARGET set KEY VALUE``.

    ``set`` is the only action §5 pins; anything else is a usage refusal naming
    it. KEY is a canonical config leaf ("display.width",
    "memu.settings.hide_toolbar", …).
    """
    if action != "set":
        raise click.UsageError(
            f"unknown action {action!r} for configure; the pinned v0 vocabulary is: set"
        )
    return _configure(options, target, key, value)


async def _configure(options: Options, target: str, key: str, value: str) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    overlay = {key: _overlay_value(key, value)}
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            await controller.configure(overlay)
        finally:
            await controller.aclose()
    payload = {"key": key, "value": json_safe(overlay[key])}
    if options.as_json:
        emit_result(_result("configure", str(ref), payload))
    else:
        emit_line(_human("configure", str(ref), payload))
    return EXIT_OK


def _overlay_value(key: str, token: str) -> Any:
    """Strictly type the CLI's VALUE against the canonical leaf it names.

    §4's flags layer is where structured CLI values parse, so this reuses the
    ONE strict parser (``emuctl.config._coerce_leaf``) instead of inventing a
    second value grammar inside the CLI. An unknown leaf stays a string: the
    backend's overlay translation owns that refusal (§4: "an unknown canonical
    key → InvalidArgumentError").
    """
    from emuctl.config import _LEAVES, _coerce_leaf

    if key not in _LEAVES:
        return token
    return _coerce_leaf(key, token, layer="flags")


def register(group: click.Group) -> None:
    """Attach every instance-driving verb to the CLI group (§5's verb list)."""
    for verb in (
        screenshot_cmd,
        click_cmd,
        swipe_cmd,
        key_cmd,
        text_cmd,
        app_group,
        start_cmd,
        stop_cmd,
        restart_cmd,
        configure_cmd,
    ):
        group.add_command(verb)

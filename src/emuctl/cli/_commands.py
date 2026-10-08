"""§5 — The Click command tree: the group, the global options, and the verbs
that answer without driving an instance (``version``, ``list``, ``probe``).

The instance-driving verbs live in ``emuctl/cli/_device.py``; the exit mapping
lives in ``_exitmap.py``; the output discipline in ``_render.py``.

CONTRACT §5's grammar, quote-checked (the whole v0 verb surface):

```
emuctl [--config PATH | --no-config] [--json]
   list                                   # one bare JSON array / human table
   probe TARGET                           # self-probe (no target) is v0.1 — §8
   screenshot TARGET (-o FILE | --stdout) # EXACTLY ONE of -o/--stdout, else usage exit 2
   click TARGET X Y [--count N] [--interval F]
   swipe TARGET X1 Y1 X2 Y2 [--duration-ms N]
   key TARGET KEYCODE                     # ^KEYCODE_[A-Z_]+$ | bare digits ⇒ keyevent
   text TARGET VALUE
   app start|installed TARGET PACKAGE
   start | stop | restart TARGET [--app PKG] [--timeout S]
   configure TARGET set KEY VALUE         # builds a one-key ConfigSpec overlay
   version [--json]
```

Deliberately NOT implemented (CONTRACT §8 deferrals): ``create`` (v0.1 club),
``clone``/``copy``/``export``/``import``, the ``vendor`` verb
(``run_vendor_cmd``), ``--yes``/confirmation gating, per-op deadline knobs
beyond the verbs §5 names, and probe self-targeting.

Interpretation decisions (CONTRACT §5 wins; these fill its silences):

- ``--json`` is a GROUP option (the grammar's top line pins it before the verb)
  and is additionally accepted on ``version`` — the one verb whose own line
  spells ``[--json]``, so ``emuctl version --json`` works as pinned. Both spellings
  reach the same flag through the same funnel.
- Click's own usage refusals (wrong arity, unknown option, bad number) exit 2
  with click's human usage text AND the pinned JSON error object, so an agent
  never faces a bare-text-only failure (§5: "errors emit a JSON error object
  on stderr" — even for argv-shape problems).
- ``version`` resolves NO config: it is the cheap early probe an agent runs
  first, so a broken ``emuctl.toml`` must not be able to fail it. Every other
  verb resolves the §4 chain (the flags layer is empty at v0 — §5 pins no
  value-shaped flag beyond the two escape hatches).
- ``LogConfig`` is "consumed ONLY by the CLI surface" (§4): a resolved
  ``log.file`` attaches (and removes) a ``FileHandler`` on the ``"emuctl"``
  logger for the duration of one invocation. With no ``log.file`` the CLI adds
  NO handler and logs nothing — §2's silence discipline and §5's clean-stderr
  promise win over a stderr log stream nobody asked for.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping, Sequence
from typing import Any

import click

from emuctl._discovery import DiscoveryReport, discover
from emuctl._identification import BackendId, InstanceRef
from emuctl._platforms import current_platform
from emuctl.cli._exitmap import EXIT_BROKEN_PIPE, EXIT_OK, EXIT_SIGINT
from emuctl.cli._exitmap import exit_for as _exit_for
from emuctl.cli._options import Options
from emuctl.cli._render import PipeClosed, document, emit_error, emit_line, emit_result
from emuctl.errors import EmuctlError, InvalidArgumentError
from emuctl.log import sanitize
from emuctl.port import CapabilityFlag

__all__ = ["cli", "run"]


# ── shared plumbing ──


def _platform_map() -> dict[str, list[str]]:
    """Every backend's supported platforms, JSON-safe and sorted.

    §5 pins "platform (os/supported)". The supported set is §3's spec-table
    truth, reached through the same sanctioned function-local registry seam
    ``connect()``/``discover()`` use (§6 rule 3: the spec table is the sole
    platform truth; the carve-out is declared in pyproject's import-linter
    ``ignore_imports`` next to the two existing ones).
    """
    from emuctl.backends import _registry

    row: dict[str, list[str]] = {}
    for backend, spec in _registry.get_registry().items():
        row[backend.value] = (
            sorted(platform.value for platform in spec.supported_platforms)
            if spec is not None
            else []
        )
    return row


def _supported_platforms(backend: BackendId) -> list[str]:
    return _platform_map().get(backend.value, [])


def _instance_rows(report: DiscoveryReport) -> list[dict[str, Any]]:
    """Flatten a discovery report into ``list``'s bare-array rows.

    §5: "one bare JSON array / human table; rows carry backend/serial/running/
    platform fields to filter on". Rows sort by (backend, key) — §5's "sorted,
    locale-invariant JSON" — and an unavailable backend contributes no rows
    (nothing plausibly exists to list).
    """
    rows: list[dict[str, Any]] = []
    for status in report.backends:
        for record in status.instances:
            rows.append(
                {
                    "ref": str(record.ref),
                    "backend": record.ref.backend.value,
                    "key": record.ref.key,
                    "serial": record.serial,
                    "running": record.running,
                    "platform": _supported_platforms(record.ref.backend),
                    "details": dict(sorted(record.details.items())),
                }
            )
    rows.sort(key=lambda row: (str(row["backend"]), str(row["key"])))
    return rows


def _human_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """The human-mode ``list`` rendering (never colored)."""
    columns = ("ref", "backend", "serial", "running", "platform")
    header = ("REF", "BACKEND", "SERIAL", "RUNNING", "PLATFORM")

    def cell(row: Mapping[str, Any], column: str) -> str:
        if column == "running":
            return "yes" if row["running"] else "no"
        if column == "platform":
            return ",".join(row["platform"])
        return str(row[column]) if row[column] is not None else "-"

    body = [tuple(cell(row, column) for column in columns) for row in rows]
    widths = [len(title) for title in header]
    for cells in body:
        for index, value in enumerate(cells):
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(title.ljust(widths[index]) for index, title in enumerate(header)).rstrip()]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(cells)).rstrip()
        for cells in body
    )
    return "\n".join(lines)


def _features(controller: Any) -> list[dict[str, Any]]:
    """§5's uniform probe rows: the WHOLE ``CapabilityFlag`` vocabulary, sorted
    by flag name, uniform ``{"flag": ..., "present": bool}`` objects."""
    return [
        {"flag": flag.value, "present": controller.has_feature(flag)}
        for flag in sorted(CapabilityFlag, key=lambda item: item.value)
    ]


def _find_record(report: DiscoveryReport, ref: InstanceRef) -> Any:
    """The discovery row for one ref, or None when the instance is absent."""
    for status in report.backends:
        for record in status.instances:
            if record.ref == ref:
                return record
    return None


def _version_string() -> str:
    """The installed distribution version (semantic-release owns it).

    A source checkout without installed metadata reports ``"unknown"`` rather
    than inventing a number.
    """
    try:
        return importlib.metadata.version("emuctl")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _human_probe(document: Mapping[str, Any]) -> str:
    """The human-mode ``probe`` rendering (never colored)."""
    target = document["target"]
    platform = document["platform"]
    state = document["state"]
    supported = ",".join(_platform_map().get(target["backend"], []))
    lines = [
        f"backend: {target['backend']}",
        f"target: {target['backend']}:{target['key']}",
        f"platform: os={platform['os']} supported={supported}",
        "state: " + " ".join(f"{key}={_shown(value)}" for key, value in state.items()),
        "features:",
    ]
    lines.extend(
        f"  {row['flag']}: {'present' if row['present'] else 'absent'}"
        for row in document["features"]
    )
    return "\n".join(lines)


def _shown(value: object) -> str:
    """Human rendering of one observed value (never a Python repr)."""
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


# ── the group + global options ──


@click.group(
    # `no_args_is_help` is a MultiCommand attribute (click 8.1+), not a context
    # setting: with `invoke_without_command=True` the group callback prints the
    # help itself, so the attribute must be off to reach it.
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["--help"]},
)
@click.option(
    "--config",
    "config_path",
    metavar="PATH",
    default=None,
    type=click.Path(dir_okay=False, path_type=str),
    help="Use this config file instead of the discovered project/user layers (§4 escape hatch).",
)
@click.option(
    "--no-config",
    "no_config",
    is_flag=True,
    default=False,
    help="Skip the project and user config layers (defaults + env only; §4 escape hatch).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the machine-readable JSON document on stdout (errors are always JSON on stderr).",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, no_config: bool, as_json: bool) -> None:
    """emuctl — control Android emulators (ADB, MEmu, BlueStacks) by canonical
    'backend:key' ref, e.g. ``adb:127.0.0.1:5555``.

    No bare serials, no inference, no ``--backend`` flag: a malformed target is
    a usage refusal (exit 2) that names the prefix, a well-formed but absent
    one exits 4.
    """
    if config_path is not None and no_config:
        raise click.UsageError("--config PATH cannot be combined with --no-config")
    ctx.obj = Options(config_path=config_path, no_config=no_config, as_json=as_json)
    if ctx.invoked_subcommand is None:
        # §5's discoverability rule ("argv/output shapes are discoverable via
        # --help"): a bare invocation prints the help on stdout and exits 0 —
        # not a failure, and never stderr noise for an agent probing the tool.
        click.echo(ctx.get_help())
        ctx.exit()


# ── version ──


@cli.command(name="version")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help='Emit {"schema_version": 1, "emuctl_version": "x.y.z"}.',
)
@click.pass_obj
def version_cmd(options: Options, as_json: bool) -> Any:
    """Print the emuctl version."""
    return _version(options, as_json)


async def _version(options: Options, as_json: bool) -> int:
    version = _version_string()
    if as_json or options.as_json:
        emit_result(document(emuctl_version=version))
    else:
        emit_line(f"emuctl {version}")
    return EXIT_OK


# ── list ──


@cli.command(name="list")
@click.pass_obj
def list_cmd(options: Options) -> Any:
    """List plausibly-present instances (read-only discovery, no I/O to any device)."""
    return _list(options)


async def _list(options: Options) -> int:
    with options.session() as config:
        report = await discover(config=config)
    rows = _instance_rows(report)
    if options.as_json:
        emit_result(rows)
    else:
        emit_line(_human_table(rows) if rows else "no instances found")
    return EXIT_OK


# ── probe ──


@cli.command(name="probe")
@click.argument("target", metavar="TARGET")
@click.pass_obj
def probe_cmd(options: Options, target: str) -> Any:
    """Probe a canonical TARGET: platform, state, and the feature rows.

    TARGET is required at v0 (self-probe without a target is §8's v0.1 club).
    A negative observation is data — the probe never fails because a device is
    unreachable.
    """
    return _probe(options, target)


async def _probe(options: Options, target: str) -> int:
    from emuctl import connect

    ref = InstanceRef.parse(target)
    with options.session() as config:
        controller = await connect(ref, config=config)
        try:
            probe = await controller.is_reachable()
            # The read-only enumeration supplies the observation half of §5's
            # state section: the backend's own record (run state, adb_state) —
            # None when the instance is absent, never an invention.
            report = await discover([ref.backend], config=config)
            record = _find_record(report, ref)
            state: dict[str, Any] = {
                "reachable": probe.reason,
                "running": record.running if record is not None else None,
                "adb_state": (record.details.get("adb_state") if record is not None else None),
            }
        finally:
            await controller.aclose()
    probe_document = document(
        backend=ref.backend.value,
        target={"backend": ref.backend.value, "key": ref.key},
        platform={"os": current_platform().value, "supported": _platform_map()},
        state=state,
        features=_features(controller),
    )
    if options.as_json:
        emit_result(probe_document)
    else:
        emit_line(_human_probe(probe_document))
    return EXIT_OK


# ── the funnel (§5's entry: one loop per invocation, one mapping site) ──


async def run(argv: Sequence[str] | None = None) -> int:
    """Drive the click tree and translate every outcome into an exit number.

    Command callbacks return the coroutine of their async body: click parses
    and validates argv synchronously and performs no I/O, so usage refusals
    never touch a transport. :func:`run` awaits the body and funnels every
    failure through the single mapping site (``_exitmap``) and the single
    error renderer (``_render``).
    """
    import inspect

    # ``args=None`` lets click read ``sys.argv[1:]`` (the console-script shape:
    # ``main()`` with no argv); an explicit list is the programmatic shape tests
    # and ``amain(argv)`` use. Never an empty list for "no argv given".
    args = None if argv is None else [str(item) for item in argv]
    try:
        outcome: Any = cli.main(args=args, prog_name="emuctl", standalone_mode=False)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return int(outcome if outcome is not None else EXIT_OK)
    except PipeClosed:
        return EXIT_BROKEN_PIPE
    except KeyboardInterrupt:
        # §5: "KeyboardInterrupt ⇒ 130 always, no traceback".
        return EXIT_SIGINT
    except click.exceptions.UsageError as exc:
        # §5: argv-shape problems are usage exit 2 — click's human usage text
        # AND the pinned JSON error object, so an agent never faces a
        # text-only failure. The typed refusal names the offending parameter
        # (click's own Parameter object) so the suggestion stays actionable.
        exc.show()
        param = getattr(exc, "param", None)
        parameter = getattr(param, "name", None)
        if not isinstance(parameter, str) or not parameter:
            parameter = "argv"
        refusal = InvalidArgumentError(
            parameter=parameter, constraint=sanitize(exc.format_message())
        )
        emit_error(refusal)
        return _exit_for(refusal)
    except click.exceptions.ClickException as exc:
        # Non-usage click failures (e.g. an unopenable file) keep click's exit
        # number and surface under the root code.
        exc.show()
        wrapped = EmuctlError(detail=sanitize(exc.format_message()))
        emit_error(wrapped)
        return _exit_for(wrapped)
    except BaseException as exc:  # noqa: BLE001 - the CLI IS the process boundary
        emit_error(exc)
        return _exit_for(exc)


#: The instance-driving verbs (§5) attach here: one import direction only
#: (``_device`` imports the shared options module, never this one).
from emuctl.cli import _device  # noqa: E402

_device.register(cli)

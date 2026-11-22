"""§7 conformance (CLI failures, §2/§5): the single exit-mapping site and the
CI drift test over the error tree, the pinned error object on stderr (always,
with or without ``--json``), stdout-empty-on-error, SIGINT → 130, the
broken-pipe → 141 discipline, the event-loop refusal, and the lazy-click
refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import re
import signal
import subprocess
import sys
import textwrap
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest

from emuctl._platforms import Platform
from emuctl.cli import amain, main
from emuctl.cli._exitmap import (
    ERROR_EXITS,
    EXIT_BROKEN_PIPE,
    EXIT_GENERAL,
    EXIT_OK,
    EXIT_SIGINT,
    EXIT_UNMAPPED,
    error_code,
    exit_for,
)
from emuctl.errors import (
    AmbiguousInstanceError,
    BackendNotAvailableError,
    BackendNotFoundError,
    ConfigError,
    ConfigValidationError,
    ControllerClosedError,
    DeviceStateError,
    EmuctlError,
    EmulatorNotReadyError,
    ExecutableNotFoundError,
    ExtrasMissingError,
    FeatureNotSupportedError,
    InstanceNotFoundError,
    InvalidArgumentError,
    InvalidSerialError,
    OperationTimeoutError,
    SyncCallInsideEventLoopError,
    VendorCommandError,
)

from ._clifake import (
    HangingHandle,
    Recorder,
    adb_transport,
    failing,
    install_transport,
    memu_transport,
    ok,
    spawn_from,
)

# SIGPIPE does not exist on Windows; resolve defensively so static checks resolve
# the same attribute set on every platform.
_SIGPIPE = getattr(signal, "SIGPIPE", None)

ADB = "adb:emulator-5554"
MEMU = "memu:pyclashbot-136"

#: The suite's own location on disk — the docstring-anchor and ledger parses
#: below read the REAL sources of truth (not copies).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ERRORS_SOURCE = _REPO_ROOT / "src" / "emuctl" / "errors.py"
_LEDGER = _REPO_ROOT / "docs" / "archive" / "CONTRACTS.md"


def _anchored_exits() -> dict[str, int]:
    """Every ``<code> → exit N`` anchor parsed from ``errors.py``'s docstrings."""
    text = _ERRORS_SOURCE.read_text(encoding="utf-8")
    return {
        code: int(exit_number)
        for code, exit_number in re.findall(r"([a-z][a-z0-9_]*) → exit (\d+)", text)
    }


#: Anchors, frozen once at import so the parametrization below is eager.
ANCHORED_EXITS: dict[str, int] = _anchored_exits()

#: Every taxonomy class with a constructible payload set and its pinned code.
TAXONOMY_CASES: tuple[tuple[type[EmuctlError], dict[str, Any], str], ...] = (
    (EmuctlError, {}, "emuctl_error"),
    (InvalidArgumentError, {"parameter": "x", "constraint": "y"}, "invalid_argument"),
    (InvalidSerialError, {"serial": "bad serial"}, "invalid_serial"),
    (ExtrasMissingError, {"extra": "memu"}, "extras_missing"),
    (ExecutableNotFoundError, {"tool": "memuc", "searched": ["a", "b"]}, "executable_not_found"),
    (
        ConfigError,
        {"layer": "user", "path": "emuctl.toml", "reason": "unreadable", "value": None},
        "config",
    ),
    (
        ConfigValidationError,
        {"layer": "flags", "path": "display.width", "value": "x", "expected": "an int"},
        "config_validation",
    ),
    (
        OperationTimeoutError,
        {"operation": "click", "deadline": 1.0, "elapsed": 1.5},
        "timeout",
    ),
    (
        VendorCommandError,
        {
            "command": ["adb", "devices"],
            "returncode": 1,
            "output_reason": "exit",
            "stderr_excerpt": "boom",
        },
        "vendor_command_failed",
    ),
    (DeviceStateError, {"serial": "emulator-5554", "state": "offline"}, "device_state"),
    (InstanceNotFoundError, {"instance_ref": "memu:ghost"}, "instance_not_found"),
    (FeatureNotSupportedError, {"feature": "configure"}, "feature_not_supported"),
    (BackendNotAvailableError, {"reason": "platform-unsupported"}, "backend_not_available"),
    (BackendNotFoundError, {"backend_id": "atari"}, "backend_not_found"),
    (
        EmulatorNotReadyError,
        {"reason": "start_failed", "cause": None},
        "not_ready",
    ),
    (ControllerClosedError, {}, "controller_closed"),
    (
        AmbiguousInstanceError,
        {"selector": "vm", "matched": ["a", "b"]},
        "ambiguous_target",
    ),
    (SyncCallInsideEventLoopError, {}, "sync_call_inside_loop"),
)

#: Every pinned (code → exit) pair, deduplicated, in table order.
PINNED_PAIRS: tuple[tuple[str, int], ...] = tuple(
    sorted({code: ERROR_EXITS[code] for _, _, code in TAXONOMY_CASES}.items())
)


@pytest.fixture(autouse=True)
def _evict_backend_modules() -> Generator[None]:
    """Leave no ``emuctl.backends*`` trace (repo convention)."""
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n.startswith("emuctl.backends")]:
        if name not in before:
            evicted = sys.modules.pop(name)
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, child, None) is evicted:
                delattr(parent, child)


@contextlib.contextmanager
def _captured() -> Generator[tuple[Recorder, Recorder]]:
    """Capture §5's two channels for one invocation.

    Both are replaced INSIDE the call (a context manager, not a fixture):
    pytest's own capture re-installs ``sys.stdout`` at the start of every test
    phase, so a fixture-time patch is silently clobbered before the call runs.
    The recorder is also the only way to carry the RAW PNG bytes — pytest's
    capture decodes as text and cannot hold them.
    """
    with pytest.MonkeyPatch.context() as patched:
        streams = (Recorder(), Recorder())
        patched.setattr(sys, "stdout", streams[0])
        patched.setattr(sys, "stderr", streams[1])
        yield streams


def _run(argv: Sequence[str]) -> tuple[int, str, str]:
    """Invoke the async entry point on a fresh loop (repo convention: no
    async-plugin markers) and capture both contract channels."""
    with _captured() as (out, err):
        code = asyncio.run(amain(list(argv)))
    return code, out.text, err.text


def _run_broken(argv: Sequence[str]) -> tuple[int, str]:
    """Invoke with a stdout whose every write fails the way a closed pipe does
    (§5's pipe discipline, without closing anything real)."""
    with _captured() as (out, err):
        out.broken = True
        code = asyncio.run(amain(list(argv)))
    return code, err.text


def _error(err: str) -> dict[str, Any]:
    """The pinned error object off stderr (§5's shape, in the pinned order)."""
    payload = json.loads(err.splitlines()[-1])
    assert list(payload) == ["error"]
    assert list(payload["error"]) == ["code", "exit", "message", "suggestion"]
    return payload["error"]


@pytest.fixture
def windows_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pretend Windows with every dependency present (§3's spec-table truth is
    patched at the registry — the object-form patching test_discovery uses — and
    the memuc binary is named through the env layer so the [memu] adapter's
    transport resolves without host discovery)."""
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: True)
    monkeypatch.setenv("EMUCTL_MEMU_MEMUC_PATH", "memuc")
    return _registry


def _tree_codes() -> set[str]:
    """Every ``error_code`` reachable from EmuctlError (the §2 class-tree walk)."""
    codes: set[str] = set()
    pending: list[type[EmuctlError]] = [EmuctlError]
    while pending:
        klass = pending.pop()
        codes.add(klass.error_code)
        pending.extend(klass.__subclasses__())
    return codes


# ── §2's drift test: the table and the tree agree, both directions ──


def test_exit_map_covers_every_error_code_in_the_tree() -> None:
    """§2: "A CI drift test walks the class tree and asserts every error_code
    has a table entry — no reverse-map artifact to maintain." """
    assert _tree_codes() == set(ERROR_EXITS)


@pytest.mark.parametrize(("code", "expected"), PINNED_PAIRS)
def test_pinned_code_maps_to_its_pinned_exit(code: str, expected: int) -> None:
    assert ERROR_EXITS[code] == expected


@pytest.mark.parametrize(("code", "expected"), sorted(ANCHORED_EXITS.items()))
def test_exit_map_matches_the_anchors_in_errors_docstrings(code: str, expected: int) -> None:
    """§2 (D5): every ``<code> → exit N`` anchor written into ``emuctl/errors.py``'s
    docstrings is honored by the single-site table — no doc/table drift."""
    assert ERROR_EXITS[code] == expected


def test_anchors_cover_exactly_the_tree_minus_the_root() -> None:
    """The anchor parse is not a silent subset: every tree code but the root
    carries a docstring anchor (the root's exit is the ledger's, below)."""
    assert set(ANCHORED_EXITS) == _tree_codes() - {"emuctl_error"}


def test_exit_table_matches_the_archive_ledger() -> None:
    """§2 (D5/D6): the WHOLE single-site table against the archive ledger
    (``docs/archive/CONTRACTS.md``: "0 ok · 1 general ... · 2 usage (...)·...").
    The expected mapping is PARSED from the ledger, never restated."""
    text = _LEDGER.read_text(encoding="utf-8")
    line = next(candidate for candidate in text.splitlines() if "2 usage (" in candidate)
    ledger: dict[str, int] = {}
    for segment in line.split("·"):
        header = re.match(r"\s*(\d+)\s+[a-z_]+", segment)
        if header is None:
            continue
        band = int(header.group(1))
        for code in re.findall(r"`([a-z_]+)`", segment):
            assert code not in ledger  # no ledger code listed under two bands
            ledger[code] = band
    assert set(ERROR_EXITS) == set(ledger) | {"emuctl_error"}
    for code, band in ledger.items():
        assert ERROR_EXITS[code] == band


def test_root_exits_one_per_the_ledger() -> None:
    """The root code has no docstring anchor of its own; its exit is pinned to
    ``EXIT_GENERAL`` against the ledger quote "1 general (base root / unknown
    code)" — at THIS one site only."""
    assert "1 general (base root / unknown code)" in _LEDGER.read_text(encoding="utf-8")
    assert ERROR_EXITS["emuctl_error"] == EXIT_GENERAL


def test_error_exits_is_frozen() -> None:
    """§2: "Exit numbers live in one site" — the table is read-only at import
    (no caller can remap an exit under the CLI's feet)."""
    mapping: Any = ERROR_EXITS
    with pytest.raises(TypeError):
        mapping["invalid_argument"] = 9


def test_unmapped_code_exits_nine() -> None:
    """§5: "unmapped ⇒ 9" — a taxonomy subclass whose code has no entry yet."""

    class FutureError(EmuctlError):
        error_code = "not_invented_yet"

    assert exit_for(FutureError()) == EXIT_UNMAPPED
    assert error_code(FutureError()) == "not_invented_yet"


def test_non_taxonomy_exceptions_exit_nine_as_a_broken_install() -> None:
    """§3's load_adapter note: a raw ModuleNotFoundError is "unmapped (CLI exit
    9: unmapped = broken install)" — the taxonomy root code is still rendered."""
    assert exit_for(ModuleNotFoundError("emuctl.backends.memu")) == EXIT_UNMAPPED
    assert exit_for(OSError("disk")) == EXIT_UNMAPPED
    assert exit_for(RuntimeError("boom")) == EXIT_UNMAPPED
    assert error_code(ModuleNotFoundError("x")) == "emuctl_error"


@pytest.mark.parametrize(("cls", "kwargs", "code"), TAXONOMY_CASES)
def test_exit_for_every_taxonomy_class(cls: Any, kwargs: dict[str, Any], code: str) -> None:
    exc = cls(**kwargs)
    assert exit_for(exc) == ERROR_EXITS[code]
    assert error_code(exc) == code


@pytest.mark.parametrize(("cls", "kwargs", "code"), TAXONOMY_CASES)
def test_error_object_renders_payload_interpolated_suggestions(
    cls: Any, kwargs: dict[str, Any], code: str
) -> None:
    """§7 obligation 9: the CLASS-LEVEL template interpolates the payload keys
    at the CLI render site — never ctor-baked, never absent."""
    from emuctl.cli._render import error_object, render_suggestion

    exc = cls(**kwargs)
    rendered = error_object(exc)
    assert rendered["error"]["code"] == code
    assert rendered["error"]["exit"] == ERROR_EXITS[code]
    assert rendered["error"]["message"] == str(exc)
    template = cls.suggestion
    if template:
        expected = template.format_map({name: getattr(exc, name, "") for name in cls.payload})
        assert render_suggestion(exc) == expected
        assert rendered["error"]["suggestion"] == expected
    else:
        assert rendered["error"]["suggestion"] == ""
    # the class template is untouched: rendering never bakes into the class
    assert cls.suggestion == template


def test_suggestion_rendering_never_raises_on_a_missing_payload_key() -> None:
    class Odd(EmuctlError):
        error_code = "odd"
        suggestion = "missing '{not_a_payload}'"
        payload = ("other",)

    from emuctl.cli._render import render_suggestion

    assert render_suggestion(Odd()) == ""


# ── stdout empty + the JSON error object on every error path ──


def test_error_objects_are_emitted_without_the_json_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5: "stderr = ... error objects (JSON even without --json)"; stdout on
    error is empty."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, err = _run(["--no-config", "key", ADB, "home"])
    assert code == ERROR_EXITS["invalid_argument"]
    assert out == ""
    assert _error(err)["code"] == "invalid_argument"


def test_absent_target_error_object_is_pasteable(
    monkeypatch: pytest.MonkeyPatch, windows_host: Any
) -> None:
    """§5: "Error suggestions name pasteable commands." """
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls, vms=b"0,other,MEmu,1,1\n"))
    code, _, err = _run(["--no-config", "click", MEMU, "1", "2"])
    assert code == ERROR_EXITS["instance_not_found"]
    assert "emuctl list" in _error(err)["suggestion"]


def test_platform_unsupported_backend_exits_five(monkeypatch: pytest.MonkeyPatch) -> None:
    """§3's selection gate, at exit 5: the spec table is the sole platform truth."""
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.MACOS)
    code, out, err = _run(["--no-config", "start", MEMU])
    assert code == ERROR_EXITS["backend_not_available"]
    assert out == ""
    assert _error(err)["code"] == "backend_not_available"


def test_missing_vendor_binary_exits_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """The [memu] extra resolves but memuc itself is absent ⇒
    executable_not_found ⇒ exit 3 (§3's selection order, §2's exit 3 band)."""
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: True)
    monkeypatch.delenv("EMUCTL_MEMU_MEMUC_PATH", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    code, out, err = _run(["--no-config", "click", MEMU, "1", "2"])
    assert code == 3
    assert out == ""
    assert _error(err)["code"] == "executable_not_found"


def test_unsupported_feature_exits_five(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1's derived gate survives the CLI: plain ADB has no BOOT/CONFIGURE, so
    the verb refuses with the typed feature_not_supported (exit 5)."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    for verb in ("start", "stop", "restart", "configure"):
        argv_tail = ["set", "display.width", "800"] if verb == "configure" else []
        code, out, err = _run(["--no-config", verb, ADB] + argv_tail)
        assert code == ERROR_EXITS["feature_not_supported"], verb
        assert out == ""
        assert _error(err)["code"] == "feature_not_supported"
    assert calls == []  # gate-first: no transport was ever touched


def test_vendor_failure_exits_eight(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, spawn_from(calls, lambda argv: failing()))
    code, out, err = _run(["--no-config", "click", ADB, "1", "2"])
    assert code == ERROR_EXITS["vendor_command_failed"]
    assert out == ""
    assert _error(err)["code"] == "vendor_command_failed"


def test_deadline_expiry_exits_seven(monkeypatch: pytest.MonkeyPatch, windows_host: Any) -> None:
    """§2: "Expiry always raises OperationTimeoutError — never degrades to a
    value" ⇒ the pinned timeout exit 7."""
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> Any:
        if "listvms" in argv:
            return ok(stdout=b"0,pyclashbot-136,MEmu,1,4242\n")
        return HangingHandle()

    install_transport(monkeypatch, spawn_from(calls, respond))
    code, out, err = _run(["--no-config", "start", MEMU, "--timeout", "0.05"])
    assert code == ERROR_EXITS["timeout"]
    assert out == ""
    assert _error(err)["code"] == "timeout"


def test_unexpected_internal_error_exits_nine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything outside the taxonomy is unmapped ⇒ 9, still with the pinned
    error object and never a traceback."""
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> Any:
        return RuntimeError("internal boom")

    install_transport(monkeypatch, spawn_from(calls, respond))
    code, out, err = _run(["--no-config", "click", ADB, "1", "2"])
    assert code == EXIT_UNMAPPED
    assert out == ""
    error = _error(err)
    assert error["code"] == "emuctl_error"
    assert "Traceback" not in err


def test_config_value_that_is_not_typed_exits_three(
    monkeypatch: pytest.MonkeyPatch, windows_host: Any
) -> None:
    """§4's flags layer parses the CLI's VALUE strictly: a bad value is a
    config_validation failure (exit 3), never a silently stringified overlay."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls))
    code, out, err = _run(["--no-config", "configure", MEMU, "set", "display.width", "abc"])
    assert code == ERROR_EXITS["config_validation"]
    assert out == ""
    assert _error(err)["code"] == "config_validation"
    assert calls == []  # the refusal fires before any vendor argv


def test_unknown_canonical_leaf_exits_two(
    monkeypatch: pytest.MonkeyPatch, windows_host: Any
) -> None:
    """§4: "an unknown canonical key → InvalidArgumentError" ⇒ exit 2."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls))
    code, out, err = _run(["--no-config", "configure", MEMU, "set", "not.a.leaf", "1"])
    assert code == ERROR_EXITS["invalid_argument"]
    assert out == ""
    assert _error(err)["code"] == "invalid_argument"
    assert calls == []


# ── signals ──


def test_sigint_maps_to_130_with_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5: "KeyboardInterrupt ⇒ 130 always, no traceback" — through the async
    twin and through the sync console entry."""
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> Any:
        raise KeyboardInterrupt

    install_transport(monkeypatch, spawn_from(calls, respond))
    code, out, err = _run(["--no-config", "click", ADB, "1", "2"])
    assert code == EXIT_SIGINT
    assert out == ""
    assert "Traceback" not in err

    code, out, err = _run(["version"])
    assert code == EXIT_OK


def test_sigint_through_the_sync_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> Any:
        raise KeyboardInterrupt

    install_transport(monkeypatch, spawn_from(calls, respond))
    assert main(["--no-config", "click", ADB, "1", "2"]) == EXIT_SIGINT


def test_broken_pipe_on_the_raw_bytes_path_exits_141_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5's pipe discipline: a closed sink on ``screenshot --stdout`` exits 141
    with no traceback and no stderr noise."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, err = _run_broken(["--no-config", "screenshot", ADB, "--stdout"])
    assert code == EXIT_BROKEN_PIPE
    assert err == ""
    assert "Traceback" not in err


def test_broken_pipe_on_a_text_result_exits_141_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same discipline on the text paths (``emuctl list | head -1``)."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, err = _run_broken(["--no-config", "list"])
    assert code == EXIT_BROKEN_PIPE
    assert err == ""


# ── the event-loop refusal ──


def test_main_inside_a_running_loop_refuses_and_names_amain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5: "main() refuses to run inside an event loop (exit 2, suggests
    `await emuctl.cli.amain(argv)`)." """
    from emuctl.errors import SyncCallInsideEventLoopError as _LoopError

    async def inside_loop() -> tuple[int, str, str]:
        with _captured() as (out, err):
            return main(["version"]), out.text, err.text

    code, out, err = asyncio.run(inside_loop())
    assert code == ERROR_EXITS["sync_call_inside_loop"]
    assert out == ""
    error = _error(err)
    assert error["code"] == "sync_call_inside_loop"
    assert error["exit"] == ERROR_EXITS["sync_call_inside_loop"]
    assert "amain" in error["message"]
    # §7.9: the suggestion is the CLASS template, rendered — never a ctor baked one
    assert error["suggestion"] == _LoopError.suggestion


def test_amain_stays_usable_inside_a_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suggested hatch works: the async twin runs on the caller's loop."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))

    async def inside_loop() -> tuple[int, str]:
        with _captured() as (out, err):
            code = await amain(["version"])
            return code, out.text

    code, out = asyncio.run(inside_loop())
    assert code == EXIT_OK
    assert out.startswith("emuctl ")


# ── the lazy-click rule (§5: a lazy-click stub exits 3 with the install hint) ──


def test_missing_click_is_an_extras_missing_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic eviction: no click in sys.modules ⇒ exit 3 + the hint, with no
    backend/vendor module imported on the way."""
    before = set(sys.modules)
    monkeypatch.setitem(sys.modules, "click", None)
    monkeypatch.delitem(sys.modules, "emuctl.cli._commands", raising=False)
    monkeypatch.delitem(sys.modules, "emuctl.cli._device", raising=False)
    code = asyncio.run(amain(["version"]))
    captured_backends = [
        name for name in set(sys.modules) - before if name.startswith("emuctl.backends")
    ]
    assert code == 3
    assert captured_backends == []


def test_missing_click_renders_the_pinned_error_object(monkeypatch: pytest.MonkeyPatch) -> None:
    script = textwrap.dedent(
        """
        import json, sys


        class NoClick:
            \"\"\"Hermetic eviction: block click at the import-hook level.\"\"\"

            def find_spec(self, fullname, path=None, target=None):
                if fullname == "click" or fullname.startswith("click."):
                    raise ImportError("click is blocked for this test")
                return None


        sys.meta_path.insert(0, NoClick())
        import emuctl.cli as cli

        code = cli.main(["version"])
        print(json.dumps({"code": code}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stderr.splitlines()
    assert lines, "the refusal must render an error object on stderr"
    error = json.loads(lines[-1])["error"]
    assert error["code"] == "extras_missing"
    assert error["exit"] == 3
    assert "emuctl[cli]" in error["suggestion"]
    assert proc.stdout.strip() == json.dumps({"code": 3})
    assert "Traceback" not in proc.stderr


def test_importing_the_cli_package_needs_no_click() -> None:
    """The lazy rule at import time: `import emuctl.cli` succeeds with click
    blocked, and nothing about the tree leaks vendor imports."""
    script = textwrap.dedent(
        """
        import sys


        class NoClick:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "click" or fullname.startswith("click."):
                    raise ImportError("click is blocked for this test")
                return None


        sys.meta_path.insert(0, NoClick())
        import emuctl.cli

        assert not any(name.startswith("emuctl.backends") for name in sys.modules)
        print("IMPORTED")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["IMPORTED"]
    assert proc.stderr == "", proc.stderr


# ── the real pipe: SIGPIPE end to end (§5's "restore SIGPIPE default, exit 141") ──


_PIPE_SCRIPT = textwrap.dedent(
    '''
    """Drive the real console entry with a scripted transport, then die the way a
    closed pipe demands."""

    import struct
    import sys

    PNG = (
        bytes((137, 80, 78, 71, 13, 10, 26, 10))
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 800, 600)
        + b"\\x08\\x02\\x00\\x00\\x00"
    )


    class Handle:
        pid = 4242
        returncode = 0

        def terminate(self):
            return None

        def kill(self):
            return None

        async def wait(self):
            return 0

        async def communicate(self, input=None):
            return (PNG, b"")


    async def spawn(argv):
        return Handle()


    import emuctl._proc as proc

    proc._default_spawn = spawn

    from emuctl.cli import main

    sys.exit(main(["--no-config", "screenshot", "adb:emulator-5554", "--stdout"]))
    '''
)


def test_a_real_closed_pipe_closes_cleanly() -> None:
    """§5's pipe discipline, observed at the OS level: the raw-bytes write to a
    sink that went away closes cleanly — the process dies of SIGPIPE (the shell
    reports 141) or exits 141, with no traceback and no stderr noise."""
    child = subprocess.Popen(
        [sys.executable, "-c", _PIPE_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert child.stdout is not None
    child.stdout.close()  # the reader is gone before the writer writes
    _, stderr = child.communicate()
    if _SIGPIPE is None:  # pragma: no cover - Windows branch
        assert child.returncode == 141, child.returncode
    else:
        assert child.returncode in (141, -_SIGPIPE), child.returncode
    assert b"Traceback" not in stderr
    assert b"BrokenPipeError" not in stderr


# ── cancellation is never caught, rewrapped, or logged as an error (§2) ──


def test_cancellation_mid_op_propagates_and_is_never_an_error_object(
    monkeypatch: pytest.MonkeyPatch,
    windows_host: Any,
) -> None:
    """§2: cancellation is "never caught, rewrapped, or logged as an error".
    ``run()``'s funnel catches ``Exception`` — NOT ``BaseException`` — so a
    ``task.cancel()`` mid-operation propagates ``CancelledError`` out of
    ``amain`` with NO error object on stderr, NO stdout noise, and NO exit
    number fabricated for it."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, spawn_from(calls, lambda argv: HangingHandle()))

    async def scenario() -> tuple[str, str]:
        with _captured() as (out, err):
            task = asyncio.create_task(amain(["--no-config", "start", MEMU, "--timeout", "60"]))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return out.text, err.text

    stdout_text, stderr_text = asyncio.run(scenario())
    assert stderr_text == ""
    assert stdout_text == ""


# ── version resolves NO config (§5's cheap early probe) ──


def test_version_resolves_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken config file cannot fail ``version`` — it never resolves the
    §4 chain, so ``--config /nonexistent`` exits 0 with stderr clean."""
    code, out, err = _run(["--config", "/nonexistent/emuctl.toml", "version"])
    assert code == EXIT_OK
    assert err == ""
    assert out.startswith("emuctl ")


# ── no ANSI color anywhere (§5: "non-TTY ⇒ no color", trivially always) ──


def test_no_ansi_escape_reaches_either_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``\\x1b`` in stdout or stderr across a human run AND a JSON run (and
    an error path) — the never-colored promise, byte-level."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    for argv in (
        ["--no-config", "list"],
        ["--json", "--no-config", "list"],
        ["--no-config", "key", ADB, "home"],  # an error path
    ):
        code, out, err = _run(argv)
        assert "\x1b" not in out, argv
        assert "\x1b" not in err, argv


# ── ConfigWarning rendering (§4: "the CLI surface renders these for the user") ──


def test_config_warnings_render_as_notes_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config file with an unknown key warns on stderr (the note names the
    key) and the unknown key is IGNORED — the command still succeeds with
    exactly one document on stdout and stderr carries nothing else."""
    conf = tmp_path / "emuctl.toml"
    conf.write_text("[display]\nwidht = 800\n", encoding="utf-8")
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, err = _run(["--config", str(conf), "--json", "click", ADB, "1", "2"])
    assert code == EXIT_OK, err
    assert len(out.splitlines()) == 1  # exactly one document, no note on stdout
    assert "warning: unknown config key" in err
    assert "display.widht" in err
    # the note is not doubled: the resolved config is folded exactly once
    assert err.count("display.widht") == 1


# ── the console-script entry point, pinned at metadata level ──


def test_console_script_entry_point_is_pinned() -> None:
    """§5: "Console script in the core distribution" — the installed metadata
    must expose ``emuctl = "emuctl.cli:main"`` under ``console_scripts``
    (importlib.metadata view of ``pyproject``'s ``[project.scripts]``)."""
    eps = importlib.metadata.entry_points(group="console_scripts")
    chosen = [ep for ep in eps if ep.name == "emuctl"]
    assert len(chosen) == 1
    assert chosen[0].value == "emuctl.cli:main"

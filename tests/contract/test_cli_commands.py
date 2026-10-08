"""§7 conformance (CLI, §5): the verb surface, arities, target grammar, key
grammar, input rules, and the output contract.

Every case drives the real entry points (``amain``/``main``) over the port's
private transport seam (see ``_clifake``) — no vendor binary, no network, no
prompt, no stdin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from emuctl.cli import amain, main
from emuctl.cli._commands import cli as cli_group
from emuctl.cli._exitmap import EXIT_OK
from emuctl.port import CapabilityFlag

from ._clifake import (
    ADB_DEVICES,
    MEMU_VMS,
    PNG,
    Recorder,
    adb_transport,
    install_transport,
    memu_transport,
    ok,
    spawn_from,
)

SERIAL = "emulator-5554"
ADB = "adb:emulator-5554"
MEMU = "memu:pyclashbot-136"

#: §5's whole pinned v0 verb surface, in the grammar block's order.
PINNED_VERBS = (
    "list",
    "probe",
    "screenshot",
    "click",
    "swipe",
    "key",
    "text",
    "app",
    "start",
    "stop",
    "restart",
    "configure",
    "version",
)

#: §8 deferrals that must NOT appear as verbs (v0.1 club / parked items).
DEFERRED_VERBS = ("create", "clone", "copy", "export", "import", "vendor", "status", "install")


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


def _run_raw(argv: Sequence[str]) -> tuple[int, Recorder, str]:
    """Invoke the async entry point on a fresh loop (repo convention: no
    async-plugin markers) and capture both contract channels."""
    with _captured() as (out, err):
        code = asyncio.run(amain(list(argv)))
    return code, out, err.text


def _run(argv: Sequence[str]) -> tuple[int, str, str]:
    """The text-channel view of :func:`_run_raw`."""
    code, recorder, err = _run_raw(argv)
    return code, recorder.text, err


def _error(err: str) -> dict[str, Any]:
    """The pinned error object off stderr (§5's shape, in the pinned order)."""
    payload = json.loads(err.splitlines()[-1])
    assert list(payload) == ["error"]
    assert list(payload["error"]) == ["code", "exit", "message", "suggestion"]
    return payload["error"]


def _no_air_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make BlueStacks Air's conf store absent, so `list` reflects the scripted
    ADB transport instead of whatever this host happens to have installed."""
    monkeypatch.setenv("EMUCTL_BLUESTACKS_CONF_PATH", "/nonexistent/emuctl-test-bluestacks.conf")


def _doc(out: str) -> Any:
    documents = [line for line in out.splitlines() if line.strip()]
    assert len(documents) == 1, f"stdout must carry exactly one document, got {documents!r}"
    return json.loads(documents[0])


@pytest.fixture
def windows_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pretend Windows with every dependency present (§3's spec-table truth is
    patched at the registry — the object-form patching test_discovery uses — and
    the memuc binary is named through the env layer so the [memu] adapter's
    transport resolves without host discovery)."""
    from emuctl._platforms import Platform
    from emuctl.backends import _registry

    monkeypatch.setattr(_registry, "current_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(_registry, "_dep_available", lambda name: True)
    monkeypatch.setenv("EMUCTL_MEMU_MEMUC_PATH", "memuc")
    return _registry


# ── the verb surface itself (§5's grammar block) ──


def test_help_lists_exactly_the_pinned_verbs() -> None:
    """§5's discoverability rule: "argv/output shapes are discoverable via
    --help". Driven through click's own harness (the framework-level twin of
    the entry points)."""
    result = CliRunner().invoke(cli_group, ["--help"], prog_name="emuctl")
    assert result.exit_code == EXIT_OK
    for verb in PINNED_VERBS:
        assert re.search(rf"^\s+{verb}\b", result.output, re.MULTILINE), (
            f"{verb} missing from --help"
        )
    for verb in DEFERRED_VERBS:
        assert not re.search(rf"^\s+{verb}\b", result.output, re.MULTILINE), (
            f"{verb} must not exist at v0"
        )


@pytest.mark.parametrize("verb", PINNED_VERBS)
def test_every_verb_has_its_own_help(verb: str) -> None:
    result = CliRunner().invoke(cli_group, [verb, "--help"], prog_name="emuctl")
    assert result.exit_code == EXIT_OK
    assert "Usage: emuctl" in result.output


def test_bare_invocation_prints_help() -> None:
    """A bare invocation is discoverability, not a failure (interpretation
    decision, recorded in ``_commands``)."""
    result = CliRunner().invoke(cli_group, [], prog_name="emuctl")
    assert result.exit_code == EXIT_OK
    assert result.output.startswith("Usage: emuctl")


def test_version_human_and_json_are_pinned() -> None:
    import importlib.metadata

    installed = importlib.metadata.version("emuctl")
    code, out, _ = _run(["version"])
    assert code == EXIT_OK
    assert out == f"emuctl {installed}\n"

    for argv in (["version", "--json"], ["--json", "version"]):
        code, out, _ = _run(argv)
        assert code == EXIT_OK
        document = _doc(out)
        assert document == {"schema_version": 1, "emuctl_version": installed}
        assert list(document) == ["schema_version", "emuctl_version"]
        # deterministic: the identical argv twice renders the identical bytes
        _, again, _ = _run(argv)
        assert again == out


def test_version_json_document_key_order_is_deterministic() -> None:
    code, out, _ = _run(["version", "--json"])
    assert code == EXIT_OK
    first = out
    _, second, _ = _run(["version", "--json"])
    assert first == second
    assert list(json.loads(out)) == ["schema_version", "emuctl_version"]


def test_version_resolves_no_config() -> None:
    """Interpretation ledger (`_commands`, "``version`` resolves NO config"):
    version is the cheapest early probe, so even a --config PATH pointing at
    an unreadable file (which would be a typed ConfigError exit 3 for any
    other verb) is never opened for it — exit 0, clean stdout document."""
    code, out, err = _run(["--json", "--config", "/nonexistent/emuctl.toml", "version"])
    assert code == EXIT_OK, err
    document = _doc(out)
    assert document["schema_version"] == 1
    assert err == ""


# ── arity refusals (§5: "arity PER VERB") ──


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "extra"],
        ["probe"],
        ["probe", "adb:x", "adb:y"],
        ["screenshot", "adb:x"],
        ["screenshot", "adb:x", "-o", "x.png", "--stdout"],
        ["screenshot", "adb:x", "--stdout", "--json"],
        ["click", "adb:x", "1"],
        ["click", "adb:x", "1", "2", "3"],
        ["click", "adb:x", "1", "2", "--count", "0"],
        ["click", "adb:x", "1", "2", "--count", "many"],
        ["click", "adb:x", "one", "2"],
        ["swipe", "adb:x", "1", "2", "3"],
        ["swipe", "adb:x", "1", "2", "3", "4", "--duration-ms", "-5"],
        ["key", "adb:x"],
        ["key", "adb:x", "KEYCODE_HOME", "extra"],
        ["text", "adb:x"],
        ["app", "start", "adb:x"],
        ["app", "installed"],
        ["app", "relaunch", "adb:x", "com.example"],
        ["app", "start", "adb:x", "com.example", "extra"],
        ["start"],
        ["stop", "adb:x", "extra"],
        ["restart"],
        ["configure", "adb:x", "set"],
        ["configure", "adb:x", "set", "display.width"],
        ["configure", "adb:x", "get", "display.width", "800"],
        ["version", "extra"],
        ["not-a-verb"],
        ["--config", "cf.toml", "--no-config", "list"],
    ],
)
def test_usage_refusals_exit_two_with_empty_stdout(argv: list[str]) -> None:
    code, out, err = _run(argv)
    assert code == 2, err
    assert out == ""
    # §5: an agent never faces a bare-text-only failure — the JSON error object
    # is always on stderr.
    assert json.loads(err.splitlines()[-1])["error"]["exit"] == 2


# ── target grammar (§5: canonical backend:key ONLY) ──


@pytest.mark.parametrize(
    "target",
    [
        "emulator-5554",  # bare serial
        "127.0.0.1:5555",  # bare host:port
        "myvm",  # bare name
        "foo:bar",  # unknown backend
        ":emulator-5554",  # empty backend
    ],
)
def test_malformed_targets_refuse_with_the_prefix_hint(target: str) -> None:
    """§5: "a malformed shape ⇒ exit 2 telling the caller to prefix" — the hint
    lives ONCE in §3's identity layer (never re-spelled in the CLI)."""
    code, out, err = _run(["click", target, "1", "2"])
    assert code == 2
    assert out == ""
    error = _error(err)
    assert error["code"] == "invalid_argument"
    assert "adb:127.0.0.1:5555" in error["suggestion"]


@pytest.mark.parametrize("target", ["adb:", "adb:   ", "adb:has space"])
def test_wellformed_shape_with_an_unusable_key_refuses(target: str) -> None:
    """A canonical prefix with an empty/invalid key is still a caller refusal
    (§2's invalid_argument / invalid_serial family, both exit 2)."""
    code, out, err = _run(["click", target, "1", "2"])
    assert code == 2
    assert out == ""
    assert _error(err)["code"] in {"invalid_argument", "invalid_serial"}


def test_well_formed_but_absent_target_exits_four(
    monkeypatch: pytest.MonkeyPatch, windows_host: Any
) -> None:
    """§5: "well-formed but absent ⇒ exit 4" — the MEmu adapter's stale-ref
    observation (§3 identity lifetime) surfaces as instance_not_found."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls, vms=b"0,other-vm,MEmu,1,1\n"))
    code, out, err = _run(["--no-config", "click", MEMU, "1", "2"])
    assert code == 4
    assert out == ""
    assert json.loads(err.splitlines()[-1])["error"]["code"] == "instance_not_found"


# ── the key grammar (§5: ^KEYCODE_[A-Z_]+$ | bare digits) ──


@pytest.mark.parametrize(
    ("token", "ok_token"),
    [
        ("KEYCODE_HOME", "KEYCODE_HOME"),
        ("KEYCODE_APP_SWITCH", "KEYCODE_APP_SWITCH"),
        ("66", "66"),
        ("0", "0"),
    ],
)
def test_key_accepts_the_pinned_grammar(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    ok_token: str,
) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, _ = _run(["--no-config", "key", ADB, token])
    assert code == EXIT_OK, out
    assert calls[-1][-2:] == ["keyevent", ok_token]


@pytest.mark.parametrize(
    "token",
    [
        "home",
        "KEYCODE_home",
        "KEYCODE_",
        "1a",
        "h ome",
        "",
        "HOME",
        # trailing whitespace must refuse too (§5: ``^...$`` means the WHOLE
        # token — the regex anchors at ``\Z``, not at the pre-``$`` newline):
        "KEYCODE_HOME\n",
        "66\n",
        "KEYCODE_HOME\r",
        "66\r",
        "KEYCODE_HOME\t",
    ],
)
def test_key_refuses_anything_else_as_usage_exit_two(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """§5: "anything else ⇒ usage exit 2 (no silent text)" — the refusal names
    the grammar and the escaping route, and no vendor argv ever runs."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, err = _run(["--no-config", "key", ADB, token])
    assert code == 2
    assert out == ""
    assert calls == []
    error = json.loads(err.splitlines()[-1])["error"]
    assert error["code"] == "invalid_argument"
    assert "KEYCODE_" in error["suggestion"]


# ── happy paths over the fake transport ──


def test_click_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, _ = _run(["--no-config", "--json", "click", ADB, "100", "200"])
    assert code == EXIT_OK
    document = _doc(out)
    assert document["schema_version"] == 1
    assert document["command"] == "click"
    assert document["target"] == ADB
    assert document["result"] == {"x": 100, "y": 200, "count": 1, "interval": 0.0}
    assert calls == [
        [
            "adb",
            "-H",
            "127.0.0.1",
            "-P",
            "5037",
            "-s",
            SERIAL,
            "shell",
            "input",
            "tap",
            "100",
            "200",
        ]
    ]


def test_click_count_and_interval_repeat_one_tap_per_count(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, _, _ = _run(["--no-config", "click", ADB, "5", "6", "--count", "3", "--interval", "0"])
    assert code == EXIT_OK
    assert (
        calls
        == [
            [
                "adb",
                "-H",
                "127.0.0.1",
                "-P",
                "5037",
                "-s",
                SERIAL,
                "shell",
                "input",
                "tap",
                "5",
                "6",
            ]
        ]
        * 3
    )


def test_swipe_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, _ = _run(
        ["--no-config", "--json", "swipe", ADB, "1", "2", "3", "4", "--duration-ms", "500"],
    )
    assert code == EXIT_OK
    assert _doc(out)["result"]["duration_ms"] == 500
    assert calls[-1][-6:] == ["swipe", "1", "2", "3", "4", "500"]


def test_text_passes_through_verbatim_and_is_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    secret = "1337 4242"  # noqa: S105 - a typed PIN, not a credential
    code, out, _ = _run(["--no-config", "--json", "text", ADB, secret])
    assert code == EXIT_OK
    # escaping is the ADB mechanic's (§1/§4), verified here only as passthrough
    assert calls[-1][-2:] == ["text", "1337%s4242"]
    # the CLI never echoes the value back (a PIN is a secret)
    assert secret not in out
    assert _doc(out)["result"] == {"chars": len(secret)}


def test_app_start_launch_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, _ = _run(["--no-config", "--json", "app", "start", ADB, "com.example.game"])
    assert code == EXIT_OK
    assert _doc(out)["command"] == "app start"
    assert _doc(out)["result"] == {"package": "com.example.game", "started": True}
    assert [call[-1] for call in calls] == ["1", "com.example.game"]


@pytest.mark.parametrize(
    ("pm_out", "installed"),
    [
        (b"package:com.example.game\n", True),
        (b"package:com.example.game\r\n", True),
        (b"", False),
    ],
)
def test_app_installed_reports_the_observation(
    monkeypatch: pytest.MonkeyPatch,
    pm_out: bytes,
    installed: bool,
) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls, installed=pm_out))
    code, out, _ = _run(["--no-config", "--json", "app", "installed", ADB, "com.example.game"])
    assert code == EXIT_OK
    assert _doc(out)["result"] == {"package": "com.example.game", "installed": installed}
    assert calls[-1][-4:] == ["pm", "list", "packages", "com.example.game"]


def test_screenshot_stdout_emits_raw_png_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5: raw PNG bytes for `screenshot --stdout` (the ONLY raw-bytes path) —
    no wrapper, no base64, no trailing newline."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls, screencap=PNG + b"tail-bytes"))
    code, recorder, _ = _run_raw(["--no-config", "screenshot", ADB, "--stdout"])
    assert code == EXIT_OK
    assert recorder.raw == PNG + b"tail-bytes"
    assert recorder.text == ""  # no wrapper document, no base64, no newline
    assert calls[-1][-3:] == ["exec-out", "screencap", "-p"]


def test_screenshot_output_writes_the_file_and_reports_geometry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    destination = tmp_path / "frame.png"
    code, out, err = _run(["--no-config", "--json", "screenshot", ADB, "-o", str(destination)])
    assert code == EXIT_OK, err
    assert destination.read_bytes() == PNG
    document = _doc(out)
    assert document["result"] == {
        "path": str(destination),
        "bytes": len(PNG),
        "width": 800,
        "height": 600,
    }
    assert err == ""


def test_screenshot_output_overwrites_an_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5: "-o FILE overwrites an existing file — documented, not gated"."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    destination = tmp_path / "frame.png"
    destination.write_bytes(b"stale-contents")
    code, _, _ = _run(["--no-config", "screenshot", ADB, "-o", str(destination)])
    assert code == EXIT_OK
    assert destination.read_bytes() == PNG


def test_screenshot_output_to_an_unopenable_path_is_an_unmapped_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§2's taxonomy has no I/O class, so a failed ``-o`` write exits 9 with the
    pinned error object (never a traceback, never a silent success)."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, err = _run(
        ["--no-config", "--json", "screenshot", ADB, "-o", "/nonexistent-dir/frame.png"]
    )
    assert code == 9
    assert out == ""
    error = _error(err)
    assert error["code"] == "emuctl_error"
    assert "Traceback" not in err


def test_log_config_is_consumed_by_the_cli_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§4: "``LogConfig`` ... consumed ONLY by the CLI surface". A resolved
    ``log.file`` gets the library's DEBUG narration for the invocation; no file
    leaves the ``emuctl`` logger with exactly its one NullHandler (§2)."""
    import logging

    log_path = tmp_path / "emuctl.log"
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, _, err = _run(["--config", str(_config_file(tmp_path, log_path)), "click", ADB, "1", "2"])
    assert code == EXIT_OK, err
    assert "DEBUG" in log_path.read_text(encoding="utf-8")
    logger = logging.getLogger("emuctl")
    assert [type(handler) for handler in logger.handlers] == [logging.NullHandler]


def _config_file(tmp_path: Path, log_path: Path) -> Path:
    """A one-line emuctl.toml pinning the log destination (§4's flag-only
    --config escape hatch)."""
    path = tmp_path / "emuctl.toml"
    path.write_text(f'[log]\nlevel = "debug"\nfile = "{log_path}"\n', encoding="utf-8")
    return path


def test_list_emits_one_bare_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5: "`list` is a bare array; rows carry backend/serial/running/platform
    fields to filter on"."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls, devices=ADB_DEVICES))
    _no_air_store(monkeypatch)
    code, out, _ = _run(["--json", "--no-config", "list"])
    assert code == EXIT_OK
    rows = json.loads(out)
    assert isinstance(rows, list)  # a bare array, not a wrapper document
    # sorted by (backend, key) in codepoint order — no locale, no timestamps
    assert [row["ref"] for row in rows] == [
        "adb:127.0.0.1:5555",
        "adb:ABCDEF123456",
        "adb:emulator-5554",
    ]
    assert [row["running"] for row in rows] == [True, False, True]
    assert [row["serial"] for row in rows] == ["127.0.0.1:5555", "ABCDEF123456", SERIAL]
    assert rows[0]["backend"] == "adb"
    assert rows[0]["platform"] == ["linux", "macos", "windows"]
    assert rows[0]["details"] == {"adb_state": "device"}
    # deterministic ordering: the identical invocation renders identical bytes
    _, again, _ = _run(["--json", "--no-config", "list"])
    assert again == out


def test_list_human_mode_is_a_table(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    _no_air_store(monkeypatch)
    code, out, _ = _run(["--no-config", "list"])
    assert code == EXIT_OK
    assert out.splitlines()[0].split() == ["REF", "BACKEND", "SERIAL", "RUNNING", "PLATFORM"]
    assert "adb:emulator-5554" in out
    assert "yes" in out
    assert "no" in out  # the offline row


def test_list_with_no_instances_is_an_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls, devices=b"List of devices attached\n\n"))
    _no_air_store(monkeypatch)
    code, out, _ = _run(["--json", "--no-config", "list"])
    assert code == EXIT_OK
    assert out == "[]\n"


def test_config_escape_hatch_reads_the_named_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§4's flag-only escape hatches: ``--config PATH`` replaces the discovered
    project/user layers, and an unreadable one is a typed environment refusal
    (exit 3) — never a traceback."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    _no_air_store(monkeypatch)
    code, _, err = _run(["--config", "/nonexistent/emuctl.toml", "list"])
    assert code == 3
    assert _error(err)["code"] == "config"

    conf = tmp_path / "emuctl.toml"
    conf.write_text("[log]\nlevel = 'debug'\n", encoding="utf-8")
    code, out, err = _run(["--config", str(conf), "--json", "probe", ADB])
    assert code == EXIT_OK, err
    assert _doc(out)["target"] == {"backend": "adb", "key": SERIAL}


def test_probe_document_is_the_pinned_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    _no_air_store(monkeypatch)
    code, out, err = _run(["--json", "--no-config", "probe", ADB])
    assert code == EXIT_OK, err
    document = _doc(out)
    assert document["schema_version"] == 1
    assert document["backend"] == "adb"
    assert document["target"] == {"backend": "adb", "key": SERIAL}
    assert document["platform"]["os"]
    # D3: supported is the TARGET backend's list — for adb, the three platforms.
    assert document["platform"]["supported"] == ["linux", "macos", "windows"]
    assert document["state"]["reachable"] == "ok"
    assert document["state"]["running"] is True
    assert document["state"]["adb_state"] == "device"
    # the WHOLE CapabilityFlag vocabulary, sorted by flag name, uniform rows
    features = document["features"]
    assert [row["flag"] for row in features] == sorted(flag.value for flag in CapabilityFlag)
    assert features[0] == {"flag": "apk_install", "present": True}
    assert all(set(row) == {"flag", "present"} for row in features)
    assert {"flag": "boot", "present": False} in features  # plain ADB has no BOOT

    # D3, human channel: the supported list reads the SAME document field, so
    # the two renderings cannot disagree about the target's platforms.
    _, human_out, human_err = _run(["--no-config", "probe", ADB])
    assert human_err == ""
    assert "platform: os=" in human_out
    assert "supported=linux,macos,windows" in human_out


def test_probe_platform_supported_names_the_target_backend(
    monkeypatch: pytest.MonkeyPatch,
    windows_host: Any,
) -> None:
    """D3 divergence pin: probing a windows-only backend must report THAT
    backend's supported list in both renderings — not the whole platform map
    (which would leak adb's three-platform list into a MEmu probe)."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls, vms=MEMU_VMS))

    code, out, err = _run(["--json", "--no-config", "probe", MEMU])
    assert code == EXIT_OK, err
    assert _doc(out)["platform"]["supported"] == ["windows"]

    code, out, err = _run(["--no-config", "probe", MEMU])
    assert code == EXIT_OK, err
    assert "platform: os=" in out
    assert "supported=windows" in out
    assert "supported=linux,macos,windows" not in out  # the divergence, refused


def test_probe_reports_unreachable_as_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1's probes exemption survives the CLI: a negative observation is data,
    not a failure (exit 0, no error object)."""
    calls: list[list[str]] = []

    def respond(argv: list[str]) -> Any:
        if argv[-1] == "devices":
            return ok(stdout=b"List of devices attached\n")
        if "echo" in argv:
            return ok(stdout=b"", stderr=b"error: device offline", returncode=1)
        return ok()

    install_transport(monkeypatch, spawn_from(calls, respond))
    _no_air_store(monkeypatch)
    code, out, err = _run(["--json", "--no-config", "probe", ADB])
    assert code == EXIT_OK
    assert err == ""
    assert _doc(out)["state"]["reachable"] == "offline"


def test_probe_of_an_absent_instance_reports_nulls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls, devices=b"List of devices attached\n"))
    code, out, _ = _run(["--no-config", "--json", "probe", ADB])
    assert code == EXIT_OK
    state = _doc(out)["state"]
    assert state["running"] is None and state["adb_state"] is None


# ── the lifecycle/config verbs over a real (scripted) MEmu adapter ──


def test_start_stop_and_restart_over_the_memu_transport(
    monkeypatch: pytest.MonkeyPatch,
    windows_host: Any,
) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls, vms=MEMU_VMS))
    for verb in ("start", "stop"):
        code, out, err = _run(["--no-config", "--json", verb, MEMU])
        assert code == EXIT_OK, err
        assert _doc(out)["command"] == verb
    assert ["memuc", "-n", "pyclashbot-136", "start", "-b"] in calls
    assert ["memuc", "-n", "pyclashbot-136", "stop"] in calls


def test_restart_with_app_and_timeout_over_the_memu_transport(
    monkeypatch: pytest.MonkeyPatch,
    windows_host: Any,
) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls, vms=MEMU_VMS))
    code, out, err = _run(
        ["--no-config", "--json", "restart", MEMU, "--app", "com.example.game", "--timeout", "30"],
    )
    assert code == EXIT_OK, err
    document = _doc(out)
    assert document["result"] == {"app": "com.example.game", "timeout": 30.0}
    assert ["memuc", "-n", "pyclashbot-136", "stop"] in calls  # stop-if-running
    assert ["memuc", "-n", "pyclashbot-136", "start", "-b"] in calls
    assert any("connect" in call for call in calls)  # the attach phase


def test_configure_builds_a_one_key_overlay(
    monkeypatch: pytest.MonkeyPatch,
    windows_host: Any,
) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls, vms=MEMU_VMS))
    code, out, err = _run(
        ["--no-config", "--json", "configure", MEMU, "set", "display.width", "800"],
    )
    assert code == EXIT_OK, err
    document = _doc(out)
    assert document["result"] == {"key": "display.width", "value": 800}
    # the typed value reaches the vendor translation, not the literal string
    assert any("setconfigex" in call and call[-1] == "800" for call in calls)


def test_configure_accepts_a_renderer_leaf(
    monkeypatch: pytest.MonkeyPatch,
    windows_host: Any,
) -> None:
    calls: list[list[str]] = []
    install_transport(monkeypatch, memu_transport(calls, vms=MEMU_VMS))
    code, out, err = _run(
        ["--no-config", "--json", "configure", MEMU, "set", "display.renderer", "directx"],
    )
    assert code == EXIT_OK, err
    assert _doc(out)["result"] == {"key": "display.renderer", "value": "directx"}
    assert any("graphics_render_mode" in call for call in calls)


# ── stdin and prompt discipline (§5: "no prompts, ever; stdin is never read") ──


def test_no_verb_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingStdin:
        def read(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the CLI must never read stdin")

        def readline(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the CLI must never read stdin")

        def fileno(self) -> int:
            raise AssertionError("the CLI must never inspect stdin")

    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    monkeypatch.setattr(sys, "stdin", ExplodingStdin())
    _no_air_store(monkeypatch)
    code, _, _ = _run(["--json", "--no-config", "list"])
    assert code == EXIT_OK
    code, _, _ = _run(["screenshot", ADB, "--stdout"])
    assert code == EXIT_OK
    code, _, _ = _run(["app", "installed", ADB, "com.example.game"])
    assert code == EXIT_OK


def test_cli_sources_contain_no_prompt_machinery() -> None:
    """§5's no-prompts promise, pinned at the source level: no click prompt /
    confirm / hidden input anywhere in the CLI package."""
    sources = sorted(Path(__file__).resolve().parents[2].glob("src/emuctl/cli/*.py"))
    assert sources, "the CLI package must exist"
    banned = ("prompt(", "confirm(", "getpass", "hide_input", "input(", "pause(")
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{source.name} must not contain {token}"


# ── the CliRunner surface (click's own harness targets the same tree) ──


def test_the_entry_points_drive_one_tree() -> None:
    """One tree, one grammar: the group the entry points drive IS the group
    click's own harness drives — no second copy of the command surface.

    (CliRunner cannot drive an ASYNC verb body: the callbacks return the
    coroutine their caller awaits, so the harness pins shapes/help only.)
    """
    from emuctl.cli._commands import cli

    assert cli is cli_group
    # D7: the surface is EXACTLY the pinned verb set — no spy verb, no gap —
    # and the app group's subverbs too.
    assert set(cli.commands) == set(PINNED_VERBS)
    app_group: Any = cli.commands["app"]
    assert set(app_group.commands) == {"start", "installed"}


def test_cli_runner_reports_usage_errors_as_exit_two() -> None:
    """Arity refusals are exit 2 through click's harness too (same tree)."""
    result = CliRunner().invoke(cli_group, ["click", "adb:x", "1"], prog_name="emuctl")
    assert result.exit_code == 2
    assert "Usage: emuctl" in result.output


def test_main_returns_the_exit_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console-script shape: main(argv) → int (the wrapper sys.exit()s it)."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    assert main(["version"]) == EXIT_OK


def test_screenshot_stdout_refuses_json_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5 pins the refusal from the ONLY ``--json`` spelling that exists: it is
    a GROUP option (§5's grammar put it on the top line), so the group-level
    argv is the real refusal path for "a raw-bytes path cannot carry a JSON
    wrapper". The per-verb spelling (``screenshot … --json``) has no such
    option — it hits click's UNKNOWN-OPTION usage refusal, a distinct message
    (D9: don't mistake the two paths for the same pin)."""
    calls: list[list[str]] = []
    install_transport(monkeypatch, adb_transport(calls))
    code, out, err = _run(["--json", "screenshot", ADB, "--stdout"])
    assert code == 2
    assert out == ""
    assert calls == []
    error = json.loads(err.splitlines()[-1])["error"]
    assert error["code"] == "invalid_argument"
    assert "--stdout" in error["message"]  # the typed refusal names the conflict

    code, out, err = _run(["screenshot", ADB, "--json", "--stdout"])
    assert code == 2
    assert out == ""
    assert calls == []
    verb_spelling = json.loads(err.splitlines()[-1])["error"]
    assert "No such option" in verb_spelling["message"]  # a different refusal
    assert verb_spelling["message"] != error["message"]

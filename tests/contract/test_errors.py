"""§7.1 conformance: taxonomy codes, payloads, stable __str__, picklability."""

import pickle
from string import Formatter
from typing import Any

import pytest

from emuctl import errors
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
    resolve_error_code,
)

CASES: tuple[tuple[type[EmuctlError], dict[str, Any], str], ...] = (
    (EmuctlError, {}, "emuctl_error"),
    (
        InvalidArgumentError,
        {"parameter": "timeout", "constraint": "must be > 0"},
        "invalid_argument",
    ),
    (InvalidSerialError, {"serial": "  "}, "invalid_serial"),
    (ExtrasMissingError, {"extra": "memu"}, "extras_missing"),
    (
        ExecutableNotFoundError,
        {"tool": "adb", "searched": ("/usr/bin/adb",)},
        "executable_not_found",
    ),
    (
        ConfigError,
        {"layer": "user", "path": "emuctl.toml", "reason": "bad_version", "value": "99"},
        "config",
    ),
    (
        ConfigValidationError,
        {"layer": "project", "path": "display.width", "value": -1, "expected": "int > 0"},
        "config_validation",
    ),
    (
        OperationTimeoutError,
        {"operation": "start", "deadline": 120.0, "elapsed": 120.5},
        "timeout",
    ),
    (
        VendorCommandError,
        {
            "command": ("adb", "connect"),
            "returncode": 1,
            "output_reason": "exit",
            "stderr_excerpt": "refused",
        },
        "vendor_command_failed",
    ),
    (DeviceStateError, {"serial": "emulator-5554", "state": "offline"}, "device_state"),
    (InstanceNotFoundError, {"instance_ref": "adb:127.0.0.1:5555"}, "instance_not_found"),
    (FeatureNotSupportedError, {"feature": "configure"}, "feature_not_supported"),
    (
        BackendNotAvailableError,
        {"reason": "platform-unsupported"},
        "backend_not_available",
    ),
    (BackendNotFoundError, {"backend_id": "nope"}, "backend_not_found"),
    (
        EmulatorNotReadyError,
        {"reason": "timeout", "cause": ValueError("spawn failed")},
        "not_ready",
    ),
    (ControllerClosedError, {}, "controller_closed"),
    (
        AmbiguousInstanceError,
        {"selector": "emu", "matched": ("adb:emu", "memu:emu")},
        "ambiguous_target",
    ),
    (SyncCallInsideEventLoopError, {}, "sync_call_inside_loop"),
)


def _taxonomy() -> list[type[EmuctlError]]:
    return [
        value
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, EmuctlError)
    ]


def test_no_extra_public_classes() -> None:
    assert sorted(cls.__name__ for cls in _taxonomy()) == sorted(
        cls.__name__ for cls, _, _ in CASES
    )


def test_pure_tree_no_stdlib_mixing() -> None:
    allowed: set[object] = {EmuctlError, Exception, BaseException, object}
    allowed.update(_taxonomy())
    for cls in _taxonomy():
        assert len(cls.__bases__) == 1
        assert issubclass(cls, EmuctlError) or cls is EmuctlError
        for base in cls.__mro__:
            assert base in allowed, f"{cls.__name__} mixes in {base!r}"


@pytest.mark.parametrize(("cls", "kwargs", "code"), CASES)
def test_error_code_payload_args_str(
    cls: type[EmuctlError], kwargs: dict[str, Any], code: str
) -> None:
    exc = cls(**kwargs)
    assert exc.args == ()
    assert exc.error_code == code
    assert resolve_error_code(exc) == code
    for key, value in kwargs.items():
        stored = getattr(exc, key)
        assert stored == value or (isinstance(value, (list, tuple)) and tuple(value) == stored)
    assert type(exc).payload == tuple(kwargs)
    text = str(exc)
    assert text.startswith(code)
    assert "\n" not in text and "\r" not in text
    assert str(cls(**kwargs)) == text  # stable
    # suggestion is a per-class template, never baked into the raised string
    if type(exc).suggestion:
        assert type(exc).suggestion not in text


def test_str_sanitizes_detail() -> None:
    exc = InvalidArgumentError(parameter="x", constraint="y", detail="a\nb\rc\x00d" * 60)
    text = str(exc)
    assert "\n" not in text and "\r" not in text and "\x00" not in text
    assert len(text) <= 400


def test_resolve_error_code_mro_and_fallback() -> None:
    assert resolve_error_code(InvalidSerialError(serial="s")) == "invalid_serial"
    assert resolve_error_code(ValueError("plain")) == "emuctl_error"
    assert resolve_error_code(EmuctlError()) == "emuctl_error"


def test_suggestion_templates_are_renderable() -> None:
    exc = ExtrasMissingError(extra="memu")
    assert type(exc).suggestion.format(extra=exc.extra) != ""


@pytest.mark.parametrize(("cls", "kwargs", "code"), CASES)
def test_suggestion_template_keys_are_declared_payload_keys(
    cls: type[EmuctlError], kwargs: dict[str, Any], code: str
) -> None:
    """Every {key} in a class suggestion template must be a declared payload key."""
    template_keys = {
        field.split("!")[0].split(":")[0].split(".")[0].split("[")[0]
        for _, field, _, _ in Formatter().parse(cls.suggestion)
        if field
    }
    assert template_keys <= set(cls.payload), (
        f"{cls.__name__}.suggestion names {sorted(template_keys - set(cls.payload))} "
        f"outside payload {list(cls.payload)}"
    )


@pytest.mark.parametrize(("cls", "kwargs", "code"), CASES)
def test_pickle_round_trip(cls: type[EmuctlError], kwargs: dict[str, Any], code: str) -> None:
    original = cls(**kwargs)
    clone = pickle.loads(pickle.dumps(original))  # noqa: S301 - round-tripping our own bytes
    assert type(clone) is cls
    assert clone.args == ()
    assert clone.error_code == code
    assert str(clone) == str(original)


def test_pickle_across_real_process_boundary() -> None:
    import base64
    import subprocess
    import sys

    script = (
        "import base64, pickle, sys; "
        "sys.path.insert(0, 'src'); "
        "from emuctl.errors import VendorCommandError; "
        "exc = VendorCommandError(command=('adb', 'shell'), returncode=1, "
        "output_reason='exit', stderr_excerpt='boom', detail='x'); "
        "sys.stdout.buffer.write(base64.b64encode(pickle.dumps(exc)))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, check=True, timeout=60
    )
    import pickle as _pickle

    clone = _pickle.loads(base64.b64decode(proc.stdout))  # noqa: S301 - child sent our own bytes
    assert type(clone) is VendorCommandError
    assert clone.args == ()
    assert clone.command == ("adb", "shell")
    assert clone.returncode == 1
    assert resolve_error_code(clone) == "vendor_command_failed"
    assert str(clone) != ""

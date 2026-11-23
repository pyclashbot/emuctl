import asyncio
import logging
import os
import pickle
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

import pytest

from emuctl import _proc
from emuctl._proc import ProcessExecutor, ProcessResult, run_process
from emuctl.errors import InvalidArgumentError, OperationTimeoutError
from emuctl.timing import DEFAULT


class FakeProcess:
    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        *,
        hang: bool = False,
        stubborn: bool = False,
        fail_with: BaseException | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._returncode: int | None = None
        self._scripted_rc = returncode
        self._hang = hang
        self._stubborn = stubborn
        self._fail_with = fail_with
        self.pid: int = 4242
        self.terminate_calls = 0
        self.kill_calls = 0

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self._stubborn:
            self._returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        if self._returncode is None:
            self._returncode = -9

    async def wait(self) -> int:
        if self._hang and self._returncode is None:
            await asyncio.sleep(30.0)
        if self._returncode is None:
            self._returncode = self._scripted_rc
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        assert input is None
        if self._fail_with is not None:
            raise self._fail_with
        if self._hang:
            await asyncio.sleep(30.0)
        self._returncode = self._scripted_rc
        return (self._stdout, self._stderr)


class Harness:
    def __init__(self, proc: FakeProcess) -> None:
        self.proc = proc
        self.seen: list[list[str]] = []

    async def spawn(self, argv: Sequence[str]) -> FakeProcess:
        self.seen.append(list(argv))
        return self.proc


def test_argv_passed_verbatim_and_result_returned() -> None:
    proc = FakeProcess(stdout=b"out", stderr=b"err", returncode=0)
    harness = Harness(proc)
    executor = ProcessExecutor(_spawn=harness.spawn)
    result = asyncio.run(executor.run(["adb", "-s", "emulator-5554", "shell", "echo hi"], operation="probe"))
    assert harness.seen == [["adb", "-s", "emulator-5554", "shell", "echo hi"]]
    assert result == ProcessResult(stdout=b"out", stderr=b"err", returncode=0)


def test_nonzero_exit_is_data_never_raised() -> None:
    proc = FakeProcess(stdout=b"nope", stderr=b"device offline", returncode=1)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)
    result = asyncio.run(executor.run(["adb", "shell", "true"], operation="probe"))
    assert result.returncode == 1
    assert result.stdout == b"nope"


@pytest.mark.parametrize(
    "bad",
    ["adb devices", "", [], ["adb", 5], [b"adb"], ["adb", None], ["adb", ["-s"]]],
)
def test_argv_misuse_rejected_before_io(bad: object) -> None:
    async def spawn(argv: Sequence[str]) -> FakeProcess:
        raise AssertionError("spawn must not run on invalid argv")

    executor = ProcessExecutor(_spawn=spawn)
    with pytest.raises(InvalidArgumentError):
        asyncio.run(executor.run(bad, operation="probe"))  # ty: ignore[invalid-argument-type]


def test_timeout_abandons_child_and_raises_operation_timeout() -> None:
    proc = FakeProcess(hang=True)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)
    with pytest.raises(OperationTimeoutError) as excinfo:
        asyncio.run(executor.run(["adb", "wait-for-device"], operation="boot", timeout=0.1))
    exc = excinfo.value
    assert exc.operation == "boot"
    assert exc.deadline is not None and exc.elapsed is not None
    # Windows timer granularity (≈15.6 ms default tick) can report an elapsed
    # a hair under the nominal budget even though the full budget was awaited.
    assert exc.elapsed >= 0.1 - 0.02
    assert exc.elapsed < 5.0
    assert proc.terminate_calls == 1
    assert proc.returncode is not None  # reaped, no straggler


def test_timeout_escalates_to_kill_for_stubborn_child() -> None:
    proc = FakeProcess(hang=True, stubborn=True)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)
    with pytest.raises(OperationTimeoutError):
        asyncio.run(executor.run(["adb", "wait-for-device"], operation="boot", timeout=0.1))
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1


def test_default_sentinel_resolves_via_shared_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProcess(hang=True)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)
    monkeypatch.setattr(_proc, "QUERY_TIMEOUT", 0.1)
    with pytest.raises(OperationTimeoutError):
        asyncio.run(executor.run(["adb", "wait-for-device"], operation="boot", timeout=DEFAULT))
    assert proc.terminate_calls == 1


def test_cancellation_propagates_unmodified_and_abandons() -> None:
    marker = asyncio.CancelledError("injected")
    proc = FakeProcess(fail_with=marker)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)
    with pytest.raises(asyncio.CancelledError) as excinfo:
        asyncio.run(executor.run(["adb", "wait-for-device"], operation="boot"))
    assert excinfo.value is marker
    assert proc.terminate_calls == 1


def test_task_cancel_abandons_child_without_timeout_conflation() -> None:
    proc = FakeProcess(hang=True)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)

    async def main() -> None:
        task = asyncio.ensure_future(executor.run(["adb", "wait-for-device"], operation="boot"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert proc.terminate_calls == 1


def test_close_is_idempotent_and_sweeps_stragglers() -> None:
    proc = FakeProcess(hang=True)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)

    async def main() -> None:
        task = asyncio.ensure_future(executor.run(["adb", "wait-for-device"], operation="boot"))
        await asyncio.sleep(0.05)
        await executor.close()  # sweeps the in-flight child
        await executor.close()  # idempotent on empty set
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await executor.close()

    asyncio.run(main())
    assert proc.terminate_calls >= 1


def test_creationflags_no_window_on_windows_zero_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _proc._creationflags() == 0
    if not hasattr(subprocess, "CREATE_NO_WINDOW"):
        pytest.skip("CREATE_NO_WINDOW is unavailable on this platform")
    monkeypatch.setattr(sys, "platform", "win32")
    assert _proc._creationflags() == subprocess.CREATE_NO_WINDOW


def test_default_spawn_passes_argv_and_no_window_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_create(*argv: str, **kwargs: object) -> FakeProcess:
        captured["argv"] = list(argv)
        captured.update(kwargs)
        return FakeProcess(stdout=b"hi\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    proc = asyncio.run(_proc._default_spawn(["echo", "hi"]))
    assert captured["argv"] == ["echo", "hi"]
    assert "shell" not in captured
    assert captured["creationflags"] == _proc._creationflags()
    assert isinstance(proc, FakeProcess)


def test_argv_debug_logged_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    proc = FakeProcess()
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)
    with caplog.at_level(logging.DEBUG, logger="emuctl._proc"):
        asyncio.run(executor.run(["adb", "shell", "a\nb"], operation="probe"))
    messages = [r.getMessage() for r in caplog.records if r.name == "emuctl._proc"]
    assert any("adb" in m for m in messages)
    assert not any("a\nb" in m for m in messages)


def test_process_result_picklable() -> None:
    result = ProcessResult(stdout=b"x", stderr=b"y", returncode=3)
    assert pickle.loads(pickle.dumps(result)) == result  # noqa: S301 - round-tripping our own bytes


def test_real_process_timeout_evidence() -> None:
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(OperationTimeoutError):
        asyncio.run(run_process(argv, operation="real-timeout-probe", timeout=0.3))


def test_real_process_success_and_cancel_evidence() -> None:
    result = asyncio.run(run_process([sys.executable, "-c", "print('hi')"], operation="real-probe"))
    # Child line ending is platform-defined (LF vs CRLF); executor must not translate bytes.
    assert result.stdout.replace(b"\r\n", b"\n") == b"hi\n"
    assert b"hi" in result.stdout
    assert result.returncode == 0

    async def main() -> None:
        task = asyncio.ensure_future(
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                operation="real-cancel-probe",
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


@pytest.mark.parametrize("bad_timeout", [0, 0.0, -1, -0.5, float("inf"), float("nan")])
def test_nonpositive_timeout_rejected_before_spawn(bad_timeout: float) -> None:
    calls: list[list[str]] = []

    async def spawn(argv: Sequence[str]) -> FakeProcess:
        calls.append(list(argv))
        raise AssertionError("spawn must not run on invalid timeout")

    executor = ProcessExecutor(_spawn=spawn)
    with pytest.raises(InvalidArgumentError):
        asyncio.run(executor.run(["adb", "devices"], operation="probe", timeout=bad_timeout))
    assert calls == []


def test_hanging_spawn_is_bounded_by_deadline() -> None:
    async def hanging_spawn(argv: Sequence[str]) -> FakeProcess:
        await asyncio.sleep(30.0)
        raise AssertionError("unreachable")

    executor = ProcessExecutor(_spawn=hanging_spawn)
    start = time.monotonic()
    with pytest.raises(OperationTimeoutError) as excinfo:
        asyncio.run(executor.run(["adb", "wait-for-device"], operation="boot", timeout=0.1))
    elapsed = time.monotonic() - start
    # Windows timer granularity (≈15.6 ms default tick) can report an elapsed
    # a hair under the nominal budget even though the full budget was awaited.
    assert excinfo.value.elapsed is not None and excinfo.value.elapsed >= 0.1 - 0.02
    assert elapsed < 5.0


def _assert_real_child_dead(proc: Any, *, timeout_s: float = 5.0) -> None:
    pid = proc.pid
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            break
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        time.sleep(0.05)
    if proc.returncode is None:
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        raise AssertionError("real child straggler still alive after timeout/cancel")


def test_real_process_timeout_leaves_no_straggler() -> None:
    seen: list[Any] = []

    async def capturing_spawn(argv: Sequence[str]) -> Any:
        proc = await _proc._default_spawn(argv)
        seen.append(proc)
        return proc

    executor = ProcessExecutor(_spawn=capturing_spawn)
    with pytest.raises(OperationTimeoutError):
        asyncio.run(
            executor.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                operation="real-timeout-probe",
                timeout=0.3,
            )
        )
    assert len(seen) == 1
    _assert_real_child_dead(seen[0])


def test_real_process_cancel_leaves_no_straggler() -> None:
    seen: list[Any] = []

    async def capturing_spawn(argv: Sequence[str]) -> Any:
        proc = await _proc._default_spawn(argv)
        seen.append(proc)
        return proc

    executor = ProcessExecutor(_spawn=capturing_spawn)

    async def main() -> None:
        task = asyncio.ensure_future(
            executor.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                operation="real-cancel-probe",
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert len(seen) == 1
    _assert_real_child_dead(seen[0])


def test_close_during_inflight_then_executor_reusable() -> None:
    hang = FakeProcess(hang=True)
    ok = FakeProcess(stdout=b"ok", stderr=b"", returncode=0)
    calls = 0

    async def spawn(argv: Sequence[str]) -> FakeProcess:
        nonlocal calls
        calls += 1
        return hang if calls == 1 else ok

    executor = ProcessExecutor(_spawn=spawn)

    async def main() -> ProcessResult:
        task = asyncio.ensure_future(executor.run(["adb", "wait-for-device"], operation="probe"))
        await asyncio.sleep(0.05)
        await executor.close()
        assert hang.terminate_calls >= 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await executor.run(["adb", "devices"], operation="probe")

    result = asyncio.run(main())
    assert result == ProcessResult(stdout=b"ok", stderr=b"", returncode=0)


def test_second_cancel_during_abandon_still_cancels_and_cleans_up() -> None:
    marker = asyncio.CancelledError("injected")

    class SlowReap(FakeProcess):
        async def wait(self) -> int:
            if self._returncode is None and self.terminate_calls > 0:
                await asyncio.sleep(0.3)
                self._returncode = -15
                return self._returncode
            return await super().wait()

    proc = SlowReap(fail_with=marker)
    executor = ProcessExecutor(_spawn=Harness(proc).spawn)

    async def drive() -> None:
        task = asyncio.ensure_future(executor.run(["adb", "wait-for-device"], operation="probe"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert proc.terminate_calls >= 1
    assert proc.returncode is not None


# ── spawn_detached: the executor-owned fire-and-forget GUI launch (§6) ──


def test_spawn_detached_argv_validation_matches_run() -> None:
    calls: list[list[str]] = []

    async def spawn(argv: Sequence[str]) -> FakeProcess:
        calls.append(list(argv))
        raise AssertionError("spawn must not run on invalid argv")

    executor = ProcessExecutor(_spawn=spawn)
    with pytest.raises(InvalidArgumentError):
        asyncio.run(executor.spawn_detached("player --instance x"))
    with pytest.raises(InvalidArgumentError):
        asyncio.run(executor.spawn_detached(["player", 5]))  # ty: ignore[invalid-argument-type]
    assert calls == []


def test_spawn_detached_is_untracked_and_never_swept() -> None:
    proc = FakeProcess(hang=True)
    harness = Harness(proc)
    executor = ProcessExecutor(_spawn=harness.spawn)

    async def main() -> None:
        spawned = await executor.spawn_detached(["player", "--instance", "x"])
        assert spawned is proc
        assert harness.seen == [["player", "--instance", "x"]]
        await executor.close()  # sweeps tracked children — NOT this one

    asyncio.run(main())
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0
    assert proc.returncode is None  # never reaped; the GUI child keeps its life


def test_spawn_detached_default_spawner_discards_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_create(*argv: str, **kwargs: object) -> FakeProcess:
        captured["argv"] = list(argv)
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    asyncio.run(_proc._default_spawn_detached(["player", "--instance", "x"]))
    assert captured["argv"] == ["player", "--instance", "x"]
    assert captured["stdout"] == _proc.DEVNULL
    assert captured["stderr"] == _proc.DEVNULL
    assert captured["creationflags"] == _proc._creationflags()
    assert "timeout" not in captured  # fire-and-forget: fire() only, forget() by caller

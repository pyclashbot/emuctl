# emuctl — research notes

*Working notes from reviewing `pymemuc` (v0.6.0 + unreleased `fix/custom-resolution-multi-arg`) and `py-clash-bot`@master (Oct 6, 2026), to carry into this project's design. emuctl is the name for a generalized ADB/Android emulator controller for Python — pymemuc's job, but vendor-agnostic and a higher abstraction.*

## 1. The dependency and each project's role

**`pymemuc`** is a standalone, general-purpose Python wrapper around MEmu's `memuc.exe` vendor CLI. Published to PyPI (latest tag `v0.6.0`), intentionally dependency-free (`dependencies = []`). Exposes VM lifecycle (create/start/stop/clone/rename/export/import), config get/set, and VM-level commands (click/keystroke/APK/GPS/ADB passthrough).

**`py-clash-bot`** is the consumer. It depends on `pymemuc>=0.6.0,<0.7` (`pyproject.toml:22`) but only for **one of four** emulator backends — its `MemuEmulatorController`. It has its own abstraction (`emulators/base.py` → `AdbBasedController` → 4 concrete controllers) and drives BlueStacks, Google Play Games, and plain ADB devices through **direct `adb` subprocess calls**, not through pymemuc.

Real relationship: `pymemuc` is the MEmu-specific adapter library; `py-clash-bot` is a multi-backend client that uses pymemuc for exactly the MEmu backend.

Note: the local `pymemuc` checkout is **not** what py-clash-bot installs. It's on branch `fix/custom-resolution-multi-arg` with unreleased work (uv/hatchling migration, `set_configuration_vm` multi-arg fix, `rename_vm` fix). The PyPI `0.6.0` that py-clash-bot resolves is several commits behind `HEAD`.

## 2. General interfacing model

**pymemuc — "vendor CLI as the API."** Everything is `subprocess.Popen(memuc.exe, [args])` (`_memuc.py:79-135`), with `SUCCESS` / `Value:` string parsing of stdout. Windows-only: `PyMemuc.__init__` either reads the MEmu install path from the registry, or requires an explicit `memuc_path`, else raises (`pymemuc.py:74-82`). ADB is reached *through* memuc (`send_adb_command_vm` → `memuc ... adb ...`). Methods are module-level functions injected into the class namespace via `from ._command import (...)` inside the class body (`pymemuc.py:21-64`) — no real inheritance.

**py-clash-bot — "port + adapters."** `BaseEmulatorController` defines the contract (create/configure/restart/start/stop/click/swipe/screenshot/install_apk/start_app/is_app_installed/is_reachable). `AdbBasedController` implements the primitives once over `adb`, auto-scoping commands with `-s <serial>` and validating serials against injection patterns (`adb_base.py:20-24,141-153`). Concrete controllers differ mainly in *discovery/config/launch* (registry, config files, ports, process names). `restart()` is the boot primitive; `EmulatorNotReadyError` is the fail-fast contract.

## 3. Commonalities

- **Subprocess-driven, screen-scraping philosophy.** Neither uses a game/emulator API beyond the vendor CLI or ADB; both treat the emulator as a black box.
- **Platform gating.** pymemuc gates on Windows (`WIN32`/`WINREG_EN`, `_constants.py`); py-clash-bot gates MEmu/Google Play to Windows and BlueStacks to Win+macOS (`supported_platforms`).
- **Named lifecycle.** Both model create/start/stop; py-clash-bot layers `configure` + `restart` on top.
- **Typed errors.** pymemuc has `PyMemucError`/`PyMemucTimeoutExpired`; py-clash-bot has `EmulatorNotReadyError`.
- **Bundled-vs-system binary discovery** is a shared concern: registry lookups (pymemuc `_get_memu_top_level`, py-clash-bot `_get_bluestacks_registry_value`) and bundled-ADB resolution (`find_adb`).
- **pymemuc and py-clash-bot converged on the same MEmu facts**: Android 13 `"136"` VM named `pyclashbot-136`, 419×633 @ 160dpi, hidden toolbar, CPU/RAM caps, DirectX/OpenGL ints.

## 4. Differences

| Aspect | pymemuc | py-clash-bot |
|---|---|---|
| Scope | MEmu only | 4 backends |
| Transport | `memuc.exe` (ADB via memuc) | direct `adb` for 3 backends; memuc only for MEmu |
| Architecture | function-injection into a class | ABC + inheritance |
| Multi-instance | global (`stopall`, kill all MEmu procs) | BlueStacks scopes to one instance |
| Screenshot | `screencap -p \| base64` via memuc | `exec-out screencap -p` binary via adb |
| Tests | none in-package | pytest, offline + `emulator` marker |
| Deps | zero | opencv, numpy, psutil, pymemuc, ttkbootstrap… |
| Packaging | hatchling + hatch-vcs, PyPI | setuptools, MSI/DMG builds |

## 5. Issues

### pymemuc

- **No `py.typed`.** `_types.py` defines `VMInfo`/`ConfigKeys` but nothing marks the package typed — py-clash-bot enforces `ty`, so pymemuc imports degrade to untyped. Cheap win.
- **Empty-string argv entries.** Optional flags are passed as `""` (`list_vm_info`, `_manage.py:264,274,279`). `memuc_run` inserts them verbatim, so memuc sees blank arguments. Fragile; should be filtered.
- **Exception hierarchy mismatch.** `PyMemucIndexError(PyMemucException)` but `PyMemucException(Exception)` — it is **not** a `PyMemucError` (`exceptions.py:17-34`). Callers catching `PyMemucError` (py-clash-bot does, `memu.py:444`) won't catch index errors. Also `retryable` only catches `PyMemucError`/`PyMemucTimeoutExpired`, so index errors bypass retry by design.
- **Brittle stdout parsing.** `get_configuration_vm` checks `"Value" in output` then does `split("Value: ")[1]` (`_manage.py:345-349`) — a "Value" without the exact `"Value: "` raises `IndexError`. All commands depend on the literal English `SUCCESS` substring.
- **`retryable` can mask real failures.** `start_vm`/`stop_vm`/`create_vm` are retried 3× blind, with no backoff and no idempotency check.
- **Untyped TODOs shipped.** `send_adb_command_vm` and `get_public_ip_vm` don't parse/validate output; `set_accelerometer_vm` is marked "doesn't work"; `export_vm` "verify functionality".

### py-clash-bot

- **Config file path bug (real, high-impact).** `_read_config_data` opens a hardcoded relative Windows path `r"pyclashbot\emulators\configs\memu_config.json"` (`memu.py:216`). It only resolves when CWD is the repo root — in a frozen MSI/DMG build it silently isn't found, so user overrides in `memu_config.json` are ignored and defaults win. Should be resolved relative to the package (`importlib.resources`).
- **`__del__` disables base cleanup.** `MemuEmulatorController.__del__` (`memu.py:166-167`) and `GooglePlayEmulatorController.__del__` override the base destructor **without calling `super().__del__`**, so the base auto-stop never runs. The MEmu one also logs via `self.logger`, which can be torn down at interpreter exit. Relies entirely on worker `terminate()`.
- **App-installed check can false-negative.** MEmu `start_app` calls `get_app_info_list_vm`, which returns `[]` on a 10s timeout (`_command.py:542-543`); memu then raises `EmulatorNotReadyError("<pkg> is not installed")` (`memu.py:1443-1449`). A slow-but-fine VM turns into a fatal "not installed". BlueStacks/ADB check via `pm list packages` directly, which is more reliable.
- **Global process kill breaks multi-instance.** `_close_everything_memu` kills `MEmuConsole.exe`/`MEmu.exe`/`MEmuHeadless.exe` process-wide on every `restart()` (`memu.py:779-795`), unlike BlueStacks' instance-scoped stop. Any other MEmu VM is collateral.
- **Heavier screenshot path.** memuc → adb → `screencap | base64` text round-trip per frame vs. the direct binary `exec-out screencap -p` the other backends use. Slower, and it depends on stripping memuc's injected `already connected to 127.0.0.1:` line with a regex (`memu.py:73`) — if the wording changes, base64 decoding corrupts.
- **Incomplete port.** Both `MemuEmulatorController.install_apk` and `GooglePlayEmulatorController.install_apk` raise `NotImplementedError` (`memu.py:1427-1431`, `google_play.py:463-467`) despite the base contract promising it.
- **Massive bespoke debug logging.** `memu.py` is ~1,480 lines because of the `DEBUG_CONFIGURATION` nested-logging style; compare BlueStacks (669) and Google Play (522). It's bot-policy-free (good, per the adapter rules) but the volume hurts reviewability, and `_read_config_data`'s debug block references an undefined `config_file_path` in the "not found" branch if ever enabled (`memu.py:247-248`).
- **Unbounded screenshot loop.** `MemuScreenCapture.__getitem__` loops `while True` with a 0.1s sleep and no timeout (`memu.py:118-131`) — safe only because the worker is `terminate()`d, but it violates the repo's own "no unbounded retry loops" guidance.

### Cross-project

- **Version skew.** py-clash-bot pins `pymemuc<0.7`, so it gets `0.6.0` while the local checkout already fixes `set_configuration_vm` for multi-token config values and `rename_vm`. py-clash-bot's current values are single-token, so it dodges the multi-arg bug today — but the pin will silently hide the fix until a release and a bump.
- **Duplicated ADB/registry/discovery logic** now exists in two places (pymemuc and py-clash-bot's `adb_base.py`), with independent injection-validation and device-discovery implementations.

## 6. Positives

- **pymemuc** is genuinely reusable: zero runtime deps, clean public surface, `retryable` decorator, good docstrings/docs, i18n'd Sphinx docs, and typed config-key vocabulary. The registry autodetect makes it "just work" on a normal Windows install.
- **py-clash-bot's abstraction is strong where pymemuc is narrow.** `AdbBasedController` gives click/swipe/screenshot/app-launch for free, serial-injection validation is a real security control, and the port/adapter split (policy in `worker.py`, mechanics in adapters) is clean and testable. Fail-fast `EmulatorNotReadyError` with bounded internal waits is well designed.
- The two compose sensibly: py-clash-bot's MEmu adapter uses pymemuc for the vendor-specific bits (VM create/start/config/ADB-passthrough) while staying behind the same interface as the direct-ADB backends.

## 7. Bottom line

pymemuc is the MEmu-specific "vendor CLI wrapper"; py-clash-bot is the multi-backend orchestrator that consumes it for one backend. The dependency is narrow and well-contained. Most actionable findings: the **relative config-path bug** (breaks user config in frozen builds), the **overridden `__del__` skipping base cleanup**, the **timeout-as-not-installed false negative**, and on the pymemuc side **missing `py.typed`**, the **`PyMemucIndexError` hierarchy gap**, and empty-string argv entries. The MEmu backend also has heavier I/O (double subprocess + base64 screenshots) and global process kills that make it the least multi-instance-friendly of the four.

## 8. Seeds for emuctl

Things the review surfaced that should shape this project's API:

- **Port/adapter shape wins.** One port (`BaseEmulatorController`-like), adapters per vendor; interaction primitives (click/swipe/screenshot/app lifecycle) implemented once over raw ADB, shared by all ADB-capable backends.
- **Zero-dep discipline wins.** pymemuc's `dependencies = []` made it drop-in. Keep emuctl's core dependency-free (or stdlib-only), push opencv/numpy behind optional extras so screenshot decode is optional.
- **Fail-fast error contract.** A single `EmulatorNotReadyError`-style exception with **bounded** internal waits — never unbounded retry loops, never human-in-the-loop prompts deep in the library.
- **`restart()` is the boot primitive.** Construction does cheap discovery/config only; booting is explicit and owns stop → configure-while-stopped → start → launch app → reach detectable state.
- **Never parse vendor stdout for control flow.** pymemuc's `SUCCESS`/`Value:` string matching is its biggest liability; where a vendor CLI must be wrapped, prefer state verification (`vm_is_running`, `adb get-state`) over greeting-text parsing.
- **Scope kills to instances.** No global process-name sweeps (`taskkill /im`, `pkill -f`) — target by serial/port/PID so multi-instance users aren't collateral.
- **Resolve bundled resources through the package**, never relative CWD paths (`importlib.resources`) — the memu_config.json bug.
- **Mark `py.typed` from day one.**
- **Device/serial validation as a security control**, not an afterthought (injection patterns in `adb_base.py`).
- **Screenshot transport: binary `exec-out screencap -p`** only; skip base64 round-trips and regex-stripping of vendor noise.

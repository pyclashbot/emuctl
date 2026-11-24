# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Before 1.0, a minor release may contain breaking changes; those are called out below.

## [Unreleased]

### Added

- `CHANGELOG.md`, maintained by hand, with an entry for every user-visible change.
- Google Play Games on PC backend adapter (`google-play:<serial>`): bundled-ADB discovery, interaction, and display configuration on Windows (boot stays gated).

## [0.1.0] - 2026-11-23

First public pre-release. v0.1 ships the plain ADB backend only. MEmu, BlueStacks,
AVD, and Google Play are not included in this release and remain unverified.

### Added

- Async controller API (`emuctl.connect()`) and sync facade
  (`emuctl.connect_sync()`), sharing one controller. The sync facade owns one
  anyio portal thread, supports context-manager exit, and requires an explicit
  `close()`.
- ADB backend (`adb:<serial>`): discovery, PNG screenshots, tap/swipe/key/text
  input, app launch, installed-package checks, and APK install.
- Display `CONFIGURE` on ADB: `configure()` writes `display.width`,
  `display.height`, and `display.dpi`, and returns the `DisplayState` observed
  immediately before the write, so callers can restore it explicitly with
  `configure(previous.to_spec())`.
- Capability declaration (`FEATURES`, `features`, `has_feature()`); unsupported
  operations raise `FeatureNotSupportedError` instead of failing in the vendor
  call.
- Typed error hierarchy with stable `error_code` strings and payloads.
- Layered configuration: `resolve_config()` merges flags, `EMUCTL_*`
  environment variables, project `emuctl.toml` / `[tool.emuctl]`, and user
  config into a validated `EmuctlConfig`.
- `select_instance()` to choose one discovered instance by serial or canonical
  ref; no match raises `InstanceNotFoundError`, multiple matches raise
  `AmbiguousInstanceError`.
- Per-controller environment overlay for ADB invocations (for example, a
  different `ADB_MDNS` per instance).
- Serial validation (`is_valid_serial()`, `validate_serial()`) and the
  stdlib-only `png_dimensions()` helper.
- One deadline per call with bounded internal waits; no unbounded retry loops.

### Changed

- v0.1 is intentionally narrow: the public surface was trimmed to the ADB
  essentials, and package extras no longer advertise backends that do not ship.
- `start_app()` force-stops the package first, launches with `monkey` while
  excluding syskeys, and falls back to plain `monkey` if that launch fails.

### Fixed

- ADB transport recovery: transport-loss stderr (`offline`, `unauthorized`,
  disconnected / `device not found`, connection reset) now raises
  `DeviceStateError`, and the next operation lazily re-issues a bounded
  `adb connect` instead of leaving the controller unusable.
- The local ADB server can cold-start on a custom port: `-H` is only passed for
  a genuinely remote host.
- Interrupted clicks and swipes send one bounded, error-swallowed pointer `UP`,
  so a timeout or vendor failure does not leave the device mid-gesture.
- `start_app()` fails fast when the launch command exits 0 but no process
  appears within the confirmation window, instead of reporting a successful
  launch.

### Removed

- MEmu, BlueStacks 5, BlueStacks Air, AVD, and Google Play adapters and extras
  are not part of v0.1. Their backend ids stay reserved; selecting one raises
  `BackendNotAvailableError`.
- The CLI surface and instance `create()` are out of scope for v0.1.

[Unreleased]: https://github.com/pyclashbot/emuctl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pyclashbot/emuctl/releases/tag/v0.1.0

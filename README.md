# emuctl

A small, typed controller for Android devices over ADB. One async API, one sync facade,
explicit capabilities, and typed errors.

**Status:** v0.1, pre-release. The plain ADB backend ships on all platforms, alongside
Google Play Games on PC (Windows) and BlueStacks Air (macOS Apple Silicon). MEmu,
BlueStacks 5, and AVD are future, unverified work.

## Install

Not on PyPI yet. Install from git:

```bash
pip install "emuctl @ git+https://github.com/pyclashbot/emuctl"
```

or with uv:

```bash
uv add "emuctl @ git+https://github.com/pyclashbot/emuctl"
```

Python 3.12+.

## Quick start

```python
import emuctl

with emuctl.connect_sync("adb:127.0.0.1:5555") as emu:
    emu.configure({"display.width": 419, "display.height": 633, "display.dpi": 160})
    emu.start_app("com.example.app")

    png = emu.screenshot()
    emu.click(120, 300)
```

Async works the same way:

```python
import asyncio
import emuctl


async def main() -> None:
    emu = await emuctl.connect("adb:127.0.0.1:5555")
    try:
        await emu.configure({"display.width": 419, "display.height": 633})
    finally:
        await emu.close()


asyncio.run(main())
```

`connect()` and `connect_sync()` take a canonical `backend:key` target or an
`emuctl.InstanceRef`. See what exists first:

```python
report = asyncio.run(emuctl.discover())
emu = emuctl.connect_sync("adb:emulator-5554")
```

## What v0.1 covers

- ADB transport (`adb:<serial>`) on every platform: device discovery, screenshots, taps,
  swipes, keys, text, app start and APK install.
- Google Play Games on PC (`google-play:<serial>`, Windows): discovery, screenshots,
  input, app launch and install checks, and display `CONFIGURE` against the bundled ADB.
  Boot and APK install stay gated: the launcher owns lifecycle and the vendor adapter has
  no install path.
- BlueStacks Air (`bluestacks-air:<instance>`, macOS Apple Silicon): discovery through
  `bluestacks.conf`, `start`/`stop`/`restart`, screenshots, input, apps, APK install, and
  `CONFIGURE` (renderer, geometry, fps, CPU/RAM, vsync/high-FPS) through the conf store.
- Display `CONFIGURE`: set width, height, and DPI through the public surface, with the
  previous `DisplayState` returned for an explicit restore.
- Capability probes (`features`, `has_feature`) so unsupported operations fail with
  `FeatureNotSupportedError`, not a vendor surprise.
- Typed errors, one deadline per call, and a required `close()` exit path. There is no
  `atexit` hook; keep `close()` (or the context manager) on every exit.

## Design

The behavioral contract is [CONTRACT.md](CONTRACT.md). The short version: selection is
static and refusal-before-import; adapters stay behind one port; the sync facade owns a
portal thread and must be closed.

## License

MIT. See [LICENSE](LICENSE).

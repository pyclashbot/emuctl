# emuctl — design brief

*A higher-level, vendor-agnostic Android emulator controller library for Python: what pymemuc was to MEmu, generalized to emulators and devices — easy to use above all, built on the shared learnings from reviewing `pymemuc` and `py-clash-bot` (see `NOTES.md`).*

Status: **vision locked 2026-10-06**, not committed. Research phase may revise the deferred items below; the locked items are the direction. **v0.2 (2026-10-06): synthesized after research/artifacts 01–07 — see §8 deltas and §9 verify-before-code; §2 original decisions retained for the record.**

## 1. Identity

- **Name:** `emuctl`
- **What it is:** a library that consolidates emulator control — interaction primitives plus engine-specific lifecycle operations (`create`, `start`, `stop`, `copy`/clone, `configure`) where the engine provides them, with deliberate, discoverable handling of the gaps between engines.
- **What it is not:** a screen-scraping/botting framework, a game API, or a drop-in shim for pymemuc. It treats the emulator as a black box driven over ADB and vendor CLIs.
- **First consumer:** `py-clash-bot` (its `emulators/` layer is the port/adapter shape this generalizes). Integration may require edits to py-clash-bot; drop-in compatibility is not required.

## 2. Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Programming model | **Async core + sync wrapper.** Async is native for the ADB/control layer; a thin sync convenience surface serves py-clash-bot and casual users. The sync bridge is the highest-risk layer and a prime test target. |
| 2 | v0 backend matrix | **ADB devices (core), MEmu, BlueStacks 5.** Google Play Games and AVD/Android Studio are named fast-follows. Backends port from battle-tested py-clash-bot adapters. |
| 3 | Missing-feature model | **One source of truth:** each adapter *declares* a capability set. Introspection (`has_feature` / `features`) reads it, and call-time failure raises a single derived `FeatureNotSupportedError`. Never hand-written raises per method, never silent no-ops. |
| 4 | Data contract | **`screenshot() -> bytes`** (PNG) in a stdlib-only core. Decode/format helpers (numpy BGR, etc.) live in optional extras, added when consumers need them. Core gains no required dependency. |
| 5 | CLI | **Optional sibling subpackage, `emuctl[cli]`.** Doubles as the machine/agent interface: stable JSON output, scriptable exit codes, commands like list/probe/screenshot/click/app-start. The library itself never ships a required console script. |
| 6 | Release pattern | **Conventional-commit auto-release:** CI parses commits on main → semver bump, generated changelog, GitHub Release, tag, and publish via **PyPI Trusted Publishing (OIDC)** — no API tokens. Git workflow stays "commit normally"; tags are cut by CI. Tool candidates: python-semantic-release or release-please. |
| 7 | Python floor | **3.12+** (matches py-clash-bot exactly; no compat shims). |

## 3. Distribution shape

- **PyPI package:** `emuctl`.
- **Per-backend extras** mirroring the adapter layout, following the standard `pip` optional-dependencies pattern (`uvicorn[standard]`, `celery[msgpack]`):
  - Core install: always available, ADB-device support, and nothing vendor-specific.
  - `emuctl[memu]`, `emuctl[bluestacks]`, later `emuctl[google-play]`, `emuctl[avd]` — each backend's overhead (path discovery deps, platform bits) installs only when that backend is wanted.
  - `emuctl[all]` — every supported backend, one knob.
  - `emuctl[cli]` — the agent/inspection tool.
  - Non-core runtime helpers if introduced later follow the same shape (e.g. `emuctl[image]`).
- **Multi-platform:** supported wherever each emulator applies — MEmu = Windows; BlueStacks = Windows + macOS; ADB core = macOS, Linux, Windows. Platform support is declared per adapter and surfaced through the same capability mechanism (a backend unsupported on the current platform is not chosen, not a runtime surprise).
- **Principle:** *"I get only what I want where I want."* Default install stays small; nothing loads for backends or features not selected.

## 4. Maintainability & extensibility commitments

- **Adapter enforcement:** a strict port (protocol/ABC) plus a shared ADB primitive layer; a new emulator is implemented by filling discovery/config/launch and declaring features — interaction primitives come free. Empty/default-set capabilities make missing engine features deliberate, not accidental.
- **Utilities for development:** offline fake/stub emulator for contract tests (no emulator needed), a live-emulator harness, and the `emuctl[cli]` as the manual + agent probe.
- **Testing:** automated where possible, not maximal. Contract tests run offline against the stub; live hardware tests are opt-in/marked, run in CI only if a broker exists.
- **Agent support:** repo guides (`AGENTS.md`) written so agent work lands in the right layer; the CLI doubles as the agent's hands.
- **Tooling:** uv, ty, ruff, pre-commit, CI (GitHub Actions), OIDC publishing — the Astral-modern stack throughout.

## 5. Non-goals (v0)

- Drop-in compatibility with pymemuc or py-clash-bot's current API.
- Game-level logic, image recognition, or state machines (that stays in consumers like py-clash-bot).
- Provisioning-as-config (IaC) beyond noting it as research.
- Coordinate scaling of screen positions or automation payloads beyond emulator-level knobs.

## 6. Deferred to research (phase 3, may revise direction above)

1. **Capability/provisioning abstraction prior art** — IaC-style models (Pulumi, SST, Alchemy): how they declare provider capabilities and degradation, and whether an emuctl analogue supports declarative provisioning of Android instances.
2. Async/sync bridge design (anyio? asyncio native? sync wrapper semantics and cancellation).
3. Extras naming and packaging details against real PyPI prior art.
4. Auto-release tooling choice: python-semantic-release vs release-please.
5. Offline test-stub fidelity (protocol-level fake ADB vs behavioral fake controller).

## 7. Inherited principles (from the `NOTES.md` review)

One port, adapters per vendor whose primitives are implemented once over raw ADB · zero-dep core, extras carry weight · fail-fast typed errors with bounded internal waits, never unbounded loops or human-in-the-loop prompts · `restart()` as the explicit boot primitive, construction is cheap discovery only · never parse vendor stdout for control flow — verify state · process kills scoped to instances (serial/port/PID), never global · bundled resources resolved through the package, never relative CWD · `py.typed` from day one · device/serial validation as a security control · binary screenshot transport (`exec-out screencap -p`), no base64 round-trips.

## 8. Synthesis revisions (v0.2, post-research)

Research artifacts 01–07 (`research/`) upheld every locked decision; this section records what they **concretize** (shape now known, not just direction). Original §2 stays for the record.

### 8.1 API model (decision 1 → refined)
- Async core: **pure asyncio**, no abstraction dependency. Sync wrapper: **anyio blocking portals** (`start_blocking_portal` / `BlockingPortalProvider`), with anyio strictly confined to the bridge layer.
- Bridge rules: never `asyncio.run()`-per-call, never nest_asyncio; async context managers cross via `wrap_async_context_manager`; cancellation documented as "cancel = abandon"; a running event loop detected inside the sync facade raises a designed error (never a deadlock).
- **Subprocess-under-bridge risk resolved at doc level:** Python ≥3.8's `ThreadedChildWatcher` removed the main-thread subprocess limitation (bpo-35635/35621); anyio documents no Proactor/coroutine subprocess veto (`research/08-verification.md`). A one-shot GHA repro remains as cheap insurance (§9.1), plus a regression test in the bridge test surface.

### 8.2 Capability model (decision 3 → concretized)
- `CapabilityFlag` enum → per-adapter-class `frozenset` (declared once) → single shared `_require()` helper that raises the derived `FeatureNotSupportedError(feature=…)` — the declared set is the only place support is written.
- **Nominal ABC** for the port (Protocol runtime checks can't verify signatures; py-clash-bot precedent). Optional **maturity status** per flag (experimental → supported, libgit2-style graduation). Same set surfaced by CLI `probe`; **platform gating folded into the same mechanism**.
- Decision (was open in §6.1): declared-static flags at v0, not version-probed — split recorded as an open design note.

### 8.3 CLI (decision 5 → concretized, with one placement fix)
- **Console script lives in the core distribution** — pip ignores `[extra]` on entry points (pip#9726), so `emuctl` installs with core but fails with an actionable *"install `emuctl[cli]`"* message via lazy click import. CLI *code* stays in-tree behind the extra (no sibling distribution). Framework: **click**.
- Agent contract: JSON → stdout, noise → stderr, documented exit-code map (0–10) with `{code, message, suggestion}` error objects mirroring the library's exceptions; generic verbs + `--backend` selector with capability-derived refusals (no per-backend subcommands).
- Config precedence: flag → `EMUCTL_*` env → project `emuctl.toml`/`[tool.emuctl]` → user XDG → defaults → vendor-binary discovery; typed config models live in the library.

### 8.4 Release pipeline (decision 6 → concretized)
- **python-semantic-release** is the tool: config in `pyproject.toml`, cuts tag + changelog + GitHub Release itself; **static** version in pyproject bumped via `version_toml = ["pyproject.toml:project.version"]` (the documented, PSR-dogfooded form). The PSR release step must also **refresh `uv.lock` inside `build_command`** (`uv lock --upgrade-package "$PACKAGE_NAME" && git add uv.lock`) — a version bump alone leaves the lock stale and breaks the next `uv` run (official PSR *UV Project Setup* guide; `research/08-verification.md`).
- Publishing: CI job with `pypa/gh-action-pypi-publish`, `id-token: write`, named environment; PyPI "pending publisher" setup once; attestations default-on under Trusted Publishing. No API tokens.
- Build backend: **`uv_build`** (stable 2025-07, `uv init` default since 0.12) — pure-Python, right-sized. Commit `uv.lock` (invisible to pip consumers), PEP 735 `[dependency-groups]` for dev/test/lint, `uv publish` only as fallback.
- Repo shape: **single-package repo, no uv workspace**; `uv init --lib` src layout; one root `ruff.toml`/`ty.toml`/`.pre-commit-config.yaml`; **CI installs the built wheel in a scratch venv** for the test job so tests can't silently depend on sibling extras.

### 8.5 Extras & distribution (decision 3 of distribution → concretized)
- Hyphen-normalized extras per PEP 685 (`emuctl[google-play]`); `all` extra is an **explicit dependency list**, never self-referential extras (CI sync check against backing extras).
- Optional backends live in-tree (`emuctl.backends.memu`, …), imported lazily via **PEP 562 `__getattr__` + deferred `importlib.import_module` at adapter selection** — never at `import emuctl`.
- Missing-extra failure: typed error raised at selection time naming the exact `pip install 'emuctl[memu]'`.
- Windows gating: **env markers on the extra's dependencies** (install-time answer) *plus* runtime adapter selection honoring `supported_platforms` (runtime answer).

### 8.6 Offline testing (§4 commitment → concretized)
- **Three-layer hybrid:** (1) behavioral fake of the port as the *primary* offline surface, parametrized into the same **contract/conformance suite** that runs against real backends; (2) minimal **socket-level fake ADB server** (AOSP `fake_adbd` precedent) only for transport-level tests; (3) **recorded real-hardware byte fixtures backing the fake** — fidelity comes from fixtures, not fake logic.
- Dedicated **bridge test surface**: portal reuse, exception propagation, cancellation, mid-op shutdown (per §8.1).
- Live tier: generalize py-clash-bot's ordered-`SUITE` marker-gated shape; never required for offline CI.

### 8.7 IaC / declarative provisioning (research queue item → resolution)
Not a v0 concern — all surveyed IaC tools need persisted state engines that would duplicate emulator-owned state. **Cheap transfers adopted instead:** provider-schema-as-capability-surface (into §8.2), declarative argument objects for lifecycle ops, honest per-vendor not-implemented tables (auto-generated from `probe`), escape-hatch transform hooks.

## 9. Verify-before-code (hardware/engineering gates, not research)

1. **One-shot Windows repro (downgraded from blocking — insurance only):** the main-thread subprocess limitation behind the bridge concern was fixed in 3.8 (`ThreadedChildWatcher`); run the `workflow_dispatch` GHA repro (windows 2022/2025 × 3.12/3.13/3.13t) once to confirm on real Windows, them fold a regression check into the §8.6 bridge test surface. Full plan discussed earlier this session; script not yet drafted.
2. Wheel-in-scratch-venv CI job proves the scratch-venv story before first release.
3. Screencap byte-fixture capture protocol — designed when the first live harness lands (these fixtures don't exist anywhere yet).

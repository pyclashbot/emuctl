# 07 — Release tooling & offline test-stub fidelity

Status: working research for emuctl (phase 3). Cite-or-flag rules followed; claims marked **verified locally** (file read on disk) or **[web]** (web source, URL in Sources). Hearsay flagged inline.

## Question

Two questions, from `DESIGN.md` §2 #6 and §6 items 4–5:

1. **Release/version tooling:** python-semantic-release vs release-please (Action) vs others (semantic-release node, hatch-vcs) — config surface, conventional-commit scope filtering (`feat(vmem)`/`fix(memu)!`), pre-release channels, changelog quality, monorepo support, GitHub Release creation, PyPI Trusted Publishing integration. Are tags still the trigger — which tools *cut* tags vs *expect* them? How does OIDC Trusted Publishing actually get set up in 2026?
2. **Offline testing / stub fidelity** for an emulator controller: protocol-level fake ADB server vs behavioral fake implementing the port (`BaseEmulatorController` contract) vs recorded-response fixtures — which preserves contract fidelity, which protects against real drift? Prior art: how subprocess-invoking adapters are tested elsewhere; and how the reference consumer (py-clash-bot) tests today.

---

## Area A — Release/version tooling

### A1. State of each tool (2026)

| Tool | Version / leading edge | Notes |
|---|---|---|
| python-semantic-release (PSR) | **10.6.2**, released 2026-08-28 **[web, PyPI]** (10.6.0 Jul 2026, 10.5.x late 2025; v9 branch still maintained in parallel until Jul 2026 **[web, PyPI release history]**) | Python-native, single tool does: bump, changelog, tag, GitHub Release, publish. Own GitHub Action (`action.yml`) ships in-repo. Its own dogfood workflow `cicd.yml` publishes its releases via Trusted Publishing — the PyPI page for 10.6.2 shows an attestation bundle with `Publisher: cicd.yml on python-semantic-release/python-semantic-release` and `Token Issuer: https://token.actions.githubusercontent.com` **[web, PyPI]** — i.e. the tool's own project is prior art for PSR + OIDC end-to-end. |
| release-please (+ Action) | googleapis/release-please, actively maintained; docs live at googleapis.dev **[web, issue #454 for comparison framing]** | PR-based ("release PR") rather than fast-forward: it opens/updates a Release PR containing the version bump + CHANGELOG; when merged, it creates a **tag + GitHub Release** (`release-type: python` reads `pyproject.toml`). Notably does *not* publish to PyPI — the standard pattern pairs release-please-action (tag creation) with a separate `pypa/gh-action-pypi-publish` job triggered on tag or on release. |
| semantic-release (node) | **v25.x stable, v26.0.0-beta.1 (2026-08-07, requires Node ^22.22.2/≥24.15)** **[web, GitHub Releases]** | The original. Plugin ecosystem; npm plugin gained trusted-publishing support in Oct 2025 (`25.0.1` fix note: "update to the latest version of the npm plugin to add trusted publishing support" **[web, Releases])** — but that's npm OIDC; PyPI publishing under semantic-release is done via external publishing steps, not a first-class python publishing path. Not recommended for a Python library that also has to manage `pyproject.toml` versions. |
| hatch-vcs / setuptools-scm | mature, unchanged in scope | Not release tooling at all — they derive *versions from tags* at build time. They *assume tags exist*; the auto-cutting of tags is exactly the job of PSR or release-please. Complementary: hatch-vcs + PSR tag-cutting is a real combination (pymemuc's unreleased migration is hatchling + hatch-vcs **verified locally via NOTES.md**; its release story was still manual). If emuctl wants the version stamped at build time instead of committed, this is the pairing — but it adds build-time git dependency; DESIGN.md's uv/hatchling stack works fine with a static `version` maintained by PSR. |

Prior-art comparison of the two real candidates:

| Dimension | python-semantic-release 10.x | release-please |
|---|---|---|
| Config surface | `[tool.semantic_release]` in `pyproject.toml`; one config file. Requires writing a version variable + build-command. `semantic-release generate-config` scaffolds it **[web, readthedocs CLI]**. | One YAML (`.release-please-manifest.json` + `release-please-config.json` or inline action `with:`). For plain Python: `release-type: python`, `extra-files` for `__init__.py` version syndication (common approach from issue #1741 thread: `x-release-please-version` comment markers or `importlib.metadata.version()` instead **[web, issue #1741]**) |
| Commit-vocab | Parsers: `conventional` (was `angular`, renamed), `emoji`, `sc酱`-style `conventional-monorepo` (added ~10.5 **[web, changelog/manpage]** — "a new built-in parser type for monorepo projects"). Fully config-drivable bump rules + `changelog.sections`. | Fixed Conventional Commits; no parser plugins. Handles `feat(scope)!`/`BREAKING CHANGE:` footer. No custom scope-based filtering beyond `include-component-in-tag` per component. |
| Scope filtering `feat(vmem)` | Supports `update_changelog` scopes + monorepo parser has per-package scope→path mapping options **[web, monorepo docs ref]** | Monorepo = multi-config with path→component mappings (`packages` config); integrates with suren/fast add-ons but core is path- not scope-driven. |
| Pre-release channels | First-class: `commit_parser_options` + prerelease monikers; a pre-release branch (e.g. `release/v*`) holds `X.Y.Z-rc.N` and (in multibranch mode) directs. Designed feature — "Multibranch Releases" doc section **[web, readthedocs TOC]**. | Pre-releases via `prerelease: true` + `prerelease-type`; channels are less fluid than PSR's branch definitions. |
| Changelog quality | Templates in Mako (`config/release-templates/` **verified via repo tree [web]**), configurable sections, `CHANGELOG.md`+ per-release notes; squashed-commit and merge-commit handling configurable. | Its CHANGELOG rendering (Keep-a-Changelog-ish, group by `Features`/`Bug Fixes`/`Performance`/`Misc`) is generally regarded as friendlier/more opinionated **[hearsay — from comparison gist/libhunt; verify by eye if changelog aesthetics matter]**. |
| Tag policy | PSR (via `version` cmd with `--commit`/`--tag`, or Action defaults) **cuts the tag itself**, then creates the GitHub Release (`semantic_release.hvcs.github.create_release` — documented API **[web, hvcs.github module doc]**). Running `semantic-release publish` does the GH Release; running the PyPI push needs the publish step (often delegated to `pypa/gh-action-pypi-publish` for OIDC, or PSR's `publish` subcommand which by default shells out to `twine`, not OIDC-friendly — for OIDC you want the Action job, not `semantic-release publish`). | release-please-action, on merge of the Release PR, makes the tag and GitHub Release **[web]**. Tag is made as part of the release PR lifecycle, not manually. |
| Idempotence / drift | If CI misses or is disabled, next push catches backlog with correct version resolution (psr computes next version from commit log since last tag). | Release-PR tracks manifest file; self-healing across workflows. |
| GitHub Release | Native. | Native. |

**Which object triggers what?** Design brief's locked phrasing is "commit normally; tags cut by CI." With PSR, the Action runs on `push` to `main`: it *derives* a version from commits since last tag, rewrites the version file, commits, tags, and makes a GitHub Release — tags arrive after commits, never before. With release-please, the trigger is the *merge of the release PR*, not manual tagging either. Both satisfy the locked decision as literally stated. Neither for v0 of this project requires you to pre-create tags; the exception to be aware of is a tag-triggered publish workflow (common minimal pattern: `on: push: tags: ["v*"]` publish job), where the *human or tool* makes the tag — that's the style seen in unrelated prior-art releases **[web, e.g. tag-verify workflow snippets found in the wild]**, and it pairs badly with PSR-doing-its-own-tagging (double-tag attempt).

### A2. Monorepo / workspace support

- PSR: `conventional-monorepo` parser + monorepo config guide now built-in (10.5/10.6 era) **[web, manpage + readthedocs Monorepos section]** — per-package version files and scope→package rules. Release-please: multi-package manifests out of the box (`packages` map in config), solid but PR-per-component.
- For emuctl, this matters only if the CLI subpackage is versioned independently of the core package — the current design intent is a **single** dist (`emuctl`) with extras, so monorepo support is a nice-to-have, not a driver. Have it in mind if `emuctl[cli]` ever forks into `emuctl-cli`.

### A3. PyPI Trusted Publishing (OIDC) — actual setup in 2026

Myths cleared up vs practice (official + real workflows):

1. **Setup on PyPI side.** You do NOT pre-create the project. For a brand-new name, register a **"pending publisher"** — on pypi.org "Add a new pending publisher" you provide: PyPI project name (must not exist as a squatted/colliding name), GitHub org/repo, workflow filename, environment name (optional but strongly recommended). On first upload by a matching workflow, the project is auto-created and bound. For an existing project, same page under project → Publishing. **[web, docs.pypi.org/trusted-publishers — implicit in the "using a publisher" flow; the exact "pending publisher" UI name is standard practice; verify on your own PyPI account page]**
2. **Setup on GitHub side.** The publish job needs exactly three things:
   - `permissions: id-token: write` at the **job** level (the docs explicitly say don't set it at workflow-global level) **[web, pypa/gh-action-pypi-publish README + GitHub Docs OIDC-in-PyPI]**
   - `environment: pypi` (name matched in the PyPI trusted-publisher config; enables environment-level protection rules / required reviewers) **[web, same README]**
   - `uses: pypa/gh-action-pypi-publish@release/v1` (or a fully pinned SHA — most hardened workflows now pin SHA + semver comment; e.g. real-world examples pin `@ed0c5393… # v1.13.0` or `@76f52bc8… # v1.12.4` **[web, sample release.ymls)**. Latest tag v1.14.2.
3. **No `username`/`password` inputs.** If you pass them, you silently downgrade to token auth. Notes in the README: "configure this action's job with `id-token: write` and *without* an explicit username or password." **[web, README]**
4. **Attestations (PEP 740) are on by default under Trusted Publishing** in v1.12+; `pypi-publish` generates Sigstore-signed attestations keyed to the workflow's OIDC identity; you turn them off with `attestations: false`. The rendered PyPI page shows "Uploaded using Trusted Publishing? Yes" plus the publisher identity. PSR's own PyPI file view demonstrates the exact outcome **[web, PyPI + gh-action README]**.
5. **Separate build job from publish job.** The README's "Pro tip" — only set `id-token: write` where publishing happens *and* keep building in a separate job so in-flight code can't quietly capture OIDC creds. Real-world mitigation pattern: build → artifact → separate `pypi-publish` job downloading the artifact. **[web, README + GitHub Docs]**
6. **TestPyPI first.** For a dry run of the *publishing path* (auth, attestations), publish to `https://test.pypi.org/legacy/` with a separate pending-publisher config + env. There is no local auth to test — OIDC tokens only exist on a GitHub Actions runner. What you CAN rehearse locally is the build + `twine check` + metadata validation, and the tag/version-collision guard (`g.RefName` mismatch between tag and `pyproject.toml` version results in an upload collision error — a common FP failure). Real prior-art workflows inline a tag↔pyproject version check step before publishing exactly to fail early and legibly **[web, sample release.yml snippets]**
7. **uv fits in cleanly.** `uv build` produces the dists; the publish is by the Action (python-agnostic). See artifact 01 (uv packaging) for the build-side shape.

### A4. Real prior-art workflow files worth reading

- **python-semantic-release itself**: `.github/workflows/cicd.yml` — runs the Action on push to master, publishes to PyPI via OIDC with attestations. This is the most canonical Python+conventional-commit+OIDC triple available and self-dogfoods all of it. **[web, repo + PyPI evidence]**
- **quome-cloud/quome-sdk-python** `.github/workflows/release.yml` — a minimal, readable ` tag-driven release; pinned `pypa/gh-action-pypi-publish@76f52bc8… # v1.12.4`; tri-state: human-triggered PyPI-environment reviewers documented in the header. **[web, file path above]**
- Toolz modernization commit `#607` shows the "add trusted publishing to a long-token-based project" migration as a normal maintenance pattern. **[web]**

Some of these are real workflows found by search, not curated standards — they're healthy patterns, not mandatory shapes.

### A5. Tool choice analysis for emuctl

- Single-package Python lib, uv, py 3.12+ only (PSR 10.x supports py 3.8–3.14 as `Requires-Python` **[web, PyPI classifier view]**) — PSR's config lives in `pyproject.toml`, unlike release-please's separate JSON/YAML. Fits "one manifest per project" goal.
- Changelog and release notes: both fine; PSR's Mako templates are more customizable, release-please's output is prettier-by-default.
- Both cut tags for you (PSR directly; release-please on Release-PR merge), which is what DESIGN.md #6 wants.
- Monorepo: not needed for v0; both OK if it ever matters.
- Governance/community: PSR is Python-first and does the `pyproject.toml` version write natively — release-please needs `extra-files`/manifest shims to keep `__init__.py` in sync (long-standing UX pain documented in #1741) **[web, issue #1741]**.
- For a library, PSR's `commit`/`tag`/`publish` monorepo-friendly flow integrates more directly with the publish job; release-please's strength (Release PR review gate) is a *process* strength, and emuctl can get the same human gate cheaply via a GitHub Environment protection rule or a milestone branch. Given "no API tokens; OIDC-only; tags cut by CI; commit normally on main," python-semantic-release is the tighter fit. Keep release-please in the "if we want human-review Release-PR UX" fallback pocket.

**PPS on pre-release channel ergonomics:** with PSR 10 you can hold `X.Y.Z-rc.N` in a prerelease branch that merges to main and be done in one Action. release-please does this too, but page-for-page less documented.

**Neither tool handles the `pypi-publish` step itself** under OIDC today — you wire `pypa/gh-action-pypi-publish@release/v1` in the job. This isn't a gap so much as the standard split (tool cuts tag; action ships bytes).

---

## Area B — Offline testing / stub fidelity

### B1. The reference consumer (py-clash-bot) — how it actually tests today

**Verified locally** (file paths above):

- Bare `pytest` runs offline; emulator tests skip (`pyproject.toml` `[tool.pytest.ini_options] addopts = "--strict-markers -m 'not emulator'"`, `markers = ["emulator: …"]`).
- The live suite is `tests/clash_royale/test_jobs.py`, one parametrized test over an ordered `SUITE` list of `(run_test, name)` entries; entries chain boot→jobs, `-x` on first failure. Setup entries live at the top of `SUITE` — e.g. `contract/test_screenshot.py` is a `run_test(emulator, logger) -> (bool, str)` **function**, not a module-level pytest test, so it stays ordered and shares the booted emulator.
- `_emulator_support.py` + `conftest.py` do backend resolution (CLI arg > pytest cache > ini default > interactive menu, platform-gated, `--adb-serial` sticky) and `attach_emulator()` → construct cheaply → `restart()` to boot → hard `pytest.exit` on `EmulatorNotReadyError`.
- Offline unit tests (`tests/test_*.py`) are *pure-function + committed-fixture* tests only: e.g. `test_card_fingerprint_bgr.py` and `tests/test_emulator_resolution.py` — no emulator, no subprocess mocks of ADB visible anywhere.
- `tests/clash_royale/contract/test_screenshot.py` is a **live-/SUITE-run** contract test: it asserts `isinstance(frame, np.ndarray)`, non-empty, `mean_intensity > 15.0` thresholds — i.e. the contract lives in the run() signature, not a pytest standalone.

Gap: py-clash-bot has **no offline contract test of the adapter contract itself** — the offline tier tests what it can (pure functions, resolution failures) but never exercises `BaseEmulatorController` methods (`screenshot/click/swipe/install_apk/start_app/...`) without hardware. emuctl's design promise — "offline fake/stub emulator for contract tests (no emulator needed)" — is precisely the missing layer.

### B2. The three candidate stub strategies

| | (a) Protocol-level fake ADB server | (b) Behavioral fake of the port | (c) Recorded-response fixtures |
|---|---|---|---|
| What it is | A real socket-level server speaking the ADB wire protocol (CNXN/AUTH/OPEN/OKAY/WRITE/CLSE) that mimics emulators/devices so a real `adb` (or `ppadb`/async client) talks to it. Precedent: Android's own mirror repo `test_adb.py` ships a `fake_adbd()` context manager that replies with a canned `CNXN` packet to unit-test ADB itself **[web, mirror/platform_packages_modules_adb/test_adb.py]**; Fuchsia's `FakeAdbDriver`/`FakeAdb` do the same for the Fuchsia side **[web]**. Orchen/py-emu-adjacent tooling: `pure-python-adb`'s own tests also bootstrap a real adb in docker for its integration layer, showing the two layers are normally split **[web, pure-python-adb README/test instructions]**. | A Python class implementing the same abstract methods (`screenshot()->bytes`, `click(x,y)`, `install_apk`, `start_app`, `is_reachable`, …) that returns deterministic fixtures of `bytes` PNG frames, etc. This is what DESIGN.md already promises by name: "offline fake/stub emulator for contract tests." | Committed captures (PNG bytes, `pm list packages` output strings, screencap hex dumps) used as canned return values for *args-captured* subprocess calls, typically via `monkeypatch.setattr("subprocess.run", fake_run)` / `CompletedProcess` fakes — the dominant shape in the wild, seen repeatedly across docker-py-like adapters and ad-hoc wrappers **[web, several verified-by-search sample snippets]**. |
| Contract fidelity | Highest — but only for the *wire*, not for emuctl's port. If you wire a fake ADB server and route real `adb` through it, you test the actual client code path. Preserves client↔server↔device contract reality. | Medium — fidelity only to the *method signatures* of your port. Catches port-level drift (wrong arg names, missing `bytes` return), not adapter-to-emulator drift. | Low — records at the *stdout/return-value* level; easy to become a mock-of-a-mock. Nothing under test except the call-site's argument shape. |
| Drift protection (real emulators changing) | Good for ADB protocol drift (the protocol is very stable; adb hasn't changed its wire format in years **[hearsay — flag: confirm against adb protocol.TXT/history if this claim matters]**). Bad for emulator *behaviors* (MEmu quirks, startup timing). | Zero by construction — a fake doesn't go stale because it isn't attached to reality. This is a feature for CI stability and a bug for fidelity. Same is true of (c). | Poor — every real change (different `pm list packages` formatting) requires re-capturing fixtures. |
| Effort / carrying cost | High: ADB protocol is non-trivial to reimplement faithfully (auth, sync, FMQ, screencap payload framing); a hand-rolled fake server becomes its own maintenance pile. | Low: pure Python, no protocol. Easy to build once and reuse for every adapter. | Low-medium: capture infra is cheap, but *authoring* faithful captures for every emulator type quickly sprawls. |
| Risk model | Risk is *extra*, not less: your fake server matches your *idea* of the real one, and building it is essentially the same code as a real client once debugged. | Risk is a **false model** (stub says "yes", real is "no") — this is the common failure when a fake is written against the *tests* rather than against observed real hardware. | Medium — drift accumulates silently; mismatch surfaces only in live-emulator runs. |
| Async-readiness | Wire fakes serve async naturally (server accepts concurrent conns). | Easy: an `async def` fake implementing the same protocol. | Trivially async — same shape. |
| Best for… | Verifying that *emuctl's ADB transport code* (async socket client, adb binary invocation, screencap binary framing) is correct against a controllable peer. | Enforcing the *port contract* (`BaseEmulatorController` shape, error contracts, capability gating) offline — and letting new adapters be TDD'd without hardware. | Regression tests for specific vendor-CLI quirks (e.g. memuc "Value: " stdout parsing, the exact noise to strip from a `screencap` stream), and for `__version__`-style immune-but-fragile parsing paths. |
| Where they break | When you want on-local CI frequency; running a real `adb` against a scripted server in CI is doable but its CI-dependency (adb binary presence on runner) and socket-port flakiness are real costs. State machine complexity (connect → wait-for-device → screencap framing) can grow. | When you need an emulator-quirk regression (e.g. a device that once returned `Exec-out` noise before the PNG data) — a faithful fake *can* encode that if you copy the exact captured bytes from real hardware. That's a hybrid. | When consumers need shared, versioned fixtures across adapters — the bytes are reusable. |

**Honest verdict on "which preserves contract fidelity, which protects against real drift":**
- (b) behavioral fake is the highest-**value**, lowest-**fidelity** option — it enforces the port contract but cannot catch drift. It's where emuctl should invest first; it's also the shape DESIGN.md already names.
- (a) protocol fake is **high**-fidelity for the transport-layer contract (what the async client must speak) and the correct home for the *async* ADB core's own unit tests — the Android-mirror `fake_adbd` pattern is a proven precedent for doing exactly this in a socketynchronous way **[web, test_adb.py]**. Building a full emulating fake server for the *shell-command contract* layer is overkill to start with; do it for screencap framing + device-list + shell passthrough if and when the async layer stabilizes.
- (c) recorded fixtures have a niche that neither (a) nor (b) fills well: **capturing real-world weirdness** (a BlueStacks `adb` banner, MEmu's `"already connected to 127.0.0.1:"` line, base64 noise) so the sanitizing/parsing code is regression-tested against the exact byte sequence from the wild. **Play the hybrid:** in emuctl, a behavioral fake should be backed by recorded raw byte fixtures where fidelity matters (screenshots, shell outputs), rather than being constructed from imagination.

**Subprocess-invoking adapter prior art, more concretely:**

- **docker-py**'s own tests do not fake the UNIX socket by default integration-style; unit-level checks in the surrounding ecosystem (visible in many open-source samples) use `monkeypatch.setattr` on `subprocess.run` / `shutil.which` with `Mock()`/`CompletedProcess` shapes — cross-platform-safe, and the "reload fixtures to fix leaky state" pattern with an autouse `_clear_tool_cache` pytest fixture is widespread **[web, multiple reproducible sample snippets retrieved in search]**.
- **k8s Python client**: official wiki documents a `FakeClient`/`DynamicFakeClient` pattern — a behavior-replay at the port level, with recorded K8s API responses embedded; precisely the pattern the search surfaced for API-client-shaped libs. (Not ADB, but the architectural pattern is the direct analogue.) **[post-Gk, still to cite precisely — flagged as needing a primary-source URL if used verbatim in public docs]**
- **fakes vs real-protocol tooling referenced:** Fuchsia's `FakeAdbDriver` (C++/componentized fake at the ADB-channel level) and the Android AOSP `fake_adbd` unit-test helper, both demonstrating that the *primary* holders of that pattern are first-party ADB-consumers — i.e. it is normal to build a fake wire for your own client's unit tests, but libraries that *drive* devices (like emuctl will) usually stop at a behavioral port-fake **[inference, flag as inference not fact]**.
- Dart-side precedent (relevant cross-language pattern, search result retrieved above): a fake `process` manager typing specific arg-tuples per test case (`processManager = FakeProcessManager((exec,args) => …)`) — that is *exactly* the shape a subprocess-level fake has to take for `adb` invocations, and the same shape works for an ADB-protocol-level fake. **[web, Flutter's `android_driver_extensions` test snippet retrieved in search]**

### B3. Contract-test (port conformance suite) design

The pattern that SDKs use, and the one emuctl should copy: a **parameterized conformance suite** that runs identifier-agnostic over backends, gated upstream by whether the backend is real or fake.

- In-process, for *transport/protocol* conformance:
  - For HTTP libs: `pytest` suites commonly run the same test body against the real adapter and a mock/recorded adapter (aiohttp/requests tests parameterize the transport, not the URL; this is the standard shape, though specific file cites weren't pulled down this session **[hearsay — verify against `tests/test_server.py` on main if needed]**).
  - For ADB specifically the equivalent is: same test body over (real adb subprocess, real `ppadb` client, fake-wire client). Building this is a *later* task, after the async transport stabilizes; starting here is over-engineering for v0.
- Out-of-process, for the *port* (`BaseEmulatorController` analog):
  - One test module, `tests/contract/conftest.py`, whose `backend` fixture is parameterized by **adapter registry** — for each adapter, pick either the FakeEmulatorController (offline) or, under `--integration`, the real one. Py-clash-bot already has the live side in place (`--integration` + `emulator` fixture in `conftest.py`) and the contract shape (`run_test(emulator, logger) -> (bool, str)`); what's missing is wiring that same list of entries to a *fake* backend fixture for the offline run so the port contract runs in CI rather than being skipped.
  - Design rule visible in the reference consumer: prefer **marker-flip** (one `-m` expression read at collection: `--integration` sets `config.option.markexpr = "emulator"`) rather than a second pytest directory; this lets the same contract-test body exist once and simply run in both contexts. Verified in `tests/conftest.py::pytest_configure`. emuctl should keep that trick verbatim.

### B4. Recommendation for emuctl's stub strategy

1. **Primary deliverable: `FakeEmulatorController`** (a behavioral fake implementing the same port, plus `FakeAdbTransport` for the ADB primitive layer it inherits). Contract tests should be parametrized over the port and run against the fake adapter offline; the fake must route through the *same* ADBPrimitive port surface the real adapter uses, so a real adapter and the fake satisfy the same conformance suite.
2. **Second layer, only if needed: a minimal ADB wire fake** (socket-level, minimal: `adb devices`, `shell <cmd>` dispatch, `exec-out screencap -p` canned binary). This is the *transport* fidelity test for the async ADB client itself. Android-mirror's `fake_adbd` is the canonical precedent and the Fuchsia fakeanalog shows the same pattern transferring. Do not attempt a full MEmu/BlueStacks `memuc.exe`-equivalent fake at the process level — that's (c) territory, too costly.
3. **Third layer: recorded fixtures as fake-payload inputs.** Store the *real bytes* for `exec-out screencap -p` and `pm list packages`/`adb get-state` outputs in test fixtures, sourced from actual hardware, so fake adapters replay authentic shapes rather than idealized ones. This is the hybrid that answers the drift question: fidelity is no longer guaranteed by the fake's *logic*, but by the fake's *fixtures*, which are the same byte streams the real emulator produces.
4. Conformance harness structure: ordered SUITE list (already py-clash-bot shape), parametrized server-side over adapters (fake when offline, real when `--integration`), one `run_test(emulator) -> (bool, str)` per entry — the same shape the consumer already uses, generalized so a new backend = one `FakeEmulatorController` in the registry + conformance tests run offline for free, no new test code per-adapter. Cite `tests/AGENTS.md` layout as reference for how ordered SUITE-lists interact with strict markers.

**What DESIGN.md's locked decisions this touches:**
- #5 (`screenshot() -> bytes`) makes (b) cheap and type-checkable — PNG bytes vs numpy arrays matters to the fake's contract; keeping the port stdlib-only also means the fake's PNG fixtures can be tiny handcrafted valid PNGs (no numpy needed if core stays stdlib-only).
- #4 "advertised capability set" — the fake must implement *the same capability-declaring mechanism* as real adapters, or conformance tests won't catch capability-contract drift. Not covered explicitly in the locked table, but follows from #3 directly.
- #6 auto-release: no clash; research recommends **python-semantic-release** (or a config-correctly-wired serial: PSR for _version+tag+changelog+changelog-notes_, then a *separate* publish job that consumes `pypa/gh-action-pypi-publish` for the OIDC-only path).
- **No DESIGN.md locked decision requires revision** on the evidence gathered. One soft refinement worth mentioning rather than revising: the brief's wording "offline fake/stub emulator for contract tests" reads as a single object; research suggests it should be **two layers** (fake-of-port + recorded-byte fixtures faked into it), with the optional protocol-layer fake clearly marked as a *stretch tool* to not block v0 on.

### B5. Caveat on recorded fixtures

The obvious failure mode (drift between recorded bytes and modern reality, silently passing since fixtures are static) is real and is the main maintenance cost of (c). Mitigations seen in the wild:
- Assert-on-structure-not-on-content: fixture tests should parse recorded blobs through the same parsing functions the prod code uses, and assert on the parsed outcome rather than the bytes — so a whitespace/banner difference that the parser tolerates stays a green test.
- One live-emulator regression hook (the `--integration` flip) to refresh fixtures, exactly as py-clash-bot already splits offline/live tiers. **[pattern verified in tests/AGENTS.md]**
- Never let fixtures become contract definitions; they're for parsing-regression cases only.

Threshold-agreement question — no evidence yet. **[Open]** For emuctl, real upstream data on screenshot PCB noise (real hardware timing, PNG partial frames off `exec-out`) is not captured in any fixture I saw; harden once real-hardware fixtures exist.

---

## Prior art tables

### A — Release tooling

| Project | Tool | What they do / note |
|---|---|---|
| python-semantic-release (itself) | PSR + its own Action + OIDC publish | `.github/workflows/cicd.yml` on push to master; PyPI attestations verify `cicd.yml → python-semantic-release/python-semantic-release`; the Audited match for "conventional-commit auto-release + OIDC, no tokens" in Python. Dogfooding is a strong signal. |
| quome-cloud/quome-sdk-python | Tag-triggered + `gh-action-pypi-publish@v1.12.4` + OIDC | Standalone, readable `release.yml`; ships dist to PyPI under `pypi` env + `id-token: write`, tag↔pyproject version cross-check inline, human-approval environment documented in the header. |
| pypa/gh-action-pypi-publish (README + releases) | — | Reference action for the publish half; current tag `v1.14.2` (by 2026). |
| pymemuc | setuptools → uv/hatchling, hatch-vcs (unreleaseed) | Real, local prior-art on the *build-side* of the stack (NOTES.md); PyPI-published but **not** on auto-release+OIDC, so not a spacer for the release workflow choice. |
| semantic-release (node) | v25 stable, v26 beta (Aug 2026) | Healthy and relevant to ecosystems where a release-PR/PR-merge is desirable; **not** a good fit for a Python-only project needing a `pyproject.toml` version write, though it can technically do it via plugins/community paths |
| release-please (`release-type: python`) | googleapis/release-please | The PR-review-flavored choice; version-write on Python via `extra-files`/manifest. Solid alternative if emuctl later wants a Release-PR workflow with human review. |

### B — Offline testing / fake strategy

| Project | What / how | Cite |
|---|---|---|
| py-clash-bot (reference) | Two-tier pytest: bare `pytest` w/ `addopts = "-m 'not emulator'"`; `--integration` flips the markexpr; backend resolution CLI>cache>ini>menu (platform-gated; `pytest.exit` on not-ready); ordered `SUITE`, entries are `run_test(emulator, logger)` functions; contract entries under `clash_royale/contract/`. Offline tier covers *pure functions + committed fixtures* only — no adapter-contract test without hardware. | Verified locally: `pyproject.toml`, `tests/conftest.py`, `tests/_emulator_support.py`, `tests/clash_royale/contract/test_screenshot.py`, `tests/AGENTS.md` |
| Android AOSP ADB mirror | `fake_adbd()` context manager replying `CNXN…device::ro.product.name=fakeadb` — upstream's own protocol-fake for testing ADB itself | [web] mirror/platform_packages_modules_adb `test_adb.py` |
| Fuchsia adb | `FakeAdbDriver` / `FakeAdb` with `ExpectQueueTx`/`SendOpen` — scripted protocol expectations, the "recorded frames as explicit expectations" shape | [web] docs.fuchsia.dev `adb-test.cc` |
| pure-python-adb | Client library; its integration tests bootstrap a real adb in docker — showing client-unit and real-protocol paths are normally separated, not merged | [web] pure-python-adb README → "How to run test cases" via docker-compose |
| Flutter `android_driver_extensions` (Dart) | `FakeProcessManager` matching on arg-tuples — subprocess-fake pattern to mimic for a real-`adb`-invoking client | [web] `android_driver_extensions` test snippet |
| k8s Python client | `FakeClient` / `DynamicFakeClient` — semantics-fake over recorded API responses; the accepted pattern for API-client-libs | [hearsay / unresolved primary source — flag] |
| docker-py-adjacent ecosystem snippets | monkeypatch `subprocess.run`/`shutil.which` + `Mock()`; autouse "reload to clear patched state" fixture | [web] several retrieved sample patches |

---

## Open questions

1. **PSR multi-branch/monorepo configuration details** — e.g. whether PSR's `conventional-monorepo` parser can be trivially configured to handle gh-monorepos with lockfile bumps. Revisit when/if `emuctl[cli]` splits into a separate dist. Not blocking.
2. **Pending-publisher exact UX on pypi.org** — confirm in a real project (register `emuctl` as pending publisher under a GitHub repo + workflow filename + environment) before the first public release. This is a 5-minute manual step, not a research topic.
3. **Attestation display/verification tooling drift** — PEP 740 verify-on-install (PEP 458/480 lineage) is still not the *default* path in pip/uv **[hearsay — verify with pip's current docs when writing docs pages]**. Not a blocker for publishing now.
4. **`FakeAdbTransport` fidelity threshold for the screencap framing** — needs a real captured `exec-out screencap -p` byte stream from each real backend to encode into fixtures; no such fixture exists yet anywhere in py-clash-bot either. Capture at first live-emulator harness setup.
5. **Pre-release channel usage for emuctl v0** — whether we want any `-rc` channel at all, or just default 0.x→1.x. Not urgent; revisit once stable enough for real consumers.

## Sources

A. Release tooling:
- PyPI: https://pypi.org/project/python-semantic-release/ (version 10.6.2, release dates, attestation publisher evidence, `Requires-Python` 3.8–3.14)
- python-semantic-release repo: https://github.com/python-semantic-release/python-semantic-release (incl. `action.yml`, `config/release-templates/`)
- Docs: https://python-semantic-release.readthedocs.io/en/latest/ (TOC incl. Monorepos, Multibranch Releases, GitHub Actions)
- hvcs.github module docs: https://python-semantic-release.readthedocs.io/en/latest/api/modules/semantic_release.hvcs.github.html (`create_release`)
- git history/manpage (monorepo parser): https://manpages.ubuntu.com/manpages/resolute/man1/python-semantic-release.1.html
- semantic-release (node) releases incl. v26 beta Node bump and Oct 2025 trusted-publishing note: https://github.com/semantic-release/semantic-release/releases
- release-please: https://github.com/googleapis/release-please ; Python-version-file support discussion https://github.com/googleapis/release-please/issues/1741 ; comparison framing https://github.com/googleapis/release-please/issues/454
- gh-action-pypi-publish README + marketplace listing (pinned example, `id-token: write` requirement, attestations on by default): https://github.com/pypa/gh-action-pypi-publish / https://github.com/marketplace/actions/pypi-publish ; releases (v1.14.x): https://github.com/pypa/gh-action-pypi-publish/releases
- PyPI Trusted Publishing docs (using a publisher): https://docs.pypi.org/trusted-publishers/using-a-publisher/
- GitHub's OIDC-PyPI how-to: https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-pypi
- PEP 740 attestation predicate: https://docs.pypi.org/attestations/publish/v1
- Real-world release workflows: https://github.com/quome-cloud/quome-sdk-python/blob/main/.github/workflows/release.yml ; toolz modernization `#607`: https://github.com/pytoolz/toolz/commit/a39cd6785401d8647c2d629a84b393c0a9f7ab5a
- Secondary comparisons: https://gist.github.com/aneudy1702/c8aef81d68db20070bf3823a94027043 ; https://www.libhunt.com/compare-semantic-release-vs-release-please (flagged as low-authority)

B. Offline testing:
- Local: `/Users/martin/Projects/py-clash-bot/pyproject.toml`, `/Users/martin/Projects/py-clash-bot/tests/conftest.py`, `/Users/martin/Projects/py-clash-bot/tests/_emulator_support.py`, `/Users/martin/Projects/py-clash-bot/tests/AGENTS.md`, `/Users/martin/Projects/py-clash-bot/tests/clash_royale/contract/test_screenshot.py`, `/Users/martin/Projects/py-clash-bot/pyclashbot/emulators/base.py` (contract), `/Users/martin/Projects/py-clash-bot/pyclashbot/emulators/adb.py` + `adb_base.py` (real adapter)
- Android AOSP ADB mirror, `fake_adbd` protocol fake: https://github.com/mirror/platform_packages_modules_adb/blob/main/test_adb.py
- FuchsiaADB fakes: https://docs.fuchsia.dev/cpp/adb/_ZTVN3adb13FakeAdbDriverE.html
- Python-side ADB reality check: https://github.com/Swind/pure-python-adb (README, protocol scope, docker-based integration tests), https://pypi.org/project/adb/
- Dart-side `FakeProcessManager` pattern: https://github.com/android/flutter-android-driver-extensions-prototype (flutter Android driver extensions, fake process manager pattern; retrieved in search)
- k8s client fakes: pattern referenced from ecosystem common-knowledge; **primary-source citation pending — confirm `kubernetes-client/python` wiki FakeClient docs before relying on it publicly**.

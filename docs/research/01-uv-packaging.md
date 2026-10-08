# 01 — uv packaging (build backend, project layout, lockfile, publishing)

*Research artifact, phase 3. Working research, not standards. All web citations fetched 2026-10-06; version facts are current as of that date.*

## 1. Question

How should emuctl's packaging be done on the uv stack (settings, build backend, layout, lockfile policy, publishing) — specifically:

- Does a **uv-native build backend** exist in 2026, what does Astral recommend vs hatchling, and how mature is it?
- `uv init --lib` conventions; `[tool.uv]` settings relevant to libraries.
- `uv.lock` for a **published library**: commit it? Does it affect consumers? `uv publish` in 2026 (Trusted Publishing/OIDC, keyring, PEP 740 attestations).
- Per-extras resolution, dynamic versioning (hatch-vcs vs uv-dynamic-versioning vs uv_build-native).
- What prominent uv-based libraries actually do (real pyprojects).

## 2. Findings

1. **A uv-native build backend exists and is stable.** Astral ships a native PEP 517 backend (`uv_build`), documented as "a great choice for most Python projects… reasonable defaults… very fast" [1]. It was declared **stable on 2025-07-02** (PR "Stabilize the uv build backend" removed the "preview" notices and stopped warning); at that time the `uv init` default was still hatchling with a note that the default "will change to `uv` in a future version" [2]. It publishes as a slim separate PyPI package (`uv_build`; ~32M downloads/month as of 2026-08, dist marked "Production/Stable") [3], while the `uv` binary also embeds a copy and uses it directly during `uv build` when versions are compatible [1]. Pure-Python only; extension modules need another backend (maturin/scikit-build-core/setuptools/hatchling) [1][4].

2. **`uv_build` 0.12 works with older uv releases and the version line is currently 0.12.x.** Current `[build-system]` template in docs: `requires = ["uv_build>=0.12.23,<0.13"]` [1]. Astral recommends pinning an **upper bound** on `uv_build` because the backend follows uv's versioning policy [1]. uv 0.12.0 (2026-07-28) changelog notes zero breaking backend config changes and instructs existing projects to widen `<0.12` → `<0.13` [5]. This bound means periodic CI/chore maintenance (a one-line bump on each minor), which emuctl's conventional-commit flow should expect.

3. **`uv init --lib` is now a fully packaged template with `uv_build`.** Since uv 0.12.0, `uv init` "defines a build system using `uv_build`, places source in `src/<name>/`, and includes a `[project.scripts]` entry" by default [5]. The `--lib` template additionally:
   - implies `--package` (libraries always packaged) [4];
   - uses **src layout**, described as "particularly valuable when developing libraries" [4];
   - includes **`py.typed`** by default [4];
   - emits `[build-system] requires = ["uv_build>=…,<…"]` / `build-backend = "uv_build"` [4];
   - creates the conventional tree: `.python-version`, `README.md`, `pyproject.toml`, `src/<name>/__init__.py`, `tests/` [4][6].
   Alternative backends selectable via `--build-backend` (hatchling, flit-core, pdm-backend, setuptools, maturin, scikit-build-core) [4].

4. **Default structure assumes one root module; configurable otherwise.** `uv_build` expects `src/<package_name>/__init__.py`; `tool.uv.build-backend.module-name` / `module-root` / `namespace = true` handle deviations (namespace mode disables safety checks and is discouraged for new projects) [7].

5. **`[tool.uv]` settings relevant to a library (2026 state):**
   - **`[dependency-groups]` (PEP 735) is the standard for dev deps**; `tool.uv.dev-dependencies` is legacy — docs say its use "is not recommended anymore", contents are merged with `dependency-groups.dev`, and the field "will be deprecated and removed" [8][9]. The `dev` group is synced by default; `uv add --dev` still writes to the legacy field if it exists [8].
   - **`constraint-dependencies`** (and `build-constraint-dependencies`, `override-dependencies`) exist and are read from the workspace root; they are uv-specific resolver knobs (constraints without installing; e.g., bounding transitive deps like `pydantic` who uses them to pin under a major) [10]. Not published in package metadata — they're project-internal only.
   - **`required-version`** pins the uv binary version for the project (useful to keep CI/agents on a compatible uv) [11].
   - **`tool.uv.keyring-provider = "subprocess"`** enables keyring auth for index URLs / uploads (subprocess mode only) [12].
   - **`tool.uv.package`** forces/forbids building the project [13]; `tool.uv.sources` / `cache-keys` for dev-only dependency sources and rebuild triggers [4][13].
   - Extras belong in `[project.optional-dependencies]` (published); dev groups are never published — the split is documented and standard-shaped [8].

6. **`uv.lock` for a published library: commit it; consumers are unaffected.** The uv docs say the lockfile "should be checked into version control" [14], and uv maintainers confirm lockfile contents "do not" affect pip resolution for consumers — it only makes the project's own dev/CI envs reproducible [15]. Astral (konstin) suggests CI variants on top of the lock: `uv sync --resolution lowest` to test your own declared lower bounds, and `uv sync --upgrade` in an allowed-failure job to catch dependency rot [15]. There is no `uv.lock` auto-gitignore for `--lib` (a request to add one has not been acted on) [16]. Community guidance converges on the same answer: lockfile is invisible downstream, so committing it is pure upside for contributors/CI [17]. `uv.lock` is uv-format-specific (not `pylock.toml`); `uv export -o pylock.toml` exists if a standard lock export is needed [14].

7. **`uv publish` supports PyPI Trusted Publishing (OIDC) natively — as of 2026-10 the recommended path.** With a Trusted Publisher configured on PyPI and `id-token: write` in the workflow, no credentials are needed; uv fetches the OIDC token, uses `__token__`, and proactively invalidates the short-lived token after publishing [18]. uv also supports an optional `--check-url` pre-check against the index before uploading [18]. PyPI itself no longer accepts username/password upload — tokens (or trusted publish) only [18]. Full worked example: `trusted-publishing-examples` repo with `.github/workflows/release.yml` [19].

8. **Keyring: supported for publishing, but not needed for OIDC.** `uv publish --keyring-provider=subprocess` reads credentials from the OS keyring via the `keyring` CLI (no `.pypirc` support; scoped per-URL+username tokens via URL query hack) [12][20]. For emuctl's CI-only OIDC flow keyring is irrelevant locally except for maintainer hot-fix publishes [20][18].

9. **Attestations (PEP 740): `uv publish` can *upload* but not *generate* them.** Since uv 0.9.12 (PR #16731, 2025-11), `uv publish` collects matching `*.publish.attestation` files in `dist/` and uploads them automatically; `--no-attestations` exists for non-supporting indexes [21]. Generation inside uv is still open (issue #15618, PR #19489 in progress) [22]. Practical routes in 2026:
   - `pypa/gh-action-pypi-publish` generates attestations by default with Trusted Publishing and stays pure-OIDC (this is what FastAPI org moved to in 2026, replacing a bare `uv publish` step) [23][24];
   - or keep `uv publish` + generate attestations beforehand via Astral's `astral-sh/attest-action` (explicitly "early-dev/unstable") [25][23].
   PyPI attestation signing is Sigstore keyless (Fulcio/Rekor) tied to the Trusted Publisher identity [26]. This composes cleanly with emuctl's locked decision #6 (conventional-commit auto-release + OIDC publishing) — it's a publish-step detail, flagged for the release-tooling artifact (07).

10. **Dynamic versioning — theamaCurrent options and their tension with `uv_build`:**
    - `uv_build` has no `dynamic = ["version"]` support; it requires a static `project.version` (open issues #14946, #14037; blocked workstreams) [27][28]. Existing libraries have specifically stayed on hatchling because of this [27].
    - Route A: `hatchling` + `hatch-vcs` (setuptools-scm-style VCS tag versioning; 6k+ usages) [27].
    - Route B: `hatchling` + `uv-dynamic-versioning` (ninoseki; dunamai-based, poetry-dynamic-versioning-inspired; explicitly "doesn't work with the uv build backend right now" per astral-sh/uv#14561) and it cannot use `uv_build` [29].
    - Route C: native `[tool.uv.build-backend] version.source = "vcs"` with `tag-pattern` — shown working in issue #14946 with `uv_build>=0.8.3` [30]. *Uncertain*: this appears in an issue thread as a working config; confirm against the current uv docs' build-backend settings before relying on it (the docs-focused search did not surface a `version.source = "vcs"` doc page; it might be newer than the fetched docs snapshot or a preview feature). Treat Route C as plausible-but-unverified-2026.
    - Route D (what release tools do): **static version in pyproject, bumped by the release tool** (`uv version <x.y.z>` / `uv version --bump …` is the uv-native command for this) — matches semantic-release/CC-based flows exactly, no dynamic metadata anywhere, and keeps `uv_build` usable [27]. `__version__` can come from `importlib.metadata` if needed at runtime [27].
    - For emuctl, Route D is the natural fit given locked decision #6 (CI derives the version from conventional commits): no plugin, no VCS-tag projection, works with `uv_build`.
    - Extras/resolution note: **per-extras resolution just works** — `[project.optional-dependencies]` are standard published metadata; the resolver instantiates extras per install request. uv does not special-case extras beyond dependency extras markers. (No distinct uv mechanism; this is stock PEP 621 behavior — flagged as "no 2026-specific uv feature found here".)

11. **Real-world pyprojects (cited):**
    - **Astral's own docs templates** (`uv init --lib` output): `uv_build` + src layout + `py.typed` [4].
    - **fastapi** repo: workflows build with `uv build`, publish via OIDC; in 2026 switched final publish step from `uv publish` to `pypa/gh-action-pypi-publish@v1.14.0` to gain default PEP 740 attestations (FastAPI discussion by maintainers; touches `fastapi`, `typer`, `sqlmodel`, etc.) [23][24].
    - **trusted-publishing-examples** (POC by uv maintainer konstin): `.github/workflows/release.yml` using `uv publish` + PyPI Trusted Publisher config; published artifact visible on PyPI [19].
    - **ninoseki/uv-dynamic-versioning** pyproject: `build-system requires = ["hatchling", "uv-dynamic-versioning"]` / `build-backend = "hatchling.build"` (because uv_build excludes dynamic versioning) [29].
    - **hatch/hatch-vcs-style monorepo example** from an engineer comment in astral-sh/uv#8714: `[tool.hatch.version] source = "vcs"` with namespaced tag patterns (`tag-pattern = "^my-project/v(?P<base>…)"`) and `relative_to = "pyproject.toml"` [27].
    - *(Not exhaustively audited — sample of prominent, uv-adjacent repos, not a representative sweep.)*

## 3. Prior art table

| Project | What it does | Points of interest for emuctl |
|---|---|---|
| uv docs `uv init --lib` template [4] | `uv_build`, src layout, `py.typed`, build-system block | The default shape emuctl should clone |
| astral-sh/uv changelog 0.12.0 [5] | `uv init` now packaged-by-default with `uv_build` | Signals uv_build is the intended default going forward |
| fastapi (+ artifacts of typer/sqlmodel etc.) [23][24] | OIDC publish via gh-action-pypi-publish for default PEP 740 attestations; `uv build` kept | Publish-step pattern emuctl can adopt |
| trusted-publishing-examples [19] | End-to-end `uv publish` + Trusted Publisher example, `release.yml` on PyPI | Reference for the minimal OIDC workflow |
| ninoseki/uv-dynamic-versioning [29] | Hatchling hook for VCS versions (works only via hatchling, not uv_build) | Shows what switch emuctl would need if VCS-versioning is ever required — likely never |
| hatch/hatch-vcs monorepo user [27] | Namespaced tags + VCS versioning under hatchling | Prior art for VCS-versioning; not needed for conventional-commit flow |
| py-pymemuc / py-clash-bot | (See NOTES.md review) | Not uv-packaging leaders; emuctl would lead here rather than copy |

*(Note: no single prominent library was found in this pass that pairs `uv_build` + `hatch-vcs`; that combo is impossible by design — VCS versioning routes through hatchling.)*

## 4. Recommendation for emuctl

Concrete choices uv should make at v0:

1. **Build backend: use `uv_build`** (stable since 2025-07, now default in `uv init` since uv 0.12) with the upper-bounded `[build-system] requires = ["uv_build>=0.12,<0.13"]` pattern [1][2][5]. emuctl is pure Python (screenshot bytes come from an ADB binary, not an extension), which is exactly uv_build's supported scope [7]. No DESIGN.md revision needed — this refines, not contradicts, locked decision #7 (uv tooling).
2. **Scaffold via `uv init --lib`** and keep its conventions: `src/emuctl/` layout with `py.typed` included by default [4] (matches inherited principle "`py.typed` from day one"). Avoid the `--bare` route; emuctl wants the tests/ dirs and README scaffolding.
3. **Dev dependencies: `[dependency-groups]` (PEP 735), never `tool.uv.dev-dependencies`.** Use a `dev` group plus a `test` group if test-only deps exist; publish never sees them [8]. Lint/type tooling (ty, ruff, pre-commit) lives in groups, not in published metadata.
4. **`[tool.uv]` settings to adopt:** `required-version` (pin a compatible uv for agents/CI consistency) [11]; `constraint-dependencies` only when a specific transitive bound is needed (currently: none expected — emuctl's zero-dep core means no constraint pressure at v0) [10]. Do **not** use `uv.toml` for project config — it suppresses the `[tool.uv]` block [31]; keep everything in `pyproject.toml`.
5. **Commit `uv.lock`.** It is invisible to pip-installed consumers [15][17]; it buys reproducible dev envs, reproducible CI, and enables the CI patterns Astral recommends (`uv sync --resolution lowest` for lower-bound verification; allowed-failure `--upgrade` catch-job) [15]. Conventional-commit release chores should also update the lock when regenerating.
6. **Publishing: Trusted Publishing (OIDC) + `uv publish`, with attestations via `pypa/gh-action-pypi-publish`** as the recommended publish action (default-attestations) [18][23][24] — or keep pure `uv publish` + `astral-sh/attest-action` if you want an all-Astral flow and accept an early-stage action [25]. Either satisfies locked decision #6 as written; no revision required, but note the split: build = `uv build`, publish = the action. Keyring only for a maintainer's local emergency publish [12][20]. API-token families should be avoided (PyPI no longer accepts username/password uploads) [18].
7. **Versioning: static version in `pyproject.toml`, bumped by the release tooling** (`uv version --bump …` or semantic-release/repo-bumper) [27]. Avoid dynamic VCS versioning entirely for emuctl — it would force migrating off `uv_build` (hatch-vcs or uv-dynamic-versioning both live in the hatchling world [27][29]), adds a PEP-440 edge case layer, and buys nothing given locked decision #6's CI-derived version. If VCS-tag versioning is ever demanded later, `uv_build`'s native `version.source = "vcs"`-style config (`uv>=0.8.3`) is the candidate [30] — **unverified against current docs**; validate first.
8. **Per-extras packaging: plain PEP 621 `[project.optional-dependencies]`.** No uv-specific per-extras resolver knob exists; extras are standard and work as documented (see artifact 03 — extras naming per adapter: `memu`, `bluestacks`, `google-play`, `avd`, `all`, `cli`) [8].
9. **Workspace note: emuctl is a single published library.** No workspaces/monorepo needed at v0 (see artifact 02). `tool.uv.build-backend.module-name = "emuctl"` will be the default since the package name matches the module name.

**DESIGN.md revision check:** locked decisions #6 (auto-release + OIDC), #7 (uv tooling) and distribution shape all survive. **One revision worth flagging to the brief owner (not editing DESIGN.md):** the "tooling — uv/ty/ruff/pre-commit" line should be read as *uv = resolver/lockfile/build/publish* and the release tool (semantic-release vs release-please) will own the static `project.version`, so `uv version …` in CI is the primitive; that's artifact 07's decision, not this one.

## 5. Open questions

- **Does current `uv_build` document `[tool.uv.build-backend] version.source = "vcs"`? 2026-current status unverified** — evidence exists only in an issue thread config (`uv_build>=0.8.3`) [30]. Re-check the build-backend concepts page before relying on VCS versioning. Meanwhile, emuctl doesn't need it.
- **Attestation generation inside uv** (issue #15618, PR #19489) is in flight [22] — if it lands before emuctl's first release, revisit the publish action choice (pure-uv flow becomes possible).
- **`tool.uv.dev-dependencies` removal timeline** — deprecated in practice and slated for removal [8]; the exact uv release that deletes it is a maintenance fact to re-check whenever uv minor bumps happen (also drives the `<0.13` bound widening).
- **pylock.toml export feasibility** for downstream consumers (PEP 751 support in uv is export-only, `uv export -o pylock.toml`) [14] — probably non-goal for emuctl, but worth a line in README if a consumer wants reproducible installs against emuctl; note pylock is a standard format that also can carry attestation identities [24].
- **Ongoing adoption numbers of `uv_build` among top-360 PyPI packages** were not audited here (the FastAPI discussion mentions "166 of the top 360 packages" publish attestations) [24] — anecdata only.

## 6. Sources

1. https://docs.astral.sh/uv/concepts/build-backend/ — The uv build backend (Astral docs, 2026)
2. https://github.com/astral-sh/uv/commit/71b5ba13d76ad9e1b5bdf48941022fa7e89e4f3b — "Stabilize the uv build backend" PR #14311 (2025-07-02)
3. https://pypi.org/project/uv-build/ — uv_build on PyPI, download/version metadata (fetched 2026-10)
4. https://docs.astral.sh/uv/concepts/projects/init/ — Creating projects (`uv init --lib`)
5. https://github.com/astral-sh/uv/blob/main/CHANGELOG.md — uv CHANGELOG (uv 0.12.0, 2026-07-28)
6. https://docs.bswen.com/blog/2026-03-05-create-python-project-with-uv/ — community walkthrough of `uv init` (2026-03-05) — hearsay/secondary
7. https://github.com/astral-sh/uv/blob/main/docs/concepts/build-backend.md — build-backend in-repo docs (module-name/module-root/namespace)
8. https://docs.astral.sh/uv/concepts/projects/dependencies/ — dependency-groups vs legacy dev-dependencies (uv docs)
9. https://github.com/astral-sh/uv/issues/15406 — Deprecate `tool.uv.dev-dependencies` (2025-08-21)
10. https://docs.astral.sh/uv/reference/settings/ — `[tool.uv]` settings reference
11. https://docs.astral.sh/uv/reference/settings/ — `required-version` entry
12. https://docs.astral.sh/uv/reference/settings/ — `keyring-provider` entry
13. https://docs.astral.sh/uv/concepts/projects/config/ — project/tool.uv config docs
14. https://docs.astral.sh/uv/concepts/projects/layout/ — pyproject.toml / uv.lock ownership and export docs
15. https://github.com/astral-sh/uv/issues/10730 — uv maintainers on lockfile commit for libraries (`uv sync --resolution lowest`, etc.)
16. (same as 15) — request to auto-gitignore uv.lock in `--lib` (not implemented)
17. https://www.vibereference.com/devops-and-tools/should-you-commit-uv-lock — secondary source on lockfile policy (2026) — hearsay/secondary
18. https://docs.astral.sh/uv/guides/package/ — Building and publishing a package (OIDC, `uv publish`, check-url)
19. https://pypi.org/project/trusted-publishing-examples/ — uv maintainer's working Trusted Publishing example + `.github/workflows/release.yml`
20. https://github.com/astral-sh/uv/pull/8806 — uv `uv publish` keyring docs PR (scoped tokens, no `.pypirc`)
21. https://github.com/astral-sh/uv/pull/16731 — "Collect and upload PEP 740 attestations during `uv publish`" (2025-11-13; conflicts with the older claim that uv-publish had no attestations; landed in uv 0.9.12+)
22. https://github.com/astral-sh/uv/issues/15618 — "uv publish: create attestations" (open workstream for generating them)
23. https://fastapi/fastapi/discussions/15678 — FastAPI org discussion: swapping `uv publish` → `pypa/gh-action-pypi-publish` for PEP 740 (2026-06)
24. (same as 23) — `pypa/gh-action-pypi-publish@v1.14.0` attestations-on-by-default confirmation + "166 of top 360 packages"
25. https://github.com/astral-sh/attest-action — Astral's action for PEP 740 generation; flagged "early-dev/unstable"
26. https://docs.pypi.org/attestations/security-model/ — PyPI attestation security model (Sigstore keyless, Fulcio/Rekor)
27. https://github.com/astral-sh/uv/issues/8714 — FR: `uv build` accepts a dynamic "package version" argument (context on hatch-vcs / static-version workflows)
28. https://github.com/astral-sh/uv/issues/14946 — `[uv_build] support dynamic = ["version"]` (still unsupported; hatch-vcs/hatchling route)
29. https://github.com/ninoseki/uv-dynamic-versioning — dyn versioning plugin for uv/hatch; "doesn't work with the uv build backend right now" (ref astral-sh/uv#14561)
30. https://github.com/astral-sh/uv/issues/14946 (same thread, later comment) — `[tool.uv.build-backend] version.source = "vcs"` config shown working with `uv_build>=0.8.3`. *Uncertain — not verified against current docs.*
31. https://github.com/astral-sh/uv/issues/14308 — `tool.uv.*` config cannot live in `uv.toml` (project config must stay in `pyproject.toml`)
32. https://brandonwie.dev/posts/uv-lock-best-practice — 2026-01-28 secondary writeup: lockfile for libraries (secondary)

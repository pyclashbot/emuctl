# 02 — Monorepo / multi-package patterns with uv workspaces

*Research artifact (working research, not a standard). Written against DESIGN.md as of 2026-10-06.*

## 1. Question

From the brief:

- uv workspace mechanics in 2026: `[tool.uv.workspace]` members, `[tool.uv.sources]` for intra-workspace deps, shared vs per-package lockfiles, how `[all]`/meta packages work in a workspace.
- Is a single-package repo (one pyproject) actually the right call for emuctl vs a workspace of e.g. `emuctl` core + `emuctl-cli`? Decision factors: independent versioning vs coupled releases, import cost, IDE/dev-loop ergonomics, publishing friction.
- Real prior art of uv workspaces in the wild (Astral's own repos, ruff, other notable projects) — cite repo + config paths.
- How extras-vs-subpackage interacts with monorepo choice (can one package name expose optional extras that are separate workspace members?).
- ty / pre-commit / CI integration across workspace members.

## 2. Findings

1. **Workspace mechanics are stable and small.** A workspace is declared by a `[tool.uv.workspace]` table (with `members = ["packages/*"]` globs, optional `exclude`) in any member's `pyproject.toml`; that package becomes the workspace root. Cross-member dependencies are declared as normal project dependencies plus a `{ workspace = true }` entry in `[tool.uv.sources]`. Editable installs between members are automatic — "Dependencies between workspace members are editable." Workspace-root `[tool.uv.sources]` apply to all members unless a member overrides a given dependency. [1]
2. **One lockfile per workspace, one venv.** "In a workspace, each package defines its own pyproject.toml, but the workspace shares a single lockfile." `uv lock` operates on the whole workspace; `uv run` / `uv sync` operate on the root by default and accept `--package <member>`; `uv sync --all-packages` and `uv build --package <member>` / `uv publish` exist for targeting members. Workspaces enforce a single `requires-python` (the intersection of all members). [1][2]
3. **Per-package lockfiles are only available by *not* using a workspace** — each directory becomes an independent project with its own lock and venv, and cross-dependencies become `{ path = "../../packages/shared", editable = true }` sources instead. Official docs present this as the documented alternative ("rather than grouping … define each package as its own independent project"). [1] The docs also warn TypeScript-style: "Workspaces are not suited for cases in which members have conflicting requirements, or desire a separate virtual environment for each member." [2]
4. **uv cannot isolate member dependencies from each other.** "As Python does not provide dependency isolation, uv can't ensure that packages don't import dependencies declared by another workspace member." [1] This is a real (if usually minor) risk for a workspace whose members ship to PyPI: a member can be green locally but broken for end users because a transitive dep leaked. Mitigated by per-member `uv build --package X` + test-in-isolation CI jobs, and CI publishing from built wheels rather than source.
5. **Meta/`[all]` packages: two idiomatic patterns.**
   - *Extras within one package* (DESIGN.md's current plan): `[project.optional-dependencies] all = [...]`. Works inside a workspace too — the workspace root can even be a wheel-less "virtual" package via `tool.uv.package = false` used purely to aggregate members, and dependencies on other members still route through `{ workspace = true }` sources. [1][3]
   - *Separate meta package*: a tiny workspace member whose only dependency is the other members (e.g. root `kiln-root` depending on `kiln-ai` with `{ workspace = true }` in the Kiln AI setup). [4] A separate member is only needed if you want the meta name itself to be a *published PyPI artifact*; for `emuctl[all]` the extra approach publishes nothing extra and is strictly simpler.
6. **A member must be a real buildable package for imports to work.** Kiln AI hit `ModuleNotFoundError` in workspace mode because the member lacked a build system; uv only installs "packaged" projects into the shared venv. Non-package members can be marked `tool.uv.package = false` deliberately (decorative roots, docs). [3][5]
7. **Single-package repo is the dominant shape for small multi-entry libraries.** Concretely: uv itself is one Python distribution (`pyproject.toml` at repo root, maturin, `python/` layout) despite having ~30 Rust workspace crates — the *Python* side is a single package and the CLI ships inside it. [6] ruff is a single package too (`astral-sh/ruff` root `pyproject.toml`, still setuptools-era config at `_main`; the modern Astral template uses `uv_build`). [6]
8. **Big monorepos use workspaces — with per-package complete pyprojects and one root lockfile.** Apache Airflow migrated to a uv workspace: root `pyproject.toml` has `[tool.uv.workspace] members = [".", "airflow-core", "dev/breeze", "dev/mypy", ...]` plus a long `[tool.uv.sources]` block of `{ workspace = true }` entries, and one root `uv.lock`. Each member (e.g. `airflow-core/pyproject.toml`) is a fully-specified hatchling package. [7] LangChain similarly: "Python monorepo with multiple independently versioned packages that use uv", `libs/{core,partners/*,text-splitters,...}` each with its own `pyproject.toml`, editable installs via `[tool.uv.sources]`, and *per-package* release/versioning (`langgraph` repo cuts releases per package, e.g. `cli==0.4.32`). [8][9]
9. **The reason big monorepos split is independent release cadence/consumers, not import cost.** LangChain's own rationale (2023): "if they are all in one package there is no way for developers to reliably know which integrations are well-supported"; splitting into `langchain-core` / `langchain-community` / `langchain` let each version independently. [10] `langgraph-cli` is a separate published package with its own extras (`[inmem]`) rather than an extra of `langgraph`. [9][11] For emuctl, core and CLI are versioned and released *together by design* (locked decision #6: one auto-release pipeline, one changelog) — the strongest reason for a split doesn't apply.
10. **Import cost argument is weak here.** Import cost is controlled by lazy module loading (PEP 562 `__getattr__`, guard imports — the pattern langchain-core uses [10]), not by packaging. Whether the CLI lives in `emuctl/cli/` (submodule of the one dist) or `emuctl-cli` (separate dist importing `emuctl`), a `pip install emuctl` user gets the same import of `emuctl` only; nothing vendor-specific loads unless the backend extras pull it in (DESIGN.md §3). A CLI *subpackage within the same dist* carries zero import cost when entry point + modules are behind the `cli` extra boundary — same trick uvicorn/celery use for `[standard]`-style extras (DESIGN.md's cited prior art). What a workspace would add is only: not shipping CLI modules to `pip install emuctl` users at all — a few KB on disk.
11. **Publishing friction: workspace is slightly more, not less.** Single package: one `uv build` / `uv publish`, one OIDC workflow, one semantic-release config. Workspace: still one root lock, but `uv build --package emuctl` per artifact and the release tooling must map commits/changes to member versions; conventional-commit releasers generally assume one package per repo (release-please has multi-package configs, python-semantic-release supports multi-project but with extra wiring) — agent 7 covers details; flagging as friction, not researched here. [8][9 show multi-package releases are done in the wild, at real CI cost]
12. **Nesting is not supported, and nested pyprojects leak.** Workspaces cannot nest ("Nested workspaces are not supported"); an actively-reported 2026 issue shows sibling/nested workspaces can interfere via `[tool.uv.sources]` discovered from intermediate `pyproject.toml` files, with `override-dependencies` as the workaround and open demand for a fix (June 2026). Any future sub-project vendoring must keep the workspace flat. [12][13]
13. **ty across members.** ty discovers config from "a pyproject.toml or ty.toml file in the current directory, or in the nearest parent directory", reading `[tool.ty]`; `ty.toml` takes precedence over a sibling `pyproject.toml`. [14] Practical pattern in a workspace: one root `ty.toml` (or root `[tool.ty]`) for shared settings + `ty check` from repo root over a single venv, since the workspace has one `.venv` containing every member editable-installed [1][2] — ty resolves imports through that venv automatically [15]. Per-member overrides can live as `[tool.ty]` in each member's pyproject for directory-scoped checks. In a two-package separation you could instead run `ty check` twice with two venvs; that's more CI steps, not fewer.
14. **ruff across members.** ruff natively supports monorepos (per-file-ignores / `extend` / `preview` in one `ruff.toml`, or nested pyproject `[tool.ruff]` tables per member); nothing in a uv workspace requires per-member ruff config. Common setup: one root `ruff.toml` + rare per-dir overrides. *(Inference from ruff's documented config model; no repo claim beyond uv's own root `ruff.toml`. [6])*
15. **pre-commit across members.** pre-commit is repo-scoped, not package-scoped: one `.pre-commit-config.yaml` at the workspace root covers all members, with ruff hooks getting `--directory` or `types`/`files` filters if per-member splitting is wanted. uv ships a root `.pre-commit-config.yaml` even though it's a single (Rust) package [6]; nothing about uv workspaces changes the pre-commit model. *(Inference; uncontroversial, pre-commit semantics are unchanged by pyproject layout.)*
16. **CI targeting is a workspace selling point**: `uv run --package emuctl-cli test`, `uv build --package emuctl`, cache-keying via workspace metadata export (`uv export`/metadata API exposes the resolution graph for tooling) [2][16]. But with a single package, matrix CI is simpler (no `--package` flags, no member discovery edge cases like `--locked` requiring every member's pyproject to be present in Docker builds [17]).

Uncertainty flags: the pre-commit and ruff-across-workspace observations are inferences from documented tool behavior rather than a study of many workspace repos; [16] workspace-metadata *usage in CI* is newly documented and I did not find a notable public CI consumer.

## 3. Prior art table

| Project | Layout | What it demonstrates | Link |
|---|---|---|---|
| astral-sh/uv | Single Python package (root `pyproject.toml`, maturin, `python/uv/…`) + Rust Cargo workspace in the same repo | Even Astral ships a CLI + library binding as one dist; no uv workspace on the Python side | [6] |
| astral-sh/ruff | Single Python package at repo root | Same | [6] |
| apache/airflow | Large uv workspace: root `pyproject.toml` `[tool.uv.workspace]` members incl. `./airflow-core`, `dev/breeze`, `dev/mypy`…; long root `[tool.uv.sources]` all `{ workspace = true }`; single root `uv.lock` | Canonical big-workspace config to copy syntax from | [7] |
| langchain-ai/langchain | Monorepo of *independently versioned* packages under `libs/*`, uv + editable sources | Why monorepos split: independent release/versioning per consumer-facing package; `libs/core` = `langchain-core` | [8] |
| langchain-ai/langgraph | Per-package releases from one monorepo (e.g. tag `cli==0.4.32`); `langgraph-cli` is a separate PyPI package with its own extras | Multi-package release cadence the hard way; "CLI as separate package" as counterpoint | [9] |
| Kiln-AI/Kiln (uv branch) | Root meta package `kiln-root` + members `libs/core`, `libs/server` via `{ workspace = true }` | Meta-root aggregate pattern; also the "member must be packaged" gotcha | [3][5] |
| fedragon/uv-workspace-example | Root pyproject + `packages/my_lib` + `src/my_app`, one `uv.lock`; Makefile/dockerized | Minimal dev-loop reference incl. `uv sync --package`, `uv build --package`, `uv publish` | [18] |
| carderne/uv-workspace-example | Root + `packages/*` + Docker | Notes the triple-announce boilerplate (dependencies + sources + members) per new package | [19] |
| Union.ai docs | Documented internal monorepo pattern: several packages, one root `uv.lock`, `{ workspace = true }` sibling deps | Downstream adoption of the pattern as documented practice | [20] |

## 4. Recommendation for emuctl

**Keep a single package repo; do not create a uv workspace for `emuctl` + `emuctl-cli` at v0.** Explicitly: this does *not* require revising any DESIGN.md locked decision; locked decision #5 ("optional sibling subpackage, `emuctl[cli]`") is correct as stated and the word "subpackage" should mean *submodule of the same distribution gated behind the `cli` extra*, not a second workspace member.

- The workspace's core benefits (independent versioning/release cadence, per-member consumer isolation, mixed app+lib) are all absent here: CLI and core ship in lockstep by design (#6), the only consumer is py-clash-bot (#1's first consumer), and both are libraries to the end user. LangChain-langgraph bolted on multi-package release because consumers wanted packages at different cadences [9] — emuctl v0 has one of each artifact, released together.
- Extras already solve the "pay only for what you use" goal (DESIGN.md §3): `emuctl[memu|bluestacks|cli|all]` in one pyproject is simpler than a workspace member per backend, and a workspace member-per-backend would publish `emuctl-memu`-style names to PyPI — import-name/wheel-name clutter and extra release surface for zero user benefit. A meta `emuctl-all` package is unnecessary; an `all` extra needs no separate member (Finding 5).
- Import cost is governed by lazy loading, not packaging (Finding 10); the CLI extra keeps console-script + CLI modules out of the default install path.
- Publishing/CI friction is lower with one package: one semantic-release config, one `uv publish`, one OIDC job (Finding 11). py-clash-bot's own slow-consumption history (pinned `<0.7` lagging upstream fixes, NOTES.md §5 version skew) argues for *fewer*, larger releases, not finer-grained ones.
- Type/lint discipline is easiest unsplit: one venv, one `ty.toml`, one `ruff.toml`, one `.pre-commit-config.yaml`, `ty check .` from repo root (Findings 13–15).

**If/when to revisit:** create a workspace only when a component genuinely wants (a) its own release cadence or PyPI identity (e.g. a standalone agent daemon, or a splitting of per-backend packages if a backend ever ships heavy proprietary deps), or (b) an app + shared lib with diverging dependency graphs. The migration path is cheap and mechanical: add `[tool.uv.workspace] members = ["packages/*"]`, move the CLI into `packages/emuctl-cli` with `emuctl = { workspace = true }` in `[tool.uv.sources]`. Keep `pyproject.toml` flat and hatchling/uv-build-native (agent 1's topic) so that migration is local.

**Small caveat to respect from day one:** even with a single package, test it as its wheel (build + install into a scratch venv) in CI, so tests never silently depend on sibling extras at dev-install time — that is the cleanliness benefit people wrongly expect workspaces to grant (Finding 4/uv's own non-isolation warning [1]).

## 5. Open questions

1. Multi-package conventional-commit release tooling (python-semantic-release vs release-please, incl. per-package tags like langgraph's `cli==…`) — deferred to artifact 07.
2. Whether uv gains first-class "root-only lockfile install" ergonomics in Docker (`--no-install-workspace --locked` pyproject-presence bug closed mid-2025 [17], but nested-workspace source leakage is still open [12]) — matters only if the workspace path is ever taken.
3. Exact extras-lint/towncrier choices for documenting submodule boundaries (overlaps artifact 03 & 04; not resolved here).
4. Whether `tool.uv.package = false` root (virtual workspace root aggregating members) would ever be preferable to a real root package — only relevant post-split; behavior verified in docs [3] but not in a shipped Astral toolchain example in this research.

## 6. Sources

1. uv docs — Using workspaces: https://docs.astral.sh/uv/concepts/projects/workspaces/ (mirrors: https://fossies.org/linux/uv/docs/concepts/projects/workspaces.md)
2. uv docs — Using workspaces (restrictions/single-requires-python, `--package` model) — same URL as [1]; docs page dated 2026-10-03 in search snapshot.
3. uv docs — Configuring projects (`tool.uv.package = false`): https://docs.astral.sh/uv/concepts/projects/config/
4. uv docs — Workspace metadata export: https://docs.astral.sh/uv/reference/internals/metadata
5. astral-sh/uv#8704 — "Workspace mode not finding package dependency" (member must define a build system): https://github.com/astral-sh/uv/issues/8704
6. astral-sh/uv repo layout & root `pyproject.toml`: https://github.com/astral-sh/uv and https://github.com/astral-sh/uv/blob/main/pyproject.toml ; astral-sh/ruff: https://github.com/astral-sh/ruff
7. apache/airflow `pyproject.toml` (uv workspace + sources blocks) and root `uv.lock`: https://github.com/apache/airflow/blob/main/pyproject.toml ; member example https://github.com/apache/airflow/blob/main/airflow-core/pyproject.toml
8. langchain-ai/langchain `AGENTS.md`/`CLAUDE.md` (monorepo structure, uv, editable sources): https://github.com/langchain-ai/langchain/blob/master/AGENTS.md
9. langchain-ai/langgraph release page for `cli==0.4.32` (per-package tags): https://github.com/langchain-ai/langgraph/releases/tag/cli%3D%3D0.4.32
10. LangChain blog — Towards LangChain 0.1 (rationale for splitting packages): https://www.langchain.com/blog/the-new-langchain-architecture-langchain-core-v0-1-langchain-community-and-a-path-to-langchain-v0-1
11. langgraph-cli on PyPI (own extras `[inmem]`): https://pypi.org/project/langgraph-cli
12. astral-sh/uv#17156 — nested/workspace-source discovery issue (open, commented June 2026): https://github.com/astral-sh/uv/issues/17156
13. astral-sh/uv source — "Nested workspaces are not supported" error: https://github.com/astral-sh/uv/blob/main/crates/uv-workspace/src/workspace.rs
14. ty docs — Configuration (pyproject/ty.toml discovery, precedence): https://docs.astral.sh/ty/configuration
15. ty docs — Type checking (venv/package discovery): https://docs.astral.sh/ty/type-checking
16. uv workspace metadata docs, see [4].
17. astral-sh/uv#6685 — `--no-install-workspace --locked` requires member pyprojects (fixed via #6737): https://github.com/astral-sh/uv/issues/6685
18. fedragon/uv-workspace-example: https://github.com/fedragon/uv-workspace-example
19. carderne/uv-workspace-example: https://github.com/carderne/uv-workspace-example
20. Union.ai docs — Monorepo with uv: https://www.union.ai/docs/v2/union/user-guide/project-patterns/monorepo-with-uv
21. Community gist — UV Workspaces: Python Monorepos Done Right (Yuzu02): https://gist.github.com/Yuzu02/0f53c6f73a471077c3268c4a42aef60a
22. astral-sh/uv#5388 — `uv init` workspace-member ergonomics (PR 18572 pending): https://github.com/astral-sh/uv/issues/5388

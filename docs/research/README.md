# emuctl — research index (phase 3)

One artifact per topic, written by a dedicated research agent. Artifacts are **working research, not standards** — they inform revisions to `DESIGN.md`; the locked decisions there stay locked until research actually recommends otherwise.

## Artifact shape (every doc follows this)

1. **Question** — the direction-relevant question(s) this research must answer, verbatim from the brief.
2. **Findings** — numbered facts with citations inline (`[1]`-style). Cite official documentation, real source repos (with file paths where relevant), and release/version dates. Mark hearsay and uncertainty explicitly. State *how it's actually done in the wild*, not just how it's officially documented.
3. **Prior art table** — concrete examples (projects) with what they do and links.
4. **Recommendation for emuctl** — concrete, actionable: what to adopt, what to avoid, what stays open. Must reference DESIGC.md's locked decisions and say whether/which research suggests revising them.
5. **Open questions** — what remains unresolved and when it should be revisited.
6. **Sources** — full URL list.

Rules: cite or flag; no unsourced claims in Findings. No commits, no edits to `DESIGN.md`/`NOTES.md`.

## Artifacts

| # | File | Topic | Researcher |
|---|---|---|---|
| 01 | `01-uv-packaging.md` | uv packaging: hatchling vs uv-native build, `[tool.uv]`, lock/export, PYPI publishing integration | agent 1 |
| 02 | `02-monorepo-workspace.md` | uv workspaces / monorepo patterns for multi-package repos | agent 2 |
| 03 | `03-extras-subpackage.md` | optional extras + sub-subpackage distribution patterns | agent 3 |
| 04 | `04-cli-in-library.md` | how lib CLIs are done well (arg parsing, plugins, entry points, agent-first design) | agent 4 |
| 05 | `05-capability-abstraction.md` | abstraction patterns & prior art: capability models across vendors, IaC-style prior art (Pulumi/SST/Alchemy), emulator/ADB libs | agent 5 |
| 06 | `06-async-sync-bridge.md` | async core + sync wrapper design: prior art, tooling (anyio), cancellation semantics | agent 6 |
| 07 | `07-release-and-stub.md` | python-semantic-release vs release-please (+ OIDC publishing), AND offline test-stub fidelity patterns (fake ADB vs fake controller) | agent 7 |

## Cross-artifact brief (what every researcher was told)

> emuctl is a planned Python library: a higher-level, vendor-agnostic Android emulator controller (what pymemuc was to MEmu, generalized). Locked direction: async core + sync wrapper; ADB/ MEmu/ BlueStacks 5 at v0; declared capability sets per adapter; `screenshot() -> bytes` stdlib-only core; optional sibling CLI (`emuctl[cli]`) as agent interface; conventional-commit auto-release + PyPI Trusted Publishing (OIDC); Python 3.12+; tooling = uv/ ty/ ruff/ pre-commit; first consumer py-clash-bot.

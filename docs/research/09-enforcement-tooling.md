# 09 — Enforcement tooling for the contract set

## 1. Question

For every enforceable rule in the contract set (CONTRACTS.md §C6 "Enforcement" list, the trailing "What conformance must assert" items 1–10, and per-contract rules surfaced by the brief): is there *mature, dedicated tooling* that enforces it — especially for AGENTS-contributed code and new contributors — or is a hand-rolled pytest/AST conformance check the right (or only) answer?

## 2. Findings

**Infrastructure context (applies to every row).** The locked stack is uv / ty / ruff / pre-commit / GitHub Actions with python-semantic-release (DESIGN §4, §8). ty is still `0.0.x` beta in 2026 (0.0.7x–0.0.8x range in 2026), targets 1.0 "in 2026", and — importantly — **ty has no plugin system on purpose** [11][12]. So no rule in the contract set can be enforced by a ty plugin, in contrast to mypy; the contract set's own cut of "custom lint plugin" (#59) matches reality. ruff remains the enforcement engine for module-scope/name-scope lexical rules.

### Rule 1 — Import DAG (layers + backend independence)

- **import-linter is mature and current in 2026**: release notes show 2.15 (2026-09-04, lazy-import support in explore UI/drawgraph), 2.14 (2026-08-28, adds `broken_contract_guidance`, ignore-count display), 2.13 (2026-07). It is built **on grimp** as its graph engine (grimp 2.x supported; layers/independence algorithms rewritten for speed) [1][2].
- **A `layers` contract** is the exact normative tool for "backends → emuctl, never reverse": layers are listed high→low, higher may import lower, lower never imports higher, including indirect chains [3][4].
- **An `independence` contract** is the exact tool for "backend sibling packages never import each other" (and, via the layers contract, never reach into another family's `_`-private modules) [4].
- **Lazy-import loophole**: import-linter historically analyzes all imports regardless of nesting; 2026 releases add lazy-import visibility to its UI tooling [1]. Grimp/import-linter **cannot see `importlib.import_module(<non-literal>)` at all** — grimp's scanner only resolves literal module references. That's a feature for emuctl: the locked design says the lazy loader's dynamic import is "deliberately invisible" to the layers contract (C6).
- **ruff/ty did NOT gain import-graph rules by 2026** — ruff's `flake8-tidy-imports` is per-module-name (bans/appears wherever the banned path appears), not a graph over your package's module topology; ty has no plugin mechanism [11][12]. So import-linter (+ grimp) remains unpicked-over as of 2026. Community writeups confirm this is the standard architecture-linting answer in 2025–26 [5].

### Rule 2 — `subprocess` only in `emuctl/_proc.py` + shell=True ban

- **ruff TID251 (`banned-api`)** can ban `subprocess` *project-wide* — it is a global name/import ban, **not per-importer-scoped**. The long-requested per-source-module scoping (ruff issue #7974, opened Oct 2023) exists as a feature request; the workaround that ships today is `per-file-ignores` [6][7]:
  - ban `"subprocess"` in `flake8-tidy-imports.banned-api`,
  - exempt exactly `emuctl/_proc.py` (and the test fakes) via `tool.ruff.lint.per-file-ignores`.
  This inverted-allowlist shape works and is documented; the `per-file-ignores` mechanism is a first-class ruff feature [7]. One caveat to flag honestly: `per-file-ignores` disables *the whole TID251 rule* in `_proc.py`, so anything else you later add to `banned-api` goes unpoliced there too. Keep the banned-api list to exactly `subprocess` so the exemption stays airtight.
- **shell=True ban is natively enforced**: ruff `S602` (subprocess call with `shell=True` detected from string literals), `S603` (subprocess call without static string), `S605` (`shell=True` via os.system-seeming calls), `S607` (partial executable path) — flake8-bandit's subprocess rules, implemented in ruff since well-before 2026 [8]. Importantly S602/603/605/607 are *shadowable per-file* the same way, and TID251's `subprocess` ban already forces the argv-only discipline onto every call site since `_proc.py` is the sole importer.
- Honest verdict on scoping: ruff **can** do this rule today (global ban + per-file exemption), without an import-linter `forbidden` contract or a hand-rolled checker. A belt-and-braces import-linter `forbidden` contract (source: whole package, forbidden: `subprocess`) is possible but redundant with TID251.

### Rule 3 — Dynamic imports only through `load_adapter`

- Neither grimp nor import-linter can see dynamic imports (their scanner resolves literal-string imports; a computed `import_module(name)` is invisible) [1][5]. There is no mature 2026 tool for "dynamic imports must funnel through one named function"; the classic options are (a) a convention test walking the AST for `importlib` / `__import__` / `import_module` call-sites and asserting they occur only in `emuctl/backends/_registry.py` (`load_adapter`), or (b) a ruff `banned-api` entry for `"importlib"` exempted in `_registry.py` — legitimate and cheap, since the lexical call-site `import` statements of importlib are visible to TID251 even though the dynamic target isn't.
- Prior art for AST-walking conformance tests: it is the *standard bespoke pattern* — e.g. projects that pin "importlib appears in exactly this file" or "no qualified name X" do it as a pytest+`ast` test; the airflow-style "dynamic import guard" convention cited in the brief is about *using* dynamic imports defensively, not linting their call-sites (hearsay from the brief; not verified). **Confirmed: no off-the-shelf tooling exists; pytest/AST conformance test is the right answer**, exactly as CONTRACTS.md §C6 "Enforcement" bullet 2 already says.

### Rule 4 — Spec rows export adapter class + BackendDiscovery functions

- There is no tool whose job is "row in a table ⇒ module exports these names, finds them via `find_spec`, never via import side-effect". This is runtime behavior over a data structure the contract declares normative. **Plain pytest conformance test is the only answer** — and per item 6 of "What conformance must assert" it doubles as the "spec table is the sole platform truth" assertion. Worth adding: a tiny contract-suite helper parametrized over `get_registry()` rows does the whole job in ~40 lines; a dependency like a "schema/assertion helper" library would be ceremony. Related tool worth knowing (optional sweep): **deptry** checks declared-vs-actual dependencies per module set — useful as a *supplementary* check that backend packages' third-party deps are declared under their extras (extra-boundary promotion discipline, C6 rule 4), but it does not enforce the spec-row shape itself.

### Rule 5 — Public API stability / breaking-change detection

- **griffe** ships the canonical Python answer and is actively maintained (mkdocstrings/griffe): `griffe check` CLI and `find_breaking_changes()` over two snapshots (notably `load_git()` to compare against the last published tag) — it catches parameter removal/reordering, name removals, and signature/annotation breakage, and explicitly reasons about which "breakages" it does and doesn't recognize [9]. This is the natural CI gate for "additive-only output/API evolution": run `griffe check git:<last-release-tag>:src/emuctl/` on the release-workflow head and fail the job on breakages. Caveat: griffe is AST/AFDO-based, so *behavioral* promises (screenshot byte fidelity, exit-code mapping, error `str` stability) still need the conformance suite — griffe sees shapes, not semantics.
- Real-world practice: semver-disciplined Python SDKs (the 2026 SDK tooling ecosystem — Fern's `fern diff`, oasdiff/OpenAPI-land) all use a "diff against the last published state, fail on major" gate in CI [13]; griffe is the Python-language equivalent of that pattern, and the big Python SDKs in 2026 include no published API-diff tooling in their public release pipelines (hearsay; flagged) — the tooling exists, it's the *adoption* that's uneven. **Adopt `griffe` in the GH release workflow**; keyword args/defaults drift worth pinning (parameter reordering, default changes) is griffe's documented strength [9].

### Rule 6 — Port contract shape (nominal ABC + `__init_subclass__` wiring gate)

- **ty has no plugin system by design** — Astral explicitly declined mypy's library-plugin mechanism, preferring in-tool framework support for the most popular libraries [11][12]. So the `__init_subclass__` wiring-time gate *cannot* move into ty, and even a hypothetical future plugin wouldn't express "concrete-in-MRO per MechanicsTable name". Meanwhile the runtime `__init_subclass__` gate **is sufficient and is the idiomatic Python mechanism** — it runs at class definition, catches violations for AGENT-contributed backends the moment the module is imported (load_adapter does that), and the conformance suite statically walks every adapter class in CI for the non-imported case. The contract set already reached exactly this resolution (ruling #59: no custom lint plugin, two enforcement sites: wiring gate + conformance). Research confirms this ruling can stay locked — mypy-plugin prior art (e.g. django-stubs, sqlalchemy typing) is a caution about maintaining private-engine integrations, not a precedent worth copying; py-ty's pace of first-party framework support is not a bet to ride for a bespoke ABC design.

### Rules 7, 8, and conformance items 1–5, 7–9 — taxonomy, parity, drift checks

- **Exception taxonomy conformance** (unique `error_code`, `payload` ClassVars, picklability across workers, stable `__str__`, exit-map drift): the contract already prescribes a CI drift test walking the class tree (C2). Bespoke parametrized pytest is the right tool — a `__subclasses__`-walk parametrization over (error_code uniqueness, payload tuples ⊂ typed primitives, pickle round-trip in an actual worker/process for cross-queue fidelity, `__str__` snapshot test). Hypothesis stateful testing is *overkill* for a closed class tree; prior art in SDK test suites is table-driven parametrization (fastAPI, starlette exception docs; the anthropic SDK's error classes are API-shape-tested manually in their test dirs — flagged hearsay). **No tool exists; pytest verdict.**
- **Sync/async parity** (every public op has a sync twin with identical kwargs): **confirmed — no mature off-the-shelf tool exists at 2026** (searches over the pytest plugin index independently returned no parity-checking plugin — the parity-related plugins above are concurrency/async-execution helpers, not API-shape checkers). The anthropic 1.0 migration mess (properties becoming methods, `await` added to response methods, nothing failing at import time) is the *motivating bad world*: their parity is manually kept (flagged; their test suite doesn't ship a public parity assertion) [13]. A bespoke `inspect.signature` parametrized pytest comparing `EmulatorController` (sync facade) to `AsyncEmulatorController` public ops — name set, params, kw-only-ness, defaults — is the right answer, ~30 lines, run in CI. Note that this ALSO guards the "every I/O op carries `timeout=DEFAULT`" contract for free (add it to the same parametrization).
- Conformance items 1–5, 7–9 (fake-adapter raise taxonomy, timeout shape, cancel shape, logger silence, bridge identity/deadlock, config precedence, PNG sniff fixtures, suggestion rendering): all *behavioral*, all pytest by nature (research/07 + research/08 already own harness shape). Tool row: N/A — this is what pytest is for.

### Rule 9 — Conventional commits + pre-commit

- **python-semantic-release's own commit parser is the only gate emuctl strictly needs**: PSR tag-types-to-bump itself parses `feat/fix/!`/BREAKING CHANGE on main; nothing else needs to re-derive the bump. The gap PSR doesn't cover is *invalid commit subjects* (unconventional messages get silently ignored/fall through to no-bump). Two mature options as the pre-commit-side gate: (a) **commitlint** (JS, .commitlintrc) with the husky-style pre-commit hook — widely used but pulls Node into a Python repo; (b) **commitizen** (`cz check`, pre-configurable conventional presets) — the Python-native equivalent, mature and 2026-current (hearsay on exact version; widely adopted). For an agent-contributed repo, the *CI* check (a GitHub Actions step running `cz check --rev-range base_HEAD..HEAD` or PSR dry-run) on the merged range is the one that can't be bypassed. Verdict: adopt PSR as the source of truth at release (already locked in DESIGN §8) + commitizen `cz check` in pre-commit and CI; commitlint unnecessary for a Python-only repo.
- pre-commit handles the lexical layer (ruff, import-linter's `lint-imports`, commit checkers) so AGENTS-contributed PRs are checked identically to human ones before CI even sees them.

### Rule 10 — Agent-compliance guardrails (AGENTS.md governance)

- 2026 practice has matured past "put it in the instruction file and hope": hooks are the enforcement layer for *active* agent sessions — Claude Code hooks run shell scripts at lifecycle events (PreToolUse etc.) and block tool calls; OpenCode supports hooks via plugins; OpenAI Codex has its own hooks convention [14][15][16]. The Evil Martians pattern ("a rule in AGENTS.md is documentation; a PreToolUse hook is enforcement") has been widely taken up [15], with a robust-looking literature of production hook collections accumulating through 2026 [14].
- But hooks are *local, fail-open, per-agent*, and not faithfully applied on CI [15][16]. Therefore for emuctl's governance the **in-repo CI/backstop layer remains king**: semgrep/pytest conformance tests, import-linter, griffe diff — the rules that survive *any* agent and any human. A reasonable minimal hook set: a PreToolUse hook that runs `uv run ruff check --no-fix` on edited files, plus the CI conformance suite as the authoritative gate. There is no "agents.md lint" standard tooling (flagged); the working 2026 answer is: **hooks for fast feedback, CI policy checks as the enforcement of record** [15][16]. For opencode specifically, plugins are the hook surface [15].
- Also relevant: repo's AGENTS.md should point at the conformance suite as its own legislated test target (item 10 of "What conformance must assert" is arguably the hardest to obey without a clerk — a fresh contributor doesn't know to update the plan/suite in lockstep; that gap needs a clerked test or the contract's slow-rot discipline).

### Rule 11 sweep (optional)

- **beartype / typeguard**: not relevant to any listed rule — emuctl's `__init_subclass__` gate + dataclass `__post_init__` already do zero-dependency runtime validation; runtime type validation of public ops would churn and not enforce the contract semantics (kwarg-shape parity is inspect-based). Not adopted.
- **hypothesis**: not adopted for the closed exception-class tree (see Rule 7); useful only for config-model range fuzzing (item 7 of conformance), which is optional polish — plain parametrized pytest covers the pinned ranges.
- **deptry**: adopted, but only as a supplementary extra-boundary check (see Rule 4 note above).

## 3. Prior art table

| Project / tool | What it does | Link |
|---|---|---|
| import-linter | layers / independence / forbidden import contracts on top of grimp; v2.15 (2026-09-04), v2.14 (2026-08-28) added `broken_contract_guidance`, lazy-import UI support | [1][2][4] |
| grimp | import graph engine import-linter wraps; fast layers/independence algorithms in 2.x | [1][2] |
| ruff TID251 / per-file-ignores | banned-api name/import ban + module-scoped exemption (`emuctl/_proc.py`); issue #7974 documents the per-module-scoping hole and workaround | [6][7] |
| ruff flake8-bandit S602/603/605/607 | subprocess/shell discipline rules, natively enforced | [8] |
| griffe `check` / `find_breaking_changes` | git-tag-vs-current API breakage detection; recommended CI pattern per own docs | [9][13] |
| step up 2026 agent-governance writeups | "Hooks turn policy into fact"; hooks-with-CI architecture | [14][15][16] |
| anthropic SDK v1.0 migration (2026-08) | breakage-driven motivation for automated API diff + parity checks | [13] |
| kokonatt / posthog-style explorers | real measurement of layers/independence contracts at scale (6,059-file runs) | [5] |

## 4. Recommendation for emuctl — RULE → TOOL table

| # | Rule (CONTRACTS ref) | Verdict | Config sketch |
|---|---|---|---|
| 1 | Import DAG (layers + independence) | ✅ **mature tool**: import-linter 2.x (dev group; pre-commit + CI) | see `.importlinter_toml` below |
| 2 | `subprocess` only in `_proc.py`, never `shell=True` | ✅ **mature tool**: ruff TID251 + S602/603/605/607 with `per-file-ignores` exemption | see `ruff.toml` sketch; keep `banned-api` list to exactly `subprocess` so the per-file exemption stays airtight; noteworthy honest limitation — the exemption disables the whole TID251 rule in `_proc.py`, and neither ruff nor import-linter matches a computed `import_module(string)` |
| 3 | Dynamic imports only via `load_adapter` (spec-table-driven) | ❌ no off-the-shelf tooling; **pytest/AST conformance test** (bespoke, ~50 LOC), exactly as §C6 "Enforcement" already prescribes | parametrized test: `ast.walk` for `importlib`/`__import__`/`import_module` call-sites outside `_registry.py` → fail with file:line |
| 4 | Spec rows export adapter class + BackendDiscovery fns | ❌ no tool for this data-binding; **plain pytest conformance** (parametrize over `get_registry()`) | same test file as #3; doubles as "spec table is the sole platform truth" (item 6) |
| 5 | Public API stability at release | ✅ **mature tool**: griffe `check` in the release workflow (diff vs last tag); pytest for semantics griffe can't see | `griffe check git:<prev-release-tag>:src/emuctl/` step in the GH release workflow, fail on any Breakage |
| 6 | Port contract shape (`__init_subclass__` gate) | ✅ **runtime gate is correct and sufficient**; ty has no plugin system (by design) and can't express MRO-satisfaction — mypy-plugin prior art is a caution, not precedent; conformance pytest for non-imported statics | no tool config; the ABC's `__init_subclass__` + a static-walk pytest parametrized over `AsyncEmulatorController.__subclasses__` (recursive) |
| 7 | Exception taxonomy conformance | ❌ no tool; **pytest/AST conformance** (tree-walk parametrized, plus a real worker-process pickle check) | one `test_errors.py` parametrized over `EmuctlError.__subclasses__()` recurse; assert `error_code` global uniqueness, payload typing conformity, pickle round-trip, `exitmap` no-mismatch |
| 8 | Sync/async parity (identical kwargs) | ❌ **confirmed — no mature off-the-shelf tool exists** (searched the 2026 plugin landscape; nothing parity-shaped); **pytest/AST conformance** (~30 LOC inspect-driven parametrize over `EmulatorController` / `AsyncEmulatorController` public ops — also pins `timeout=DEFAULT` presence, giving item 5 enforcement for free) | `test_parity.py`: for each sync public op find the `.aio` twin, compare `inspect.signature` (`Signature.equals(signature=..., include_annotations=True, include_return_annotation=True)`) |
| 9 | Conventional commits + pre-commit | ✅ **mature tool**: python-semantic-release as release-side truth (locked in DESIGN §8) + commitizen `cz check` in pre-commit/CI as the Python-native gate; commitlint (Node) unnecessary for a Python-only repo | `.pre-commit-config.yaml`: hook running `cz check --allow-abort`; CI step on PRs |
| 10 | Agent-compliance guardrails | ⚠️ mixed: **hooks for active sessions** (Claude Code hooks / opencode plugins) are the mature short-feedback loop; **CI conformance suite is the enforcement of record** (hooks are fail-open, untrusted); no `agents.md-lint` standard exists (flagged) | a minimal PreToolUse hook (ruff on edited files) + the full `uv run pytest tests/conformance` as a required PR check |

### Config sketches (adoptable)

```toml
# .importlinter (or pyproject [tool.importlinter])
[importlinter]
root_package = emuctl

[importlinter:contract:layers]
name = "backends may import core, core never backends"
type = layers
layers =
    emuctl.backends
    emuctl
# grimp/import-linter deliberately cannot see load_adapter's dynamic import — the locked design.

[importlinter:contract:independent-backends]
name = "backend sibling packages never import each other"
type = independence
modules =
    emuctl.backends.adb
    emuctl.backends.memu
    emuctl.backends.bluestacks
    emuctl.backends.bluestacks_air
    emuctl.backends.google_play
    emuctl.backends.avd
```

```toml
# ruff.toml additions
[lint.flake8-tidy-imports.banned-api]
"subprocess".msg = "Subprocess discipline lives in emuctl/_proc.py only (C6)"

[lint.per-file-ignores]
"emuctl/_proc.py" = ["TID251", "S602", "S603", "S605", "S607"]
"emuctl/backends/_registry.py" = ["TID251"]        # importlib home (Rule 3 belt)
"tests/_fakes/*.py" = ["TID251"]

[lint.extend-select]
# T251 (TID), S602/S603/S605/S607 (bandit-subprocess), plus:
#   TID (functions/imports) S602, S603, S605, S607
```

**Relation to DESIGN/CONTRACTS locked decisions:** the research *confirms* the C6 Enforcement bullet exactly as written — import-linter (layers + independence) plus the two bespoke conformance assertions, wired into dev-group + pre-commit + CI. Ruling #59 (custom lint plugin cut) survives contact with ty's no-plugin reality [11][12]. Rule 5 (griffe at release) is an **addition** worth folding into the release workflow, not a revision of DESIGN §8 lock. Nothing here suggests revising the locked tool stack (uv/ty/ruff/pre-commit/CI).

**Honest overall verdict for the brief's framing:** out of the 10 rule families, formal tooling covers exactly **three** cleanly (import DAG, subprocess+shell discipline, conventional-commit gate at release) and griffe covers the release-time API-break dimension. Everything semantically contract-specific — dynamic-import funneling, spec-row conformance, wiring-gate, taxonomy, bridge parity, drift tests, rule-10 items 1–10 — **is and should remain bespoke pytest**, because the enforcement surface is emuctl's own declared vocabulary (MechanicsTable, BackendSpec, `__init_subclass__`, taxonomy class tree), for which no generic tool exists in 2026 and writing one would violate the contract's "two enforcement sites" economy.

## 5. Open questions

1. **ty 1.0 timeline** — upgrade path when ty leaves `0.0.x` (2026 target per the 2026-era dev article; still 0.0.7x–0.0.8x in current docs) — worth re-checking post-1.0 for new rule classes (strict mode, pluginless framework support) that might absorb any part of the pytest conformance suite.
2. **banned-api per-importer scoping** — if ruff lands #7974's "per-module banned-api", revisit whether the per-file-ignores exemption gets replaced with a cleaner scope for `_proc.py`.
3. **griffe behavioral coverage** — griffe detects shape breakage; *behavioral* additive-only promises (exit-code table, error `__str__`, PNG sniff) rely on the conformance pytest — revisit if an "api-diff for behavior" tool matures (currently only API-shape diffing is sampled in 2026 practice, e.g. Fern/oasdiff for OpenAPI [13]).
4. **Bridge/ReadyCheck deadlock scenario** — bespoke pytest in practice (item 5); check whether anyio/typing releases in 2026–2027 provide a runtime observability hook that simplifies it (currently custom instrumentation).
5. **opencode/agent hook format standardization** — hooks exist per-vendor (Claude Code hooks, Codex hooks, opencode plugins); no format standard proposed yet (flagged). Watch for the next 6–12 months.

## 6. Sources

- [1] Import Linter release notes, high-versioned releases — https://import-linter.readthedocs.io/en/stable/release_notes
- [2] Import Linter — layers/independence contract docs — https://import-linter.readthedocs.io
- [3] David Seddon, "Meet Import Linter" — layers contract example — https://seddonym.me/2019/05/20/meet-import-linter
- [4] Roman Imankulov, "Linter for Python Architecture" — practical layers/independence setups — https://roman.pt/posts/python-architecture-linter
- [5] Kokonatt, "Enforcing Modular Boundaries with Import Linter" — empirical 6k-file runs, exclude_type_checking_imports — https://kokonatt.com/blog/modular-boundaries-import-linter
- [6] ruff docs — banned-api (TID251), flake8-tidy-imports settings — https://docs.astral.sh/ruff/settings and https://docs.astral.sh/ruff/rules/banned-api
- [7] astral-sh/ruff issue #7974 — "support module-level banned imports" — the per-module scoping request — https://github.com/astral-sh/ruff/issues/7974
- [8] ruff docs (flake8-bandit S-rules) — S602/S603/S605/S607 subprocess discipline rules — https://docs.astral.sh/ruff (bandit-derived set)
- [9] Griffe docs — "Checking" (breaking-change API, `load_git`, `find_breaking_changes`) — https://mkdocstrings.github.io/griffe/guide/users/checking
- [11] Saurabh Kumar, "ty: Astral's Take on Python Type Checking" (2025-05) — "No Plugins, On Purpose" — https://saurabh-kumar.com/articles/2025/05/ty-astrals-take-on-python-type-checking
- [12] astral-sh/ty GitHub — 0.0.x versioning, beta status, no plugin API, Python-target support matrix — https://github.com/astral-sh/ty
- [13] Fern, "Handling breaking changes: automating semantic versioning for API SDKs" (2026-08) — oasdiff/`fern diff` required-CI pattern — https://buildwithfern.com/post/automating-semantic-versioning-api-sdks-claude
- [13b] anthropic-sdk-python v1.0 migration (2026-08) — breakage evidence, properties/methods/await drift — https://aireiter.com/blog/anthropic-python-sdk-v1-migration-guide
- [14] Ranjan Kumar, "Hooks: the enforcement layer that turns agent policy into agent fact" (2026) — https://ranjankumar.in/hooks-policy-as-code-agent-enforcement
- [15] Evil Martians, "Stop writing rules in AGENTS.md: use agent hooks and nano-staged instead" — https://evilmartians.com/chronicles/stop-writing-rules-in-agents-md-use-agent-hooks-and-nano-staged-instead
- [16] Pillar Security, "Best AI Coding Agent Security Tools (2026)" — fail-open nature of hooks, enforcement through pre-tool gates — https://www.pillar.security/blog/best-ai-coding-agent-security-tools-for-the-enterprise-2026

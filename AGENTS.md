# AGENTS.md — emuctl

- **CONTRACT.md is the operating contract.** Read it before any design change; do not edit it.
- `docs/` is archive + research — do not edit. `README.md` is human-drafted — do not create.
- Source skeleton is contract layout (§6): docstring-only files are intentional; do not delete or "complete" them except per contract sections.

## Workflow

- Conventional commits (`feat:`, `fix:`, `chore:`, …). Commitizen cz check runs on commit-msg.
- Sync deps: `make setup` (uv sync + pre-commit install). Run lint/type-check/test via `make lint`, `make type-check`, `make test`.
- Enforcement is import-linter layers/independence contracts + ruff TID251 (see §6) — keep these green.
- Never import `subprocess` outside `src/emuctl/_proc.py`; never parse vendor stdout for control flow.
- Additive-only evolution: errors/config/flags/platform truth only change where the contract says.

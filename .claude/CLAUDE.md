# Filament Python API

A Python API for the Filament platform.

## Project Structure

```
filament-python-api/
├── src/                # Application source code
├── tests/              # Unit tests (pytest)
├── plans/              # Plan documents for issue work
├── .claude/            # Claude Code config and skills
├── .claude-docker/     # Docker sandbox config
└── .github/workflows/  # CI/CD workflows
```

## Tech Stack

- **Python 3.10, 3.11, 3.12, 3.13** with strict mypy type checking
- **Poetry** for dependency management
- **pytest** for testing
- **mypy** for static type analysis

## Common Commands

### Build & Test

```bash
# Install dependencies
poetry install --with test

# Run unit tests
poetry run pytest tests/ -v

# Run tests with coverage
poetry run pytest tests/ --cov=src -v

# Run mypy type checking
poetry run mypy src/
```

### Docker Container Environment

When running inside the Docker sandbox (`.claude-docker/`), use `hostcmd` to execute allowlisted commands on the host.
The allowlist is defined in `.claude-docker/settings.json` under `host_commands`.

```bash
# Run mypy on the host
hostcmd mypy src/
```

Git commands run directly inside the container (no `hostcmd` needed).

### Read-Only Mounts

The main Filament backend repo is mounted read-only at its host path for reference:

- `D:\dev\FilamentSoftware\MVP\Filament` (host) → available inside the container

## Planning Rules

When creating plans:

- Always save plan files to `plans/`
- Name plan files descriptively based on the task, with a date prefix, e.g., `2025-01-25-auth-endpoint-plan.md`

## Generated Code

Files in `src/filament/types/generated/` are **auto-generated** and must **never** be edited by hand. If you encounter a bug, type error, or missing feature in generated code:

1. **Do not modify the generated files.** Any manual changes will be overwritten by the next codegen run.
2. Identify what needs to change and describe the fix in terms of the code-generation logic.
3. Share a clear description of the problem and a suggested prompt/instructions for fixing the codegen with the user, so they can address it upstream.

## Code Conventions

- **Python 3.10, 3.11, 3.12, 3.13** — avoid syntax only available in 3.11+ (e.g., `type` statement, `ExceptionGroup`)
- **Strict mypy** — all code must pass `mypy --strict`
- **Type annotations** on all function signatures
- Use `from __future__ import annotations` for forward references

## CI/CD

GitHub Actions workflows in `.github/workflows/`:

- `increment-version-develop.yml` / `increment-version-main.yml` — Semantic versioning
- `mypy-check.yml` — Static type checking
- `run-unit-tests.yml` — Unit tests across Python 3.10–3.13
- `validate-pull-request.yml` — PR validation (fixup commits, DO NOT MERGE, rebase check, CHANGELOG)
- `create-release.yml` — Build and publish GitHub releases on version tags

## Notes

- Never commit to git unless specifically asked to, or a skill explicitly instructs you to commit.
- Always use the `/commit` skill when creating commits. Do not commit manually.

# Filament Python API

An async Python client for the Filament platform, communicating over SignalR.

## Project Structure

```
filament-python-api/
├── src/
│   ├── filament/
│   │   ├── _client.py              # FilamentClient (async SignalR client)
│   │   ├── _serialization.py       # Dataclass <-> dict conversion for SignalR wire format
│   │   ├── _condition_builder.py   # Typed Condition(Entity).prop("field") builder
│   │   ├── _exceptions.py          # FilamentError, FilamentAuthenticationError, etc.
│   │   └── types/
│   │       ├── generated/          # Auto-generated dataclasses from backend (DO NOT EDIT)
│   │       ├── conditions.py       # DbConditionInstance hierarchy + serialization
│   │       └── options.py          # DataOption + serialization
│   └── filament_mypy_plugin.py     # Mypy plugin for typed condition builder
├── tests/              # Unit tests (pytest)
├── plans/              # Plan documents for issue work
├── .claude/            # Claude Code config and skills
├── .agent-docker/     # Docker sandbox config
└── .github/workflows/  # CI/CD workflows
```

## Tech Stack

- **Python 3.10, 3.11, 3.12, 3.13** with strict mypy type checking
- **Poetry** for dependency management
- **signalr-async** for SignalR communication with the Filament backend
- **pytest** + **pytest-asyncio** for testing
- **mypy** for static type analysis

## Architecture

`FilamentClient` is an async context manager that wraps a SignalR connection:

```python
async def main() -> None:
    async with filament.FilamentClient(
        organization_id="org-id", api_key_name="key", api_key="secret",
    ) as f:
        result = await f.read_data(request)
```

- **Authentication**: API key headers (`X-Api-Key-Name` + `X-Api-Key`) and organization header (`X-Organization-Id`)
- **Default endpoint**: `https://backend.filament-software.com:7076/api/v0` (server, port, path all configurable)
- **Serialization**: `_serialization.py` converts between Python dataclasses (snake_case) and SignalR wire format (camelCase dicts), handling `uuid_utils.UUID`, `from __future__ import annotations`, and enum `None_` naming. Includes polymorphic dispatch for `DbConditionInstance` and `DataOption` types.
- **Conditions**: `conditions.py` defines the `DbConditionInstance` hierarchy (matching the C# backend's wire format). `_condition_builder.py` provides a typed `Condition(Entity).prop("field")` API with mypy plugin support for static validation.
- **Options**: `options.py` defines `DataOption` for typed key-value options with `option_to_dict`/`option_from_dict` serialization.
- **Subscription routing**: `subscribe()` stores user callbacks keyed by subscription ID; `DataUpdated` events are routed to the matching callback
- **Mypy plugin**: `filament_mypy_plugin.py` (top-level module, outside `filament/` to avoid circular imports) narrows `Condition.prop()` return types based on entity field types. Registered in `pyproject.toml` via `plugins = ["filament_mypy_plugin"]`.

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

When running inside the Docker sandbox (`.agent-docker/`), use `hostcmd` to execute allowlisted commands on the host.
The allowlist is defined in `.agent-docker/settings.json` under `host_commands`.

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

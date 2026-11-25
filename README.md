# Filament Python API

A Python client for the Filament platform, communicating over SignalR. It comes
in two forms: the async `FilamentClient`, and the blocking `FilamentClientSync`
for code that does not use `asyncio`.

## Documentation

Full, multi-page documentation lives in [`docs/`](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/README.md):

- [Getting Started](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/getting-started.md) — install, connect, authenticate, error handling
- [Finding Entity & Connection Types](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/finding-types.md) — where every type and enum lives
- [Type Reference](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/type-reference.md) — auto-generated catalogue of every entity, connection, and enum
- [Reading Data](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/reading-data.md) — queries, ordering, paging, projections
- [Building Conditions](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/conditions.md) — the typed condition builder and operators
- [Modifying Data](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/modifying-data.md) — create, update, delete, partial updates
- [User-defined Properties](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/user-defined-properties.md) — discover, read, write, query, and administer runtime properties
- [Subscriptions](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/subscriptions.md) — real-time updates and lifecycle events
- [Data Options](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/data-options.md) — the typed `DataOption` key-value type
- [API Reference](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/api-reference.md) — `FilamentClient`, `FilamentClientSync`, the response envelope, exceptions

Runnable scripts are in [`examples/`](https://github.com/FilamentSoftware/filament-api-python/tree/main/examples/). The sections below are a quick
overview; see the docs for the full detail.

### Claude Code skill

This repo doubles as a [Claude Code](https://claude.com/claude-code) plugin that
ships a `using-filament-api` skill. Install it in any project that uses this
package so Claude has the API reference on hand:

```text
/plugin marketplace add FilamentSoftware/filament-api-python
/plugin install filament-api@filament
```

The skill routes Claude to the bundled [`docs/`](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/README.md) — it stays in
sync with the documentation rather than duplicating it. Once installed, invoke it
with `/filament-api:using-filament-api`.

## Requirements

- Python 3.10, 3.11, 3.12, or 3.13

## Installation

Stable releases are published to [PyPI](https://pypi.org/project/filament-api/):

```bash
pip install filament-api
# or
poetry add filament-api
```

If your deployment's file store uses CRC32C checksums (GCS), install the
`crc32c` extra:

```bash
pip install "filament-api[crc32c]"
```

### Installing from the internal index (Filament staff / dev builds)

Every release, including the development builds cut from `develop`, is also
published to Filament's internal Google Cloud Artifact Registry. Its virtual
repository serves those releases together with a mirror of PyPI, so use it as
your **only** index (`--index-url`, not `--extra-index-url`, which would let a
same-named package on PyPI compete with the internal one).

Access requires the `roles/artifactregistry.reader` role on the repository and
[Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials)
(e.g. `gcloud auth application-default login`). Ask your Filament contact for
access.

With pip:

```bash
pip install keyrings.google-artifactregistry-auth
pip install --index-url https://europe-west2-python.pkg.dev/filament-artifacts/python/simple/ filament-api
```

With Poetry:

```bash
poetry self add keyrings.google-artifactregistry-auth
poetry source add --priority=primary filament-internal https://europe-west2-python.pkg.dev/filament-artifacts/python/simple/
poetry add filament-api
```

Dev builds are PEP 440 pre-releases (e.g. `0.8.1.dev4`), so installers skip
them unless asked. Use `pip install --pre ...`, pin an exact version
(`filament-api==0.8.1.dev4`), or `poetry add filament-api --allow-prereleases`
(which sets `allow-prereleases = true` on the dependency).

## Usage

```python
import asyncio
import filament
from filament.types import ReadDataRequest, ReadDataTypeRequest

async def main() -> None:
    async with filament.FilamentClient(
        organization_id="my-org", api_key_name="my-key", api_key="secret",
    ) as f:
        result = await f.read_data(
            ReadDataRequest(request=ReadDataTypeRequest(type_name="UserEntity"))
        )

asyncio.run(main())
```

The server, port, and path are configurable:

```python
f = filament.FilamentClient(
    organization_id="my-org",
    api_key_name="my-key",
    api_key="secret",
    server="https://backend.filament-software.com",
    port=1234,
    path="api/v1",
)
```

### Synchronous usage

`FilamentClientSync` offers the same request methods as ordinary blocking calls,
with no `asyncio` in your code:

```python
import filament
from filament.types import ReadDataRequest, ReadDataTypeRequest

with filament.FilamentClientSync(
    organization_id="my-org", api_key_name="my-key", api_key="secret",
) as f:
    result = f.read_data(
        ReadDataRequest(request=ReadDataTypeRequest(type_name="UserEntity"))
    )
```

It takes the same constructor arguments as `FilamentClient`. Subscriptions and
event callbacks are not available on it; use `FilamentClient` for those.

Both clients can also be opened and closed explicitly instead of with a `with`
block: `f.connect()` / `f.close()` on the sync client, `await f.connect()` /
`await f.close()` on the async one.

### Conditions (Typed)

Build type-safe query conditions with the `Condition` builder. The mypy plugin validates field names and value types at type-check time.

```python
from filament import Condition
from filament.types.generated.entities.user_service import UserEntity
from filament.types.generated.enums import UserStatusEnum
from filament.types.generated.misc import ReadDataRequest, ReadDataTypeRequest

# Mypy validates that "schedule_status" exists on UserEntity
# and that the value is a UserStatusEnum
condition = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)

# Comparable fields support ordering operations
condition = Condition(UserEntity).prop("given_name").less_than("M")

# Combine conditions
condition = Condition.and_(
    Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled),
    Condition(UserEntity).prop("given_name").like("%Alice%"),
)

# Use in queries
async def main() -> None:
    async with filament.FilamentClient(
        organization_id="my-org", api_key_name="key", api_key="secret",
    ) as f:
        result = await f.read_data(
            ReadDataRequest(request=ReadDataTypeRequest(
                type_name="UserEntity",
                condition=condition,
            ))
        )
```

To enable mypy validation, add to your `pyproject.toml`:

```toml
[tool.mypy]
plugins = ["filament_mypy_plugin"]
```

### Conditions (Untyped)

For quick usage without entity class references, use the convenience constructors:

```python
from filament.types.conditions import and_, equal_to, less_than, like

condition = and_(
    equal_to("scheduleStatus", "1"),
    like("givenName", "%Alice%"),
)
```

### Data Options

Typed key-value options for service data requests:

```python
from filament.types.options import string_option, int_option, bool_option

options = {
    "mode": string_option("preview"),
    "limit": int_option(100),
    "includeDeleted": bool_option(False),
}
```

### Subscriptions

Subscribe to real-time data updates with a callback:

```python
from uuid_utils import UUID

from filament.types.generated.misc import SubscriptionConfig, ServiceDataUpdate
from filament.types.generated.enums import DataProcessingStatus, SubscriptionMode

def on_update(update: ServiceDataUpdate) -> None:
    print("Data changed:", update.modified_entities)

scenario_id = UUID(int=0)  # replace with your scenario ID

async def main() -> None:
    async with filament.FilamentClient(
        organization_id="my-org", api_key_name="my-key", api_key="secret",
    ) as f:
        result = await f.subscribe(
            SubscriptionConfig(
                scenario_id=scenario_id,
                status=DataProcessingStatus.Pending,
                type_name="TaskEntity",
                mode=SubscriptionMode.Data,
            ),
            callback=on_update,
        )
```

### Event Callbacks

Register callbacks for processing lifecycle events:

```python
async with filament.FilamentClient(
    organization_id="my-org", api_key_name="my-key", api_key="secret",
) as f:
    f.on_processing_started(lambda status: print("Started:", status.scenario_id))
    f.on_processing_completed(lambda status: print("Done:", status.scenario_id))
    f.on_processing_failed(lambda status: print("Failed:", status.message))
```

## Development

Working on the client itself needs [Poetry](https://python-poetry.org/):

```bash
# Install with test dependencies
poetry install --with test

# Run tests
poetry run pytest tests/ -v

# Run type checking
poetry run mypy src/
```

Releases are built and published by `.github/workflows/create-release.yml`: every
release goes to the internal Artifact Registry, and stable releases also go to
PyPI. See [Publishing releases](https://github.com/FilamentSoftware/filament-api-python/blob/main/docs/publishing.md) for the flow and the
one-off setup.

## License

[MIT](https://github.com/FilamentSoftware/filament-api-python/blob/main/LICENSE)

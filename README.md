# Filament Python API

An async Python client for the Filament platform, communicating over SignalR.

## Requirements

- Python 3.10, 3.11, 3.12, or 3.13
- [Poetry](https://python-poetry.org/) for dependency management

## Installation

```bash
poetry install
```

## Usage

```python
import filament
from filament.types import ReadDataRequest, ReadDataTypeRequest

async with filament.FilamentClient(api_key_name="my-key", api_key="secret") as f:
    result = await f.read_data(
        ReadDataRequest(request=ReadDataTypeRequest(type_name="UserEntity"))
    )
```

The server, port, and path are configurable:

```python
f = filament.FilamentClient(
    api_key_name="my-key",
    api_key="secret",
    server="https://dev.backend.filament-software.com",
    port=1234,
    path="api/v1",
)
```

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
async with filament.FilamentClient(api_key_name="key", api_key="secret") as f:
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

async with filament.FilamentClient(api_key_name="my-key", api_key="secret") as f:
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
f.on_processing_started(lambda status: print("Started:", status.scenario_id))
f.on_processing_completed(lambda status: print("Done:", status.scenario_id))
f.on_processing_failed(lambda status: print("Failed:", status.message))
```

## Development

```bash
# Install with test dependencies
poetry install --with test

# Run tests
poetry run pytest tests/ -v

# Run type checking
poetry run mypy src/
```

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
from filament.types.generated.misc import ReadDataRequest, ReadDataTypeRequest

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

### Subscriptions

Subscribe to real-time data updates with a callback:

```python
from filament.types.generated.misc import SubscriptionConfig, ServiceDataUpdate
from filament.types.generated.enums import DataProcessingStatus, SubscriptionMode

def on_update(update: ServiceDataUpdate) -> None:
    print("Data changed:", update.modified_entities)

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

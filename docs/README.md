# Filament Python API Documentation

An async Python client for the [Filament](https://filament-software.com) platform.
The client talks to the Filament backend over [SignalR](https://learn.microsoft.com/aspnet/core/signalr/introduction)
and exchanges strongly-typed dataclasses that mirror the backend's C# entity model.

This documentation is organised as a set of task-focused guides. If you are new,
read [Getting Started](getting-started.md) first, then dip into the topic you need.

## Contents

| Guide | What it covers |
| ----- | -------------- |
| [Getting Started](getting-started.md) | Installation, connecting, configuration, authentication, error handling, and the async lifecycle. |
| [Finding Entity & Connection Types](finding-types.md) | Where every entity type, connection type, and enum lives; naming conventions; the base-class hierarchy; how to discover what fields exist. |
| [Type Reference](type-reference.md) | Auto-generated catalogue of every entity, connection, and enum (with fields and types), regenerated from the source so it never drifts. |
| [Reading Data](reading-data.md) | `read_data`, request shapes, ordering, paging, projections, counting, and parsing results back into typed entities. |
| [Building Conditions](conditions.md) | The typed `Condition` builder, every operator, dictionary/list/comparable fields, combining conditions, the mypy plugin, link (join) conditions, and the untyped API. |
| [Modifying Data](modifying-data.md) | Creating, updating, and deleting entities; partial updates and the `specified_properties` mechanism. |
| [Subscriptions](subscriptions.md) | Real-time `DataUpdated` events, subscription modes and status flags, callbacks, condition filtering, unsubscribing, and processing-lifecycle events. |
| [Data Options](data-options.md) | The typed `DataOption` key-value type and its constructors. |
| [API Reference](api-reference.md) | `FilamentClient` method-by-method reference, the response envelope, and the exception hierarchy. |

## A 60-second tour

```python
import asyncio

import filament
from filament import Condition
from filament.types import (
    ReadDataRequest,
    ReadDataTypeRequest,
    ServiceDataRead,
    UserEntity,
    UserStatusEnum,
)


async def main() -> None:
    # 1. Open a connection (async context manager).
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        # 2. Build a typed, validated query condition.
        condition = Condition(UserEntity).prop("schedule_status").equal_to(
            UserStatusEnum.Enabled
        )

        # 3. Read data.
        result = await f.read_data(
            ReadDataRequest(
                request=ReadDataTypeRequest(type_name="UserEntity", condition=condition),
            ),
        )

        # 4. Every call returns a success/error envelope.
        if not result.success or result.data is None:
            print(f"Error: {result.message}")
            return

        # 5. Unwrap entities back into their concrete type.
        data: ServiceDataRead = result.data
        for wrapped in data.data:
            user = filament.as_entity(wrapped.entity, UserEntity)
            if user is not None:
                print(user.given_name, user.family_name)


asyncio.run(main())
```

## Runnable examples

The [`examples/`](../examples) directory contains complete, runnable scripts that
this documentation refers to:

- `create_user.py` — create an entity
- `update_user.py` — find and partially update an entity
- `delete_user.py` — find and delete an entity
- `list_users.py` — read with a property projection
- `read_with_conditions.py` — read with the typed condition builder
- `subscribe_users.py` — subscribe to real-time changes
- `subscribe_with_conditions.py` — subscribe with a condition filter

## Conventions used in these docs

- **snake_case in Python, camelCase on the wire.** You always write Python
  attribute names (`given_name`); the client converts to the backend's
  camelCase (`givenName`) for you. Some APIs (untyped conditions, the
  `properties` projection list, `update_properties`) take the wire name
  directly — those cases are called out explicitly.
- **Entity type names are strings.** Requests identify a type by its class name
  as a string, e.g. `type_name="UserEntity"`. Use the class for type-checking
  (`Condition(UserEntity)`, `as_entity(..., UserEntity)`) and the string for the
  wire (`type_name="UserEntity"`).
- **Generated code is read-only.** Everything under
  `src/filament/types/generated/` is produced by the backend's code generator.
  See [Finding Types](finding-types.md#generated-code-is-read-only).
</content>
</invoke>

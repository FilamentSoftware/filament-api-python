# Subscriptions

Subscriptions deliver **real-time updates** when entities change on the backend.
You register a callback with `FilamentClient.subscribe`; the client invokes it
with a `ServiceDataUpdate` every time a matching `DataUpdated` event arrives.

```python
result = await f.subscribe(
    SubscriptionConfig(type_name="UserEntity", mode=SubscriptionMode.Data,
                       status=DataProcessingStatus.All),
    callback=on_update,
)
```

## Setting up a subscription

`subscribe` takes a `SubscriptionConfig` and a callback, and returns a
`ValueOrErrorPublic_1[SubscriptionId]`. Keep the returned ID — you need it to
[unsubscribe](#unsubscribing).

```python
import asyncio
import filament
from filament.types import (
    DataProcessingStatus, ServiceDataUpdate, SubscriptionConfig,
    SubscriptionMode, UserEntity,
)


def on_update(update: ServiceDataUpdate) -> None:
    for changes in update.modified_entities.values():
        for entity in changes.created:
            user = filament.as_entity(entity, UserEntity)
            if user is not None:
                print(f"+ {user.given_name} {user.family_name}")


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="org", api_key_name="key", api_key="secret",
    ) as f:
        result = await f.subscribe(
            SubscriptionConfig(
                type_name="UserEntity",
                status=DataProcessingStatus.All,
                mode=SubscriptionMode.Data,
            ),
            callback=on_update,
        )
        if not result.success:
            print(f"Failed to subscribe: {result.message}")
            return

        # Keep the connection open to receive events.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if result.data is not None:
                await asyncio.shield(f.unsubscribe(result.data))
            raise


asyncio.run(main())
```

> The `async with` block must stay open for events to be delivered. The
> `asyncio.Event().wait()` above parks the coroutine indefinitely; cancel it
> (Ctrl-C) to unwind and unsubscribe. See
> [`examples/subscribe_users.py`](../examples/subscribe_users.py).

## `SubscriptionConfig` fields

| Field | Type | Default | Meaning |
| ----- | ---- | ------- | ------- |
| `type_name` | `str` | `""` | The entity type to watch (class-name string). |
| `status` | `DataProcessingStatus` | `Unknown` | Which processing stages to receive events for. |
| `mode` | `SubscriptionMode` | `None_` | Whether to receive data changes, progress, or both. |
| `condition` | `Optional[DbConditionInstance]` | `None` | Only deliver updates for entities matching this filter. |
| `scenario_id` | `UUID` | zero-UUID | Scope the subscription to a scenario. |

### `status` — `DataProcessingStatus` (a flag enum)

Selects which processing stages trigger an event. Combine with `|`:

| Member | Meaning |
| ------ | ------- |
| `Pending` | Change queued but not started. |
| `Processing` | Change actively being processed. |
| `Complete` | Change finished. |
| `All` | Shortcut for `Pending \| Processing \| Complete`. |
| `Unknown` | Default/unset. |

```python
status=DataProcessingStatus.Complete                      # only finished changes
status=DataProcessingStatus.Pending | DataProcessingStatus.Complete
```

### `mode` — `SubscriptionMode` (a flag enum)

Selects what *kind* of update you receive. Combine with `|`:

| Member | Meaning |
| ------ | ------- |
| `Data` | Entity create/update/delete changes. |
| `Progress` | Operation progress updates. |
| `None_` | No updates (default). |

```python
mode=SubscriptionMode.Data                          # data changes only
mode=SubscriptionMode.Data | SubscriptionMode.Progress
```

## The callback

The callback receives a single `ServiceDataUpdate`. It may be **sync or async** —
the client awaits coroutine callbacks and calls plain functions directly. Any
exception raised inside a callback is logged and swallowed, so one failing
callback won't tear down the connection.

### `ServiceDataUpdate` fields

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `scenario_id` | `UUID` | The scenario this update belongs to. |
| `status` | `DataProcessingStatus` | Processing stage of this update. |
| `subscription_id` | `SubscriptionId` | The (backend) subscription that produced it. |
| `modified_entities` | `dict[str, EntityServiceDataUpdate]` | Changes, keyed by entity type name. |
| `progress` | `bool` | Whether this is a progress update. |
| `progress_amount` | `float` | Progress value when `progress` is set. |
| `sequence_idx` | `int` | Monotonic ordering index. |
| `request_ids` | `list[RequestId]` | Requests that caused this update. |

### `EntityServiceDataUpdate` — the per-type change set

Each value in `modified_entities` splits changes three ways:

| Field | Type | Contents |
| ----- | ---- | -------- |
| `created` | `list[EntityBase]` | Newly created entities. |
| `updated` | `list[UpdatedEntityServiceData_1[EntityBase]]` | Updated entities + which properties changed. |
| `deleted` | `list[EntityBase]` | Deleted entities. |

For `updated`, each item has `.entity` (the new state, may be `None`) and
`.updated_properties` (the list of changed property names):

```python
def on_update(update: ServiceDataUpdate) -> None:
    for changes in update.modified_entities.values():
        for entity in changes.created:
            user = filament.as_entity(entity, UserEntity)
            if user:
                print(f"+ Created {user.user_name}")

        for upd in changes.updated:
            if upd.entity is None:
                continue
            user = filament.as_entity(upd.entity, UserEntity)
            if user:
                props = ", ".join(upd.updated_properties)
                print(f"~ Updated {user.user_name} [{props}]")

        for entity in changes.deleted:
            user = filament.as_entity(entity, UserEntity)
            if user:
                print(f"- Deleted {user.user_name}")
```

As when reading, use [`filament.as_entity`](api-reference.md#as_entity) to
downcast each `EntityBase` to its concrete type.

## Filtering with conditions

Pass a `condition` to receive updates only for matching entities. The builder is
identical to reads — see [Conditions](conditions.md).

```python
from filament import Condition
from filament.types import UserStatusEnum

condition = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)

await f.subscribe(
    SubscriptionConfig(
        type_name="UserEntity",
        status=DataProcessingStatus.All,
        mode=SubscriptionMode.Data,
        condition=condition,
    ),
    callback=on_update,
)
```

See [`examples/subscribe_with_conditions.py`](../examples/subscribe_with_conditions.py).

## Multiple subscriptions and de-duplication

`subscribe` returns a **client-generated** `SubscriptionId`, unique to each call,
even if two calls request the same type and permissions. This lets you register
several independent callbacks and unsubscribe them independently.

Internally, the backend may return the *same* subscription ID for duplicate
type/permission combinations. The client tracks the mapping and only sends the
backend `Unsubscribe` once the **last** client subscription sharing a backend ID
is removed. You don't need to manage any of this — just keep each returned
`SubscriptionId` and unsubscribe it when done.

## Unsubscribing

Cancel one subscription by its client ID:

```python
await f.unsubscribe(subscription_id)   # the SubscriptionId from subscribe()
```

Cancel everything at once:

```python
await f.unsubscribe_all()
```

Both return a `SuccessOrErrorPublic`. Leaving the `async with` block also tears
down the connection (and therefore all subscriptions); explicit unsubscribing is
good practice when you want to stop receiving events but keep the client open.

For types with [user-defined properties](user-defined-properties.md), the client
loads definitions before subscribing and converts UDP values before invoking
your callback. Its separate private definition subscription is automatically
restored after `unsubscribe_all()` and is never returned as a user subscription.

When unwinding on cancellation, wrap the unsubscribe call in `asyncio.shield` so it
isn't itself cancelled mid-flight (as the examples do).

## Processing-lifecycle events

Separately from data subscriptions, you can register callbacks for the backend's
processing lifecycle. These fire for `ProcessingStarted`, `ProcessingCompleted`,
and `ProcessingFailed` server events and receive a `ProcessingStatus`:

```python
f.on_processing_started(lambda status: print("Started:", status.scenario_id))
f.on_processing_completed(lambda status: print("Done:", status.scenario_id))
f.on_processing_failed(lambda status: print("Failed:", status.message))
```

`ProcessingStatus` carries `scenario_id`, `request_ids` (the affected requests),
and a human-readable `message`. Like data callbacks, these may be sync or async,
and you can register more than one of each. They are handy for tracking when a
[`modify_data`](modifying-data.md) request has finished processing.

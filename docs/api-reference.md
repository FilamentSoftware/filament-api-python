# API Reference

A method-by-method reference for `FilamentClient`, the response envelope every
call returns, and the exception hierarchy. For task-oriented walkthroughs, see
the topic guides linked from the [docs index](README.md).

## `FilamentClient`

```python
filament.FilamentClient(
    organization_id: str,
    api_key_name: str,
    api_key: str,
    server: str = "https://backend.filament-software.com",
    port: int = 443,
    path: str = "api/v0",
    connection_timeout: float = 10.0,
)
```

An async context manager wrapping a SignalR connection. See
[Getting Started](getting-started.md#constructor-parameters) for parameter
details. Open it with `async with`; the connection is established and
authenticated on entry and closed on exit.

```python
async with filament.FilamentClient(...) as f:
    ...
```

Entering raises [`FilamentConnectionError`](#exceptions) if the server is
unreachable or times out, and [`FilamentAuthenticationError`](#exceptions) if the
credentials are rejected.

### Methods

All methods are coroutines and must be `await`ed.

#### `read_data(request) -> ValueOrErrorPublic_1[ServiceDataRead]`

Read entity data. Takes a `ReadDataRequest`. See [Reading Data](reading-data.md).

#### `modify_data(request) -> ValueOrErrorPublic_1[RequestId]`

Create, update, or delete entities. Takes a `ModifyDataRequest`. On success
`.data` is a `RequestId`. See [Modifying Data](modifying-data.md).

#### `subscribe(subscription, callback) -> ValueOrErrorPublic_1[SubscriptionId]`

Register a callback for real-time `DataUpdated` events matching a
`SubscriptionConfig`. Returns a client-generated `SubscriptionId`. The callback
may be sync or async and is called with a `ServiceDataUpdate`. See
[Subscriptions](subscriptions.md).

#### `unsubscribe(subscription_id) -> SuccessOrErrorPublic`

Cancel one subscription by its client-generated `SubscriptionId`. The backend
`Unsubscribe` is only sent when the last client subscription sharing a backend ID
is removed.

#### `unsubscribe_all() -> SuccessOrErrorPublic`

Cancel all active subscriptions and clear local callback state.

#### `get_entity_permissions() -> ValueOrErrorPublic_1[EntityPermissions]`

Retrieve entity-level permissions for the authenticated user.

#### `get_entity_property_permissions(entity_type_name) -> ValueOrErrorPublic_1[EntityPropertyPermissions]`

Retrieve property-level permissions for a given entity type (passed as a
class-name string).

### Event-callback registration

These register callbacks for the backend's processing-lifecycle events. Each may
be called more than once to register multiple callbacks; callbacks may be sync or
async and receive a `ProcessingStatus`. See
[Subscriptions → Processing-lifecycle events](subscriptions.md#processing-lifecycle-events).

| Method | Fires on |
| ------ | -------- |
| `on_processing_started(callback)` | `ProcessingStarted` |
| `on_processing_completed(callback)` | `ProcessingCompleted` |
| `on_processing_failed(callback)` | `ProcessingFailed` |

### Implemented vs. full backend contract

The methods above are what `FilamentClient` exposes today. The backend's complete
SignalR contract is declared as Protocol classes in
`filament/types/generated/api.py`:

- `ClientMethods` — every method the server accepts. In addition to the
  implemented ones, it declares `read_connected_data`, `preview_data`,
  `switch_user`, `set_permissions_roles`, `resubscribe`, and `get_status`, which
  are **not yet wrapped** by a dedicated client method.
- `ServerEvents` — every event the server pushes: `authentication_failed`,
  `processing_started`, `processing_completed`, `processing_failed`,
  `data_updated`, `keep_alive`. The client handles all of these internally and
  surfaces the relevant ones through the callback registration methods above.

## Top-level helpers

### `as_entity`

```python
filament.as_entity(entity: EntityBase, entity_type: Type[_E]) -> Optional[_E]
```

Returns `entity` narrowed to `entity_type` if it is an instance of it, otherwise
`None`. Use it to safely downcast the `EntityBase` values you get back from reads
and subscriptions into their concrete type:

```python
user = filament.as_entity(wrapped.entity, UserEntity)
if user is not None:
    print(user.user_name)
```

### `Condition`

The typed condition builder, documented in full in [Conditions](conditions.md).

### `SpecifiedPropertiesBase`

The base class that tracks which constructor arguments were explicitly supplied,
enabling partial updates. See
[Modifying Data](modifying-data.md#partial-updates-and-specified_properties).

## The response envelope

Every call returns a **success/error envelope** rather than raising on a rejected
request. There are two shapes.

### `ValueOrErrorPublic_1[T]`

Returned by calls that produce a value (`read_data`, `modify_data`, `subscribe`,
`get_entity_permissions`, `get_entity_property_permissions`).

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `success` | `bool` | Whether the call succeeded. |
| `message` | `str` | Human-readable detail (especially on failure). |
| `error_code` | `ErrorCodes` | Machine-readable error code; `None_` when successful. |
| `error_id` | `Optional[UUID]` | Correlation ID for logging/debugging. |
| `data` | `Optional[T]` | The payload, present on success. |

```python
result = await f.read_data(request)
if not result.success or result.data is None:
    print(f"Failed ({result.error_code}): {result.message}")
    return
use(result.data)
```

> Always check both `result.success` and `result.data is not None` before using
> the payload — `data` is typed `Optional`.

### `SuccessOrErrorPublic`

Returned by calls with no payload (`unsubscribe`, `unsubscribe_all`). Same as
above minus `data`:

| Field | Type |
| ----- | ---- |
| `success` | `bool` |
| `message` | `str` |
| `error_code` | `ErrorCodes` |
| `error_id` | `Optional[UUID]` |

## Exceptions

Defined in `filament._exceptions` and re-exported at the top level. These are
raised only for **connection/authentication** failures (when entering the context
manager) — request-level failures use the envelope above instead.

```text
FilamentError                     # base — catch this to handle any client error
├── FilamentAuthenticationError   # the server rejected the credentials
└── FilamentConnectionError       # the SignalR connection failed or timed out
```

```python
try:
    async with filament.FilamentClient(...) as f:
        ...
except filament.FilamentAuthenticationError:
    ...   # bad credentials
except filament.FilamentConnectionError:
    ...   # unreachable / timed out
except filament.FilamentError:
    ...   # any other client error
```

## See also

- [Getting Started](getting-started.md)
- [Finding Entity & Connection Types](finding-types.md)
- [Reading Data](reading-data.md)
- [Building Conditions](conditions.md)
- [Modifying Data](modifying-data.md)
- [Subscriptions](subscriptions.md)
- [Data Options](data-options.md)

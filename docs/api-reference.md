# API Reference

A method-by-method reference for `FilamentClient` and its blocking counterpart
`FilamentClientSync`, the response envelope every call returns, and the exception
hierarchy. For task-oriented walkthroughs, see
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

### Connecting and closing explicitly

#### `connect() -> None`

Open the connection; what entering `async with` does. Raises the same errors.
However a `connect()` fails — an error, the connection timeout, or the caller
being cancelled (for example by `asyncio.wait_for`) — nothing is left running, so
it needs no matching `close()`. Raises [`FilamentError`](#exceptions) if the
client is already connected.

#### `close() -> None`

Close the connection; what leaving `async with` does. Safe to call more than
once, and on a client that never connected. Never raises for a connection that
is already broken.

```python
f = filament.FilamentClient(...)
await f.connect()
try:
    ...
finally:
    await f.close()
```

### Methods

All methods are coroutines and must be `await`ed.

#### `read_data(request) -> ValueOrErrorPublic_1[ServiceDataRead]`

Read entity data. Takes a `ReadDataRequest`. See [Reading Data](reading-data.md).

#### `read_connected_data(request) -> ValueOrErrorPublic_1[ServiceDataReadConnected]`

Read entities through a connection type. User-defined values on both returned
sides are hydrated; automatic response-size paging is not applied.

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
class-name string). This map is also the authoritative discovery source for
[user-defined properties](user-defined-properties.md).

#### User-defined property methods

- `get_user_defined_properties(entity_type_name)` returns cached runtime property
  helpers keyed by Python `udp_snake_case` name.
- `invalidate_user_defined_property_cache(entity_type_name=None)` clears one or
  every definition map.
- `create_user_defined_property(request)` creates a definition.
- `update_user_defined_property(request)` edits the mutable definition fields.

See [User-defined Properties](user-defined-properties.md) for construction,
conversion, permissions, conditions, and cache behavior.

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
  implemented ones, it declares `preview_data`,
  `switch_user`, `set_permissions_roles`, `resubscribe`, and `get_status`, which
  are **not yet wrapped** by a dedicated client method.
- `ServerEvents` — every event the server pushes: `authentication_failed`,
  `processing_started`, `processing_completed`, `processing_failed`,
  `data_updated`, `keep_alive`. The client handles all of these internally and
  surfaces the relevant ones through the callback registration methods above.

## `FilamentClientSync`

```python
filament.FilamentClientSync(
    organization_id: str,
    api_key_name: str,
    api_key: str,
    server: str = "https://backend.filament-software.com",
    port: int = 443,
    path: str = "api/v0",
    connection_timeout: float = 10.0,
)
```

A blocking client for code that does not use `asyncio`. It takes the same
constructor parameters as `FilamentClient` and is used as an ordinary context
manager, or with `connect()` / `close()`:

```python
with filament.FilamentClientSync(...) as f:
    result = f.read_data(request)

f = filament.FilamentClientSync(...)
f.connect()
try:
    result = f.read_data(request)
finally:
    f.close()
```

### Methods

Every request method of `FilamentClient` is available under the same name, with
the same parameters and return type, as a blocking call: `read_data`,
`read_connected_data`, `modify_data`, `get_entity_permissions`,
`get_entity_property_permissions`, the user-defined property methods, the file
methods (`request_file_upload`, `refresh_file_upload_grant`,
`complete_file_upload`, `cancel_file_upload`, `get_file_urls`, `upload_file`,
`download_file`, `download_file_reference`) and the `server_capabilities`
property. They raise the same exceptions and return the same envelopes.

Differences from `FilamentClient`:

- **No subscriptions or callbacks.** `subscribe`, `unsubscribe`,
  `unsubscribe_all` and the `on_processing_*` registration methods do not exist
  on this class. Use `FilamentClient` for real-time updates.
- **No `http_client` parameter** on `upload_file`, `download_file` and
  `download_file_reference`. Each call uses its own short-lived HTTP client.
- **Calling a method while not connected** — before `connect()`, after `close()`
  — raises [`FilamentConnectionError`](#exceptions).

### Lifecycle

- `connect()` blocks until the connection is ready. It raises
  `FilamentConnectionError` or `FilamentAuthenticationError` as the async client
  does, and `FilamentError` if the client is already connected. A failed
  `connect()` leaves nothing running.
- `close()` is safe to call more than once, on a client that never connected,
  and from a different thread. Calls still in flight on other threads are
  cancelled and raise `FilamentConnectionError`.
- A closed client can be connected again.

### How it works, and what follows from it

While connected, the client runs one background thread that owns the SignalR
connection; each method call hands its request to that thread and waits for the
answer. The thread starts in `connect()` and is gone when `close()` returns.

- **Threads.** One client can be shared between threads. Calls made from
  different threads run concurrently on the one connection and may complete in
  any order, so coordinate calls that depend on each other.
- **Async code.** Calling it from inside `async def` works, but blocks that event
  loop for the duration of each call. Use `FilamentClient` there instead.
- **Forking.** Do not carry a connected client across `fork()` (for example
  `multiprocessing` with the fork start method). Connect in the child process.
- **Interrupting.** Ctrl+C during a call raises `KeyboardInterrupt` promptly and
  cancels that call.
- **Closing with calls in flight.** `close()` cancels them and waits up to five
  seconds for their cleanup — an interrupted `upload_file` releasing its
  reservation, for instance — before closing the connection regardless.

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

Returned by every call that produces a value: `read_data`, `read_connected_data`,
`modify_data`, `subscribe`, `get_entity_permissions`,
`get_entity_property_permissions`, `get_user_defined_properties`,
`create_user_defined_property` and `update_user_defined_property`.

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
manager or calling `connect()`, or when a call is made on a connection that has
gone) — request-level failures use the envelope above instead.

```text
FilamentError                     # base — catch this to handle any client error
├── FilamentAuthenticationError   # the server rejected the credentials
├── FilamentConnectionError       # the SignalR connection failed or timed out
└── CompressedPropertyError       # a compressed property value could not be decoded (also a ValueError)
```

`CompressedPropertyError` is raised during deserialization (for example by
`filament.from_dict`) when a field marked as compressed holds an unknown
`<compression>+<encoding>:` header or a payload that fails to decode. It carries
`owner`, `field_name` and `header` attributes. Client methods do not let it
escape: `read_data` and `read_connected_data` return a failed envelope with
`ErrorCodes.SerializationError` naming the field, and a subscription update
that cannot be decoded is dropped and the error passed to the subscription's
`on_error` callback (logged at WARNING if there is none). See
[Compressed properties](reading-data.md#compressed-properties).

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

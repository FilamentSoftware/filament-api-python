# Getting Started

This guide gets you from an empty environment to a working connection that reads
data from the Filament backend.

## Requirements

- Python 3.10, 3.11, 3.12, or 3.13
- [Poetry](https://python-poetry.org/) for dependency management

The client depends only on [`pysignalr`](https://pypi.org/project/pysignalr/)
(the SignalR transport) and [`uuid-utils`](https://pypi.org/project/uuid-utils/)
(fast UUIDv7 generation).

## Installation

```bash
poetry install
```

To also install the test and type-checking tool-chains:

```bash
poetry install --with test
```

## Importing the package

The public surface lives at the top level of the `filament` package:

```python
import filament

filament.FilamentClient          # the async client
filament.Condition               # the typed condition builder
filament.as_entity               # safe downcast helper
filament.FilamentError           # base exception
filament.FilamentAuthenticationError
filament.FilamentConnectionError
filament.SpecifiedPropertiesBase # base of all entities; tracks partial updates
```

Entity classes, enums, request/response types, conditions, and data options are
re-exported from `filament.types`:

```python
from filament.types import (
    UserEntity,             # an entity
    UserStatusEnum,         # an enum
    ReadDataRequest,        # a request type
    ReadDataTypeRequest,
    ServiceDataRead,        # a response payload
)
```

See [Finding Types](finding-types.md) for the full map of what lives where.

## Connecting

`FilamentClient` is an **async context manager**. Opening the context establishes
the SignalR connection and authenticates; leaving it tears the connection down
cleanly.

```python
import asyncio
import filament


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        # `f` is a connected client; use it here.
        ...


asyncio.run(main())
```

You must call the client from within a running event loop (`asyncio.run(...)`,
or an existing loop). Every API method is a coroutine and must be `await`ed.

### Constructor parameters

| Parameter | Required | Default | Meaning |
| --------- | -------- | ------- | ------- |
| `organization_id` | yes | — | The Filament organization to connect to. |
| `api_key_name` | yes | — | The name of the API key used for authentication. |
| `api_key` | yes | — | The secret value of the API key. |
| `server` | no | `https://backend.filament-software.com` | Base URL of the backend. |
| `port` | no | `443` | Port the backend listens on. |
| `path` | no | `api/v0` | URL path prefix for the SignalR hub endpoint. |
| `connection_timeout` | no | `10.0` | Seconds to wait for the initial connection before failing. |

The effective hub URL is `f"{server}:{port}/{path}"`. With the defaults that is
`https://backend.filament-software.com:443/api/v0`.

### Pointing at a different environment

```python
f = filament.FilamentClient(
    organization_id="your-organization-id",
    api_key_name="your-api-key-name",
    api_key="your-api-key",
    server="https://staging.filament-software.com",
    port=8443,
    path="api/v1",
    connection_timeout=30.0,
)
```

## Authentication

Authentication is credential-based and happens during the connection handshake.
The three credentials (`api_key_name`, `organization_id`, `api_key`) are combined
and sent to the backend as part of the connection; you do not call a separate
login method.

If the backend rejects the credentials, opening the context manager raises
[`FilamentAuthenticationError`](api-reference.md#exceptions). If the server
cannot be reached or the handshake times out, it raises
[`FilamentConnectionError`](api-reference.md#exceptions).

```python
import filament

try:
    async with filament.FilamentClient(
        organization_id="org", api_key_name="key", api_key="wrong-secret",
    ) as f:
        ...
except filament.FilamentAuthenticationError as exc:
    print(f"Bad credentials: {exc}")
except filament.FilamentConnectionError as exc:
    print(f"Could not connect: {exc}")
```

Both inherit from `filament.FilamentError`, so you can catch everything the client
raises with a single `except filament.FilamentError`.

## The two kinds of failure

It is important to distinguish **connection/auth failures** from **request
failures**:

- **Connection and authentication failures raise exceptions** (above). They occur
  when entering the `async with` block.
- **Request failures do not raise.** Every API method returns a result envelope
  ([`ValueOrErrorPublic_1`](api-reference.md#the-response-envelope) or
  `SuccessOrErrorPublic`) carrying a `success` flag, a human-readable `message`,
  and an `error_code`. Always check `result.success` before using `result.data`.

```python
result = await f.read_data(request)
if not result.success:
    print(f"Request failed ({result.error_code}): {result.message}")
    return
if result.data is None:
    print("No data returned")
    return
# safe to use result.data here
```

## A complete first program

```python
import asyncio

import filament
from filament.types import (
    ReadDataRequest,
    ReadDataTypeRequest,
    ServiceDataRead,
    UserEntity,
)


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        result = await f.read_data(
            ReadDataRequest(
                request=ReadDataTypeRequest(
                    type_name="UserEntity",
                    properties=["givenName", "familyName", "userName"],
                ),
            ),
        )

        if not result.success or result.data is None:
            print(f"Error: {result.message}")
            return

        data: ServiceDataRead = result.data
        for wrapped in data.data:
            user = filament.as_entity(wrapped.entity, UserEntity)
            if user is None:
                continue
            print(f"{user.given_name} {user.family_name} ({user.user_name})")


if __name__ == "__main__":
    asyncio.run(main())
```

## Next steps

- Learn how to discover the available types in [Finding Types](finding-types.md).
- Filter your reads with [Conditions](conditions.md).
- Write data with [Modifying Data](modifying-data.md).
- React to changes in real time with [Subscriptions](subscriptions.md).
</content>

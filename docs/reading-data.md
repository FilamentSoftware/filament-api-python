# Reading Data

Reading entities is done with `FilamentClient.read_data`, which takes a
`ReadDataRequest` and returns a `ValueOrErrorPublic_1[ServiceDataRead]`.

```python
result = await f.read_data(
    ReadDataRequest(
        request=ReadDataTypeRequest(type_name="UserEntity"),
    ),
)
```

## Request structure

A read is two nested objects:

- `ReadDataRequest` — the outer envelope. Fields:
  - `request: ReadDataTypeRequest` — the actual query (required).
  - `scenario_id: Optional[UUID]` — scope the read to a scenario (default `None`).
- `ReadDataTypeRequest` — the query itself.

### `ReadDataTypeRequest` fields

| Field | Type | Default | Meaning |
| ----- | ---- | ------- | ------- |
| `type_name` | `str` | `""` | The entity type to read, as a class-name string (e.g. `"UserEntity"`). |
| `condition` | `Optional[DbConditionInstance]` | `None` | Filter — see [Conditions](conditions.md). |
| `order` | `Optional[DbOrderInstance]` | `None` | Sort order — see [Ordering](#ordering). |
| `mode` | `ReadMode` | `ReadMode.Data` | `Data`, `Count`, or `Distinct`. |
| `limit` | `int` | `100000` | Maximum rows to return. |
| `offset` | `int` | `0` | Rows to skip (for paging). |
| `properties` | `Optional[list[str]]` | `None` | Projection: camelCase property names to include. Required when `mode=Distinct`. |
| `apply_pending_requests` | `bool` | `True` | Apply not-yet-processed modifications before reading. |

## A basic read

```python
import filament
from filament.types import (
    ReadDataRequest, ReadDataTypeRequest, ServiceDataRead, UserEntity,
)

result = await f.read_data(
    ReadDataRequest(request=ReadDataTypeRequest(type_name="UserEntity")),
)

if not result.success or result.data is None:
    print(result.message)
else:
    data: ServiceDataRead = result.data
    for wrapped in data.data:
        user = filament.as_entity(wrapped.entity, UserEntity)
        if user is not None:
            print(user.given_name, user.family_name)
```

## Parsing the result

`read_data` returns a [`ValueOrErrorPublic_1`](api-reference.md#the-response-envelope)
envelope. On success, `result.data` is a `ServiceDataRead`:

| `ServiceDataRead` field | Type | Meaning |
| ----------------------- | ---- | ------- |
| `type_name` | `str` | The type that was read. |
| `permissions` | `Optional[EntityTypePermissions]` | Your permissions on this type. |
| `data` | `list[WrappedEntity]` | The results. |
| `entity_count` | `int` | Total count (populated when `mode=Count`). |
| `more_data` | `bool` | `True` if more rows exist beyond `limit`. |

Each element of `data` is a `WrappedEntity` whose `.entity` is an `EntityBase`.
Use [`filament.as_entity`](api-reference.md#as_entity) to safely downcast it to
the concrete type:

```python
user = filament.as_entity(wrapped.entity, UserEntity)
if user is None:
    continue   # not a UserEntity (or wrong type) — skip
print(user.user_name)
```

`as_entity` returns `None` rather than raising if the entity isn't of the
requested type, which keeps loops clean and type-checkers happy (the result is
narrowed to `Optional[UserEntity]`).

## Filtering with conditions

Attach a condition to narrow the result set. See [Conditions](conditions.md) for
the full builder.

```python
from filament import Condition
from filament.types import UserStatusEnum

condition = (
    Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
    & Condition(UserEntity).prop("given_name").like("A%")
)

result = await f.read_data(
    ReadDataRequest(
        request=ReadDataTypeRequest(type_name="UserEntity", condition=condition),
    ),
)
```

## Projections: reading only some fields

Pass `properties` (camelCase names) to fetch only the columns you need. This
reduces payload size:

```python
ReadDataTypeRequest(
    type_name="UserEntity",
    properties=["givenName", "familyName", "userName", "emailAddress"],
)
```

Unprojected fields come back as their dataclass defaults, so check
`properties` matches what you actually read.

## Ordering

Sorting is described by a `DbOrderInstance`, which holds an ordered list of
`DbOrderLevel`s (each a property + direction). Order is applied level by level.

```python
from filament.types import (
    DbOrderInstance, DbOrderLevel, DbOrderDirection,
)

order = DbOrderInstance(levels=[
    DbOrderLevel(property="familyName", direction=DbOrderDirection.Ascending),
    DbOrderLevel(property="givenName", direction=DbOrderDirection.Ascending),
])

ReadDataTypeRequest(type_name="UserEntity", order=order)
```

`property` is the camelCase wire name; `direction` is
`DbOrderDirection.Ascending` (default) or `Descending`.

## Paging

Combine `limit` and `offset`, and watch `more_data` to know when to stop:

```python
async def read_all(f, type_name, page=500):
    offset = 0
    while True:
        result = await f.read_data(
            ReadDataRequest(request=ReadDataTypeRequest(
                type_name=type_name, limit=page, offset=offset,
            )),
        )
        if not result.success or result.data is None:
            raise RuntimeError(result.message)
        yield from result.data.data
        if not result.data.more_data:
            break
        offset += page
```

## Counting and distinct values

`ReadMode` controls what the read returns:

- `ReadMode.Data` (default) — the entity rows in `data`.
- `ReadMode.Count` — just the total, in `entity_count` (`data` is empty).
- `ReadMode.Distinct` — distinct values of the projected `properties`
  (`properties` is **required** in this mode).

```python
from filament.types import ReadMode

# How many enabled users are there?
result = await f.read_data(
    ReadDataRequest(request=ReadDataTypeRequest(
        type_name="UserEntity",
        mode=ReadMode.Count,
        condition=Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled),
    )),
)
if result.success and result.data is not None:
    print("count:", result.data.entity_count)
```

## Pending requests

By default (`apply_pending_requests=True`), a read reflects modifications that
have been submitted but not yet fully processed by the backend, so you see your
own writes immediately. Set it to `False` to read only committed data.

## Reading connected data

The backend also exposes a *read-connected* operation that walks a connection
type to fetch related entities, described by `ReadConnectedDataRequest` /
`ReadConnectedDataTypeRequest` and returning `ServiceDataReadConnected`. These
request/response types are available in `filament.types`.

> **Note:** the current `FilamentClient` exposes `read_data`, `modify_data`,
> `subscribe`, `unsubscribe`, `unsubscribe_all`, `get_entity_permissions`, and
> `get_entity_property_permissions`. The backend's full SignalR contract (in
> `types/generated/api.py`) additionally defines `read_connected_data`,
> `preview_data`, `switch_user`, `set_permissions_roles`, `resubscribe`, and
> `get_status`; these are part of the wire protocol but are not yet wrapped by a
> dedicated client method. See the [API Reference](api-reference.md) for the
> methods that are implemented today.

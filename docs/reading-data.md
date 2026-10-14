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

## Compressed properties

Some large string properties are stored compressed by the backend (they carry
`compression` / `encoding` metadata on the generated dataclass field, e.g.
`ScheduleSegmentEntity.reasons_data`). Such a value is either plain text or
`<compression>+<encoding>:<payload>`, e.g. `br+base64:...` (Brotli) or
`gzip+base64:...` (gzip).

The client decodes these automatically for every read and subscription update,
so you always see plain text:

- The value's header decides how it is decoded; values with no header (such as
  rows written before compression was introduced) are returned unchanged.
- Only fields marked with the metadata are decoded; other fields are never
  touched, even if their text looks like a header.
- An unknown header, or a payload that fails to decode (invalid base64, a
  truncated stream, trailing bytes, ...), is an error rather than a raw value:
  `read_data` and `read_connected_data` return `success=False` with
  `ErrorCodes.SerializationError` and a message naming the field, a
  subscription update is dropped and the `filament.CompressedPropertyError` is
  passed to the subscription's `on_error` callback, and `filament.from_dict`
  raises it.
- Writes are not re-compressed: sending an entity back sends the plain text,
  which the backend stores as valid uncompressed text.

## Reading connected data

The backend also exposes a *read-connected* operation that walks a connection
type to fetch related entities, described by `ReadConnectedDataRequest` /
`ReadConnectedDataTypeRequest` and returning `ServiceDataReadConnected`. These
request/response types are available in `filament.types`.

`FilamentClient.read_connected_data()` implements this operation. It also loads
and applies [user-defined property](user-defined-properties.md) definitions for
both the connection and entity type before returning the result. Connected
reads do not currently use the automatic response-size paging provided by
`read_data`.

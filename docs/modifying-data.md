# Modifying Data

Creating, updating, and deleting entities all go through a single method,
`FilamentClient.modify_data`, which takes a `ModifyDataRequest` and returns a
`ValueOrErrorPublic_1[RequestId]`.

```python
result = await f.modify_data(
    ModifyDataRequest(action=DataRequestAction.Create, data={"UserEntity": [user]}),
)
```

The kind of operation is selected by the `action` field.

## `ModifyDataRequest` fields

| Field | Type | Default | Meaning |
| ----- | ---- | ------- | ------- |
| `action` | `DataRequestAction` | `Unknown` | `Create`, `Update`, or `Delete`. |
| `data` | `dict[str, list[EntityBase]]` | `{}` | Entities to modify, keyed by type-name string. |
| `update_properties` | `Optional[list[str]]` | `None` | For `Update`: the camelCase property names to write. |
| `scenario_id` | `UUID` | zero-UUID | Scenario to scope the change to. |
| `user_id` | `Optional[UUID]` | `None` | Acting user. |
| `requested_at` | `datetime` | now | Request timestamp. |
| `request_id` | `RequestId` | auto | Client-generated request identifier. |

The `data` dict groups entities by type-name string, so a single request can
touch several types:

```python
data={"UserEntity": [user_a, user_b], "TaskEntity": [task]}
```

On success, `result.data` is a `RequestId` identifying the submitted operation.
Processing is asynchronous on the backend — listen for
[processing-lifecycle events](subscriptions.md#processing-lifecycle-events) or a
[subscription](subscriptions.md) to learn when it completes.

## Creating

Construct the entity with the fields you want, then send it with
`DataRequestAction.Create`. You usually don't set `id` — it defaults to a fresh
UUIDv7.

```python
import filament
from filament.types import (
    DataRequestAction, ModifyDataRequest, UserEntity,
    UserStatusEnum, UserTypeEnum,
)

user = UserEntity(
    given_name="Jane",
    family_name="Smith",
    user_name="jsmith",
    email_address="jane.smith@example.com",
    schedule_status=UserStatusEnum.Enabled,
    user_type=UserTypeEnum.Internal,
)

result = await f.modify_data(
    ModifyDataRequest(action=DataRequestAction.Create, data={"UserEntity": [user]}),
)
if result.success:
    print(f"Created user (request id: {result.data})")
else:
    print(f"Error: {result.message}")
```

## Updating

Updates are **partial**. Construct an entity carrying only the `id` plus the
fields you want to change, set `action=Update`, and list those fields (camelCase)
in `update_properties`:

```python
from filament.types import DataRequestAction, ModifyDataRequest, UserEntity, UserStatusEnum

updated = UserEntity(id=existing_id, schedule_status=UserStatusEnum.Disabled)

result = await f.modify_data(
    ModifyDataRequest(
        action=DataRequestAction.Update,
        update_properties=["scheduleStatus"],
        data={"UserEntity": [updated]},
    ),
)
```

A typical update is *read, then write*: read the entity to find its `id`, then
build a minimal update entity. See [`examples/update_user.py`](../examples/update_user.py).

## Deleting

Delete by sending an entity that carries just its `id`:

```python
from filament.types import DataRequestAction, ModifyDataRequest, UserEntity

result = await f.modify_data(
    ModifyDataRequest(
        action=DataRequestAction.Delete,
        data={"UserEntity": [UserEntity(id=existing_id)]},
    ),
)
```

See [`examples/delete_user.py`](../examples/delete_user.py) for the full
find-then-delete flow.

## Partial updates and `specified_properties`

Why does sending a minimal entity work, when the dataclass has dozens of fields
with defaults? Because every entity extends `SpecifiedPropertiesBase`, which
records **exactly which keyword arguments you passed to the constructor**. The
serializer then sends only those fields (plus `id`, which is always included so
the server can identify the row).

```python
UserEntity(id=existing_id, schedule_status=UserStatusEnum.Disabled)
# specified_properties == ["id", "schedule_status"]
# → only id and scheduleStatus are serialized; every other field is omitted.
```

This is what makes partial updates safe: a field you *don't* mention is left
untouched on the server rather than being overwritten with a default.

Two practical consequences:

- **`update_properties` and the constructed entity should agree.** List in
  `update_properties` the same fields you actually set on the entity (in
  camelCase). The example above sets `schedule_status` and lists
  `"scheduleStatus"`.
- **To explicitly set a field to its default value** (e.g. set a string to `""`),
  you still pass it to the constructor — passing it is what marks it as
  specified.

See [Finding Types](finding-types.md#the-base-class-hierarchy) for where
`SpecifiedPropertiesBase` sits in the hierarchy.

Tenant-specific properties use the same partial-update bookkeeping. See
[User-defined Properties](user-defined-properties.md#create-or-update-entities)
for the constructor-only `_user_defined_properties` input and typed conversion.

## `DataRequestAction` values

| Value | Use |
| ----- | --- |
| `DataRequestAction.Create` | Insert new entities. |
| `DataRequestAction.Update` | Modify existing entities (with `update_properties`). |
| `DataRequestAction.Delete` | Remove entities (id only). |
| `DataRequestAction.Unknown` | Default/invalid — do not send. |

## Error handling

As with all calls, `modify_data` does not raise on a rejected request — check the
envelope:

```python
result = await f.modify_data(request)
if not result.success:
    print(f"Modify failed ({result.error_code}): {result.message}")
```

See the [API Reference](api-reference.md#the-response-envelope) for the envelope
fields.

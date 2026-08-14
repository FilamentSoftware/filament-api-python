# User-defined properties

User-defined properties (UDPs) extend generated entity and connection types at
runtime without weakening the types of their built-in fields. The client reads
their definitions from the authenticated user's property-permission map, caches
those definitions per type, and uses them to convert values on reads, writes,
connected reads, and subscription updates.

## Naming

One property has three equivalent spellings:

| Location | Example |
| --- | --- |
| Python attribute, constructor input, projection, or update list | `udp_client_reference` |
| Entity JSON sent to or received from the hub | `udpClientReference` |
| Definition name reported by the permission map | `UdpClientReference` or the backend's canonical casing |

The reserved `udp_` Python prefix is significant. Mypy treats only unknown
entity attributes beginning with that prefix as `Any`; built-in attributes keep
their generated types, and any other unknown attribute is an error.

## Discover definitions and build conditions

```python
properties_result = await client.get_user_defined_properties("TaskEntity")
if not properties_result.success or properties_result.data is None:
    raise RuntimeError(properties_result.message)

priority = properties_result.data["udp_priority"]
print(priority.label, priority.description, priority.actions)
condition = priority.condition().equal_to("High")
```

Each `UserDefinedProperty` includes its definition, effective property permission,
declared and sub-property types, and a definition-aware condition factory.
`Condition(TaskEntity).prop(...)` intentionally remains limited to generated
fields because a tenant-specific UDP cannot have a static Python type.

The permission map is scoped to the authenticated organisation and user,
including an authenticated super administrator. A definition absent from that
map must not be assumed readable or writable.

## Read values

Use `read_data` normally. Returned UDPs are flat attributes on the concrete
entity and are converted to the same Python runtime types used by generated
properties: `UUID`, `datetime`, `timedelta`, `Decimal`, primitives, and
single-level lists or dictionaries. Dictionary keys are converted to their
declared type.

```python
result = await client.read_data(request)
task = result.data.data[0].entity
print(task.udp_client_reference)
```

`read_connected_data` performs the same conversion independently for the
connection and entity sides. Before a public subscription is registered, the
client loads that type's definitions, so callbacks also receive converted
attributes.

Definitions are cached per client and type. The client privately subscribes to
`UserDefinedPropertyEntity` for cached types using the empty scenario ID, and
refreshes the authoritative permission map immediately when an accessible
definition changes. If entity data arrives with an unknown `udp_*` key, the
client refreshes once; a read fails, or a complete push update is logged and
dropped, if the key remains unknown. Call
`await client.invalidate_user_defined_property_cache(type_name)` to force a
later reload after an out-of-band change; omit `type_name` to clear all maps.

## Create or update entities

Pass several UDP values through the inherited, constructor-only
`_user_defined_properties` input:

```python
task = TaskEntity(
    name="Follow up",
    _user_defined_properties={
        "udp_client_reference": "REF-42",
        "udp_priority": "High",
    },
)
```

The mapping is validated, flattened into normal entity attributes, and then
discarded. Reading or assigning that constructor-only name raises
`AttributeError`; it is not a property bag or a wire field. Each individual
UDP is recorded in `specified_properties`, so partial updates preserve the same
omitted-versus-specified behavior as generated fields. A genuine UDP named
`udp_properties` does not collide with the constructor input.

You may also assign an individual value before calling `modify_data`:

```python
task.udp_priority = "Low"
```

An omitted UDP is untouched. Explicit `None` is serialized and requests a null
clear for a nullable definition. Projection and `update_properties` lists may
use Python `udp_snake_case`; normal property-name conversion emits camel case.
The client loads every involved type's definitions before serializing a
`ModifyData` request, and does not invoke the write if discovery fails.

## Create and edit definitions

Administrators can call `create_user_defined_property(request)` and
`update_user_defined_property(request)`. Successful creation refreshes the
target type. Successful update clears all cached type maps because the current
generated update request and response do not identify the owning type.

Names, declared types, nullability, and collection shape are immutable after
creation. `UpdateUserDefinedPropertyRequest` uses `None` to mean “leave this
value unchanged”, so this contract cannot clear an existing description or
default value. Deletion, renaming, and type migration are not exposed.

Lists and arrays have one scalar subtype. Dictionaries have scalar key and
value subtypes; keys are limited to string, enum, integer, or GUID. Nested
collections and null collection members are rejected before a write. The hub
remains authoritative for validation constraints, permissions, and supported
query operators.

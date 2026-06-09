# Building Conditions

Conditions are the filters you attach to reads and subscriptions. The client
offers two ways to build them:

- The **typed builder** — `Condition(EntityClass).prop("field")…` — which
  validates field names and value types at runtime *and* (with the mypy plugin)
  at type-check time. **This is the recommended API.**
- The **untyped constructors** — `equal_to(...)`, `and_(...)`, … — for quick,
  reference-free use where you supply wire (camelCase) names and string values
  yourself.

Both produce the same `DbConditionInstance` objects, which serialize to the
backend's condition wire format. The hierarchy mirrors the C# backend's
`DbConditionInstance` model.

## The typed builder

Start from `Condition(EntityClass)`, pick a field with `.prop("snake_case_name")`,
then call an operator:

```python
from filament import Condition
from filament.types import UserEntity, UserStatusEnum

condition = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
```

`.prop(name)` does two things:

1. **Validates** that `name` is a real field on the entity (raises
   `AttributeError` otherwise).
2. **Classifies** the field — comparable scalar, list, dict, or "base" — and
   returns a property wrapper that only exposes the operators valid for that
   kind of field.

The snake_case name you pass is converted to the camelCase wire name
automatically (`schedule_status` → `scheduleStatus`).

## Operators by field kind

### Any field — `DbConditionProperty`

Every field supports these:

| Method | Meaning | Example |
| ------ | ------- | ------- |
| `.equal_to(value)` | `EqualTo` | `prop("user_name").equal_to("jsmith")` |
| `.in_(values)` | membership in a list (`In`) | `prop("user_name").in_(["a", "b"])` |
| `.like(pattern)` | SQL-style `LIKE` | `prop("given_name").like("A%")` |
| `.null()` | `IS NULL` | `prop("middle_names").null()` |
| `.not_null()` | `IS NOT NULL` | `prop("middle_names").not_null()` |

### Comparable fields — `DbConditionComparableProperty`

Fields typed as `int`, `float`, `str`, `bool`, `UUID`, `datetime`, or `timedelta`
additionally support ordering and ranges:

| Method | Meaning |
| ------ | ------- |
| `.less_than(value)` | `LessThan` |
| `.less_than_or_equal_to(value)` | `LessThanOrEqualTo` |
| `.greater_than(value)` | `GreaterThan` |
| `.greater_than_or_equal_to(value)` | `GreaterThanOrEqualTo` |
| `.between(min, max, *, inclusive_min=True, inclusive_max=True)` | `Between` range with configurable inclusivity |

```python
from datetime import datetime, timezone
from filament import Condition
from filament.types import TaskEntity

# names alphabetically before "M"
Condition(TaskEntity).prop("name").less_than("M")

# a half-open date range: created_at in [start, end)
start = datetime(2025, 1, 1, tzinfo=timezone.utc)
end = datetime(2025, 2, 1, tzinfo=timezone.utc)
Condition(TaskEntity).prop("created_at").between(start, end, inclusive_max=False)
```

### List fields — `DbConditionListProperty`

Fields typed as `list[...]` add set operations (in addition to the common
operators above):

| Method | Meaning |
| ------ | ------- |
| `.contains(value)` | the list contains `value` (`Contains`) |
| `.intersects(values)` | the list shares any element with `values` (`Intersects`) |
| `.subset_of(values)` | every element is in `values` (`SubsetOf`) |
| `.superset_of(values)` | the list contains all of `values` (`SupersetOf`) |

### Dict fields — `DbConditionDictProperty`

Fields typed as `dict[K, V]` return a dict wrapper with three accessors that let
you condition on keys, values, or a specific key's value:

| Accessor | Returns | Conditions on |
| -------- | ------- | ------------- |
| `.key(k)` | a property wrapper (mode `DictionaryValue`) | the value stored at key `k` |
| `.keys()` | a list wrapper (mode `DictionaryKeys`) | the set of keys |
| `.values()` | a list wrapper (mode `DictionaryValues`) | the set of values |

```python
# the value at dict key "priority" equals "high"
Condition(SomeEntity).prop("metadata").key("priority").equal_to("high")

# the dict has a key "owner"
Condition(SomeEntity).prop("metadata").keys().contains("owner")

# any stored value is "draft"
Condition(SomeEntity).prop("metadata").values().contains("draft")
```

## Value conversion

Whatever Python value you pass to an operator is converted to the string wire
format the backend expects:

| Python type | Wire form |
| ----------- | --------- |
| `str` | as-is |
| `bool` | `"True"` / `"False"` |
| `int`, `float` | `str(value)` |
| `Enum` | the enum's `.value` |
| `UUID` | `str(value)` |
| `datetime` | formatted with the Filament instant format |
| `timedelta` | total seconds |
| `None` | null (used by `.null()` / `.not_null()`) |

Passing an unsupported type raises `TypeError`. For the `in_`/`intersects`/… list
operators, pass an actual sequence (not a bare string) — a scalar `str` raises
`TypeError` to catch the common `in_("abc")`-meant-as-three-chars mistake.

## Combining conditions

There are two equivalent ways to combine conditions.

### Operators (recommended for readability)

`DbConditionInstance` overloads `&` (AND), `|` (OR), and `~` (NOT):

```python
from filament import Condition
from filament.types import UserEntity, UserStatusEnum

condition = (
    Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
    & ~Condition(UserEntity).prop("given_name").null()
)
```

Precedence follows Python's own operator rules: `~` binds tighter than `&`, which
binds tighter than `|`. Use parentheses when mixing `&` and `|` to be explicit:

```python
a & (b | c)        # a AND (b OR c)
(a & b) | c        # (a AND b) OR c
```

### Static methods

`Condition.and_`, `Condition.or_`, and `Condition.not_` take any number of
conditions and read well when nesting many at once:

```python
condition = Condition.and_(
    Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled),
    Condition(UserEntity).prop("given_name").like("%Alice%"),
)
```

### Negation: `~`, `.not_`, and per-operator negatives

Three ways to negate, in increasing locality:

```python
# 1. Negate a whole condition.
~Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)

# 2. Negate at the property level (everything this wrapper produces is inverted).
Condition(UserEntity).prop("schedule_status").not_.equal_to(UserStatusEnum.Enabled)

# 3. Built-in opposites for null checks.
Condition(UserEntity).prop("middle_names").not_null()   # same as ~(... .null())
```

`.not_` returns a fresh, inverted wrapper, so it chains naturally with any
operator (`.not_.in_(...)`, `.not_.like(...)`, etc.). Applying `.not_` twice
cancels out.

## Static validation with the mypy plugin

The package ships a mypy plugin (`filament_mypy_plugin`) that narrows
`Condition.prop()`'s return type based on the entity's field type. With it
enabled, mypy will:

- flag `prop("does_not_exist")` as an error;
- only offer comparable operators (`less_than`, `between`, …) on comparable
  fields, list operators on list fields, and so on;
- check that the value you pass to `equal_to`/`in_`/… matches the field's type
  (e.g. a `UserStatusEnum` field rejects a plain `str`).

Enable it in your project's `pyproject.toml`:

```toml
[tool.mypy]
plugins = ["filament_mypy_plugin"]
```

> Inside this repository the plugin is referenced by path
> (`plugins = ["src/filament_mypy_plugin.py"]`) because the source isn't
> installed as a package during development. In a consuming project that
> installs `filament-api`, use the module name as shown above.

Runtime validation happens regardless of mypy: `.prop()` always checks that the
field exists, so a typo raises `AttributeError` even without type-checking.

## Link (join) conditions

`DbConditionLink` filters one entity type by a condition on a *connected* entity.
Build it with the `link` constructor from `filament.types.conditions`:

```python
from filament.types.conditions import link, equal_to

# Read tasks linked (via SomeConnection's "toId") to a user whose status is "1".
condition = link(
    prop="id",                       # property on the primary entity
    link_type="SomeConnection",      # the connection type name
    link_property="toId",            # property on the connection to join through
    link_condition=equal_to("scheduleStatus", "1"),  # condition on the linked entity
)
```

`link_condition` is itself a `DbConditionInstance`, so it can be any condition —
including nested combinations or further links.

## The untyped API

When you don't have (or don't want) an entity class reference, build conditions
directly from `filament.types.conditions`. You supply **camelCase** property names
and **string** values yourself, and there is no field validation:

```python
from filament.types.conditions import and_, equal_to, like, in_, between

condition = and_(
    equal_to("scheduleStatus", "1"),
    like("givenName", "%Alice%"),
)
```

Every operator has an untyped constructor:

`and_`, `or_`, `not_`, `equal_to`, `less_than`, `less_than_or_equal_to`,
`greater_than`, `greater_than_or_equal_to`, `like`, `between`, `in_`, `contains`,
`intersects`, `subset_of`, `superset_of`, `link`.

The scalar comparison constructors accept `mode` and `key` keyword arguments for
conditioning on dictionary fields directly:

```python
from filament.types.conditions import equal_to, DbConditionMode

equal_to("metadata", "high", mode=DbConditionMode.DictionaryValue, key="priority")
```

`DbConditionMode` values are `Property` (the default), `DictionaryValue`,
`DictionaryKeys`, and `DictionaryValues`.

## Using a condition

A built condition goes into the `condition` field of a read request or
subscription config:

```python
from filament.types import ReadDataRequest, ReadDataTypeRequest

await f.read_data(
    ReadDataRequest(
        request=ReadDataTypeRequest(type_name="UserEntity", condition=condition),
    ),
)
```

See [Reading Data](reading-data.md) and [Subscriptions](subscriptions.md) for the
surrounding request shapes.
</content>

# Data Options

`DataOption` is a typed key-value container used to pass per-type options to the
backend. Each option carries a `value_type` discriminator (matching the backend's
`DataOptionType` enum) and a `value` that may be a scalar, a list, or `null`. It
mirrors the C# backend's `DataOption` / `DataOptionConverter` wire format.

Options are keyed by entity type name, e.g. on the backend's
`PreviewDataRequest.options` (`dict[str, DataOption]`).

```python
from filament.types.options import string_option, int_option, bool_option

options = {
    "mode": string_option("preview"),
    "limit": int_option(100),
    "includeDeleted": bool_option(False),
}
```

## Convenience constructors

Rather than building `DataOption` by hand (and remembering the right
`value_type`), use a constructor:

| Constructor | Value type | Python type |
| ----------- | ---------- | ----------- |
| `string_option(value)` | String | `str` |
| `bool_option(value)` | Bool | `bool` |
| `int_option(value)` | Int | `int` |
| `double_option(value)` | Double | `float` |
| `decimal_option(value)` | Decimal | `decimal.Decimal` |
| `guid_option(value)` | Guid | `uuid_utils.UUID` |
| `instant_option(value)` | Instant | `datetime.datetime` |
| `duration_option(value)` | Duration | `datetime.timedelta` |
| `null_option()` | Null | — |

```python
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid_utils
from filament.types.options import (
    guid_option, instant_option, duration_option, decimal_option, null_option,
)

guid_option(uuid_utils.uuid7())
instant_option(datetime(2025, 1, 1, tzinfo=timezone.utc))
duration_option(timedelta(hours=2))
decimal_option(Decimal("19.99"))
null_option()
```

## The `DataOption` dataclass

If you need full control, construct `DataOption` directly. `value_type` is an
integer matching the generated `DataOptionType` enum:

```python
from filament.types import DataOptionType
from filament.types.options import DataOption

DataOption(value_type=DataOptionType.String.value, value="hello")
```

The constructors above are the recommended path — they set the correct
`value_type` for you.

## Scalar vs array types

`DataOptionType` defines both scalar types (String, Bool, Int, Double, Decimal,
Guid, Instant, Duration, and several NodaTime types) and their array
counterparts. The serializer recognises array value-types and converts each
element with the matching scalar rules, so an array option's `value` is a Python
list:

```python
from filament.types import DataOptionType
from filament.types.options import DataOption

DataOption(value_type=DataOptionType.StringArray.value, value=["a", "b", "c"])
```

## Serialization helpers

Two functions convert between `DataOption` and its wire dict, should you need to
do so explicitly:

```python
from filament.types.options import option_to_dict, option_from_dict

wire = option_to_dict(string_option("hello"))
# {"valueType": <int>, "value": "hello"}

option = option_from_dict(wire)
# DataOption(value_type=<int>, value="hello")
```

`option_to_dict` emits `{"valueType": <int>, "value": <scalar|array|null>}`;
`option_from_dict` reverses it, decoding GUIDs, instants (to UTC-aware
`datetime`), durations (to `timedelta`), and decimals (to `Decimal`) back into
Python types.

## Type-conversion notes

- **Decimal** is sent as a JSON number (coerced to `float` for the encoder) and
  parsed back into a `Decimal` on the way in.
- **Instant** values are emitted in Filament's instant wire format and parsed
  back as **UTC-aware** `datetime` objects.
- **NodaTime types without a direct Python equivalent** (LocalDateTime,
  LocalDate, LocalTime, Interval, DateTimeZone) round-trip as strings.
</content>

"""DataOption type for typed key-value options in service data updates.

Mirrors the C# backend's ``DataOption`` / ``DataOptionConverter`` wire format.
Each option carries a ``value_type`` discriminator (int matching
``DataOptionType`` enum values) and a ``value`` that is either a scalar,
a list, or null.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Union

import uuid_utils

from filament._wire_formats import (
    instant_from_wire,
    instant_to_wire,
    timedelta_from_wire,
    timedelta_to_wire,
)

__all__ = [
    "DataOption",
    "option_to_dict",
    "option_from_dict",
    # Convenience constructors
    "string_option",
    "bool_option",
    "int_option",
    "double_option",
    "decimal_option",
    "guid_option",
    "instant_option",
    "duration_option",
    "null_option",
]


# ---------------------------------------------------------------------------
# DataOptionType integer constants (mirrors the generated DataOptionType enum)
# ---------------------------------------------------------------------------

_VT_INVALID = 0
_VT_NULL = 1
_VT_STRING = 2
_VT_BOOL = 3
_VT_INT = 4
_VT_DOUBLE = 5
_VT_DECIMAL = 6
_VT_GUID = 7
_VT_INSTANT = 8
_VT_DURATION = 9
_VT_INTERVAL = 10
_VT_LOCAL_DATE_TIME = 11
_VT_LOCAL_DATE = 12
_VT_LOCAL_TIME = 13
_VT_DATE_TIME_ZONE = 14
_VT_STRING_ARRAY = 15
_VT_BOOL_ARRAY = 16
_VT_INT_ARRAY = 17
_VT_DOUBLE_ARRAY = 18
_VT_DECIMAL_ARRAY = 19
_VT_GUID_ARRAY = 20
_VT_INSTANT_ARRAY = 21
_VT_DURATION_ARRAY = 22
_VT_INTERVAL_ARRAY = 23
_VT_LOCAL_DATE_TIME_ARRAY = 24
_VT_LOCAL_DATE_ARRAY = 25
_VT_LOCAL_TIME_ARRAY = 26
_VT_DATE_TIME_ZONE_ARRAY = 27

_SCALAR_TYPES: frozenset[int] = frozenset(range(2, 15))  # 2..14

_ARRAY_TYPES: frozenset[int] = frozenset(range(15, 28))  # 15..27

_ARRAY_TO_SCALAR: dict[int, int] = {
    arr: arr - 13 for arr in range(15, 28)
}

_SCALAR_TO_ARRAY: dict[int, int] = {
    v: k for k, v in _ARRAY_TO_SCALAR.items()
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class DataOption:
    """A typed key-value option sent over the wire.

    ``value_type`` holds an integer matching ``DataOptionType`` enum values
    (use the generated ``DataOptionType`` enum or convenience constructors).
    ``value`` is a scalar, list, or ``None``.
    """

    value_type: int = _VT_NULL
    value: Any = field(default=None)


# ---------------------------------------------------------------------------
# Value conversion helpers
# ---------------------------------------------------------------------------

def _scalar_to_wire(value: Any, vt: int) -> Any:
    """Convert a single Python value to its wire representation."""
    if value is None:
        return None
    if vt == _VT_BOOL:
        return bool(value)
    if vt == _VT_INT:
        return int(value)
    if vt == _VT_DOUBLE:
        return float(value)
    if vt == _VT_DECIMAL:
        # C# wire format is a JSON number; coerce to float so the surrounding
        # JSON encoder serializes it numerically.
        return float(value)
    if vt == _VT_GUID:
        return str(value)
    if vt == _VT_INSTANT:
        if isinstance(value, datetime.datetime):
            return instant_to_wire(value)
        return str(value) if not isinstance(value, str) else value
    if vt == _VT_DURATION:
        if isinstance(value, datetime.timedelta):
            return timedelta_to_wire(value)
        return value
    if vt in (_VT_LOCAL_DATE_TIME, _VT_LOCAL_DATE, _VT_LOCAL_TIME):
        if isinstance(value, datetime.datetime):
            return value.isoformat()
        return str(value)
    if vt in (_VT_INTERVAL, _VT_DATE_TIME_ZONE):
        return str(value)
    # String and fallback
    return str(value) if not isinstance(value, str) else value


def _scalar_from_wire(value: Any, vt: int) -> Any:
    """Convert a wire value back to a Python value."""
    if value is None:
        return None
    if vt == _VT_BOOL:
        return bool(value)
    if vt == _VT_INT:
        return int(value)
    if vt == _VT_DOUBLE:
        return float(value)
    if vt == _VT_DECIMAL:
        return Decimal(str(value))
    if vt == _VT_GUID:
        if isinstance(value, uuid_utils.UUID):
            return value
        return uuid_utils.UUID(str(value))
    if vt == _VT_INSTANT:
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, str):
            return instant_from_wire(value)
        return value
    if vt == _VT_DURATION:
        if isinstance(value, datetime.timedelta):
            return value
        if isinstance(value, str):
            return timedelta_from_wire(value)
        return value
    if vt in (_VT_LOCAL_DATE_TIME, _VT_LOCAL_DATE, _VT_LOCAL_TIME,
              _VT_INTERVAL, _VT_DATE_TIME_ZONE):
        # NodaTime types without direct Python equivalents — keep as string.
        return str(value) if not isinstance(value, str) else value
    # String and fallback
    return str(value) if not isinstance(value, str) else value


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def option_to_dict(option: DataOption) -> dict[str, Any]:
    """Serialize a ``DataOption`` to a wire-format dict.

    Wire format: ``{"valueType": <int>, "value": <value-or-array-or-null>}``
    """
    vt = option.value_type

    if vt == _VT_NULL or vt == _VT_INVALID:
        return {"valueType": vt, "value": None}

    if vt in _SCALAR_TYPES:
        return {
            "valueType": vt,
            "value": _scalar_to_wire(option.value, vt),
        }

    if vt in _ARRAY_TYPES:
        scalar_vt = _ARRAY_TO_SCALAR.get(vt, _VT_STRING)
        values = option.value if isinstance(option.value, list) else []
        return {
            "valueType": vt,
            "value": [_scalar_to_wire(v, scalar_vt) for v in values],
        }

    # Unknown type — serialize as-is
    return {"valueType": vt, "value": option.value}


def option_from_dict(data: dict[str, Any]) -> DataOption:
    """Deserialize a wire-format dict to a ``DataOption``."""
    raw_vt = data.get("valueType", 0)
    try:
        vt = int(raw_vt)
    except (ValueError, TypeError):
        vt = _VT_INVALID

    raw_value = data.get("value")

    if vt == _VT_NULL or vt == _VT_INVALID:
        return DataOption(value_type=vt, value=None)

    if vt in _SCALAR_TYPES:
        return DataOption(
            value_type=vt,
            value=_scalar_from_wire(raw_value, vt),
        )

    if vt in _ARRAY_TYPES:
        scalar_vt = _ARRAY_TO_SCALAR.get(vt, _VT_STRING)
        if isinstance(raw_value, list):
            converted = [_scalar_from_wire(v, scalar_vt) for v in raw_value]
        else:
            converted = []
        return DataOption(value_type=vt, value=converted)

    return DataOption(value_type=vt, value=raw_value)


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def null_option() -> DataOption:
    """Create a null ``DataOption``."""
    return DataOption(value_type=_VT_NULL, value=None)


def string_option(value: str) -> DataOption:
    """Create a string ``DataOption``."""
    return DataOption(value_type=_VT_STRING, value=value)


def bool_option(value: bool) -> DataOption:
    """Create a bool ``DataOption``."""
    return DataOption(value_type=_VT_BOOL, value=value)


def int_option(value: int) -> DataOption:
    """Create an int ``DataOption``."""
    return DataOption(value_type=_VT_INT, value=value)


def double_option(value: float) -> DataOption:
    """Create a double/float ``DataOption``."""
    return DataOption(value_type=_VT_DOUBLE, value=value)


def decimal_option(value: Decimal) -> DataOption:
    """Create a decimal ``DataOption``."""
    return DataOption(value_type=_VT_DECIMAL, value=value)


def guid_option(value: uuid_utils.UUID) -> DataOption:
    """Create a GUID ``DataOption``."""
    return DataOption(value_type=_VT_GUID, value=value)


def instant_option(value: datetime.datetime) -> DataOption:
    """Create an Instant ``DataOption`` (maps to datetime)."""
    return DataOption(value_type=_VT_INSTANT, value=value)


def duration_option(value: datetime.timedelta) -> DataOption:
    """Create a Duration ``DataOption`` (maps to timedelta)."""
    return DataOption(value_type=_VT_DURATION, value=value)

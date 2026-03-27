"""Dataclass <-> dict serialization for SignalR wire format.

Adapted from the Filament integration tests' dataclass_utils.py.
Key differences from the original:
- Handles ``from __future__ import annotations`` (string annotations) via
  ``typing.get_type_hints``.
- Uses ``uuid_utils.UUID`` instead of ``uuid.UUID``.
- Maps the ``None_`` enum member name (generated code convention) instead of
  ``NoneValue`` (integration test convention).
"""
from __future__ import annotations

import datetime
import re
import sys
from collections.abc import Sequence as ABCSequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Optional, TypeVar, Type, Union, get_origin, get_args

import uuid_utils

from filament.types.generated.base.base import EntityBase

T = TypeVar("T")

DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f00Z"


# ---------------------------------------------------------------------------
# Case conversion helpers
# ---------------------------------------------------------------------------

def camel_case_from_snake_case(s: str) -> str:
    """Convert ``snake_case`` to ``camelCase``."""
    return s[0].lower() + "".join(word.title() for word in s.split("_"))[1:]


def snake_case_from_camel_case(s: str) -> str:
    """Convert ``camelCase`` to ``snake_case``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


# ---------------------------------------------------------------------------
# Internal type helpers
# ---------------------------------------------------------------------------

def _is_optional(tp: Any) -> bool:
    return get_origin(tp) is Union and type(None) in get_args(tp)


def _unwrap_optional(tp: Any) -> Any:
    return next(a for a in get_args(tp) if a is not type(None))


def _is_uuid_type(tp: Any) -> bool:
    try:
        return tp is uuid_utils.UUID
    except TypeError:
        return False


def _is_enum_type(tp: Any) -> bool:
    try:
        return isinstance(tp, type) and issubclass(tp, Enum)
    except TypeError:
        return False


def _is_dataclass_type(tp: Any) -> bool:
    try:
        return isinstance(tp, type) and is_dataclass(tp)
    except TypeError:
        return False


def _is_list_like(tp: Any) -> bool:
    origin = get_origin(tp)
    return origin is list or origin is ABCSequence


def _is_dict_like(tp: Any) -> bool:
    return get_origin(tp) is dict


def _resolve_type_hints(cls: Any) -> dict[str, Any]:
    """Resolve string annotations to real types for a dataclass class."""
    try:
        mod = sys.modules.get(cls.__module__, None)
        globalns = vars(mod) if mod else None
        return _get_type_hints_safe(cls, globalns=globalns)
    except Exception:
        # Fall back to raw field.type (will be strings if __future__ annotations
        # is used, but at least we don't crash).
        return {}


def _get_type_hints_safe(
    cls: Any,
    globalns: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Wrapper around typing.get_type_hints that handles common failures."""
    import typing
    return typing.get_type_hints(cls, globalns=globalns)


# ---------------------------------------------------------------------------
# Enum value helpers
# ---------------------------------------------------------------------------

_NONE_MEMBER = "None_"
_NONE_WIRE = "None"


def _enum_to_wire(data: Any) -> Any:
    """Convert an enum member to its wire representation (int value)."""
    if hasattr(data, "name") and data.name == _NONE_MEMBER:
        return data.value
    return data.value


def _enum_from_wire(data: Any, enum_type: Any) -> Any:
    """Convert a wire value to an enum member."""
    if isinstance(data, str):
        if data == _NONE_WIRE:
            return enum_type[_NONE_MEMBER]
        return enum_type[data]
    if isinstance(data, Enum):
        return data
    return enum_type(data)


# ---------------------------------------------------------------------------
# Value conversion (to dict)
# ---------------------------------------------------------------------------

def _convert_to_dict_value(
    data: Any,
    field_type: Any,
    generic_type_arg: Any,
    convert_uuid: bool,
) -> Any:
    # Optional[X] -> unwrap
    if _is_optional(field_type):
        inner = _unwrap_optional(field_type)
        if isinstance(inner, TypeVar):
            inner = generic_type_arg
        if data is None:
            return None
        return _convert_to_dict_value(data, inner, None, convert_uuid)

    # TypeVar -> use the resolved generic arg
    if isinstance(field_type, TypeVar):
        if generic_type_arg is not None:
            return _convert_to_dict_value(data, generic_type_arg, None, convert_uuid)
        return data

    # Dataclass -> recurse
    if _is_dataclass_type(field_type):
        return to_dict(data, convert_uuid)

    # list[X]
    if _is_list_like(field_type):
        args = get_args(field_type)
        if args:
            return [
                _convert_to_dict_value(item, args[0], generic_type_arg, convert_uuid)
                for item in data
            ]
        return list(data)

    # dict[K, V]
    if _is_dict_like(field_type):
        args = get_args(field_type)
        if args and len(args) == 2:
            return {
                _convert_to_dict_value(k, args[0], generic_type_arg, convert_uuid): _convert_to_dict_value(v, args[1], generic_type_arg, convert_uuid)
                for k, v in data.items()
            }
        return dict(data)

    # Enum
    if _is_enum_type(field_type):
        return _enum_to_wire(data) if isinstance(data, Enum) else data

    # UUID
    if _is_uuid_type(field_type) and convert_uuid:
        return str(data)

    # datetime
    if field_type is datetime.datetime:
        if isinstance(data, datetime.datetime):
            return data.strftime(DATETIME_FORMAT)
        return data

    return data


# ---------------------------------------------------------------------------
# Value conversion (from dict)
# ---------------------------------------------------------------------------

def _convert_from_dict_value(
    data: Any,
    field_type: Any,
    generic_type_arg: Any,
) -> Any:
    # Optional[X] -> unwrap
    if _is_optional(field_type):
        inner = _unwrap_optional(field_type)
        if isinstance(inner, TypeVar):
            inner = generic_type_arg
        if data is None:
            return None
        return _convert_from_dict_value(data, inner, None)

    # TypeVar -> use the resolved generic arg
    if isinstance(field_type, TypeVar):
        if generic_type_arg is not None:
            return _convert_from_dict_value(data, generic_type_arg, None)
        return data

    # Dataclass -> recurse
    if _is_dataclass_type(field_type):
        if isinstance(data, dict):
            return from_dict(data, field_type)
        return data

    # list[X]
    if _is_list_like(field_type):
        args = get_args(field_type)
        if args and isinstance(data, (list, tuple)):
            return [
                _convert_from_dict_value(item, args[0], generic_type_arg)
                for item in data
            ]
        if isinstance(data, (list, tuple)):
            return list(data)
        return data

    # dict[K, V]
    if _is_dict_like(field_type):
        args = get_args(field_type)
        if args and len(args) == 2 and isinstance(data, dict):
            return {
                _convert_from_dict_value(k, args[0], generic_type_arg): _convert_from_dict_value(v, args[1], generic_type_arg)
                for k, v in data.items()
            }
        return data

    # Enum
    if _is_enum_type(field_type):
        return _enum_from_wire(data, field_type)

    # UUID
    if _is_uuid_type(field_type):
        if isinstance(data, uuid_utils.UUID):
            return data
        if isinstance(data, str):
            return uuid_utils.UUID(data)
        return data

    # datetime
    if field_type is datetime.datetime:
        if isinstance(data, datetime.datetime):
            return data
        if isinstance(data, str):
            return datetime.datetime.strptime(data, DATETIME_FORMAT)
        return data

    # bool (must be checked before int since bool is a subclass of int)
    if field_type is bool:
        return bool(data)

    # int / float / str — coerce if needed
    if field_type in (int, float, str):
        if isinstance(data, field_type):
            return data
        return field_type(data)

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def to_dict(data: Any, convert_uuid: bool = True) -> Optional[dict[str, Any]]:
    """Convert a dataclass instance to a camelCase dict for the SignalR wire format.

    Returns ``None`` if *data* is not a dataclass instance.
    """
    if not is_dataclass(data) or isinstance(data, type):
        return None

    cls = type(data)
    hints = _resolve_type_hints(cls)
    dc_fields = fields(data)

    # Check for specified_properties filtering
    specified_properties: Optional[list[str]] = None
    if hasattr(data, "specified_properties"):
        specified_properties = getattr(data, "specified_properties")

    result: dict[str, Any] = {}
    for f in dc_fields:
        if f.name == "specified_properties" or f.metadata.get("ignore", False):
            continue

        if specified_properties is not None and f.name not in specified_properties:
            continue

        field_type = hints.get(f.name, f.type)
        camel_key = camel_case_from_snake_case(f.name)
        value = getattr(data, f.name)
        result[camel_key] = _convert_to_dict_value(value, field_type, None, convert_uuid)

    if isinstance(data, EntityBase):
        result["$type"] = data.__class__.__name__

    return result


def from_dict(data: dict[str, Any], cls: Type[T]) -> Optional[T]:
    """Create a dataclass instance of *cls* from a camelCase dict.

    Supports parameterised generics with a single type argument, e.g.
    ``from_dict(d, ValueOrErrorPublic_1[SubscriptionId])``.

    Returns ``None`` if *cls* is not a dataclass.
    """
    type_arg: Any = None
    concrete_cls: Any = cls

    # Handle parameterised generics: ValueOrError_1[SubscriptionId] etc.
    origin = get_origin(cls)
    if origin is not None:
        args = get_args(cls)
        if args:
            type_arg = args[0]
        concrete_cls = origin

    if not is_dataclass(concrete_cls):
        return None

    # concrete_cls is verified to be a dataclass class at this point.
    hints: dict[str, Any] = _resolve_type_hints(concrete_cls)
    dc_fields = fields(concrete_cls)

    dataclass_data: dict[str, Any] = {}
    for f in dc_fields:
        if f.metadata.get("ignore", False):
            continue

        camel_key = camel_case_from_snake_case(f.name)
        if camel_key not in data and f.name not in data:
            continue

        raw_value = data.get(camel_key, data.get(f.name))
        field_type = hints.get(f.name, f.type)
        dataclass_data[f.name] = _convert_from_dict_value(raw_value, field_type, type_arg)

    try:
        result: T = concrete_cls(**dataclass_data)  # type: ignore[operator,assignment]
        return result
    except TypeError:
        raise

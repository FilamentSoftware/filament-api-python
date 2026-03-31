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
import types
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
    if not s:
        return s
    return s[0].lower() + "".join(word.title() for word in s.split("_"))[1:]


def snake_case_from_camel_case(s: str) -> str:
    """Convert ``camelCase`` to ``snake_case``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


# ---------------------------------------------------------------------------
# Internal type helpers
# ---------------------------------------------------------------------------

def _is_optional(tp: Any) -> bool:
    """Return ``True`` if *tp* is ``Optional[X]`` (i.e. ``Union[X, None]``)."""
    origin = get_origin(tp)
    if origin is Union:
        return type(None) in get_args(tp)
    if isinstance(tp, types.UnionType):
        return type(None) in tp.__args__
    return False


def _unwrap_optional(tp: Any) -> Any:
    """Extract the inner type from ``Optional[X]``."""
    return next(a for a in get_args(tp) if a is not type(None))


def _is_uuid_type(tp: Any) -> bool:
    """Return ``True`` if *tp* is ``uuid_utils.UUID``."""
    try:
        return tp is uuid_utils.UUID
    except TypeError:
        return False


def _is_enum_type(tp: Any) -> bool:
    """Return ``True`` if *tp* is an :class:`~enum.Enum` subclass."""
    try:
        return isinstance(tp, type) and issubclass(tp, Enum)
    except TypeError:
        return False


def _is_dataclass_type(tp: Any) -> bool:
    """Return ``True`` if *tp* is a dataclass class (not an instance)."""
    try:
        return isinstance(tp, type) and is_dataclass(tp)
    except TypeError:
        return False


def _is_condition_type(tp: Any) -> bool:
    """Check if *tp* is ``DbConditionInstance`` or a subclass of it."""
    try:
        from filament.types.conditions import DbConditionInstance
        return isinstance(tp, type) and issubclass(tp, DbConditionInstance)
    except (TypeError, ImportError):
        return False


def _is_data_option_type(tp: Any) -> bool:
    """Check if *tp* is ``DataOption``."""
    try:
        from filament.types.options import DataOption
        return isinstance(tp, type) and issubclass(tp, DataOption)
    except (TypeError, ImportError):
        return False


def _resolve_entity_type(type_name: str) -> Optional[type]:
    """Look up a concrete EntityBase subclass by name from $type tag."""
    from filament.types.generated.base.base import EntityBase

    def _walk(cls: type) -> Optional[type]:
        for sub in cls.__subclasses__():
            if sub.__name__ == type_name:
                return sub
            found = _walk(sub)
            if found is not None:
                return found
        return None

    return _walk(EntityBase)


def _is_list_like(tp: Any) -> bool:
    """Return ``True`` if *tp* is ``list[X]`` or ``Sequence[X]``."""
    origin = get_origin(tp)
    return origin is list or origin is ABCSequence


def _is_dict_like(tp: Any) -> bool:
    """Return ``True`` if *tp* is ``dict[K, V]``."""
    return get_origin(tp) is dict


def _resolve_type_hints(cls: Any) -> dict[str, Any]:
    """Resolve string annotations to real types for a dataclass class.

    Does NOT pass ``globalns`` so that Python resolves each base class's
    annotations using its own module globals (required when the MRO spans
    multiple modules with different imports).
    """
    import typing
    try:
        return typing.get_type_hints(cls)
    except Exception:
        # Fall back to raw field.type (will be strings if __future__ annotations
        # is used, but at least we don't crash).
        return {}


# ---------------------------------------------------------------------------
# Enum value helpers
# ---------------------------------------------------------------------------

_NONE_MEMBER = "None_"
_NONE_WIRE = "None"


def _enum_to_wire(data: Any) -> Any:
    """Convert an enum member to its wire representation (int value)."""
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
    """Recursively convert a Python value to its wire-format representation.

    Handles ``Optional``, ``TypeVar``, dataclasses, lists, dicts, enums,
    UUIDs, and datetimes.
    """
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

    # DbConditionInstance -> custom polymorphic serialization
    if _is_condition_type(field_type):
        from filament.types.conditions import condition_to_dict
        return condition_to_dict(data)

    # DataOption -> custom serialization
    if _is_data_option_type(field_type):
        from filament.types.options import option_to_dict
        return option_to_dict(data)

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
    """Recursively convert a wire-format value back to its Python representation.

    Handles ``Optional``, ``TypeVar``, dataclasses (with ``$type`` dispatch),
    lists, dicts, enums, UUIDs, datetimes, and primitive coercion.
    """
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

    # DbConditionInstance -> custom polymorphic deserialization
    if _is_condition_type(field_type):
        if isinstance(data, dict):
            from filament.types.conditions import condition_from_dict
            return condition_from_dict(data)
        return data

    # DataOption -> custom deserialization
    if _is_data_option_type(field_type):
        if isinstance(data, dict):
            from filament.types.options import option_from_dict
            return option_from_dict(data)
        return data

    # Dataclass -> recurse
    if _is_dataclass_type(field_type):
        if isinstance(data, dict):
            concrete: Any = field_type
            if (
                "$type" in data
                and isinstance(field_type, type)
                and issubclass(field_type, EntityBase)
            ):
                resolved = _resolve_entity_type(data["$type"])
                if resolved is not None and issubclass(resolved, field_type):
                    concrete = resolved
            return from_dict(data, concrete)
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
    specified_properties: Optional[list[str]] = getattr(data, "specified_properties", None)

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

    # EntityBase $type dispatch: resolve concrete subclass if available
    if "$type" in data and isinstance(concrete_cls, type) and issubclass(concrete_cls, EntityBase):
        resolved = _resolve_entity_type(data["$type"])
        if resolved is not None and issubclass(resolved, concrete_cls):
            concrete_cls = resolved

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

    result: T = concrete_cls(**dataclass_data)
    return result

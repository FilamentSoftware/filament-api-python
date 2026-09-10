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
import json
import logging
import re
import types
from collections.abc import Sequence as ABCSequence
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Mapping, Optional, TypeVar, Type, Union, get_origin, get_args

import uuid_utils

from .types.generated.base.base import EntityBase

if TYPE_CHECKING:
    from .user_defined_properties import UserDefinedProperty

T = TypeVar("T")

# Re-exported so external callers can keep importing from filament._serialization.
# Explicit ``X as X`` aliases satisfy mypy strict's no-implicit-reexport rule.
from ._wire_formats import (  # noqa: E402
    DATETIME_FORMAT as DATETIME_FORMAT,
    instant_from_wire as instant_from_wire,
    instant_to_wire as instant_to_wire,
    timedelta_from_wire as timedelta_from_wire,
    timedelta_to_wire as timedelta_to_wire,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Case conversion helpers
# ---------------------------------------------------------------------------

def camel_case_from_snake_case(s: str) -> str:
    """Convert ``snake_case`` to ``camelCase``.

    Empty segments produced by leading, trailing, or doubled underscores are
    dropped, so ``"_foo_bar"`` and ``"foo__bar"`` both become ``"fooBar"``.
    """
    words = [word for word in s.split("_") if word]
    if not words:
        return ""
    return words[0].lower() + "".join(word.title() for word in words[1:])


def snake_case_from_camel_case(s: str) -> str:
    """Convert ``camelCase`` to ``snake_case``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def user_defined_python_name(name: str) -> Optional[str]:
    """Return the canonical ``udp_snake`` name for a reserved wire name."""
    if len(name) <= 3 or name[:3].lower() != "udp":
        return None
    suffix = snake_case_from_camel_case(name[3:]).lstrip("_")
    return f"udp_{suffix}" if suffix else None


def camel_case_wire_name(name: str) -> str:
    """Return the camelCase wire form of a single property name.

    Names already in wire form (i.e. contain no underscore) are passed through
    unchanged: ``camel_case_from_snake_case`` is not idempotent on camelCase
    input (it lowercases interior capitals, e.g. ``"scheduleStatus"`` would
    become ``"schedulestatus"``), so passing such names through keeps callers
    that already use the wire form working.
    """
    return camel_case_from_snake_case(name) if "_" in name else name


def camel_case_property_names(names: ABCSequence[str]) -> list[str]:
    """Normalise a list of entity property names to the camelCase wire form.

    Each name is converted from ``snake_case`` to ``camelCase`` so callers can
    use the same names as the Python dataclass fields. Names that are already
    in wire form (i.e. contain no underscore) are passed through unchanged, so
    existing callers passing camelCase names keep working. Duplicates — whether
    present in the input or produced by conversion — are removed while
    preserving first-seen order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        wire = camel_case_wire_name(name)
        if wire not in seen:
            seen.add(wire)
            result.append(wire)
    return result


# Fields whose value is a list of entity property names. When such a field holds
# a list, ``to_dict`` converts each name from snake_case to camelCase (and
# de-duplicates) so callers can use the same names as the Python dataclass fields
# rather than the camelCase wire names. Keyed by field name because the generated
# dataclasses carry no metadata to mark these fields. Conversion applies wherever
# a list-valued field with one of these names appears — including the entity
# field ``PermissionEntity.properties``, whose property names are normalised the
# same way. The ``isinstance(value, list)`` guard in ``to_dict`` skips same-named
# dict fields (e.g. the permission ``properties`` dicts on ``EntityTypePermissions``
# / ``EntityPropertyPermissions``), which are not property-name lists.
_PROPERTY_NAME_LIST_FIELDS: frozenset[str] = frozenset({
    "properties",
    "update_properties",
    "connection_properties",
    "entity_properties",
})


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
    return next((a for a in get_args(tp) if a is not type(None)), tp)


def _union_members(tp: Any) -> Optional[list[Any]]:
    """Return the non-``None`` members of a union, or ``None`` if *tp* isn't one."""
    if get_origin(tp) is Union or isinstance(tp, types.UnionType):
        return [a for a in get_args(tp) if a is not type(None)]
    return None


def _matching_union_member(data: Any, members: list[Any]) -> Any:
    """Pick the arm *data* is an instance of, falling back to the first arm.

    A union of entities can use any arm, because ``to_dict`` re-dispatches on
    the value's runtime type. A mixed union cannot: sending a ``str`` down the
    dataclass arm of ``Union[SomeEntity, str]`` serializes it to ``None`` and
    loses the value.
    """
    for member in members:
        # A parameterized generic (``list[str]``) is not a valid isinstance
        # target, and is still an instance of ``type`` before Python 3.11, so
        # screen it out rather than relying on the isinstance check alone.
        if not isinstance(member, type) or get_origin(member) is not None:
            continue
        try:
            if isinstance(data, member):
                return member
        except TypeError:
            continue
    return members[0]


def _select_union_member(data: Any, members: list[Any]) -> Any:
    """Pick the union member matching *data*'s ``$type`` tag.

    Backend navigation properties such as ``reference`` are typed as a union of
    the concrete entities they may point at. The payload names its own type, so
    use that to choose the arm; fall back to the first arm when there is no
    usable tag.

    Raise ``ValueError`` when the payload does name a type but it cannot be
    placed among the declared arms. Guessing an arm there would build one
    entity out of another entity's data and hand the caller a plausible-looking
    object whose fields silently defaulted to ``None``.
    """
    if not isinstance(data, dict):
        return members[0]

    type_name = data.get("$type")
    if not isinstance(type_name, str):
        return members[0]

    # ``$type`` dispatch only applies to unions of entities.
    entity_members = [
        m for m in members if isinstance(m, type) and issubclass(m, EntityBase)
    ]
    if not entity_members:
        return members[0]

    resolved = _resolve_entity_type(type_name)
    if resolved is None:
        raise ValueError(f"Unknown entity type in $type tag: {type_name!r}")

    for member in entity_members:
        if issubclass(resolved, member):
            return member

    raise ValueError(
        f"$type {type_name!r} is not one of the declared union members: "
        f"{[m.__name__ for m in entity_members]}"
    )


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
        from .types.conditions import DbConditionInstance
        return isinstance(tp, type) and issubclass(tp, DbConditionInstance)
    except (TypeError, ImportError):
        return False


def _is_data_option_type(tp: Any) -> bool:
    """Check if *tp* is ``DataOption``."""
    try:
        from .types.options import DataOption
        return isinstance(tp, type) and issubclass(tp, DataOption)
    except (TypeError, ImportError):
        return False


@lru_cache(maxsize=None)
def _resolve_entity_type(type_name: str) -> Optional[type]:
    """Look up a concrete EntityBase subclass by name from $type tag.

    Cached: this runs per union-typed field while deserializing every
    ``DataUpdated`` push, on the event loop. The entity hierarchy is fixed once
    ``types.generated.entities`` is imported, so the walk cannot go stale.
    """
    from .types.generated.base.base import EntityBase

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
    except (NameError, AttributeError):
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
    # Optional[X] / Union[X, Y] -> unwrap
    #
    # No ``$type`` dispatch is needed in this direction: the dataclass branch
    # below calls ``to_dict(data, ...)``, which serializes by the value's
    # runtime type. But the arm still has to be the right *kind* -- routing a
    # non-dataclass value down a dataclass arm would serialize it to ``None``
    # -- so match on the runtime type. A union left intact matches no branch
    # below and would fall through, leaking the raw object.
    members = _union_members(field_type)
    if members:
        if data is None:
            return None
        inner = _matching_union_member(data, members)
        if isinstance(inner, TypeVar):
            inner = generic_type_arg
        return _convert_to_dict_value(data, inner, None, convert_uuid)

    # TypeVar -> use the resolved generic arg, or infer from runtime value
    if isinstance(field_type, TypeVar):
        if generic_type_arg is not None:
            return _convert_to_dict_value(data, generic_type_arg, None, convert_uuid)
        return _convert_to_dict_value(data, type(data), None, convert_uuid)

    # DbConditionInstance -> custom polymorphic serialization
    if _is_condition_type(field_type):
        from .types.conditions import condition_to_dict
        return condition_to_dict(data)

    # DataOption -> custom serialization
    if _is_data_option_type(field_type):
        from .types.options import option_to_dict
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

    # datetime (Instant)
    if field_type is datetime.datetime:
        if isinstance(data, datetime.datetime):
            return instant_to_wire(data)
        return data

    # timedelta (Duration)
    if field_type is datetime.timedelta:
        if isinstance(data, datetime.timedelta):
            return timedelta_to_wire(data)
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
    # Optional[X] / Union[X, Y] -> unwrap, choosing the arm the payload names
    members = _union_members(field_type)
    if members:
        if data is None:
            return None
        inner = members[0] if len(members) == 1 else _select_union_member(data, members)
        if isinstance(inner, TypeVar):
            inner = generic_type_arg
        return _convert_from_dict_value(data, inner, None)

    # TypeVar -> use the resolved generic arg
    if isinstance(field_type, TypeVar):
        if generic_type_arg is not None:
            return _convert_from_dict_value(data, generic_type_arg, None)
        return data

    # DbConditionInstance -> custom polymorphic deserialization
    if _is_condition_type(field_type):
        if isinstance(data, dict):
            from .types.conditions import condition_from_dict
            return condition_from_dict(data)
        return data

    # DataOption -> custom deserialization
    if _is_data_option_type(field_type):
        if isinstance(data, dict):
            from .types.options import option_from_dict
            return option_from_dict(data)
        return data

    # Dataclass -> recurse (also handles parameterised generics like
    # UpdatedEntityServiceData_1[EntityBase])
    origin = get_origin(field_type)
    if _is_dataclass_type(field_type) or (origin is not None and _is_dataclass_type(origin)):
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

    # datetime (Instant)
    if field_type is datetime.datetime:
        if isinstance(data, datetime.datetime):
            return data
        if isinstance(data, str):
            return instant_from_wire(data)
        return data

    # timedelta (Duration)
    if field_type is datetime.timedelta:
        if isinstance(data, datetime.timedelta):
            return data
        if isinstance(data, str):
            return timedelta_from_wire(data)
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

def to_dict(
    data: Any,
    convert_uuid: bool = True,
    *,
    user_defined_properties: Optional[Mapping[str, UserDefinedProperty]] = None,
) -> Optional[dict[str, Any]]:
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
        converted = _convert_to_dict_value(value, field_type, None, convert_uuid)
        # Property-name conversion is write-only: these fields are request
        # inputs sent to the backend, never read back, so there is deliberately
        # no from_dict counterpart.
        if f.name in _PROPERTY_NAME_LIST_FIELDS and isinstance(converted, list):
            converted = camel_case_property_names(converted)
        result[camel_key] = converted

    if isinstance(data, EntityBase):
        for name, value in vars(data).items():
            if not name.startswith("udp_"):
                continue
            if specified_properties is not None and name not in specified_properties:
                continue
            udp_property = (
                user_defined_properties.get(name) if user_defined_properties else None
            )
            if udp_property is None and user_defined_properties is not None:
                # The caller supplied an authoritative definition map (modify_data
                # does for Create/Update), so an unmatched name is a typo rather
                # than a property we simply have no metadata for.  Guessing a wire
                # name here would send the raw Python value: Decimal, UUID,
                # datetime and timedelta then fail in the JSON encoder with an
                # error that never names the offending property.
                raise ValueError(
                    f"{name!r} is not a known user-defined property of "
                    f"{data.__class__.__name__}"
                )
            wire_name = (
                udp_property.wire_name
                if udp_property is not None
                else camel_case_from_snake_case(name)
            )
            if wire_name in result:
                continue
            result[wire_name] = (
                udp_property.adapter.to_wire(value)
                if udp_property is not None
                else value
            )
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
        if f.metadata.get("ignore", False) or not f.init:
            continue

        camel_key = camel_case_from_snake_case(f.name)
        if camel_key not in data and f.name not in data:
            # Default missing Optional fields to None so dataclasses
            # without explicit defaults still construct correctly. Skip
            # fields that define their own default / default_factory so we
            # don't clobber it — let the dataclass apply its own value.
            field_type = hints.get(f.name, f.type)
            has_default = f.default is not MISSING or f.default_factory is not MISSING
            if (
                f.name not in dataclass_data
                and _is_optional(field_type)
                and not has_default
            ):
                dataclass_data[f.name] = None
            continue

        raw_value = data.get(camel_key, data.get(f.name))
        field_type = hints.get(f.name, f.type)
        dataclass_data[f.name] = _convert_from_dict_value(raw_value, field_type, type_arg)

    result: T = concrete_cls(**dataclass_data)
    if isinstance(result, EntityBase):
        known_keys = {
            camel_case_from_snake_case(f.name)
            for f in dc_fields
            if not f.metadata.get("ignore", False)
        }
        known_keys.update(f.name for f in dc_fields)
        for key, value in data.items():
            if key in known_keys or key == "$type":
                continue
            python_name = user_defined_python_name(key)
            if python_name is not None:
                object.__setattr__(result, python_name, value)
    # Clear specified_properties for deserialized entities so all fields serialize.
    if hasattr(result, "specified_properties"):
        object.__setattr__(result, "specified_properties", None)
    _populate_json_companion_fields(result, concrete_cls, hints)
    return result


def _populate_json_companion_fields(
    instance: Any, cls: Any, hints: dict[str, Any],
) -> None:
    """Populate object fields from their JSON-string ``<name>_data`` companions.

    The backend emits certain read projections as a JSON string in a
    ``<name>_data`` field alongside a matching object field ``<name>`` that
    "arrives unset" — e.g. ``UserEntity.image_data`` (a serialized
    :class:`FileReference`) and ``UserEntity.image``
    (``Optional[FileReference]``).  Here we deserialize ``<name>_data`` into
    ``<name>`` so callers get the ready object without a manual parse.

    A field pair qualifies when ``<name>`` is (optionally) a dataclass type and a
    sibling ``<name>_data`` field exists.  A missing, empty, non-string, or
    malformed ``<name>_data`` leaves ``<name>`` as ``None``; an explicitly set
    ``<name>`` is never overwritten.
    """
    field_names = {f.name for f in fields(cls)}
    for f in fields(cls):
        data_name = f"{f.name}_data"
        if data_name not in field_names:
            continue

        field_type = hints.get(f.name, f.type)
        inner = _unwrap_optional(field_type) if _is_optional(field_type) else field_type
        if not (isinstance(inner, type) and is_dataclass(inner)):
            continue

        # The backend sends the object field unset; never clobber a real value.
        if getattr(instance, f.name, None) is not None:
            continue
        raw = getattr(instance, data_name, None)
        if not isinstance(raw, str) or not raw:
            continue

        try:
            decoded = json.loads(raw)
        except ValueError:
            logger.warning(
                "Failed to parse %s.%s from its %s JSON projection (invalid JSON)",
                getattr(cls, "__name__", cls), f.name, data_name,
            )
            continue
        # Only a JSON object maps onto the dataclass; an array or scalar is a
        # malformed projection and must not produce a bogus default instance.
        if not isinstance(decoded, dict):
            logger.warning(
                "Ignoring %s.%s: its %s projection is not a JSON object",
                getattr(cls, "__name__", cls), f.name, data_name,
            )
            continue
        try:
            parsed = from_dict(decoded, inner)
        except (ValueError, TypeError, KeyError):
            logger.warning(
                "Failed to deserialize %s.%s from its %s JSON projection",
                getattr(cls, "__name__", cls), f.name, data_name,
            )
            continue
        if parsed is not None:
            object.__setattr__(instance, f.name, parsed)

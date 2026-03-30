"""Typed condition builder for entity queries.

Provides a ``Condition(EntityClass).prop("field")`` API that validates
property names and value types at both runtime and mypy-time (via the
companion ``_mypy_plugin``).
"""
from __future__ import annotations

import datetime
import sys
import types
from collections.abc import Sequence
from dataclasses import fields as dc_fields
from enum import Enum
from typing import Any, Generic, Optional, Type, TypeVar, get_args, get_origin, Union

import uuid_utils

from filament._serialization import DATETIME_FORMAT, camel_case_from_snake_case
from filament.types.conditions import (
    DbConditionArrayComparison,
    DbConditionBetween,
    DbConditionCombine,
    DbConditionComparison,
    DbConditionComparisonArray,
    DbConditionContains,
    DbConditionInstance,
    DbConditionMode,
    DbConditionNot,
    DbConditionType,
    and_,
    not_,
    or_,
)

__all__ = [
    "Condition",
    "DbConditionProperty",
    "DbConditionComparableProperty",
    "DbConditionListProperty",
    "DbConditionDictProperty",
]

T = TypeVar("T")
TKey = TypeVar("TKey")
TValue = TypeVar("TValue")
TEntity = TypeVar("TEntity")


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------

_COMPARABLE_TYPES: frozenset[type[Any]] = frozenset(
    {int, float, str, bool, uuid_utils.UUID, datetime.datetime, datetime.timedelta}
)


def _value_to_wire(value: Any) -> Optional[str]:
    """Convert a typed Python value to the string wire format."""
    if value is None:
        return None
    # bool before int (bool is subclass of int)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, uuid_utils.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.strftime(DATETIME_FORMAT)
    if isinstance(value, datetime.timedelta):
        return str(value.total_seconds())
    return str(value)


def _values_to_wire(values: Sequence[Any]) -> list[Optional[str]]:
    """Convert a sequence of typed values to wire format."""
    return [_value_to_wire(v) for v in values]


# ---------------------------------------------------------------------------
# Runtime field classification
# ---------------------------------------------------------------------------

def _resolve_type_hints(cls: type[Any]) -> dict[str, Any]:
    """Resolve string annotations to real types for a class.

    Does NOT pass ``globalns`` so that Python resolves each base class's
    annotations using its own module globals (required when the MRO spans
    multiple modules with different imports).
    """
    import typing

    try:
        return typing.get_type_hints(cls)
    except Exception:
        return {}


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


def _classify_field(
    entity_cls: type[Any], field_name: str
) -> tuple[str, Any]:
    """Classify a field and return ``(category, inner_type)``.

    Categories: ``"comparable"``, ``"list"``, ``"dict"``, ``"base"``.
    Raises ``AttributeError`` if the field does not exist.
    """
    # Validate field exists
    known = {f.name for f in dc_fields(entity_cls)}
    if field_name not in known:
        raise AttributeError(
            f"{entity_cls.__name__!r} has no field {field_name!r}"
        )

    hints = _resolve_type_hints(entity_cls)
    tp = hints.get(field_name)
    if tp is None:
        return ("base", Any)

    # Unwrap Optional
    if _is_optional(tp):
        tp = _unwrap_optional(tp)

    # list[X]
    if get_origin(tp) is list:
        args = get_args(tp)
        elem = args[0] if args else Any
        return ("list", elem)

    # dict[K, V]
    if get_origin(tp) is dict:
        args = get_args(tp)
        if args and len(args) == 2:
            return ("dict", args)
        return ("dict", (Any, Any))

    # Comparable scalar
    if isinstance(tp, type) and tp in _COMPARABLE_TYPES:
        return ("comparable", tp)

    return ("base", tp)


# ---------------------------------------------------------------------------
# Property wrappers
# ---------------------------------------------------------------------------

class DbConditionProperty(Generic[T]):
    """Typed property wrapper — supports ``equal_to``, ``in_``, ``like``, ``null``."""

    def __init__(
        self,
        wire_name: str,
        mode: DbConditionMode = DbConditionMode.Property,
        key: Optional[str] = None,
        *,
        _invert: bool = False,
    ) -> None:
        """Initialise the property wrapper.

        :param wire_name: The camelCase property name used in the wire format.
        :param mode: Condition mode (property, dictionary key/value, etc.).
        :param key: Optional dictionary key for ``DictionaryValue`` mode.
        :param _invert: If ``True``, conditions produced by this wrapper are
            automatically wrapped in a ``DbConditionNot``.
        """
        self._wire_name = wire_name
        self._mode = mode
        self._key = key
        self._invert = _invert

    def _wrap(self, condition: DbConditionInstance) -> DbConditionInstance:
        """Wrap *condition* in a ``DbConditionNot`` if this property is inverted."""
        if self._invert:
            return DbConditionNot(child=condition)
        return condition

    @property
    def not_(self) -> DbConditionProperty[T]:
        """Return a negated version of this property wrapper."""
        return DbConditionProperty[T](
            self._wire_name, self._mode, self._key, _invert=not self._invert
        )

    def equal_to(self, value: T) -> DbConditionInstance:
        """Create an ``EqualTo`` comparison condition."""
        return self._wrap(DbConditionComparison(
            condition_type=DbConditionType.EqualTo,
            property=self._wire_name,
            value=_value_to_wire(value),
            mode=self._mode,
            key=self._key,
        ))

    def in_(self, values: Sequence[T]) -> DbConditionInstance:
        """Create an ``In`` membership condition."""
        return self._wrap(DbConditionComparisonArray(
            property=self._wire_name,
            values=_values_to_wire(values),
            mode=self._mode,
            key=self._key,
        ))

    def like(self, pattern: str) -> DbConditionInstance:
        """Create a ``Like`` pattern-matching condition."""
        return self._wrap(DbConditionComparison(
            condition_type=DbConditionType.Like,
            property=self._wire_name,
            value=pattern,
            mode=self._mode,
            key=self._key,
        ))

    def null(self) -> DbConditionInstance:
        """Create an ``EqualTo None`` condition (IS NULL)."""
        return self._wrap(DbConditionComparison(
            condition_type=DbConditionType.EqualTo,
            property=self._wire_name,
            value=None,
            mode=self._mode,
            key=self._key,
        ))

    def not_null(self) -> DbConditionInstance:
        """Create a NOT NULL condition."""
        inner = DbConditionComparison(
            condition_type=DbConditionType.EqualTo,
            property=self._wire_name,
            value=None,
            mode=self._mode,
            key=self._key,
        )
        if self._invert:
            # double negation = positive
            return inner
        return DbConditionNot(child=inner)


class DbConditionComparableProperty(DbConditionProperty[T]):
    """Typed property wrapper for comparable fields — adds ordered comparisons."""

    @property
    def not_(self) -> DbConditionComparableProperty[T]:
        """Return a negated version of this comparable property wrapper."""
        return DbConditionComparableProperty[T](
            self._wire_name, self._mode, self._key, _invert=not self._invert
        )

    def less_than(self, value: T) -> DbConditionInstance:
        """Create a ``LessThan`` comparison condition."""
        return self._wrap(DbConditionComparison(
            condition_type=DbConditionType.LessThan,
            property=self._wire_name,
            value=_value_to_wire(value),
            mode=self._mode,
            key=self._key,
        ))

    def less_than_or_equal_to(self, value: T) -> DbConditionInstance:
        """Create a ``LessThanOrEqualTo`` comparison condition."""
        return self._wrap(DbConditionComparison(
            condition_type=DbConditionType.LessThanOrEqualTo,
            property=self._wire_name,
            value=_value_to_wire(value),
            mode=self._mode,
            key=self._key,
        ))

    def greater_than(self, value: T) -> DbConditionInstance:
        """Create a ``GreaterThan`` comparison condition."""
        return self._wrap(DbConditionComparison(
            condition_type=DbConditionType.GreaterThan,
            property=self._wire_name,
            value=_value_to_wire(value),
            mode=self._mode,
            key=self._key,
        ))

    def greater_than_or_equal_to(self, value: T) -> DbConditionInstance:
        """Create a ``GreaterThanOrEqualTo`` comparison condition."""
        return self._wrap(DbConditionComparison(
            condition_type=DbConditionType.GreaterThanOrEqualTo,
            property=self._wire_name,
            value=_value_to_wire(value),
            mode=self._mode,
            key=self._key,
        ))

    def between(
        self,
        min_val: T,
        max_val: T,
        *,
        inclusive_min: bool = True,
        inclusive_max: bool = True,
    ) -> DbConditionInstance:
        """Create a ``Between`` range condition."""
        min_wire = _value_to_wire(min_val)
        max_wire = _value_to_wire(max_val)
        if min_wire is None or max_wire is None:
            raise ValueError("between() bounds must not be None")
        return self._wrap(DbConditionBetween(
            property=self._wire_name,
            value_min=min_wire,
            value_max=max_wire,
            inclusive_min=inclusive_min,
            inclusive_max=inclusive_max,
            mode=self._mode,
            key=self._key,
        ))


class DbConditionListProperty(DbConditionProperty[T]):
    """Typed property wrapper for list fields — adds set operations."""

    @property
    def not_(self) -> DbConditionListProperty[T]:
        """Return a negated version of this list property wrapper."""
        return DbConditionListProperty[T](
            self._wire_name, self._mode, self._key, _invert=not self._invert
        )

    def contains(self, value: T) -> DbConditionInstance:
        """Create a ``Contains`` membership condition."""
        return self._wrap(DbConditionContains(
            property=self._wire_name,
            value=_value_to_wire(value),
            mode=self._mode,
        ))

    def intersects(self, values: Sequence[T]) -> DbConditionInstance:
        """Create an ``Intersects`` set condition."""
        return self._wrap(DbConditionArrayComparison(
            condition_type=DbConditionType.Intersects,
            property=self._wire_name,
            values=_values_to_wire(values),
            mode=self._mode,
        ))

    def subset_of(self, values: Sequence[T]) -> DbConditionInstance:
        """Create a ``SubsetOf`` set condition."""
        return self._wrap(DbConditionArrayComparison(
            condition_type=DbConditionType.SubsetOf,
            property=self._wire_name,
            values=_values_to_wire(values),
            mode=self._mode,
        ))

    def superset_of(self, values: Sequence[T]) -> DbConditionInstance:
        """Create a ``SupersetOf`` set condition."""
        return self._wrap(DbConditionArrayComparison(
            condition_type=DbConditionType.SupersetOf,
            property=self._wire_name,
            values=_values_to_wire(values),
            mode=self._mode,
        ))


class DbConditionDictProperty(Generic[TKey, TValue]):
    """Typed property wrapper for dict fields — provides key/keys/values accessors."""

    def __init__(self, wire_name: str) -> None:
        """Initialise the dict property wrapper.

        :param wire_name: The camelCase property name used in the wire format.
        """
        self._wire_name = wire_name

    def key(self, key_value: TKey) -> DbConditionProperty[TValue]:
        """Access a specific dictionary key (mode=DictionaryValue)."""
        return DbConditionProperty[TValue](
            self._wire_name,
            mode=DbConditionMode.DictionaryValue,
            key=_value_to_wire(key_value),
        )

    def keys(self) -> DbConditionListProperty[TKey]:
        """Access dictionary keys (mode=DictionaryKeys)."""
        return DbConditionListProperty[TKey](
            self._wire_name,
            mode=DbConditionMode.DictionaryKeys,
        )

    def values(self) -> DbConditionListProperty[TValue]:
        """Access dictionary values (mode=DictionaryValues)."""
        return DbConditionListProperty[TValue](
            self._wire_name,
            mode=DbConditionMode.DictionaryValues,
        )


# ---------------------------------------------------------------------------
# Condition builder
# ---------------------------------------------------------------------------

class Condition(Generic[TEntity]):
    """Typed condition builder for entity queries.

    Usage::

        Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Active)
        Condition.and_(cond1, cond2)
    """

    def __init__(self, entity_cls: Type[TEntity]) -> None:
        """Initialise the condition builder for the given entity class.

        :param entity_cls: The dataclass entity type whose fields will be
            validated when calling :meth:`prop`.
        """
        self._entity_cls = entity_cls

    def prop(self, name: str) -> DbConditionProperty[Any]:
        """Return a typed property wrapper for the named field.

        At runtime, validates that the field exists and classifies its type.
        The mypy plugin narrows the return type statically.
        """
        wire_name = camel_case_from_snake_case(name)
        category, _inner = _classify_field(self._entity_cls, name)

        if category == "comparable":
            return DbConditionComparableProperty[Any](wire_name)
        if category == "list":
            return DbConditionListProperty[Any](wire_name)
        if category == "dict":
            return DbConditionDictProperty[Any, Any](wire_name)  # type: ignore[return-value]
        return DbConditionProperty[Any](wire_name)

    @staticmethod
    def and_(*conditions: DbConditionInstance) -> DbConditionCombine:
        """Create an ``And`` condition combining multiple conditions."""
        return and_(*conditions)

    @staticmethod
    def or_(*conditions: DbConditionInstance) -> DbConditionCombine:
        """Create an ``Or`` condition combining multiple conditions."""
        return or_(*conditions)

    @staticmethod
    def not_(condition: DbConditionInstance) -> DbConditionNot:
        """Negate a condition."""
        return not_(condition)

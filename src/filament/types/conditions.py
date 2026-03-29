"""Condition types for database query filtering.

Mirrors the C# backend's ``DbConditionInstance`` hierarchy and wire format
defined by ``DbConditionInstanceConverter``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

__all__ = [
    # Enums
    "DbConditionType",
    "DbConditionMode",
    # Base + concrete classes
    "DbConditionInstance",
    "DbConditionNot",
    "DbConditionCombine",
    "DbConditionComparison",
    "DbConditionBetween",
    "DbConditionComparisonArray",
    "DbConditionArrayComparison",
    "DbConditionContains",
    "DbConditionLink",
    # Serialization
    "condition_to_dict",
    "condition_from_dict",
    # Convenience constructors
    "and_",
    "or_",
    "not_",
    "equal_to",
    "less_than",
    "less_than_or_equal_to",
    "greater_than",
    "greater_than_or_equal_to",
    "like",
    "between",
    "in_",
    "contains",
    "intersects",
    "subset_of",
    "superset_of",
    "link",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DbConditionType(str, Enum):
    """Discriminator values for the wire-format ``condition`` field."""

    Not = "Not"
    And = "And"
    Or = "Or"
    EqualTo = "EqualTo"
    LessThan = "LessThan"
    LessThanOrEqualTo = "LessThanOrEqualTo"
    GreaterThan = "GreaterThan"
    GreaterThanOrEqualTo = "GreaterThanOrEqualTo"
    Like = "Like"
    Between = "Between"
    In = "In"
    Contains = "Contains"
    Intersects = "Intersects"
    SubsetOf = "SubsetOf"
    SupersetOf = "SupersetOf"
    Link = "Link"


class DbConditionMode(str, Enum):
    """Mode values for property-based conditions."""

    Property = "Property"
    DictionaryValue = "DictionaryValue"
    DictionaryKeys = "DictionaryKeys"
    DictionaryValues = "DictionaryValues"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DbConditionInstance:
    """Base class for all condition instances.

    Not instantiated directly — use one of the concrete subclasses.
    """

    pass


@dataclass
class DbConditionNot(DbConditionInstance):
    """Negates a child condition."""

    child: DbConditionInstance = field(default_factory=DbConditionInstance)


@dataclass
class DbConditionCombine(DbConditionInstance):
    """Combines multiple conditions with ``And`` / ``Or`` logic."""

    condition_type: DbConditionType = DbConditionType.And
    children: list[DbConditionInstance] = field(default_factory=list)


@dataclass
class DbConditionComparison(DbConditionInstance):
    """Single-value comparison: ``EqualTo``, ``LessThan``, ``LessThanOrEqualTo``,
    ``GreaterThan``, ``GreaterThanOrEqualTo``, ``Like``.
    """

    condition_type: DbConditionType = DbConditionType.EqualTo
    property: str = field(default_factory=str)
    value: Optional[str] = field(default=None)
    mode: DbConditionMode = DbConditionMode.Property
    key: Optional[str] = field(default=None)


@dataclass
class DbConditionBetween(DbConditionInstance):
    """Range condition with configurable inclusive/exclusive bounds."""

    property: str = field(default_factory=str)
    value_min: str = field(default_factory=str)
    value_max: str = field(default_factory=str)
    inclusive_min: bool = True
    inclusive_max: bool = True
    mode: DbConditionMode = DbConditionMode.Property
    key: Optional[str] = field(default=None)


@dataclass
class DbConditionComparisonArray(DbConditionInstance):
    """``In`` condition: checks whether a value is in a list."""

    property: str = field(default_factory=str)
    values: list[Optional[str]] = field(default_factory=list)
    mode: DbConditionMode = DbConditionMode.Property
    key: Optional[str] = field(default=None)


@dataclass
class DbConditionArrayComparison(DbConditionInstance):
    """Set operations: ``Intersects``, ``SubsetOf``, ``SupersetOf``."""

    condition_type: DbConditionType = DbConditionType.Intersects
    property: str = field(default_factory=str)
    values: list[Optional[str]] = field(default_factory=list)
    mode: DbConditionMode = DbConditionMode.Property


@dataclass
class DbConditionContains(DbConditionInstance):
    """Membership check for lists or dictionaries."""

    property: str = field(default_factory=str)
    value: Optional[str] = field(default=None)
    mode: DbConditionMode = DbConditionMode.Property


@dataclass
class DbConditionLink(DbConditionInstance):
    """Cross-entity join condition."""

    property: str = field(default_factory=str)
    link_type: str = field(default_factory=str)
    link_property: str = field(default_factory=str)
    link_condition: DbConditionInstance = field(default_factory=DbConditionInstance)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def condition_to_dict(condition: DbConditionInstance) -> dict[str, Any]:
    """Serialize a condition instance to a camelCase wire-format dict."""

    if isinstance(condition, DbConditionNot):
        return {
            "condition": DbConditionType.Not.value,
            "child": condition_to_dict(condition.child),
        }

    if isinstance(condition, DbConditionCombine):
        return {
            "condition": condition.condition_type.value,
            "children": [condition_to_dict(c) for c in condition.children],
        }

    if isinstance(condition, DbConditionComparison):
        result: dict[str, Any] = {
            "condition": condition.condition_type.value,
            "property": condition.property,
        }
        if condition.mode != DbConditionMode.Property:
            result["mode"] = condition.mode.value
        if condition.key is not None:
            result["key"] = condition.key
        result["value"] = condition.value
        return result

    if isinstance(condition, DbConditionBetween):
        result = {
            "condition": DbConditionType.Between.value,
            "property": condition.property,
        }
        if condition.mode != DbConditionMode.Property:
            result["mode"] = condition.mode.value
        if condition.key is not None:
            result["key"] = condition.key
        result["valueMin"] = condition.value_min
        result["valueMax"] = condition.value_max
        result["inclusiveMin"] = condition.inclusive_min
        result["inclusiveMax"] = condition.inclusive_max
        return result

    if isinstance(condition, DbConditionComparisonArray):
        result = {
            "condition": DbConditionType.In.value,
            "property": condition.property,
        }
        if condition.mode != DbConditionMode.Property:
            result["mode"] = condition.mode.value
        if condition.key is not None:
            result["key"] = condition.key
        result["values"] = list(condition.values)
        return result

    if isinstance(condition, DbConditionArrayComparison):
        result = {
            "condition": condition.condition_type.value,
            "property": condition.property,
        }
        if condition.mode != DbConditionMode.Property:
            result["mode"] = condition.mode.value
        result["values"] = list(condition.values)
        return result

    if isinstance(condition, DbConditionContains):
        result = {
            "condition": DbConditionType.Contains.value,
            "property": condition.property,
        }
        if condition.mode != DbConditionMode.Property:
            result["mode"] = condition.mode.value
        result["value"] = condition.value
        return result

    if isinstance(condition, DbConditionLink):
        return {
            "condition": DbConditionType.Link.value,
            "property": condition.property,
            "linkType": condition.link_type,
            "linkProperty": condition.link_property,
            "linkCondition": condition_to_dict(condition.link_condition),
        }

    # Base DbConditionInstance should not be serialized directly
    raise TypeError(f"Cannot serialize {type(condition).__name__} to dict")


# Map from wire discriminator string to the parsing function
_CONDITION_TYPE_COMPARISON = frozenset({
    DbConditionType.EqualTo.value,
    DbConditionType.LessThan.value,
    DbConditionType.LessThanOrEqualTo.value,
    DbConditionType.GreaterThan.value,
    DbConditionType.GreaterThanOrEqualTo.value,
    DbConditionType.Like.value,
})

_CONDITION_TYPE_ARRAY_COMPARISON = frozenset({
    DbConditionType.Intersects.value,
    DbConditionType.SubsetOf.value,
    DbConditionType.SupersetOf.value,
})


def condition_from_dict(data: dict[str, Any]) -> DbConditionInstance:
    """Deserialize a camelCase wire-format dict to a condition instance."""

    condition_type_str: str = data["condition"]

    if condition_type_str == DbConditionType.Not.value:
        return DbConditionNot(
            child=condition_from_dict(data["child"]),
        )

    if condition_type_str in (DbConditionType.And.value, DbConditionType.Or.value):
        return DbConditionCombine(
            condition_type=DbConditionType(condition_type_str),
            children=[condition_from_dict(c) for c in data["children"]],
        )

    if condition_type_str in _CONDITION_TYPE_COMPARISON:
        return DbConditionComparison(
            condition_type=DbConditionType(condition_type_str),
            property=data["property"],
            value=data.get("value"),
            mode=DbConditionMode(data.get("mode", DbConditionMode.Property.value)),
            key=data.get("key"),
        )

    if condition_type_str == DbConditionType.Between.value:
        return DbConditionBetween(
            property=data["property"],
            value_min=data.get("valueMin", ""),
            value_max=data.get("valueMax", ""),
            inclusive_min=data.get("inclusiveMin", True),
            inclusive_max=data.get("inclusiveMax", True),
            mode=DbConditionMode(data.get("mode", DbConditionMode.Property.value)),
            key=data.get("key"),
        )

    if condition_type_str == DbConditionType.In.value:
        return DbConditionComparisonArray(
            property=data["property"],
            values=data.get("values", []),
            mode=DbConditionMode(data.get("mode", DbConditionMode.Property.value)),
            key=data.get("key"),
        )

    if condition_type_str in _CONDITION_TYPE_ARRAY_COMPARISON:
        return DbConditionArrayComparison(
            condition_type=DbConditionType(condition_type_str),
            property=data["property"],
            values=data.get("values", []),
            mode=DbConditionMode(data.get("mode", DbConditionMode.Property.value)),
        )

    if condition_type_str == DbConditionType.Contains.value:
        return DbConditionContains(
            property=data["property"],
            value=data.get("value"),
            mode=DbConditionMode(data.get("mode", DbConditionMode.Property.value)),
        )

    if condition_type_str == DbConditionType.Link.value:
        return DbConditionLink(
            property=data["property"],
            link_type=data.get("linkType", ""),
            link_property=data.get("linkProperty", ""),
            link_condition=condition_from_dict(data["linkCondition"]),
        )

    raise ValueError(f"Unknown condition type: {condition_type_str!r}")


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def and_(*conditions: DbConditionInstance) -> DbConditionCombine:
    """Create an ``And`` condition combining multiple conditions."""
    return DbConditionCombine(
        condition_type=DbConditionType.And, children=list(conditions)
    )


def or_(*conditions: DbConditionInstance) -> DbConditionCombine:
    """Create an ``Or`` condition combining multiple conditions."""
    return DbConditionCombine(
        condition_type=DbConditionType.Or, children=list(conditions)
    )


def not_(condition: DbConditionInstance) -> DbConditionNot:
    """Negate a condition."""
    return DbConditionNot(child=condition)


def equal_to(
    prop: str,
    value: Optional[str],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionComparison:
    """Create an ``EqualTo`` comparison condition."""
    return DbConditionComparison(
        condition_type=DbConditionType.EqualTo,
        property=prop,
        value=value,
        mode=mode,
        key=key,
    )


def less_than(
    prop: str,
    value: Optional[str],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionComparison:
    """Create a ``LessThan`` comparison condition."""
    return DbConditionComparison(
        condition_type=DbConditionType.LessThan,
        property=prop,
        value=value,
        mode=mode,
        key=key,
    )


def less_than_or_equal_to(
    prop: str,
    value: Optional[str],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionComparison:
    """Create a ``LessThanOrEqualTo`` comparison condition."""
    return DbConditionComparison(
        condition_type=DbConditionType.LessThanOrEqualTo,
        property=prop,
        value=value,
        mode=mode,
        key=key,
    )


def greater_than(
    prop: str,
    value: Optional[str],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionComparison:
    """Create a ``GreaterThan`` comparison condition."""
    return DbConditionComparison(
        condition_type=DbConditionType.GreaterThan,
        property=prop,
        value=value,
        mode=mode,
        key=key,
    )


def greater_than_or_equal_to(
    prop: str,
    value: Optional[str],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionComparison:
    """Create a ``GreaterThanOrEqualTo`` comparison condition."""
    return DbConditionComparison(
        condition_type=DbConditionType.GreaterThanOrEqualTo,
        property=prop,
        value=value,
        mode=mode,
        key=key,
    )


def like(
    prop: str,
    value: Optional[str],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionComparison:
    """Create a ``Like`` pattern-matching condition."""
    return DbConditionComparison(
        condition_type=DbConditionType.Like,
        property=prop,
        value=value,
        mode=mode,
        key=key,
    )


def between(
    prop: str,
    value_min: str,
    value_max: str,
    *,
    inclusive_min: bool = True,
    inclusive_max: bool = True,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionBetween:
    """Create a ``Between`` range condition."""
    return DbConditionBetween(
        property=prop,
        value_min=value_min,
        value_max=value_max,
        inclusive_min=inclusive_min,
        inclusive_max=inclusive_max,
        mode=mode,
        key=key,
    )


def in_(
    prop: str,
    values: list[Optional[str]],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
    key: Optional[str] = None,
) -> DbConditionComparisonArray:
    """Create an ``In`` membership condition."""
    return DbConditionComparisonArray(
        property=prop,
        values=values,
        mode=mode,
        key=key,
    )


def contains(
    prop: str,
    value: Optional[str],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
) -> DbConditionContains:
    """Create a ``Contains`` membership condition."""
    return DbConditionContains(property=prop, value=value, mode=mode)


def intersects(
    prop: str,
    values: list[Optional[str]],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
) -> DbConditionArrayComparison:
    """Create an ``Intersects`` set condition."""
    return DbConditionArrayComparison(
        condition_type=DbConditionType.Intersects,
        property=prop,
        values=values,
        mode=mode,
    )


def subset_of(
    prop: str,
    values: list[Optional[str]],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
) -> DbConditionArrayComparison:
    """Create a ``SubsetOf`` set condition."""
    return DbConditionArrayComparison(
        condition_type=DbConditionType.SubsetOf,
        property=prop,
        values=values,
        mode=mode,
    )


def superset_of(
    prop: str,
    values: list[Optional[str]],
    *,
    mode: DbConditionMode = DbConditionMode.Property,
) -> DbConditionArrayComparison:
    """Create a ``SupersetOf`` set condition."""
    return DbConditionArrayComparison(
        condition_type=DbConditionType.SupersetOf,
        property=prop,
        values=values,
        mode=mode,
    )


def link(
    prop: str,
    link_type: str,
    link_property: str,
    link_condition: DbConditionInstance,
) -> DbConditionLink:
    """Create a ``Link`` cross-entity join condition."""
    return DbConditionLink(
        property=prop,
        link_type=link_type,
        link_property=link_property,
        link_condition=link_condition,
    )

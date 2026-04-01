"""Tests for the typed condition builder."""
from __future__ import annotations

import datetime

import pytest
import uuid_utils

from filament._condition_builder import (
    Condition,
    DbConditionComparableProperty,
    DbConditionDictProperty,
    DbConditionListProperty,
    DbConditionProperty,
    _classify_field,
    _value_to_wire,
)
from filament.types.conditions import (
    DbConditionArrayComparison,
    DbConditionBetween,
    DbConditionCombine,
    DbConditionComparison,
    DbConditionComparisonArray,
    DbConditionContains,
    DbConditionMode,
    DbConditionNot,
    DbConditionType,
    condition_to_dict,
)
from filament.types.generated.entities.task_service import TaskEntity, TaskGroupEntity
from filament.types.generated.entities.user_service import UserEntity
from filament.types.generated.enums import UserStatusEnum


# ---------------------------------------------------------------------------
# _value_to_wire
# ---------------------------------------------------------------------------


class TestValueToWire:
    def test_none(self) -> None:
        assert _value_to_wire(None) is None

    def test_str(self) -> None:
        assert _value_to_wire("hello") == "hello"

    def test_int(self) -> None:
        assert _value_to_wire(42) == "42"

    def test_float(self) -> None:
        assert _value_to_wire(3.14) == "3.14"

    def test_bool_true(self) -> None:
        assert _value_to_wire(True) == "True"

    def test_bool_false(self) -> None:
        assert _value_to_wire(False) == "False"

    def test_bool_not_treated_as_int(self) -> None:
        # bool is subclass of int; ensure True != "1"
        assert _value_to_wire(True) != "1"

    def test_enum(self) -> None:
        result = _value_to_wire(UserStatusEnum.Enabled)
        assert result == str(UserStatusEnum.Enabled.value)

    def test_uuid(self) -> None:
        uid = uuid_utils.uuid7()
        assert _value_to_wire(uid) == str(uid)

    def test_datetime(self) -> None:
        dt = datetime.datetime(2025, 1, 15, 10, 30, 0, 123456)
        result = _value_to_wire(dt)
        assert "2025-01-15" in result
        assert result.endswith("Z")

    def test_timedelta(self) -> None:
        td = datetime.timedelta(hours=2, minutes=30)
        result = _value_to_wire(td)
        assert result == str(td.total_seconds())


# ---------------------------------------------------------------------------
# _classify_field
# ---------------------------------------------------------------------------


class TestClassifyField:
    def test_str_is_comparable(self) -> None:
        cat, _ = _classify_field(UserEntity, "given_name")
        assert cat == "comparable"

    def test_float_is_comparable(self) -> None:
        cat, _ = _classify_field(UserEntity, "compensation_per_hour_suggested")
        assert cat == "comparable"

    def test_uuid_is_comparable(self) -> None:
        cat, _ = _classify_field(UserEntity, "tax_credit_location_id")
        assert cat == "comparable"

    def test_int_is_comparable(self) -> None:
        cat, _ = _classify_field(TaskEntity, "user_count")
        assert cat == "comparable"

    def test_bool_is_comparable(self) -> None:
        cat, _ = _classify_field(TaskEntity, "requires_review")
        assert cat == "comparable"

    def test_timedelta_is_comparable(self) -> None:
        cat, _ = _classify_field(TaskEntity, "duration")
        assert cat == "comparable"

    def test_enum_is_base(self) -> None:
        cat, _ = _classify_field(UserEntity, "schedule_status")
        assert cat == "base"

    def test_optional_str_is_comparable(self) -> None:
        cat, _ = _classify_field(UserEntity, "middle_names")
        assert cat == "comparable"

    def test_optional_entity_is_base(self) -> None:
        cat, _ = _classify_field(UserEntity, "office_location")
        assert cat == "base"

    def test_list_field_is_list(self) -> None:
        cat, _ = _classify_field(TaskGroupEntity, "ancestor_group_ids")
        assert cat == "list"

    def test_dict_field_is_dict(self) -> None:
        cat, _ = _classify_field(TaskEntity, "contexts")
        assert cat == "dict"

    def test_nonexistent_raises(self) -> None:
        with pytest.raises(AttributeError, match="nonexistent"):
            _classify_field(UserEntity, "nonexistent")


# ---------------------------------------------------------------------------
# Condition.prop — runtime behavior
# ---------------------------------------------------------------------------


class TestConditionProp:
    def test_returns_comparable_for_str(self) -> None:
        prop = Condition(UserEntity).prop("given_name")
        assert isinstance(prop, DbConditionComparableProperty)

    def test_returns_comparable_for_int(self) -> None:
        prop = Condition(TaskEntity).prop("user_count")
        assert isinstance(prop, DbConditionComparableProperty)

    def test_returns_base_for_enum(self) -> None:
        prop = Condition(UserEntity).prop("schedule_status")
        assert isinstance(prop, DbConditionProperty)
        assert not isinstance(prop, DbConditionComparableProperty)

    def test_returns_list_for_list(self) -> None:
        prop = Condition(TaskGroupEntity).prop("ancestor_group_ids")
        assert isinstance(prop, DbConditionListProperty)

    def test_returns_dict_for_dict(self) -> None:
        prop = Condition(TaskEntity).prop("contexts")
        assert isinstance(prop, DbConditionDictProperty)

    def test_nonexistent_raises(self) -> None:
        with pytest.raises(AttributeError):
            Condition(UserEntity).prop("nonexistent")


# ---------------------------------------------------------------------------
# Property wrapper methods
# ---------------------------------------------------------------------------


class TestPropertyMethods:
    def test_equal_to(self) -> None:
        result = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.EqualTo
        assert result.property == "scheduleStatus"
        assert result.value == str(UserStatusEnum.Enabled.value)

    def test_in_(self) -> None:
        result = Condition(UserEntity).prop("given_name").in_(["Alice", "Bob"])
        assert isinstance(result, DbConditionComparisonArray)
        assert result.values == ["Alice", "Bob"]

    def test_like(self) -> None:
        result = Condition(UserEntity).prop("given_name").like("%Alice%")
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.Like
        assert result.value == "%Alice%"

    def test_null(self) -> None:
        result = Condition(UserEntity).prop("middle_names").null()
        assert isinstance(result, DbConditionComparison)
        assert result.value is None

    def test_not_null(self) -> None:
        result = Condition(UserEntity).prop("middle_names").not_null()
        assert isinstance(result, DbConditionNot)
        inner = result.child
        assert isinstance(inner, DbConditionComparison)
        assert inner.value is None

    def test_less_than(self) -> None:
        result = Condition(TaskEntity).prop("user_count").less_than(10)
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.LessThan
        assert result.value == "10"

    def test_greater_than(self) -> None:
        result = Condition(TaskEntity).prop("user_count").greater_than(5)
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.GreaterThan

    def test_less_than_or_equal_to(self) -> None:
        result = Condition(TaskEntity).prop("user_count").less_than_or_equal_to(10)
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.LessThanOrEqualTo

    def test_greater_than_or_equal_to(self) -> None:
        result = Condition(TaskEntity).prop("user_count").greater_than_or_equal_to(1)
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.GreaterThanOrEqualTo

    def test_between(self) -> None:
        result = Condition(TaskEntity).prop("user_count").between(1, 10)
        assert isinstance(result, DbConditionBetween)
        assert result.value_min == "1"
        assert result.value_max == "10"
        assert result.inclusive_min is True
        assert result.inclusive_max is True

    def test_between_exclusive(self) -> None:
        result = Condition(TaskEntity).prop("user_count").between(
            1, 10, inclusive_min=False, inclusive_max=False
        )
        assert isinstance(result, DbConditionBetween)
        assert result.inclusive_min is False
        assert result.inclusive_max is False


# ---------------------------------------------------------------------------
# List property methods
# ---------------------------------------------------------------------------


class TestListPropertyMethods:
    def test_contains(self) -> None:
        result = Condition(TaskGroupEntity).prop("ancestor_group_ids").contains(
            uuid_utils.uuid7()
        )
        assert isinstance(result, DbConditionContains)
        assert result.property == "ancestorGroupIds"

    def test_intersects(self) -> None:
        result = Condition(TaskGroupEntity).prop("ancestor_group_ids").intersects(
            [uuid_utils.uuid7()]
        )
        assert isinstance(result, DbConditionArrayComparison)
        assert result.condition_type == DbConditionType.Intersects

    def test_subset_of(self) -> None:
        result = Condition(TaskGroupEntity).prop("ancestor_group_ids").subset_of(
            [uuid_utils.uuid7()]
        )
        assert isinstance(result, DbConditionArrayComparison)
        assert result.condition_type == DbConditionType.SubsetOf

    def test_superset_of(self) -> None:
        result = Condition(TaskGroupEntity).prop("ancestor_group_ids").superset_of(
            [uuid_utils.uuid7()]
        )
        assert isinstance(result, DbConditionArrayComparison)
        assert result.condition_type == DbConditionType.SupersetOf


# ---------------------------------------------------------------------------
# Dict property methods
# ---------------------------------------------------------------------------


class TestDictPropertyMethods:
    def test_key(self) -> None:
        prop = Condition(TaskEntity).prop("contexts")
        assert isinstance(prop, DbConditionDictProperty)
        key_prop = prop.key("scope")
        result = key_prop.equal_to("some_value")
        assert isinstance(result, DbConditionComparison)
        assert result.mode == DbConditionMode.DictionaryValue
        assert result.key == "scope"

    def test_keys(self) -> None:
        prop = Condition(TaskEntity).prop("contexts")
        assert isinstance(prop, DbConditionDictProperty)
        keys_prop = prop.keys()
        result = keys_prop.contains("scope")
        assert isinstance(result, DbConditionContains)
        assert result.mode == DbConditionMode.DictionaryKeys

    def test_values(self) -> None:
        prop = Condition(TaskEntity).prop("contexts")
        assert isinstance(prop, DbConditionDictProperty)
        values_prop = prop.values()
        result = values_prop.contains("some_value")
        assert isinstance(result, DbConditionContains)
        assert result.mode == DbConditionMode.DictionaryValues


# ---------------------------------------------------------------------------
# .not_ property
# ---------------------------------------------------------------------------


class TestNotProperty:
    def test_not_equal_to(self) -> None:
        result = Condition(UserEntity).prop("given_name").not_.equal_to("Alice")
        assert isinstance(result, DbConditionNot)
        inner = result.child
        assert isinstance(inner, DbConditionComparison)
        assert inner.condition_type == DbConditionType.EqualTo
        assert inner.value == "Alice"

    def test_not_less_than(self) -> None:
        result = Condition(TaskEntity).prop("user_count").not_.less_than(10)
        assert isinstance(result, DbConditionNot)
        inner = result.child
        assert isinstance(inner, DbConditionComparison)
        assert inner.condition_type == DbConditionType.LessThan

    def test_not_null_on_inverted_is_positive(self) -> None:
        # .not_.not_null() = double negation = IS NULL
        result = Condition(UserEntity).prop("middle_names").not_.not_null()
        assert isinstance(result, DbConditionComparison)
        assert result.value is None

    def test_not_contains(self) -> None:
        result = Condition(TaskGroupEntity).prop("ancestor_group_ids").not_.contains(
            uuid_utils.uuid7()
        )
        assert isinstance(result, DbConditionNot)
        assert isinstance(result.child, DbConditionContains)


# ---------------------------------------------------------------------------
# Condition static methods
# ---------------------------------------------------------------------------


class TestConditionStatic:
    def test_and(self) -> None:
        c1 = Condition(UserEntity).prop("given_name").equal_to("Alice")
        c2 = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
        result = Condition.and_(c1, c2)
        assert isinstance(result, DbConditionCombine)
        assert result.condition_type == DbConditionType.And
        assert len(result.children) == 2

    def test_or(self) -> None:
        c1 = Condition(UserEntity).prop("given_name").equal_to("Alice")
        c2 = Condition(UserEntity).prop("given_name").equal_to("Bob")
        result = Condition.or_(c1, c2)
        assert isinstance(result, DbConditionCombine)
        assert result.condition_type == DbConditionType.Or

    def test_not(self) -> None:
        c1 = Condition(UserEntity).prop("given_name").equal_to("Alice")
        result = Condition.not_(c1)
        assert isinstance(result, DbConditionNot)


# ---------------------------------------------------------------------------
# Wire format equivalence
# ---------------------------------------------------------------------------


class TestWireFormatEquivalence:
    def test_typed_equal_to_matches_untyped(self) -> None:
        typed = Condition(UserEntity).prop("given_name").equal_to("Alice")
        typed_wire = condition_to_dict(typed)
        assert typed_wire == {
            "condition": "EqualTo",
            "property": "givenName",
            "value": "Alice",
        }

    def test_typed_between_matches_wire(self) -> None:
        typed = Condition(TaskEntity).prop("user_count").between(1, 10)
        typed_wire = condition_to_dict(typed)
        assert typed_wire == {
            "condition": "Between",
            "property": "userCount",
            "valueMin": "1",
            "valueMax": "10",
            "inclusiveMin": True,
            "inclusiveMax": True,
        }

    def test_typed_dict_key_matches_wire(self) -> None:
        typed = Condition(TaskEntity).prop("contexts").key("scope").equal_to("project")
        typed_wire = condition_to_dict(typed)
        assert typed_wire == {
            "condition": "EqualTo",
            "property": "contexts",
            "mode": "DictionaryValue",
            "key": "scope",
            "value": "project",
        }

    def test_typed_nested_matches_wire(self) -> None:
        typed = Condition.and_(
            Condition(UserEntity).prop("given_name").equal_to("Alice"),
            Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled),
        )
        typed_wire = condition_to_dict(typed)
        assert typed_wire["condition"] == "And"
        assert len(typed_wire["children"]) == 2


# ---------------------------------------------------------------------------
# Operator combining (&, |, ~) with typed builder
# ---------------------------------------------------------------------------


class TestOperatorCombiningTyped:
    def test_and_operator(self) -> None:
        a = Condition(UserEntity).prop("given_name").equal_to("Alice")
        b = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
        result = a & b
        assert isinstance(result, DbConditionCombine)
        assert result.condition_type == DbConditionType.And
        assert len(result.children) == 2

    def test_or_operator(self) -> None:
        a = Condition(UserEntity).prop("given_name").equal_to("Alice")
        b = Condition(UserEntity).prop("given_name").equal_to("Bob")
        result = a | b
        assert isinstance(result, DbConditionCombine)
        assert result.condition_type == DbConditionType.Or

    def test_invert_operator(self) -> None:
        a = Condition(UserEntity).prop("given_name").equal_to("Alice")
        result = ~a
        assert isinstance(result, DbConditionNot)

    def test_operator_equivalent_to_static_methods(self) -> None:
        """& and | produce the same wire format as Condition.and_/or_."""
        a = Condition(UserEntity).prop("given_name").equal_to("Alice")
        b = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)

        operator_wire = condition_to_dict(a & b)
        static_wire = condition_to_dict(Condition.and_(a, b))
        assert operator_wire == static_wire

        operator_wire = condition_to_dict(a | b)
        static_wire = condition_to_dict(Condition.or_(a, b))
        assert operator_wire == static_wire

    def test_precedence_with_typed_builder(self) -> None:
        """~a | b & c parses as (~a) | (b & c) with typed conditions."""
        a = Condition(UserEntity).prop("given_name").equal_to("Alice")
        b = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
        c = Condition(TaskEntity).prop("user_count").greater_than(0)

        result = ~a | b & c
        wire = condition_to_dict(result)
        assert wire["condition"] == "Or"
        assert wire["children"][0]["condition"] == "Not"
        assert wire["children"][1]["condition"] == "And"

    def test_complex_expression(self) -> None:
        """Real-world-style query using operators."""
        active = Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
        alice = Condition(UserEntity).prop("given_name").equal_to("Alice")
        bob = Condition(UserEntity).prop("given_name").equal_to("Bob")

        result = active & (alice | bob)
        wire = condition_to_dict(result)
        assert wire["condition"] == "And"
        assert wire["children"][0]["condition"] == "EqualTo"
        assert wire["children"][0]["property"] == "scheduleStatus"
        assert wire["children"][1]["condition"] == "Or"
        assert len(wire["children"][1]["children"]) == 2

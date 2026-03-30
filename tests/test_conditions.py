"""Tests for the conditions module."""
from __future__ import annotations

from filament._serialization import from_dict, to_dict
from filament.types.conditions import (
    DbConditionArrayComparison,
    DbConditionBetween,
    DbConditionCombine,
    DbConditionComparison,
    DbConditionComparisonArray,
    DbConditionContains,
    DbConditionInstance,
    DbConditionLink,
    DbConditionMode,
    DbConditionNot,
    DbConditionType,
    and_,
    between,
    condition_from_dict,
    condition_to_dict,
    contains,
    equal_to,
    greater_than,
    greater_than_or_equal_to,
    in_,
    intersects,
    less_than,
    less_than_or_equal_to,
    like,
    link,
    not_,
    or_,
    subset_of,
    superset_of,
)
from filament.types.generated.misc import ReadDataTypeRequest


# ---------------------------------------------------------------------------
# condition_to_dict
# ---------------------------------------------------------------------------


class TestConditionToDict:
    def test_not(self) -> None:
        cond = DbConditionNot(child=equal_to("name", "Alice"))
        result = condition_to_dict(cond)
        assert result == {
            "condition": "Not",
            "child": {
                "condition": "EqualTo",
                "property": "name",
                "value": "Alice",
            },
        }

    def test_and(self) -> None:
        cond = and_(equal_to("a", "1"), equal_to("b", "2"))
        result = condition_to_dict(cond)
        assert result["condition"] == "And"
        assert len(result["children"]) == 2

    def test_or(self) -> None:
        cond = or_(equal_to("a", "1"), equal_to("b", "2"))
        result = condition_to_dict(cond)
        assert result["condition"] == "Or"

    def test_equal_to(self) -> None:
        cond = equal_to("status", "Active")
        result = condition_to_dict(cond)
        assert result == {
            "condition": "EqualTo",
            "property": "status",
            "value": "Active",
        }

    def test_equal_to_null(self) -> None:
        cond = equal_to("deletedAt", None)
        result = condition_to_dict(cond)
        assert result["value"] is None

    def test_equal_to_with_mode(self) -> None:
        cond = equal_to(
            "settings",
            "dark",
            mode=DbConditionMode.DictionaryValue,
            key="theme",
        )
        result = condition_to_dict(cond)
        assert result["mode"] == "DictionaryValue"
        assert result["key"] == "theme"

    def test_less_than(self) -> None:
        cond = less_than("age", "30")
        result = condition_to_dict(cond)
        assert result["condition"] == "LessThan"

    def test_less_than_or_equal_to(self) -> None:
        cond = less_than_or_equal_to("age", "30")
        result = condition_to_dict(cond)
        assert result["condition"] == "LessThanOrEqualTo"

    def test_greater_than(self) -> None:
        cond = greater_than("age", "18")
        result = condition_to_dict(cond)
        assert result["condition"] == "GreaterThan"

    def test_greater_than_or_equal_to(self) -> None:
        cond = greater_than_or_equal_to("age", "18")
        result = condition_to_dict(cond)
        assert result["condition"] == "GreaterThanOrEqualTo"

    def test_like(self) -> None:
        cond = like("name", "%alice%")
        result = condition_to_dict(cond)
        assert result == {
            "condition": "Like",
            "property": "name",
            "value": "%alice%",
        }

    def test_between(self) -> None:
        cond = between("age", "18", "65", inclusive_min=True, inclusive_max=False)
        result = condition_to_dict(cond)
        assert result == {
            "condition": "Between",
            "property": "age",
            "valueMin": "18",
            "valueMax": "65",
            "inclusiveMin": True,
            "inclusiveMax": False,
        }

    def test_in(self) -> None:
        cond = in_("status", ["Active", "Pending"])
        result = condition_to_dict(cond)
        assert result == {
            "condition": "In",
            "property": "status",
            "values": ["Active", "Pending"],
        }

    def test_contains(self) -> None:
        cond = contains("tags", "urgent")
        result = condition_to_dict(cond)
        assert result == {
            "condition": "Contains",
            "property": "tags",
            "value": "urgent",
        }

    def test_intersects(self) -> None:
        cond = intersects("tags", ["a", "b"])
        result = condition_to_dict(cond)
        assert result["condition"] == "Intersects"
        assert result["values"] == ["a", "b"]

    def test_subset_of(self) -> None:
        cond = subset_of("roles", ["admin", "user"])
        result = condition_to_dict(cond)
        assert result["condition"] == "SubsetOf"

    def test_superset_of(self) -> None:
        cond = superset_of("roles", ["admin"])
        result = condition_to_dict(cond)
        assert result["condition"] == "SupersetOf"

    def test_link(self) -> None:
        cond = link("userId", "UserEntity", "id", equal_to("active", "True"))
        result = condition_to_dict(cond)
        assert result == {
            "condition": "Link",
            "property": "userId",
            "linkType": "UserEntity",
            "linkProperty": "id",
            "linkCondition": {
                "condition": "EqualTo",
                "property": "active",
                "value": "True",
            },
        }

    def test_mode_omitted_when_property(self) -> None:
        """Default mode 'Property' should NOT appear in wire format."""
        cond = equal_to("name", "test")
        result = condition_to_dict(cond)
        assert "mode" not in result

    def test_key_omitted_when_none(self) -> None:
        cond = equal_to("name", "test")
        result = condition_to_dict(cond)
        assert "key" not in result


# ---------------------------------------------------------------------------
# condition_from_dict
# ---------------------------------------------------------------------------


class TestConditionFromDict:
    def test_not(self) -> None:
        data = {
            "condition": "Not",
            "child": {"condition": "EqualTo", "property": "x", "value": "1"},
        }
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionNot)
        assert isinstance(result.child, DbConditionComparison)

    def test_and(self) -> None:
        data = {
            "condition": "And",
            "children": [
                {"condition": "EqualTo", "property": "a", "value": "1"},
                {"condition": "EqualTo", "property": "b", "value": "2"},
            ],
        }
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionCombine)
        assert result.condition_type == DbConditionType.And
        assert len(result.children) == 2

    def test_equal_to(self) -> None:
        data = {"condition": "EqualTo", "property": "name", "value": "Alice"}
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.EqualTo
        assert result.property == "name"
        assert result.value == "Alice"
        assert result.mode == DbConditionMode.Property

    def test_mode_defaults_to_property(self) -> None:
        data = {"condition": "EqualTo", "property": "x", "value": "1"}
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionComparison)
        assert result.mode == DbConditionMode.Property

    def test_between(self) -> None:
        data = {
            "condition": "Between",
            "property": "age",
            "valueMin": "18",
            "valueMax": "65",
            "inclusiveMin": True,
            "inclusiveMax": False,
        }
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionBetween)
        assert result.value_min == "18"
        assert result.value_max == "65"
        assert result.inclusive_min is True
        assert result.inclusive_max is False

    def test_in(self) -> None:
        data = {"condition": "In", "property": "status", "values": ["A", "B"]}
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionComparisonArray)
        assert result.values == ["A", "B"]

    def test_intersects(self) -> None:
        data = {"condition": "Intersects", "property": "tags", "values": ["x"]}
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionArrayComparison)
        assert result.condition_type == DbConditionType.Intersects

    def test_contains(self) -> None:
        data = {"condition": "Contains", "property": "tags", "value": "urgent"}
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionContains)

    def test_link(self) -> None:
        data = {
            "condition": "Link",
            "property": "userId",
            "linkType": "UserEntity",
            "linkProperty": "id",
            "linkCondition": {
                "condition": "EqualTo",
                "property": "active",
                "value": "True",
            },
        }
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionLink)
        assert result.link_type == "UserEntity"
        assert isinstance(result.link_condition, DbConditionComparison)

    def test_like(self) -> None:
        data = {"condition": "Like", "property": "name", "value": "%test%"}
        result = condition_from_dict(data)
        assert isinstance(result, DbConditionComparison)
        assert result.condition_type == DbConditionType.Like


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_simple_equal_to(self) -> None:
        original = equal_to("name", "Alice")
        wire = condition_to_dict(original)
        restored = condition_from_dict(wire)
        assert isinstance(restored, DbConditionComparison)
        assert restored.property == original.property
        assert restored.value == original.value
        assert restored.condition_type == original.condition_type

    def test_nested_and_or(self) -> None:
        original = and_(
            or_(equal_to("a", "1"), equal_to("b", "2")),
            not_(equal_to("c", "3")),
        )
        wire = condition_to_dict(original)
        restored = condition_from_dict(wire)
        assert isinstance(restored, DbConditionCombine)
        assert len(restored.children) == 2
        assert isinstance(restored.children[0], DbConditionCombine)
        assert isinstance(restored.children[1], DbConditionNot)

    def test_between_round_trip(self) -> None:
        original = between("age", "18", "65", inclusive_min=True, inclusive_max=False)
        wire = condition_to_dict(original)
        restored = condition_from_dict(wire)
        assert isinstance(restored, DbConditionBetween)
        assert restored.value_min == "18"
        assert restored.inclusive_max is False

    def test_link_round_trip(self) -> None:
        original = link("userId", "User", "id", equal_to("status", "Active"))
        wire = condition_to_dict(original)
        restored = condition_from_dict(wire)
        assert isinstance(restored, DbConditionLink)
        assert isinstance(restored.link_condition, DbConditionComparison)


# ---------------------------------------------------------------------------
# Integration with generic serializer
# ---------------------------------------------------------------------------


class TestSerializerIntegration:
    def test_to_dict_with_condition(self) -> None:
        req = ReadDataTypeRequest(
            type_name="UserEntity",
            condition=equal_to("name", "Alice"),
        )
        result = to_dict(req)
        assert result is not None
        assert result["condition"] == {
            "condition": "EqualTo",
            "property": "name",
            "value": "Alice",
        }

    def test_to_dict_with_none_condition(self) -> None:
        req = ReadDataTypeRequest(type_name="UserEntity", condition=None)
        result = to_dict(req)
        assert result is not None
        assert result["condition"] is None

    def test_from_dict_with_condition(self) -> None:
        data = {
            "typeName": "UserEntity",
            "condition": {
                "condition": "EqualTo",
                "property": "status",
                "value": "Active",
            },
        }
        result = from_dict(data, ReadDataTypeRequest)
        assert result is not None
        assert isinstance(result.condition, DbConditionComparison)
        assert result.condition.value == "Active"

    def test_from_dict_with_none_condition(self) -> None:
        data = {"typeName": "UserEntity", "condition": None}
        result = from_dict(data, ReadDataTypeRequest)
        assert result is not None
        assert result.condition is None

    def test_to_dict_nested_condition(self) -> None:
        req = ReadDataTypeRequest(
            type_name="UserEntity",
            condition=and_(
                equal_to("active", "True"),
                less_than("age", "100"),
            ),
        )
        result = to_dict(req)
        assert result is not None
        cond = result["condition"]
        assert cond["condition"] == "And"
        assert len(cond["children"]) == 2


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


class TestConvenienceConstructors:
    def test_and(self) -> None:
        cond = and_(equal_to("a", "1"), equal_to("b", "2"))
        assert isinstance(cond, DbConditionCombine)
        assert cond.condition_type == DbConditionType.And

    def test_or(self) -> None:
        cond = or_(equal_to("a", "1"))
        assert isinstance(cond, DbConditionCombine)
        assert cond.condition_type == DbConditionType.Or

    def test_not(self) -> None:
        cond = not_(equal_to("x", "1"))
        assert isinstance(cond, DbConditionNot)

    def test_all_comparison_types(self) -> None:
        assert less_than("x", "1").condition_type == DbConditionType.LessThan
        assert less_than_or_equal_to("x", "1").condition_type == DbConditionType.LessThanOrEqualTo
        assert greater_than("x", "1").condition_type == DbConditionType.GreaterThan
        assert greater_than_or_equal_to("x", "1").condition_type == DbConditionType.GreaterThanOrEqualTo
        assert like("x", "%a%").condition_type == DbConditionType.Like

    def test_in(self) -> None:
        cond = in_("status", ["A", "B"])
        assert isinstance(cond, DbConditionComparisonArray)

    def test_contains(self) -> None:
        cond = contains("tags", "x")
        assert isinstance(cond, DbConditionContains)

    def test_set_operations(self) -> None:
        assert intersects("x", ["a"]).condition_type == DbConditionType.Intersects
        assert subset_of("x", ["a"]).condition_type == DbConditionType.SubsetOf
        assert superset_of("x", ["a"]).condition_type == DbConditionType.SupersetOf

    def test_link(self) -> None:
        cond = link("userId", "User", "id", equal_to("active", "True"))
        assert isinstance(cond, DbConditionLink)
        assert cond.link_type == "User"


# ---------------------------------------------------------------------------
# Subclass relationship
# ---------------------------------------------------------------------------


class TestSubclassing:
    def test_all_types_are_instances_of_base(self) -> None:
        instances: list[DbConditionInstance] = [
            DbConditionNot(child=equal_to("x", "1")),
            DbConditionCombine(),
            DbConditionComparison(),
            DbConditionBetween(),
            DbConditionComparisonArray(),
            DbConditionArrayComparison(),
            DbConditionContains(),
            DbConditionLink(link_condition=equal_to("x", "1")),
        ]
        for inst in instances:
            assert isinstance(inst, DbConditionInstance)

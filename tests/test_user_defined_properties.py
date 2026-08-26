"""Runtime tests for user-defined-property construction and conversion."""
from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

import pytest
from uuid_utils import UUID, uuid7

from filament._serialization import from_dict, to_dict
from filament.types.conditions import DbConditionComparison
from filament.types.generated.entities.task_service import TaskEntity
from filament.types.generated.enums import (
    PermissionAction,
    TypeSchemaPropertyType,
)
from filament.types.generated.misc import (
    EntityPropertyPermission,
    EntityPropertyPermissions,
    UserDefinedPropertyInfo,
)
from filament.user_defined_properties import (
    UserDefinedValueAdapter,
    build_user_defined_properties,
    hydrate_user_defined_properties,
)


def _definition(
    name: str,
    property_type: TypeSchemaPropertyType,
    *sub_types: TypeSchemaPropertyType,
) -> UserDefinedPropertyInfo:
    return UserDefinedPropertyInfo(
        id=uuid7(),
        name=name,
        label=name,
        property_type=property_type,
        sub_property_types=list(sub_types),
    )


def test_constructor_mapping_installs_and_tracks_udp_attributes() -> None:
    task = TaskEntity(
        _user_defined_properties={"udp_budget": Decimal("12.50"), "udp_note": "x"}
    )

    assert task.udp_budget == Decimal("12.50")
    assert task.udp_note == "x"
    assert task.specified_properties == ["id", "udp_budget", "udp_note"]
    # Reads resolve to the residual InitVar default rather than raising: a
    # raising read guard breaks dataclasses.replace(), which reads every
    # defaulted InitVar with getattr() to rebuild the instance.
    assert task._user_defined_properties is None

    task.udp_later = 3
    assert task.specified_properties == [
        "id", "udp_budget", "udp_note", "udp_later",
    ]


@pytest.mark.parametrize("name", ["budget", "user_budget", "UdpBudget", 3])
def test_constructor_mapping_rejects_non_udp_keys(name: object) -> None:
    with pytest.raises(ValueError, match="beginning with 'udp_'"):
        TaskEntity(_user_defined_properties={name: 1})  # type: ignore[dict-item]


def test_constructor_mapping_is_write_only() -> None:
    task = TaskEntity()
    with pytest.raises(AttributeError, match="constructor-only"):
        task._user_defined_properties = {"udp_x": 1}


@pytest.mark.parametrize(
    "property_type,wire,python_value",
    [
        (TypeSchemaPropertyType.Bool, False, False),
        (TypeSchemaPropertyType.Int, "12", 12),
        (TypeSchemaPropertyType.Double, "1.25", 1.25),
        (TypeSchemaPropertyType.Decimal, "12.50", Decimal("12.50")),
        (TypeSchemaPropertyType.Guid, "018f3f4e-7b5c-7e7d-8000-000000000001", UUID("018f3f4e-7b5c-7e7d-8000-000000000001")),
        (TypeSchemaPropertyType.Duration, "0:00:01:30", datetime.timedelta(seconds=90)),
    ],
)
def test_scalar_adapter_round_trip(
    property_type: TypeSchemaPropertyType, wire: object, python_value: object,
) -> None:
    adapter = UserDefinedValueAdapter(_definition("UdpValue", property_type))
    assert adapter.from_wire(wire) == python_value
    assert adapter.from_wire(adapter.to_wire(python_value)) == python_value


def test_collection_adapter_round_trip_and_typed_dictionary_keys() -> None:
    guid = uuid7()
    list_adapter = UserDefinedValueAdapter(_definition(
        "UdpAmounts", TypeSchemaPropertyType.List, TypeSchemaPropertyType.Decimal,
    ))
    assert list_adapter.from_wire(["1.5", "2.5"]) == [Decimal("1.5"), Decimal("2.5")]
    assert list_adapter.to_wire([Decimal("1.5")]) == ["1.5"]

    dict_adapter = UserDefinedValueAdapter(_definition(
        "UdpById",
        TypeSchemaPropertyType.Dictionary,
        TypeSchemaPropertyType.Guid,
        TypeSchemaPropertyType.Int,
    ))
    assert dict_adapter.from_wire({str(guid): "4"}) == {guid: 4}
    assert dict_adapter.to_wire({guid: 4}) == {str(guid): 4}


def test_invalid_collection_shape_is_rejected() -> None:
    with pytest.raises(ValueError, match="one element type"):
        UserDefinedValueAdapter(_definition(
            "UdpValues", TypeSchemaPropertyType.List,
        ))
    with pytest.raises(ValueError, match="Unsupported dictionary key"):
        UserDefinedValueAdapter(_definition(
            "UdpValues",
            TypeSchemaPropertyType.Dictionary,
            TypeSchemaPropertyType.Bool,
            TypeSchemaPropertyType.String,
        ))


def test_deserialization_captures_flat_udp_values_then_hydrates() -> None:
    task = from_dict(
        {"$type": "TaskEntity", "id": str(uuid7()), "udpBudget": "12.50"},
        TaskEntity,
    )
    assert task is not None
    assert task.udp_budget == "12.50"

    definition = _definition("UdpBudget", TypeSchemaPropertyType.Decimal)
    permissions = EntityPropertyPermissions(properties={
        "udpBudget": EntityPropertyPermission(
            actions=PermissionAction.All,
            user_defined_data=definition,
        ),
    })
    properties = build_user_defined_properties("TaskEntity", permissions)
    assert hydrate_user_defined_properties(task, properties) == set()
    assert task.udp_budget == Decimal("12.50")
    assert to_dict(task, user_defined_properties=properties)["udpBudget"] == "12.50"  # type: ignore[index]


@pytest.mark.parametrize("wire_name", ["udpBudget", "UdpBudget", "UDPBudget"])
def test_deserialization_recognises_udp_prefix_case_insensitively(
    wire_name: str,
) -> None:
    task = from_dict(
        {"$type": "TaskEntity", "id": str(uuid7()), wire_name: 4}, TaskEntity,
    )
    assert task is not None
    assert task.udp_budget == 4


def test_registry_property_builds_definition_aware_condition() -> None:
    definition = _definition("UdpBudget", TypeSchemaPropertyType.Decimal)
    permission = EntityPropertyPermission(
        actions=PermissionAction.Read,
        user_defined_data=definition,
    )
    properties = build_user_defined_properties(
        "TaskEntity", EntityPropertyPermissions(properties={"udpBudget": permission})
    )

    udp_property = properties["udp_budget"]
    assert udp_property.wire_name == "udpBudget"
    assert udp_property.permission is permission
    condition = udp_property.condition().equal_to(Decimal("7.25"))
    assert isinstance(condition, DbConditionComparison)
    assert condition.property == "udpBudget"
    assert condition.value == "7.25"


def test_specified_properties_base_initvar_annotation_is_not_stringified() -> None:
    """``InitVar`` must stay a real object so ``get_type_hints`` works on 3.10.

    ``_specified_properties`` deliberately omits ``from __future__ import
    annotations``.  With it, the annotation becomes the string
    ``"InitVar[...]"``, which Python 3.10's ``typing.get_type_hints`` evaluates
    and then rejects with ``TypeError: Forward references must evaluate to
    types``.  Python 3.11 relaxed that check, so a regression here is invisible
    on every version except 3.10.
    """
    import typing

    from filament._specified_properties import SpecifiedPropertiesBase

    annotation = SpecifiedPropertiesBase.__annotations__["_user_defined_properties"]
    assert not isinstance(annotation, str)
    assert isinstance(annotation, dataclasses.InitVar)

    # The real guarantee: resolving hints on a UDP-capable entity must not raise.
    hints = typing.get_type_hints(TaskEntity)
    assert hints["id"] is not None


def test_unknown_udp_name_is_rejected_when_definitions_are_supplied() -> None:
    """A typo must fail with a property-specific error, not a raw wire value.

    ``modify_data`` supplies an authoritative definition map, so an unmatched
    ``udp_`` name is a caller typo. Guessing a wire name would send the raw
    Python value and fail later inside the JSON encoder without naming the
    property.
    """
    definition = _definition("UdpBudget", TypeSchemaPropertyType.Decimal)
    permission = EntityPropertyPermission(
        actions=PermissionAction.Read, user_defined_data=definition,
    )
    properties = build_user_defined_properties(
        "TaskEntity", EntityPropertyPermissions(properties={"udpBudget": permission})
    )

    task = TaskEntity(id=uuid7(), _user_defined_properties={"udp_bugdet": Decimal("1")})

    with pytest.raises(ValueError, match="udp_bugdet"):
        to_dict(task, user_defined_properties=properties)

    # Without a definition map the lenient fallback is preserved.
    assert "udpBugdet" in to_dict(task)


@pytest.mark.parametrize(
    ("property_type", "comparable"),
    [
        # The backend's user-defined condition policy refuses ordering
        # operators for Bool and Interval only, and restricts Enum to
        # equality/In. Date and time types are ordered domains.
        (TypeSchemaPropertyType.String, True),
        (TypeSchemaPropertyType.LocalDate, True),
        (TypeSchemaPropertyType.LocalTime, True),
        (TypeSchemaPropertyType.LocalDateTime, True),
        (TypeSchemaPropertyType.DateTimeZone, True),
        (TypeSchemaPropertyType.Decimal, True),
        (TypeSchemaPropertyType.Bool, False),
        (TypeSchemaPropertyType.Interval, False),
        (TypeSchemaPropertyType.Enum, False),
    ],
)
def test_ordering_operators_match_backend_condition_policy(
    property_type: TypeSchemaPropertyType, comparable: bool,
) -> None:
    from filament._condition_builder import DbConditionComparableProperty

    definition = _definition("UdpValue", property_type)
    permission = EntityPropertyPermission(
        actions=PermissionAction.Read, user_defined_data=definition,
    )
    properties = build_user_defined_properties(
        "TaskEntity", EntityPropertyPermissions(properties={"udpValue": permission})
    )

    condition = properties["udp_value"].condition()
    assert isinstance(condition, DbConditionComparableProperty) is comparable


def test_dataclasses_replace_works_on_entities() -> None:
    """``dataclasses.replace()`` must work on every entity.

    ``replace()`` reads each defaulted ``InitVar`` with ``getattr()`` to rebuild
    the instance, so guarding reads of ``_user_defined_properties`` -- with a
    ``__getattribute__`` hook or a data descriptor -- breaks it on all supported
    Python versions. Writes stay guarded.
    """
    task = TaskEntity(id=uuid7())

    replaced = dataclasses.replace(task)
    assert isinstance(replaced, TaskEntity)
    assert replaced.id == task.id

    with pytest.raises(AttributeError, match="constructor-only"):
        task._user_defined_properties = {"udp_x": 1}

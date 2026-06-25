"""Tests for the _serialization module."""
from __future__ import annotations

import datetime

from uuid_utils import UUID, uuid7

from filament._serialization import (
    camel_case_from_snake_case,
    camel_case_property_names,
    from_dict,
    snake_case_from_camel_case,
    to_dict,
)
from filament.types.generated.base.base import EntityBase
from filament.types.generated.entities.permissions_service import PermissionEntity
from filament.types.generated.entities.user_service import UserEntity
from filament.types.generated.enums import (
    DataProcessingStatus,
    DataRequestAction,
    ErrorCodes,
    ReadMode,
    SubscriptionMode,
)
from filament.types.generated.misc import (
    DataUpdateRejectedNotification,
    DbOrderInstance,
    DbOrderLevel,
    ModifyDataRequest,
    ProcessingStatus,
    ReadConnectedDataTypeRequest,
    ReadDataRequest,
    ReadDataTypeRequest,
    RequestId,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionId,
    SuccessOrErrorPublic,
    ValueOrErrorPublic_1,
)


# ---------------------------------------------------------------------------
# Case conversion
# ---------------------------------------------------------------------------


class TestCaseConversion:
    def test_snake_to_camel(self) -> None:
        assert camel_case_from_snake_case("hello_world") == "helloWorld"
        assert camel_case_from_snake_case("id") == "id"
        assert camel_case_from_snake_case("scenario_id") == "scenarioId"

    def test_camel_to_snake(self) -> None:
        assert snake_case_from_camel_case("helloWorld") == "hello_world"
        assert snake_case_from_camel_case("scenarioId") == "scenario_id"

    def test_camel_case_property_names_converts_snake_case(self) -> None:
        assert camel_case_property_names(["schedule_status", "owner_user_id"]) == [
            "scheduleStatus",
            "ownerUserId",
        ]

    def test_camel_case_property_names_passes_through_camel_case(self) -> None:
        # Names already in wire form (no underscore) are left untouched.
        assert camel_case_property_names(["scheduleStatus", "id"]) == [
            "scheduleStatus",
            "id",
        ]

    def test_camel_case_property_names_dedupes_preserving_order(self) -> None:
        assert camel_case_property_names(
            ["schedule_status", "scheduleStatus", "id", "schedule_status"]
        ) == ["scheduleStatus", "id"]


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


class TestToDict:
    def test_simple_struct(self) -> None:
        sid = SubscriptionId(id=UUID("12345678-1234-5678-1234-567812345678"))
        result = to_dict(sid)
        assert result is not None
        assert result["id"] == "12345678-1234-5678-1234-567812345678"

    def test_subscription_config(self) -> None:
        sc = SubscriptionConfig(
            scenario_id=UUID("00000000-0000-0000-0000-000000000000"),
            status=DataProcessingStatus.Pending,
            type_name="TestEntity",
            condition=None,
            mode=SubscriptionMode.Data,
        )
        result = to_dict(sc)
        assert result is not None
        assert result["scenarioId"] == "00000000-0000-0000-0000-000000000000"
        assert result["status"] == DataProcessingStatus.Pending.value
        assert result["typeName"] == "TestEntity"
        assert result["condition"] is None
        assert result["mode"] == SubscriptionMode.Data.value

    def test_none_enum_value(self) -> None:
        sc = SubscriptionConfig(
            mode=SubscriptionMode.None_,
        )
        result = to_dict(sc)
        assert result is not None
        assert result["mode"] == 0

    def test_nested_dataclass(self) -> None:
        req = ReadDataRequest(
            scenario_id=UUID("00000000-0000-0000-0000-000000000000"),
            request=ReadDataTypeRequest(
                type_name="UserEntity",
                mode=ReadMode.Data,
                limit=10,
            ),
        )
        result = to_dict(req)
        assert result is not None
        assert result["scenarioId"] == "00000000-0000-0000-0000-000000000000"
        assert isinstance(result["request"], dict)
        assert result["request"]["typeName"] == "UserEntity"
        assert result["request"]["limit"] == 10

    def test_non_dataclass_returns_none(self) -> None:
        assert to_dict("not a dataclass") is None
        assert to_dict(42) is None

    def test_list_of_request_ids(self) -> None:
        ps = ProcessingStatus(
            scenario_id=uuid7(),
            request_ids=[RequestId(id=uuid7()), RequestId(id=uuid7())],
            message="test",
        )
        result = to_dict(ps)
        assert result is not None
        assert len(result["requestIds"]) == 2
        assert isinstance(result["requestIds"][0], dict)

    def test_datetime_formatting(self) -> None:
        dt = datetime.datetime(2025, 1, 15, 10, 30, 45, 123456)
        ps = ProcessingStatus(
            scenario_id=uuid7(),
            request_ids=[],
            message="",
        )
        # ModifyDataRequest has a datetime field (requested_at)
        req = ModifyDataRequest(
            requested_at=dt,
            action=DataRequestAction.Create,
        )
        result = to_dict(req)
        assert result is not None
        assert result["requestedAt"] == "2025-01-15T10:30:45.12345600Z"

    def test_read_data_request_converts_property_names(self) -> None:
        req = ReadDataTypeRequest(
            type_name="UserEntity",
            mode=ReadMode.Distinct,
            properties=["schedule_status", "owner_user_id"],
        )
        result = to_dict(req)
        assert result is not None
        assert result["properties"] == ["scheduleStatus", "ownerUserId"]

    def test_read_data_request_dedupes_property_names(self) -> None:
        req = ReadDataTypeRequest(
            type_name="UserEntity",
            mode=ReadMode.Distinct,
            # snake_case and the equivalent camelCase collapse to one entry.
            properties=["schedule_status", "scheduleStatus", "schedule_status"],
        )
        result = to_dict(req)
        assert result is not None
        assert result["properties"] == ["scheduleStatus"]

    def test_modify_data_request_converts_update_properties(self) -> None:
        req = ModifyDataRequest(
            action=DataRequestAction.Update,
            update_properties=["schedule_status", "owner_user_id"],
        )
        result = to_dict(req)
        assert result is not None
        assert result["updateProperties"] == ["scheduleStatus", "ownerUserId"]

    def test_read_connected_data_request_converts_property_lists(self) -> None:
        req = ReadConnectedDataTypeRequest(
            connection_type_name="WorksOn",
            entity_type_name="UserEntity",
            connection_properties=["start_date"],
            entity_properties=["schedule_status", "owner_user_id"],
        )
        result = to_dict(req)
        assert result is not None
        assert result["connectionProperties"] == ["startDate"]
        assert result["entityProperties"] == ["scheduleStatus", "ownerUserId"]

    def test_property_names_default_none_is_preserved(self) -> None:
        req = ReadDataTypeRequest(type_name="UserEntity", mode=ReadMode.Data)
        result = to_dict(req)
        assert result is not None
        assert result["properties"] is None

    def test_permission_entity_converts_property_names(self) -> None:
        # PermissionEntity.properties is an entity field (not a request filter)
        # but shares the "properties" name, so its property names are normalised
        # the same way — callers can pass snake_case here too.
        entity = PermissionEntity(properties=["schedule_status", "owner_user_id"])
        result = to_dict(entity)
        assert result is not None
        assert result["properties"] == ["scheduleStatus", "ownerUserId"]


# ---------------------------------------------------------------------------
# from_dict
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_simple_struct(self) -> None:
        data = {"id": "12345678-1234-5678-1234-567812345678"}
        result = from_dict(data, SubscriptionId)
        assert result is not None
        assert result.id == UUID("12345678-1234-5678-1234-567812345678")

    def test_subscription_config(self) -> None:
        data = {
            "scenarioId": "00000000-0000-0000-0000-000000000000",
            "status": 1,  # Pending
            "typeName": "TestEntity",
            "condition": None,
            "mode": 1,  # Data
        }
        result = from_dict(data, SubscriptionConfig)
        assert result is not None
        assert result.scenario_id == UUID("00000000-0000-0000-0000-000000000000")
        assert result.status == DataProcessingStatus.Pending
        assert result.type_name == "TestEntity"
        assert result.mode == SubscriptionMode.Data

    def test_nested_dataclass(self) -> None:
        data = {
            "scenarioId": "00000000-0000-0000-0000-000000000000",
            "request": {
                "typeName": "UserEntity",
                "mode": 0,
                "limit": 10,
                "offset": 0,
                "applyPendingRequests": False,
            },
        }
        result = from_dict(data, ReadDataRequest)
        assert result is not None
        assert result.request.type_name == "UserEntity"
        assert result.request.limit == 10

    def test_generic_type(self) -> None:
        data = {
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": "12345678-1234-5678-1234-567812345678"},
        }
        result = from_dict(data, ValueOrErrorPublic_1[SubscriptionId])
        assert result is not None
        assert result.success is True
        assert result.data is not None
        assert result.data.id == UUID("12345678-1234-5678-1234-567812345678")

    def test_generic_type_error(self) -> None:
        data = {
            "success": False,
            "message": "Something went wrong",
            "errorCode": 2,  # InternalError
            "data": None,
        }
        result = from_dict(data, ValueOrErrorPublic_1[SubscriptionId])
        assert result is not None
        assert result.success is False
        assert result.message == "Something went wrong"
        assert result.error_code == ErrorCodes.InternalError
        assert result.data is None

    def test_list_field(self) -> None:
        rid1 = str(uuid7())
        rid2 = str(uuid7())
        data = {
            "scenarioId": str(uuid7()),
            "requestIds": [{"id": rid1}, {"id": rid2}],
            "message": "test",
        }
        result = from_dict(data, ProcessingStatus)
        assert result is not None
        assert len(result.request_ids) == 2
        assert result.request_ids[0].id == UUID(rid1)

    def test_missing_fields_use_defaults(self) -> None:
        data = {"success": True, "message": "ok"}
        result = from_dict(data, SuccessOrErrorPublic)
        assert result is not None
        assert result.success is True
        assert result.error_id is None

    def test_non_dataclass_returns_none(self) -> None:
        assert from_dict({}, str) is None


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_subscription_config_roundtrip(self) -> None:
        original = SubscriptionConfig(
            scenario_id=UUID("11111111-2222-3333-4444-555555555555"),
            status=DataProcessingStatus.Pending,
            type_name="ScopeEntity",
            condition=None,
            mode=SubscriptionMode.Data | SubscriptionMode.Progress,
        )
        serialized = to_dict(original)
        assert serialized is not None
        restored = from_dict(serialized, SubscriptionConfig)
        assert restored is not None
        assert restored.scenario_id == original.scenario_id
        assert restored.status == original.status
        assert restored.type_name == original.type_name
        assert restored.mode == original.mode

    def test_read_data_request_roundtrip(self) -> None:
        original = ReadDataRequest(
            scenario_id=uuid7(),
            request=ReadDataTypeRequest(
                type_name="TaskEntity",
                order=DbOrderInstance(
                    levels=[DbOrderLevel(property="name")],
                ),
                mode=ReadMode.Data,
                limit=50,
                offset=10,
            ),
        )
        serialized = to_dict(original)
        assert serialized is not None
        restored = from_dict(serialized, ReadDataRequest)
        assert restored is not None
        assert restored.request.type_name == original.request.type_name
        assert restored.request.limit == original.request.limit
        assert restored.request.offset == original.request.offset
        assert restored.request.order is not None
        assert len(restored.request.order.levels) == 1

    def test_value_or_error_roundtrip(self) -> None:
        original = ValueOrErrorPublic_1[SubscriptionId](
            success=True,
            message="",
            error_code=ErrorCodes.None_,
            error_id=None,
            data=SubscriptionId(id=uuid7()),
        )
        serialized = to_dict(original)
        assert serialized is not None
        restored = from_dict(serialized, ValueOrErrorPublic_1[SubscriptionId])
        assert restored is not None
        assert restored.success is True
        assert restored.data is not None
        assert original.data is not None
        assert restored.data.id == original.data.id

    def test_service_data_update_roundtrip(self) -> None:
        """Round-trips the new part_idx / part_count fields on ServiceDataUpdate."""
        original = ServiceDataUpdate(
            scenario_id=UUID("11111111-2222-3333-4444-555555555555"),
            status=DataProcessingStatus.Pending,
            request_ids=[RequestId(id=uuid7())],
            subscription_id=SubscriptionId(id=uuid7()),
            modified_entities={},
            progress=True,
            progress_amount=0.5,
            sequence_idx=42,
            part_idx=1,
            part_count=3,
        )
        serialized = to_dict(original)
        assert serialized is not None
        restored = from_dict(serialized, ServiceDataUpdate)
        assert restored is not None
        assert restored.scenario_id == original.scenario_id
        assert restored.status == original.status
        assert restored.subscription_id.id == original.subscription_id.id
        assert restored.progress is True
        assert restored.progress_amount == 0.5
        assert restored.sequence_idx == 42
        assert restored.part_idx == 1
        assert restored.part_count == 3
        assert len(restored.request_ids) == 1
        assert restored.request_ids[0].id == original.request_ids[0].id

    def test_data_update_rejected_notification_roundtrip(self) -> None:
        original = DataUpdateRejectedNotification(
            subscription_id=SubscriptionId(id=uuid7()),
            actual_bytes=4_194_304,
            limit_bytes=1_048_576,
            reason="ServiceDataUpdate exceeds negotiated limit",
        )
        serialized = to_dict(original)
        assert serialized is not None
        restored = from_dict(serialized, DataUpdateRejectedNotification)
        assert restored is not None
        assert restored.subscription_id.id == original.subscription_id.id
        assert restored.actual_bytes == 4_194_304
        assert restored.limit_bytes == 1_048_576
        assert restored.reason == original.reason


# ---------------------------------------------------------------------------
# EntityBase $type round-trip
# ---------------------------------------------------------------------------


class TestEntityBaseRoundTrip:
    def test_entity_tracks_specified_properties(self) -> None:
        """Direct construction records only the explicitly passed kwargs."""
        user = UserEntity(given_name="Alice", family_name="Smith")
        props = getattr(user, "specified_properties", None)
        assert props is not None
        assert "given_name" in props
        assert "family_name" in props
        assert "created_at" not in props
        assert "created_by_id" not in props
        assert "updated_at" not in props
        assert "updated_by_id" not in props

    def test_specified_properties_includes_id(self) -> None:
        """``id`` is always present even when not explicitly passed."""
        user = UserEntity(given_name="Alice")
        props = getattr(user, "specified_properties", None)
        assert props is not None
        assert "id" in props

    def test_explicit_id_not_duplicated(self) -> None:
        """Passing ``id`` explicitly does not create a duplicate entry."""
        my_id = uuid7()
        user = UserEntity(id=my_id, given_name="Alice")
        props = getattr(user, "specified_properties", None)
        assert props is not None
        assert props.count("id") == 1

    def test_to_dict_filters_to_specified_properties(self) -> None:
        """Audit fields are excluded from serialized output."""
        user = UserEntity(given_name="Alice", family_name="Smith")
        result = to_dict(user)
        assert result is not None
        assert "givenName" in result
        assert "familyName" in result
        assert "id" in result
        assert "$type" in result
        assert "createdAt" not in result
        assert "createdById" not in result
        assert "updatedAt" not in result
        assert "updatedById" not in result

    def test_from_dict_clears_specified_properties(self) -> None:
        """Deserialized entities have specified_properties=None so all fields serialize."""
        data = {
            "$type": "UserEntity",
            "id": str(uuid7()),
            "givenName": "Alice",
            "familyName": "Smith",
            "createdAt": "2025-01-15T10:30:45.12345600Z",
        }
        restored = from_dict(data, EntityBase)
        assert restored is not None
        assert getattr(restored, "specified_properties", "missing") is None
        # Re-serializing should include all fields (no filtering)
        reserialized = to_dict(restored)
        assert reserialized is not None
        assert "createdAt" in reserialized

    def test_entity_base_type_dispatch(self) -> None:
        """to_dict writes $type; from_dict with EntityBase target uses it to restore
        the concrete subclass."""
        original = UserEntity(given_name="Alice", family_name="Smith")

        serialized = to_dict(original)
        assert serialized is not None
        assert "$type" in serialized
        assert serialized["$type"] == "UserEntity"

        restored = from_dict(serialized, EntityBase)
        assert restored is not None
        assert isinstance(restored, UserEntity), (
            f"Expected UserEntity but got {type(restored).__name__}"
        )
        assert restored.given_name == "Alice"
        assert restored.family_name == "Smith"

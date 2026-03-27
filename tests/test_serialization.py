"""Tests for the _serialization module."""
from __future__ import annotations

import datetime

from uuid_utils import UUID, uuid7

from filament._serialization import (
    camel_case_from_snake_case,
    from_dict,
    snake_case_from_camel_case,
    to_dict,
)
from filament.types.generated.enums import (
    DataProcessingStatus,
    DataRequestAction,
    ErrorCodes,
    ReadMode,
    SubscriptionMode,
)
from filament.types.generated.misc import (
    DbOrderInstance,
    DbOrderLevel,
    ModifyDataRequest,
    ProcessingStatus,
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

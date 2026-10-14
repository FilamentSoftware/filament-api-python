"""Tests for the _serialization module."""
from __future__ import annotations

import base64
import datetime
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import brotli
import pytest
from uuid_utils import UUID, uuid7

from filament import _serialization
from filament._exceptions import CompressedPropertyError
from filament._serialization import (
    backend_camel_case,
    camel_case_from_snake_case,
    camel_case_property_names,
    from_dict,
    snake_case_from_camel_case,
    to_dict,
)
from filament._wire_formats import instant_from_wire, instant_to_wire
from filament.types.generated.base.base import EntityBase
from filament.types.generated.entities.note_service import NoteEntity
from filament.types.generated.entities.permissions_service import PermissionEntity
from filament.types.generated.entities.scope_item_service import ScopeItemEntity
from filament.types.generated.entities.task_service import TaskEntity
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
from filament.types.generated.shared import FileReference


# ---------------------------------------------------------------------------
# Case conversion
# ---------------------------------------------------------------------------


class TestCaseConversion:
    def test_snake_to_camel(self) -> None:
        assert camel_case_from_snake_case("hello_world") == "helloWorld"
        assert camel_case_from_snake_case("id") == "id"
        assert camel_case_from_snake_case("scenario_id") == "scenarioId"

    def test_snake_to_camel_drops_empty_segments(self) -> None:
        # Leading / trailing / doubled underscores yield empty segments that
        # are dropped so the result stays well-formed camelCase.
        assert camel_case_from_snake_case("_foo_bar") == "fooBar"
        assert camel_case_from_snake_case("foo__bar") == "fooBar"
        assert camel_case_from_snake_case("trailing_") == "trailing"
        assert camel_case_from_snake_case("__") == ""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("UdpFooBar", "udpFooBar"),
            ("udpFooBar", "udpFooBar"),
            # A digit/letter boundary starts a new word in both directions.
            ("UdpFoo2bar", "udpFoo2Bar"),
            ("UdpFoo2Bar", "udpFoo2Bar"),
            ("Udp2ndValue", "udp2NdValue"),
            # Casing is otherwise preserved: capital runs stay capitalised.
            ("UDPFooBar", "uDPFooBar"),
            # Separators are dropped and start a new word; leading ones are ignored.
            ("_foo_bar", "fooBar"),
            ("foo bar", "fooBar"),
            ("", ""),
        ],
    )
    def test_backend_camel_case_mirrors_backend_to_camel_case(
        self, name: str, expected: str,
    ) -> None:
        assert backend_camel_case(name) == expected

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

    def test_camel_case_property_names_empty_list(self) -> None:
        assert camel_case_property_names([]) == []

    def test_camel_case_property_names_handles_extra_underscores(self) -> None:
        # Leading, trailing, and doubled underscores produce empty segments that
        # are dropped, so the camelCase output stays well-formed.
        assert camel_case_property_names(["_foo_bar"]) == ["fooBar"]
        assert camel_case_property_names(["a__b"]) == ["aB"]
        assert camel_case_property_names(["trailing_"]) == ["trailing"]


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
        # PermissionEntity.include_properties / exclude_properties are entity
        # fields (not request filters) holding property names, so they are
        # normalised the same way — callers can pass snake_case here too.
        entity = PermissionEntity(
            include_properties=["schedule_status", "owner_user_id"],
            exclude_properties=["start_date"],
        )
        result = to_dict(entity)
        assert result is not None
        assert result["includeProperties"] == ["scheduleStatus", "ownerUserId"]
        assert result["excludeProperties"] == ["startDate"]


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


class TestJsonCompanionFields:
    """`<name>` object fields auto-populated from their `<name>_data` JSON sibling.

    The backend sends projections like ``UserEntity.imageData`` as a JSON string
    with the object field ``image`` left unset; ``from_dict`` deserializes the
    string into ``image``.
    """

    def test_image_populated_from_image_data(self) -> None:
        image_json = json.dumps({
            "success": True,
            "message": "",
            "errorCode": 0,
            "url": "https://storage/read/1",
            "refreshable": True,
            "metadata": {"contentType": "image/png", "size": 12, "width": 4, "height": 4},
        })
        user = from_dict({"id": str(uuid7()), "imageData": image_json}, UserEntity)

        assert user is not None
        # The raw projection is preserved...
        assert user.image_data == image_json
        # ...and the object field is deserialized from it.
        assert isinstance(user.image, FileReference)
        assert user.image.url == "https://storage/read/1"
        assert user.image.metadata is not None
        assert user.image.metadata.content_type == "image/png"

    def test_absent_image_data_leaves_image_none(self) -> None:
        user = from_dict({"id": str(uuid7())}, UserEntity)
        assert user is not None
        assert user.image_data is None
        assert user.image is None

    def test_empty_image_data_leaves_image_none(self) -> None:
        user = from_dict({"id": str(uuid7()), "imageData": ""}, UserEntity)
        assert user is not None
        assert user.image is None

    def test_malformed_image_data_leaves_image_none(self) -> None:
        # A malformed projection must not fail the whole entity deserialization.
        user = from_dict({"id": str(uuid7()), "imageData": "not json"}, UserEntity)
        assert user is not None
        assert user.image_data == "not json"
        assert user.image is None

    def test_image_data_raising_attribute_error_leaves_image_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # from_dict can raise AttributeError for a malformed nested object (the
        # same family file_reference_from_json normalises); that must leave the
        # one field None rather than fail the whole entity.
        real_from_dict = _serialization.from_dict

        def from_dict_raising_for_file_reference(data: Any, cls: Any) -> Any:
            if cls is FileReference:
                raise AttributeError("malformed nested object")
            return real_from_dict(data, cls)

        monkeypatch.setattr(
            _serialization, "from_dict", from_dict_raising_for_file_reference,
        )
        payload = json.dumps({"success": True, "errorCode": 0})
        with caplog.at_level(logging.WARNING, logger="filament._serialization"):
            user = real_from_dict({"id": str(uuid7()), "imageData": payload}, UserEntity)

        assert user is not None
        assert user.image_data == payload
        assert user.image is None
        assert "Failed to deserialize UserEntity.image" in caplog.text

    @pytest.mark.parametrize("payload", ["[]", "123", '"text"', "null", "true"])
    def test_non_object_image_data_leaves_image_none(self, payload: str) -> None:
        # Valid JSON that is not an object (array/scalar) must be ignored, not
        # coerced into a bogus default FileReference or raised.
        user = from_dict({"id": str(uuid7()), "imageData": payload}, UserEntity)
        assert user is not None
        assert user.image is None


@dataclass(kw_only=True)
class _CompressedInner:
    body: Optional[str] = field(
        default=None, metadata={"compression": "br", "encoding": "base64"},
    )


@dataclass(kw_only=True)
class _CompressedOuter:
    foo_data: Optional[str] = None
    foo: Optional[_CompressedInner] = None


class TestJsonCompanionCompressedFields:
    """Compressed fields inside a ``<name>_data`` JSON projection.

    A decode failure there must surface as ``CompressedPropertyError`` rather
    than being swallowed as a malformed projection (leaving ``<name>`` None).
    """

    def test_valid_compressed_value_in_projection_decodes(self) -> None:
        encoded = "br+base64:" + base64.b64encode(
            brotli.compress("café ☃".encode("utf-8"))
        ).decode("ascii")
        outer = from_dict(
            {"fooData": json.dumps({"body": encoded})}, _CompressedOuter,
        )
        assert outer is not None
        assert outer.foo is not None
        assert outer.foo.body == "café ☃"

    def test_invalid_compressed_value_in_projection_raises(self) -> None:
        with pytest.raises(
            CompressedPropertyError, match=r"_CompressedInner\.body",
        ):
            from_dict(
                {"fooData": json.dumps({"body": "br+base64:!!!not-base64"})},
                _CompressedOuter,
            )


class TestInstantWireFormat:
    """`instant_from_wire` parses the backend Instant format at any precision.

    The canonical wire form carries up to 8 significant fractional digits (e.g.
    ``...:57.23421050Z``); the parser must handle that, not only the special case
    where the last two digits are ``00``.
    """

    def test_parses_eight_digit_fraction(self) -> None:
        dt = instant_from_wire("2026-08-11T14:42:57.23421050Z")
        assert dt == datetime.datetime(
            2026, 8, 11, 14, 42, 57, 234210, tzinfo=datetime.timezone.utc
        )

    def test_parses_six_significant_digits_padded(self) -> None:
        # The form to_wire emits: 6 microsecond digits + "00".
        dt = instant_from_wire("2025-01-15T10:30:45.12345600Z")
        assert dt.microsecond == 123456
        assert dt.tzinfo == datetime.timezone.utc

    def test_parses_without_fraction(self) -> None:
        dt = instant_from_wire("2026-08-11T14:42:57Z")
        assert dt.microsecond == 0
        assert dt.tzinfo == datetime.timezone.utc

    def test_truncates_sub_microsecond_digits(self) -> None:
        # 9 fractional digits (nanoseconds) — truncate to microseconds.
        dt = instant_from_wire("2026-08-11T14:42:57.123456789Z")
        assert dt.microsecond == 123456

    def test_requires_trailing_z(self) -> None:
        with pytest.raises(ValueError):
            instant_from_wire("2026-08-11T14:42:57.23421050")

    def test_roundtrip_through_to_wire(self) -> None:
        original = datetime.datetime(
            2026, 8, 11, 14, 42, 57, 234210, tzinfo=datetime.timezone.utc
        )
        assert instant_from_wire(instant_to_wire(original)) == original


# ---------------------------------------------------------------------------
# Polymorphic union fields
# ---------------------------------------------------------------------------

@dataclass
class _BareUnionHolder:
    """A union with no ``None`` arm — a shape codegen does not emit yet."""

    ref: Union[ScopeItemEntity, NoteEntity] = field(
        default_factory=lambda: NoteEntity()
    )


@dataclass
class _CollectionUnionHolder:
    """A union of two parameterised collection arms."""

    ref: Union[list[str], dict[str, str]] = field(default_factory=list)


@dataclass
class _BroadCollectionUnionHolder:
    """A broad arm ahead of a parameterised collection arm."""

    ref: Union[object, dict[str, datetime.datetime]] = ""


@dataclass
class _BroadUnionHolder:
    """A union whose broad arm precedes the specific one."""

    ref: Union[object, ScopeItemEntity] = ""


@dataclass
class _MixedUnionHolder:
    """A union mixing a dataclass arm with a plain scalar arm."""

    ref: Union[ScopeItemEntity, str] = ""


class TestUnionFieldDispatch:
    """Backend navigation properties such as ``reference`` are generated as a
    union of the concrete entities they may point at (e.g.
    ``TaskEntity.reference: Optional[Union[ScopeItemEntity, TaskEntity]]``).
    Deserialization must pick the arm named by the payload's ``$type`` tag
    rather than defaulting to the first one.
    """

    def test_picks_first_union_arm_by_type_tag(self) -> None:
        task = from_dict(
            {"name": "T", "reference": {"$type": "ScopeItemEntity", "name": "SI"}},
            TaskEntity,
        )
        assert task is not None
        assert isinstance(task.reference, ScopeItemEntity)
        assert task.reference.name == "SI"

    def test_picks_later_union_arm_by_type_tag(self) -> None:
        task = from_dict(
            {"name": "T", "reference": {"$type": "TaskEntity", "name": "REF"}},
            TaskEntity,
        )
        assert task is not None
        assert isinstance(task.reference, TaskEntity)
        assert task.reference.name == "REF"

    def test_untagged_payload_falls_back_to_first_arm(self) -> None:
        task = from_dict({"name": "T", "reference": {"name": "SI"}}, TaskEntity)
        assert task is not None
        assert isinstance(task.reference, ScopeItemEntity)

    def test_unknown_type_tag_raises(self) -> None:
        """An unrecognised ``$type`` must not silently build the first arm."""
        with pytest.raises(ValueError, match="NotAnEntity"):
            from_dict(
                {"name": "T", "reference": {"$type": "NotAnEntity", "name": "X"}},
                TaskEntity,
            )

    def test_type_tag_outside_union_raises(self) -> None:
        """A known entity that is not a declared arm must not be coerced."""
        with pytest.raises(
            ValueError, match="not one of the declared union members"
        ):
            from_dict(
                {"name": "T", "reference": {"$type": "UserEntity", "name": "U"}},
                TaskEntity,
            )

    def test_null_union_field_stays_none(self) -> None:
        task = from_dict({"name": "T", "reference": None}, TaskEntity)
        assert task is not None
        assert task.reference is None

    def test_single_arm_optional_entity_still_dispatches(self) -> None:
        """``Optional[X]`` keeps the existing ``$type`` behaviour."""
        note = from_dict(
            {"title": "N", "on": {"$type": "TaskEntity", "name": "T"}}, NoteEntity
        )
        assert note is not None
        assert isinstance(note.on, TaskEntity)
        assert note.on.name == "T"

    def test_bare_union_field_serializes(self) -> None:
        """A union with no ``None`` arm must still be unwrapped by ``to_dict``.

        Codegen only emits ``Optional[Union[...]]`` today, so this guards the
        shape rather than a current type: an un-unwrapped union matches no
        branch in ``_convert_to_dict_value`` and would leak the raw entity.
        """
        wire = to_dict(_BareUnionHolder(ref=NoteEntity(title="N")))
        assert wire is not None
        assert isinstance(wire["ref"], dict)
        assert wire["ref"]["title"] == "N"

    def test_mixed_union_keeps_non_dataclass_value(self) -> None:
        """A non-dataclass value must not be routed down a dataclass arm.

        With the dataclass arm declared first, taking ``members[0]`` would send
        the ``str`` through ``to_dict``, which returns ``None`` for a
        non-dataclass and would drop the value silently.
        """
        wire = to_dict(_MixedUnionHolder(ref="plain string"))
        assert wire is not None
        assert wire["ref"] == "plain string"

    def test_mixed_union_still_serializes_entity_arm(self) -> None:
        wire = to_dict(_MixedUnionHolder(ref=ScopeItemEntity(name="SI")))
        assert wire is not None
        assert isinstance(wire["ref"], dict)
        assert wire["ref"]["name"] == "SI"

    def test_broad_union_arm_does_not_shadow_the_specific_one(self) -> None:
        """A superclass arm must not win over the arm that actually matches.

        Every value is an instance of ``object``, so returning the first
        isinstance match would pick ``object`` here, leave the entity
        unserialized, and hand a raw dataclass to the JSON encoder.
        """
        wire = to_dict(_BroadUnionHolder(ref=ScopeItemEntity(name="SI")))
        assert wire is not None
        assert isinstance(wire["ref"], dict)
        assert wire["ref"]["name"] == "SI"

    def test_collection_union_picks_the_dict_arm(self) -> None:
        """A dict value must not be serialized through the list arm."""
        wire = to_dict(_CollectionUnionHolder(ref={"k": "v"}))
        assert wire is not None
        assert wire["ref"] == {"k": "v"}

    def test_collection_union_picks_the_list_arm(self) -> None:
        wire = to_dict(_CollectionUnionHolder(ref=["a", "b"]))
        assert wire is not None
        assert wire["ref"] == ["a", "b"]

    def test_broad_arm_does_not_shadow_a_collection_arm(self) -> None:
        """Specificity ranking must see through a parameterised arm's origin.

        The value is a dict of datetimes, so routing it through the ``object``
        arm leaves the datetimes unconverted rather than in wire format --
        a passthrough that a dict of plain strings would not expose.
        """
        wire = to_dict(
            _BroadCollectionUnionHolder(
                ref={"k": datetime.datetime(2025, 1, 15, 10, 30, 45, 123456)}
            )
        )
        assert wire is not None
        assert wire["ref"] == {"k": "2025-01-15T10:30:45.12345600Z"}

    def test_union_field_round_trips(self) -> None:
        original = TaskEntity(name="T", reference=TaskEntity(name="REF"))
        restored = from_dict(to_dict(original) or {}, TaskEntity)
        assert restored is not None
        assert isinstance(restored.reference, TaskEntity)
        assert restored.reference.name == "REF"

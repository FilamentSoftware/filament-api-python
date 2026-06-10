"""Tests for the DataOption module."""
from __future__ import annotations

import datetime
from decimal import Decimal

import uuid_utils

from filament._serialization import from_dict, to_dict
from filament.types.generated.enums import DataOptionType
from filament.types.generated.misc import PreviewDataRequest
from filament.types.options import (
    DataOption,
    bool_option,
    decimal_option,
    double_option,
    duration_option,
    guid_option,
    instant_option,
    int_option,
    null_option,
    option_from_dict,
    option_to_dict,
    string_option,
)


# ---------------------------------------------------------------------------
# option_to_dict
# ---------------------------------------------------------------------------


class TestOptionToDict:
    def test_null(self) -> None:
        result = option_to_dict(null_option())
        assert result == {"valueType": 1, "value": None}

    def test_string(self) -> None:
        result = option_to_dict(string_option("hello"))
        assert result == {"valueType": 2, "value": "hello"}

    def test_bool_true(self) -> None:
        result = option_to_dict(bool_option(True))
        assert result == {"valueType": 3, "value": True}

    def test_bool_false(self) -> None:
        result = option_to_dict(bool_option(False))
        assert result == {"valueType": 3, "value": False}

    def test_int(self) -> None:
        result = option_to_dict(int_option(42))
        assert result == {"valueType": 4, "value": 42}

    def test_double(self) -> None:
        result = option_to_dict(double_option(3.14))
        assert result == {"valueType": 5, "value": 3.14}

    def test_decimal(self) -> None:
        result = option_to_dict(decimal_option(Decimal("99.99")))
        assert result == {"valueType": 6, "value": 99.99}

    def test_guid(self) -> None:
        uid = uuid_utils.uuid7()
        result = option_to_dict(guid_option(uid))
        assert result == {"valueType": 7, "value": str(uid)}

    def test_instant(self) -> None:
        dt = datetime.datetime(2025, 6, 15, 10, 30, 0)
        result = option_to_dict(instant_option(dt))
        assert result["valueType"] == 8
        assert isinstance(result["value"], str)
        assert "2025-06-15" in result["value"]

    def test_duration(self) -> None:
        td = datetime.timedelta(hours=2, minutes=30)
        result = option_to_dict(duration_option(td))
        assert result["valueType"] == 9
        assert result["value"] == "0:02:30:00"

    def test_invalid(self) -> None:
        opt = DataOption(value_type=DataOptionType.Invalid.value, value=None)
        result = option_to_dict(opt)
        assert result == {"valueType": 0, "value": None}

    def test_string_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.StringArray.value, value=["a", "b", "c"]
        )
        result = option_to_dict(opt)
        assert result == {"valueType": 15, "value": ["a", "b", "c"]}

    def test_int_array(self) -> None:
        opt = DataOption(value_type=DataOptionType.IntArray.value, value=[1, 2, 3])
        result = option_to_dict(opt)
        assert result == {"valueType": 17, "value": [1, 2, 3]}

    def test_guid_array(self) -> None:
        uid1 = uuid_utils.uuid7()
        uid2 = uuid_utils.uuid7()
        opt = DataOption(
            value_type=DataOptionType.GuidArray.value, value=[uid1, uid2]
        )
        result = option_to_dict(opt)
        assert result["valueType"] == 20
        assert result["value"] == [str(uid1), str(uid2)]


# ---------------------------------------------------------------------------
# option_from_dict
# ---------------------------------------------------------------------------


class TestOptionFromDict:
    def test_null(self) -> None:
        result = option_from_dict({"valueType": 1, "value": None})
        assert result.value_type == DataOptionType.Null.value
        assert result.value is None

    def test_string(self) -> None:
        result = option_from_dict({"valueType": 2, "value": "hello"})
        assert result.value_type == DataOptionType.String.value
        assert result.value == "hello"

    def test_bool(self) -> None:
        result = option_from_dict({"valueType": 3, "value": True})
        assert result.value_type == DataOptionType.Bool.value
        assert result.value is True

    def test_int(self) -> None:
        result = option_from_dict({"valueType": 4, "value": 42})
        assert result.value_type == DataOptionType.Int.value
        assert result.value == 42

    def test_double(self) -> None:
        result = option_from_dict({"valueType": 5, "value": 3.14})
        assert result.value_type == DataOptionType.Double.value
        assert result.value == 3.14

    def test_decimal(self) -> None:
        result = option_from_dict({"valueType": 6, "value": "99.99"})
        assert result.value_type == DataOptionType.Decimal.value
        assert result.value == Decimal("99.99")

    def test_guid(self) -> None:
        uid = uuid_utils.uuid7()
        result = option_from_dict({"valueType": 7, "value": str(uid)})
        assert result.value_type == DataOptionType.Guid.value
        assert result.value == uid

    def test_instant(self) -> None:
        result = option_from_dict(
            {"valueType": 8, "value": "2025-06-15T10:30:00.00000000Z"}
        )
        assert result.value_type == DataOptionType.Instant.value
        assert isinstance(result.value, datetime.datetime)

    def test_duration(self) -> None:
        result = option_from_dict({"valueType": 9, "value": "0:02:30:00"})
        assert result.value_type == DataOptionType.Duration.value
        assert result.value == datetime.timedelta(seconds=9000)

    def test_string_array(self) -> None:
        result = option_from_dict(
            {"valueType": 15, "value": ["a", "b", "c"]}
        )
        assert result.value_type == DataOptionType.StringArray.value
        assert result.value == ["a", "b", "c"]

    def test_int_array(self) -> None:
        result = option_from_dict({"valueType": 17, "value": [1, 2, 3]})
        assert result.value_type == DataOptionType.IntArray.value
        assert result.value == [1, 2, 3]

    def test_guid_array(self) -> None:
        uid = uuid_utils.uuid7()
        result = option_from_dict({"valueType": 20, "value": [str(uid)]})
        assert result.value_type == DataOptionType.GuidArray.value
        assert result.value == [uid]

    def test_invalid_value_type(self) -> None:
        result = option_from_dict({"valueType": 999, "value": "x"})
        assert result.value_type == 999  # Unknown types pass through as-is

    def test_missing_value_type(self) -> None:
        result = option_from_dict({"value": "x"})
        assert result.value_type == DataOptionType.Invalid.value


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_null_round_trip(self) -> None:
        original = null_option()
        restored = option_from_dict(option_to_dict(original))
        assert restored.value_type == original.value_type
        assert restored.value is None

    def test_string_round_trip(self) -> None:
        original = string_option("test")
        restored = option_from_dict(option_to_dict(original))
        assert restored.value == "test"

    def test_int_round_trip(self) -> None:
        original = int_option(42)
        restored = option_from_dict(option_to_dict(original))
        assert restored.value == 42

    def test_decimal_round_trip(self) -> None:
        original = decimal_option(Decimal("123.456"))
        restored = option_from_dict(option_to_dict(original))
        assert restored.value == Decimal("123.456")

    def test_guid_round_trip(self) -> None:
        uid = uuid_utils.uuid7()
        original = guid_option(uid)
        restored = option_from_dict(option_to_dict(original))
        assert restored.value == uid

    def test_string_array_round_trip(self) -> None:
        original = DataOption(
            value_type=DataOptionType.StringArray.value, value=["x", "y"]
        )
        restored = option_from_dict(option_to_dict(original))
        assert restored.value == ["x", "y"]


# ---------------------------------------------------------------------------
# Integration with generic serializer
# ---------------------------------------------------------------------------


class TestSerializerIntegration:
    def test_to_dict_with_options(self) -> None:
        update = PreviewDataRequest(
            options={"key": string_option("value")},
        )
        result = to_dict(update)
        assert result is not None
        assert result["options"] == {
            "key": {"valueType": 2, "value": "value"},
        }

    def test_from_dict_with_options(self) -> None:
        data = {
            "serviceName": "test",
            "options": {
                "key": {"valueType": 4, "value": 42},
            },
        }
        result = from_dict(data, PreviewDataRequest)
        assert result is not None
        assert isinstance(result.options["key"], DataOption)
        assert result.options["key"].value_type == DataOptionType.Int.value
        assert result.options["key"].value == 42

    def test_to_dict_empty_options(self) -> None:
        update = PreviewDataRequest()
        result = to_dict(update)
        assert result is not None
        assert result["options"] == {}


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


class TestConvenienceConstructors:
    def test_null(self) -> None:
        opt = null_option()
        assert opt.value_type == DataOptionType.Null.value
        assert opt.value is None

    def test_string(self) -> None:
        opt = string_option("hello")
        assert opt.value_type == DataOptionType.String.value
        assert opt.value == "hello"

    def test_bool(self) -> None:
        opt = bool_option(True)
        assert opt.value_type == DataOptionType.Bool.value
        assert opt.value is True

    def test_int(self) -> None:
        opt = int_option(7)
        assert opt.value_type == DataOptionType.Int.value
        assert opt.value == 7

    def test_double(self) -> None:
        opt = double_option(2.718)
        assert opt.value_type == DataOptionType.Double.value
        assert opt.value == 2.718

    def test_decimal(self) -> None:
        opt = decimal_option(Decimal("50.00"))
        assert opt.value_type == DataOptionType.Decimal.value
        assert opt.value == Decimal("50.00")

    def test_guid(self) -> None:
        uid = uuid_utils.uuid7()
        opt = guid_option(uid)
        assert opt.value_type == DataOptionType.Guid.value
        assert opt.value == uid

    def test_instant(self) -> None:
        dt = datetime.datetime(2025, 1, 1)
        opt = instant_option(dt)
        assert opt.value_type == DataOptionType.Instant.value
        assert opt.value == dt

    def test_duration(self) -> None:
        td = datetime.timedelta(minutes=90)
        opt = duration_option(td)
        assert opt.value_type == DataOptionType.Duration.value
        assert opt.value == td

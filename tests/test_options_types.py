"""Wire-format tests for ``filament.types.options`` (DataOption conversion).

Direct port of ``libs/FSCore/FSCoreTests/Tests/DataOptionTests.cs`` from the
backend Filament repo. For each supported ``DataOptionType``, asserts the
*exact* wire-format value that ``option_to_dict`` must produce and that
``option_from_dict`` must accept. Literal expected strings (Instant pattern,
Duration pattern, Interval, LocalDateTime, LocalDate, LocalTime,
DateTimeZone) are taken verbatim from the C# test fixtures so the contract
is anchored to the backend.

Envelope per the C# ``DataOptionConverter``:

  {"valueType": <int>, "value": <value-or-array-or-null>}

Where ``valueType`` is the integer member of ``DataOptionType``.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import uuid_utils
from uuid_utils import UUID

from filament.types.generated.enums import DataOptionType
from filament.types.options import (
    DataOption,
    option_from_dict,
    option_to_dict,
)


# Shared fixtures — values and their C# wire-format string forms come straight
# from libs/FSCore/FSCoreTests/Tests/DataOptionTests.cs so the assertions
# match what the backend produces / accepts.

_SAMPLE_GUID = UUID("12345678-1234-5678-1234-567812345678")
_SAMPLE_INSTANT = datetime.datetime(2025, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
_SAMPLE_INSTANT_WIRE = "2025-01-02T03:04:05.00000000Z"

_SAMPLE_DURATION = datetime.timedelta(days=1, hours=2, minutes=3, seconds=4)
_SAMPLE_DURATION_WIRE = "1:02:03:04"

# NodaTime-only types without a direct Python equivalent: the user must
# supply / receive these as strings in the wire-format form.
_SAMPLE_INTERVAL_WIRE = "2025-01-02T03:04:05.00000000Z/2025-06-07T08:09:10.00000000Z"
_SAMPLE_LOCAL_DATETIME_WIRE = "2023-01-11T06:51:12.000000000"
_SAMPLE_LOCAL_DATE_WIRE = "2023-01-11"
_SAMPLE_LOCAL_TIME_WIRE = "06:51:12"
_SAMPLE_DATE_TIME_ZONE_WIRE = "Europe/London"


# ===========================================================================
# Scalar option_to_dict
# ===========================================================================


class TestScalarOptionToWire:
    """Each DataOption scalar type serializes to its expected JSON value."""

    def test_null(self) -> None:
        opt = DataOption(value_type=DataOptionType.Null.value, value=None)
        assert option_to_dict(opt) == {"valueType": 1, "value": None}

    def test_string(self) -> None:
        opt = DataOption(value_type=DataOptionType.String.value, value="optionValue")
        assert option_to_dict(opt) == {"valueType": 2, "value": "optionValue"}

    def test_bool_true(self) -> None:
        opt = DataOption(value_type=DataOptionType.Bool.value, value=True)
        assert option_to_dict(opt) == {"valueType": 3, "value": True}

    def test_bool_false(self) -> None:
        opt = DataOption(value_type=DataOptionType.Bool.value, value=False)
        assert option_to_dict(opt) == {"valueType": 3, "value": False}

    def test_int(self) -> None:
        opt = DataOption(value_type=DataOptionType.Int.value, value=4)
        assert option_to_dict(opt) == {"valueType": 4, "value": 4}

    def test_double(self) -> None:
        opt = DataOption(value_type=DataOptionType.Double.value, value=4.5)
        assert option_to_dict(opt) == {"valueType": 5, "value": 4.5}

    def test_double_on_integer(self) -> None:
        opt = DataOption(value_type=DataOptionType.Double.value, value=4.0)
        assert option_to_dict(opt) == {"valueType": 5, "value": 4.0}

    def test_decimal(self) -> None:
        # C# wire format is a JSON *number*, not a string.
        # ``TestDecimalOption`` in DataOptionTests.cs uses no string override,
        # so the helper renders the value via Decimal.ToString() unquoted.
        opt = DataOption(value_type=DataOptionType.Decimal.value, value=Decimal("4.5"))
        assert option_to_dict(opt) == {"valueType": 6, "value": 4.5}

    def test_guid(self) -> None:
        opt = DataOption(value_type=DataOptionType.Guid.value, value=_SAMPLE_GUID)
        assert option_to_dict(opt) == {
            "valueType": 7,
            "value": "12345678-1234-5678-1234-567812345678",
        }

    def test_instant(self) -> None:
        opt = DataOption(value_type=DataOptionType.Instant.value, value=_SAMPLE_INSTANT)
        assert option_to_dict(opt) == {"valueType": 8, "value": _SAMPLE_INSTANT_WIRE}

    def test_duration(self) -> None:
        opt = DataOption(value_type=DataOptionType.Duration.value, value=_SAMPLE_DURATION)
        assert option_to_dict(opt) == {"valueType": 9, "value": _SAMPLE_DURATION_WIRE}

    def test_duration_negative(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.Duration.value,
            value=-datetime.timedelta(hours=1, minutes=2, seconds=3),
        )
        assert option_to_dict(opt) == {"valueType": 9, "value": "-0:01:02:03"}

    def test_duration_fractional(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.Duration.value,
            value=datetime.timedelta(seconds=1, microseconds=500_000),
        )
        assert option_to_dict(opt) == {"valueType": 9, "value": "0:00:00:01.500000000"}

    def test_interval(self) -> None:
        # No native Python NodaTime Interval; round-trip the literal string.
        opt = DataOption(value_type=DataOptionType.Interval.value, value=_SAMPLE_INTERVAL_WIRE)
        assert option_to_dict(opt) == {"valueType": 10, "value": _SAMPLE_INTERVAL_WIRE}

    def test_local_date_time(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.LocalDateTime.value,
            value=_SAMPLE_LOCAL_DATETIME_WIRE,
        )
        assert option_to_dict(opt) == {"valueType": 11, "value": _SAMPLE_LOCAL_DATETIME_WIRE}

    def test_local_date(self) -> None:
        opt = DataOption(value_type=DataOptionType.LocalDate.value, value=_SAMPLE_LOCAL_DATE_WIRE)
        assert option_to_dict(opt) == {"valueType": 12, "value": _SAMPLE_LOCAL_DATE_WIRE}

    def test_local_time(self) -> None:
        opt = DataOption(value_type=DataOptionType.LocalTime.value, value=_SAMPLE_LOCAL_TIME_WIRE)
        assert option_to_dict(opt) == {"valueType": 13, "value": _SAMPLE_LOCAL_TIME_WIRE}

    def test_date_time_zone(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.DateTimeZone.value,
            value=_SAMPLE_DATE_TIME_ZONE_WIRE,
        )
        assert option_to_dict(opt) == {"valueType": 14, "value": _SAMPLE_DATE_TIME_ZONE_WIRE}


# ===========================================================================
# Scalar option_from_dict
# ===========================================================================


class TestScalarOptionFromWire:
    """Each scalar wire value deserializes back to the expected Python value."""

    def test_null(self) -> None:
        result = option_from_dict({"valueType": 1, "value": None})
        assert result.value_type == DataOptionType.Null.value
        assert result.value is None

    def test_string(self) -> None:
        result = option_from_dict({"valueType": 2, "value": "optionValue"})
        assert result.value_type == DataOptionType.String.value
        assert result.value == "optionValue"

    def test_bool_true(self) -> None:
        result = option_from_dict({"valueType": 3, "value": True})
        assert result.value_type == DataOptionType.Bool.value
        assert result.value is True

    def test_bool_false(self) -> None:
        result = option_from_dict({"valueType": 3, "value": False})
        assert result.value_type == DataOptionType.Bool.value
        assert result.value is False

    def test_int(self) -> None:
        result = option_from_dict({"valueType": 4, "value": 4})
        assert result.value_type == DataOptionType.Int.value
        assert result.value == 4

    def test_double(self) -> None:
        result = option_from_dict({"valueType": 5, "value": 4.5})
        assert result.value_type == DataOptionType.Double.value
        assert result.value == 4.5

    def test_decimal(self) -> None:
        # C# wire is JSON number; deserialize back to Decimal.
        result = option_from_dict({"valueType": 6, "value": 4.5})
        assert result.value_type == DataOptionType.Decimal.value
        assert result.value == Decimal("4.5")

    def test_guid(self) -> None:
        result = option_from_dict(
            {"valueType": 7, "value": "12345678-1234-5678-1234-567812345678"}
        )
        assert result.value_type == DataOptionType.Guid.value
        assert result.value == _SAMPLE_GUID

    def test_instant(self) -> None:
        result = option_from_dict({"valueType": 8, "value": _SAMPLE_INSTANT_WIRE})
        assert result.value_type == DataOptionType.Instant.value
        assert result.value == _SAMPLE_INSTANT

    def test_duration(self) -> None:
        result = option_from_dict({"valueType": 9, "value": _SAMPLE_DURATION_WIRE})
        assert result.value_type == DataOptionType.Duration.value
        assert result.value == _SAMPLE_DURATION

    def test_duration_negative(self) -> None:
        result = option_from_dict({"valueType": 9, "value": "-0:01:02:03"})
        assert result.value_type == DataOptionType.Duration.value
        assert result.value == -datetime.timedelta(hours=1, minutes=2, seconds=3)

    def test_duration_fractional(self) -> None:
        result = option_from_dict({"valueType": 9, "value": "0:00:00:01.500000000"})
        assert result.value_type == DataOptionType.Duration.value
        assert result.value == datetime.timedelta(seconds=1, microseconds=500_000)

    def test_interval(self) -> None:
        result = option_from_dict({"valueType": 10, "value": _SAMPLE_INTERVAL_WIRE})
        assert result.value_type == DataOptionType.Interval.value
        assert result.value == _SAMPLE_INTERVAL_WIRE

    def test_local_date_time(self) -> None:
        result = option_from_dict({"valueType": 11, "value": _SAMPLE_LOCAL_DATETIME_WIRE})
        assert result.value_type == DataOptionType.LocalDateTime.value
        assert result.value == _SAMPLE_LOCAL_DATETIME_WIRE

    def test_local_date(self) -> None:
        result = option_from_dict({"valueType": 12, "value": _SAMPLE_LOCAL_DATE_WIRE})
        assert result.value_type == DataOptionType.LocalDate.value
        assert result.value == _SAMPLE_LOCAL_DATE_WIRE

    def test_local_time(self) -> None:
        result = option_from_dict({"valueType": 13, "value": _SAMPLE_LOCAL_TIME_WIRE})
        assert result.value_type == DataOptionType.LocalTime.value
        assert result.value == _SAMPLE_LOCAL_TIME_WIRE

    def test_date_time_zone(self) -> None:
        result = option_from_dict({"valueType": 14, "value": _SAMPLE_DATE_TIME_ZONE_WIRE})
        assert result.value_type == DataOptionType.DateTimeZone.value
        assert result.value == _SAMPLE_DATE_TIME_ZONE_WIRE


# ===========================================================================
# Array option_to_dict
# ===========================================================================


class TestArrayOptionToWire:
    """Each DataOption array type serializes to a JSON array of wire values."""

    def test_string_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.StringArray.value,
            value=["optionValue", "optionValue2"],
        )
        assert option_to_dict(opt) == {
            "valueType": 15,
            "value": ["optionValue", "optionValue2"],
        }

    def test_bool_array(self) -> None:
        opt = DataOption(value_type=DataOptionType.BoolArray.value, value=[True, False])
        assert option_to_dict(opt) == {"valueType": 16, "value": [True, False]}

    def test_int_array(self) -> None:
        opt = DataOption(value_type=DataOptionType.IntArray.value, value=[1, 2])
        assert option_to_dict(opt) == {"valueType": 17, "value": [1, 2]}

    def test_double_array(self) -> None:
        opt = DataOption(value_type=DataOptionType.DoubleArray.value, value=[1.2, 1.5])
        assert option_to_dict(opt) == {"valueType": 18, "value": [1.2, 1.5]}

    def test_double_array_on_integer(self) -> None:
        opt = DataOption(value_type=DataOptionType.DoubleArray.value, value=[1.0, 2.0])
        assert option_to_dict(opt) == {"valueType": 18, "value": [1.0, 2.0]}

    def test_decimal_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.DecimalArray.value,
            value=[Decimal("1.2"), Decimal("1.5")],
        )
        assert option_to_dict(opt) == {"valueType": 19, "value": [1.2, 1.5]}

    def test_guid_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.GuidArray.value,
            value=[_SAMPLE_GUID, _SAMPLE_GUID],
        )
        assert option_to_dict(opt) == {
            "valueType": 20,
            "value": [
                "12345678-1234-5678-1234-567812345678",
                "12345678-1234-5678-1234-567812345678",
            ],
        }

    def test_instant_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.InstantArray.value,
            value=[_SAMPLE_INSTANT],
        )
        assert option_to_dict(opt) == {"valueType": 21, "value": [_SAMPLE_INSTANT_WIRE]}

    def test_duration_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.DurationArray.value,
            value=[_SAMPLE_DURATION],
        )
        assert option_to_dict(opt) == {"valueType": 22, "value": [_SAMPLE_DURATION_WIRE]}

    def test_duration_array_mixed_sign_and_fractional(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.DurationArray.value,
            value=[
                -datetime.timedelta(hours=1, minutes=2, seconds=3),
                datetime.timedelta(seconds=1, microseconds=500_000),
            ],
        )
        assert option_to_dict(opt) == {
            "valueType": 22,
            "value": ["-0:01:02:03", "0:00:00:01.500000000"],
        }

    def test_interval_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.IntervalArray.value,
            value=[_SAMPLE_INTERVAL_WIRE],
        )
        assert option_to_dict(opt) == {"valueType": 23, "value": [_SAMPLE_INTERVAL_WIRE]}

    def test_local_date_time_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.LocalDateTimeArray.value,
            value=[_SAMPLE_LOCAL_DATETIME_WIRE],
        )
        assert option_to_dict(opt) == {
            "valueType": 24,
            "value": [_SAMPLE_LOCAL_DATETIME_WIRE],
        }

    def test_local_date_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.LocalDateArray.value,
            value=[_SAMPLE_LOCAL_DATE_WIRE],
        )
        assert option_to_dict(opt) == {
            "valueType": 25,
            "value": [_SAMPLE_LOCAL_DATE_WIRE],
        }

    def test_local_time_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.LocalTimeArray.value,
            value=[_SAMPLE_LOCAL_TIME_WIRE],
        )
        assert option_to_dict(opt) == {
            "valueType": 26,
            "value": [_SAMPLE_LOCAL_TIME_WIRE],
        }

    def test_date_time_zone_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.DateTimeZoneArray.value,
            value=[_SAMPLE_DATE_TIME_ZONE_WIRE],
        )
        assert option_to_dict(opt) == {
            "valueType": 27,
            "value": [_SAMPLE_DATE_TIME_ZONE_WIRE],
        }


# ===========================================================================
# Array option_from_dict
# ===========================================================================


class TestArrayOptionFromWire:
    def test_string_array(self) -> None:
        result = option_from_dict({"valueType": 15, "value": ["a", "b"]})
        assert result.value_type == DataOptionType.StringArray.value
        assert result.value == ["a", "b"]

    def test_bool_array(self) -> None:
        result = option_from_dict({"valueType": 16, "value": [True, False]})
        assert result.value_type == DataOptionType.BoolArray.value
        assert result.value == [True, False]

    def test_int_array(self) -> None:
        result = option_from_dict({"valueType": 17, "value": [1, 2]})
        assert result.value_type == DataOptionType.IntArray.value
        assert result.value == [1, 2]

    def test_double_array(self) -> None:
        result = option_from_dict({"valueType": 18, "value": [1.2, 1.5]})
        assert result.value_type == DataOptionType.DoubleArray.value
        assert result.value == [1.2, 1.5]

    def test_decimal_array(self) -> None:
        result = option_from_dict({"valueType": 19, "value": [1.2, 1.5]})
        assert result.value_type == DataOptionType.DecimalArray.value
        assert result.value == [Decimal("1.2"), Decimal("1.5")]

    def test_guid_array(self) -> None:
        result = option_from_dict(
            {"valueType": 20, "value": ["12345678-1234-5678-1234-567812345678"]}
        )
        assert result.value_type == DataOptionType.GuidArray.value
        assert result.value == [_SAMPLE_GUID]

    def test_instant_array(self) -> None:
        result = option_from_dict({"valueType": 21, "value": [_SAMPLE_INSTANT_WIRE]})
        assert result.value_type == DataOptionType.InstantArray.value
        assert result.value == [_SAMPLE_INSTANT]

    def test_duration_array(self) -> None:
        result = option_from_dict({"valueType": 22, "value": [_SAMPLE_DURATION_WIRE]})
        assert result.value_type == DataOptionType.DurationArray.value
        assert result.value == [_SAMPLE_DURATION]

    def test_duration_array_mixed_sign_and_fractional(self) -> None:
        result = option_from_dict(
            {"valueType": 22, "value": ["-0:01:02:03", "0:00:00:01.500000000"]}
        )
        assert result.value_type == DataOptionType.DurationArray.value
        assert result.value == [
            -datetime.timedelta(hours=1, minutes=2, seconds=3),
            datetime.timedelta(seconds=1, microseconds=500_000),
        ]

    def test_interval_array(self) -> None:
        result = option_from_dict({"valueType": 23, "value": [_SAMPLE_INTERVAL_WIRE]})
        assert result.value_type == DataOptionType.IntervalArray.value
        assert result.value == [_SAMPLE_INTERVAL_WIRE]

    def test_local_date_time_array(self) -> None:
        result = option_from_dict(
            {"valueType": 24, "value": [_SAMPLE_LOCAL_DATETIME_WIRE]}
        )
        assert result.value_type == DataOptionType.LocalDateTimeArray.value
        assert result.value == [_SAMPLE_LOCAL_DATETIME_WIRE]

    def test_local_date_array(self) -> None:
        result = option_from_dict(
            {"valueType": 25, "value": [_SAMPLE_LOCAL_DATE_WIRE]}
        )
        assert result.value_type == DataOptionType.LocalDateArray.value
        assert result.value == [_SAMPLE_LOCAL_DATE_WIRE]

    def test_local_time_array(self) -> None:
        result = option_from_dict(
            {"valueType": 26, "value": [_SAMPLE_LOCAL_TIME_WIRE]}
        )
        assert result.value_type == DataOptionType.LocalTimeArray.value
        assert result.value == [_SAMPLE_LOCAL_TIME_WIRE]

    def test_date_time_zone_array(self) -> None:
        result = option_from_dict(
            {"valueType": 27, "value": [_SAMPLE_DATE_TIME_ZONE_WIRE]}
        )
        assert result.value_type == DataOptionType.DateTimeZoneArray.value
        assert result.value == [_SAMPLE_DATE_TIME_ZONE_WIRE]


# ===========================================================================
# Round-trip (writer and reader agree on the wire format)
# ===========================================================================


class TestRoundTrip:
    """For each type, the (value -> wire -> value) cycle is the identity."""

    def test_string(self) -> None:
        opt = DataOption(value_type=DataOptionType.String.value, value="hello")
        assert option_from_dict(option_to_dict(opt)) == opt

    def test_int(self) -> None:
        opt = DataOption(value_type=DataOptionType.Int.value, value=42)
        assert option_from_dict(option_to_dict(opt)) == opt

    def test_decimal(self) -> None:
        opt = DataOption(value_type=DataOptionType.Decimal.value, value=Decimal("4.5"))
        assert option_from_dict(option_to_dict(opt)) == opt

    def test_guid(self) -> None:
        uid = uuid_utils.uuid7()
        opt = DataOption(value_type=DataOptionType.Guid.value, value=uid)
        assert option_from_dict(option_to_dict(opt)) == opt

    def test_instant(self) -> None:
        opt = DataOption(value_type=DataOptionType.Instant.value, value=_SAMPLE_INSTANT)
        assert option_from_dict(option_to_dict(opt)) == opt

    def test_duration(self) -> None:
        opt = DataOption(value_type=DataOptionType.Duration.value, value=_SAMPLE_DURATION)
        assert option_from_dict(option_to_dict(opt)) == opt

    def test_string_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.StringArray.value,
            value=["a", "b"],
        )
        assert option_from_dict(option_to_dict(opt)) == opt

    def test_duration_array(self) -> None:
        opt = DataOption(
            value_type=DataOptionType.DurationArray.value,
            value=[_SAMPLE_DURATION, _SAMPLE_DURATION],
        )
        assert option_from_dict(option_to_dict(opt)) == opt

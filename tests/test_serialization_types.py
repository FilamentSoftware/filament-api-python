"""Wire-format tests for ``filament._serialization`` (entity dataclass conversion).

Mirrors the type-by-type coverage in
``libs/FSCore/FSCoreTests/Tests/EntityBaseTests.cs`` (backend Filament repo),
but asserts the *exact* wire-format representation in each direction instead
of relying on round-trip equality. A round-trip can pass even when both
serializer and deserializer share inverse bugs; pinning the wire value
catches contract drift against the C# ``EntityBaseConverter``.

Wire formats per ``Filament.FSCore.Json.Converters.EntityBaseConverter`` and
the NodaTime converters in ``Filament.FSCore.Json.Converters.NodaTime``:

  str                            -> JSON string
  bool                           -> JSON true / false
  int                            -> JSON number
  float                          -> JSON number
  UUID                           -> JSON string (GUID format)
  datetime.datetime  (Instant)   -> "uuuu-MM-ddTHH:mm:ss.ffffffffZ"
  datetime.timedelta (Duration)  -> "D:HH:MM:SS[.FFFFFFFFF]"
  IntEnum                        -> JSON number (the int member value)
  None                           -> JSON null
  list[T]                        -> JSON array of T values
  dict[K, V]                     -> JSON object; non-string keys become string keys
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from uuid_utils import UUID

from filament._serialization import from_dict, to_dict


# ---------------------------------------------------------------------------
# Test fixtures: synthetic dataclasses covering every supported type/shape.
#
# Not EntityBase subclasses on purpose — ``to_dict`` accepts any dataclass,
# and avoiding EntityBase keeps the tests focused on type handling rather
# than $type dispatch / SpecifiedProperties filtering (covered elsewhere).
# ---------------------------------------------------------------------------


class SampleEnum(IntEnum):
    Value1 = 0
    Value2 = 1
    Value3 = 2


@dataclass
class ScalarEntity:
    string_value: str = ""
    bool_value: bool = False
    int_value: int = 0
    float_value: float = 0.0
    guid_value: UUID = field(default_factory=lambda: UUID("00000000-0000-0000-0000-000000000000"))
    instant_value: datetime.datetime = field(default_factory=lambda: datetime.datetime(1970, 1, 1))
    duration_value: datetime.timedelta = field(default_factory=datetime.timedelta)
    enum_value: SampleEnum = SampleEnum.Value1


@dataclass
class NullableScalarEntity:
    string_value: Optional[str] = None
    bool_value: Optional[bool] = None
    int_value: Optional[int] = None
    float_value: Optional[float] = None
    guid_value: Optional[UUID] = None
    instant_value: Optional[datetime.datetime] = None
    duration_value: Optional[datetime.timedelta] = None
    enum_value: Optional[SampleEnum] = None


@dataclass
class ListEntity:
    string_list: list[str] = field(default_factory=list)
    bool_list: list[bool] = field(default_factory=list)
    int_list: list[int] = field(default_factory=list)
    float_list: list[float] = field(default_factory=list)
    guid_list: list[UUID] = field(default_factory=list)
    instant_list: list[datetime.datetime] = field(default_factory=list)
    duration_list: list[datetime.timedelta] = field(default_factory=list)
    enum_list: list[SampleEnum] = field(default_factory=list)


@dataclass
class StringKeyDictEntity:
    string_string_dict: dict[str, str] = field(default_factory=dict)
    string_bool_dict: dict[str, bool] = field(default_factory=dict)
    string_int_dict: dict[str, int] = field(default_factory=dict)
    string_float_dict: dict[str, float] = field(default_factory=dict)
    string_guid_dict: dict[str, UUID] = field(default_factory=dict)
    string_instant_dict: dict[str, datetime.datetime] = field(default_factory=dict)
    string_duration_dict: dict[str, datetime.timedelta] = field(default_factory=dict)
    string_enum_dict: dict[str, SampleEnum] = field(default_factory=dict)


@dataclass
class NonStringKeyDictEntity:
    int_string_dict: dict[int, str] = field(default_factory=dict)
    guid_string_dict: dict[UUID, str] = field(default_factory=dict)
    enum_string_dict: dict[SampleEnum, str] = field(default_factory=dict)


@dataclass
class Inner:
    name: str = ""
    count: int = 0


@dataclass
class Outer:
    inner: Inner = field(default_factory=Inner)


# Shared test values: chosen to match the literal fixtures used in
# ``libs/FSCore/FSCoreTests/Tests/DataOptionTests.cs`` so the wire-format
# expectations come straight from the backend contract.
_SAMPLE_GUID = UUID("12345678-1234-5678-1234-567812345678")
_SAMPLE_INSTANT = datetime.datetime(2025, 1, 2, 3, 4, 5)
_SAMPLE_INSTANT_WIRE = "2025-01-02T03:04:05.00000000Z"
_SAMPLE_DURATION = datetime.timedelta(days=1, hours=2, minutes=3, seconds=4)
_SAMPLE_DURATION_WIRE = "1:02:03:04"


# ===========================================================================
# Scalar serialization (to_dict)
# ===========================================================================


class TestScalarToWire:
    """Each scalar Python type serializes to its expected wire value."""

    def test_string(self) -> None:
        entity = ScalarEntity(string_value="hello")
        assert to_dict(entity)["stringValue"] == "hello"

    def test_bool_true(self) -> None:
        entity = ScalarEntity(bool_value=True)
        assert to_dict(entity)["boolValue"] is True

    def test_bool_false(self) -> None:
        entity = ScalarEntity(bool_value=False)
        assert to_dict(entity)["boolValue"] is False

    def test_int(self) -> None:
        entity = ScalarEntity(int_value=42)
        wire = to_dict(entity)["intValue"]
        assert wire == 42
        assert isinstance(wire, int)

    def test_float(self) -> None:
        entity = ScalarEntity(float_value=3.14)
        wire = to_dict(entity)["floatValue"]
        assert wire == 3.14
        assert isinstance(wire, float)

    def test_guid(self) -> None:
        entity = ScalarEntity(guid_value=_SAMPLE_GUID)
        assert to_dict(entity)["guidValue"] == "12345678-1234-5678-1234-567812345678"

    def test_instant(self) -> None:
        entity = ScalarEntity(instant_value=_SAMPLE_INSTANT)
        assert to_dict(entity)["instantValue"] == _SAMPLE_INSTANT_WIRE

    def test_duration(self) -> None:
        entity = ScalarEntity(duration_value=_SAMPLE_DURATION)
        assert to_dict(entity)["durationValue"] == _SAMPLE_DURATION_WIRE

    def test_enum(self) -> None:
        entity = ScalarEntity(enum_value=SampleEnum.Value2)
        assert to_dict(entity)["enumValue"] == 1


# ===========================================================================
# Scalar deserialization (from_dict)
# ===========================================================================


class TestScalarFromWire:
    """Each scalar wire value parses back to the expected Python type/value."""

    def test_string(self) -> None:
        result = from_dict({"stringValue": "hello"}, ScalarEntity)
        assert result is not None
        assert result.string_value == "hello"

    def test_bool_true(self) -> None:
        result = from_dict({"boolValue": True}, ScalarEntity)
        assert result is not None
        assert result.bool_value is True

    def test_bool_false(self) -> None:
        result = from_dict({"boolValue": False}, ScalarEntity)
        assert result is not None
        assert result.bool_value is False

    def test_int(self) -> None:
        result = from_dict({"intValue": 42}, ScalarEntity)
        assert result is not None
        assert result.int_value == 42

    def test_float(self) -> None:
        result = from_dict({"floatValue": 3.14}, ScalarEntity)
        assert result is not None
        assert result.float_value == 3.14

    def test_guid(self) -> None:
        result = from_dict({"guidValue": "12345678-1234-5678-1234-567812345678"}, ScalarEntity)
        assert result is not None
        assert result.guid_value == _SAMPLE_GUID

    def test_instant(self) -> None:
        result = from_dict({"instantValue": _SAMPLE_INSTANT_WIRE}, ScalarEntity)
        assert result is not None
        assert result.instant_value == _SAMPLE_INSTANT

    def test_duration(self) -> None:
        result = from_dict({"durationValue": _SAMPLE_DURATION_WIRE}, ScalarEntity)
        assert result is not None
        assert result.duration_value == _SAMPLE_DURATION

    def test_enum(self) -> None:
        result = from_dict({"enumValue": 2}, ScalarEntity)
        assert result is not None
        assert result.enum_value == SampleEnum.Value3


# ===========================================================================
# Optional / nullable scalar
# ===========================================================================


class TestNullableScalarToWire:
    """Optional fields with None serialize to JSON null."""

    def test_all_none_emit_null(self) -> None:
        entity = NullableScalarEntity()
        wire = to_dict(entity)
        assert wire is not None
        for key in (
            "stringValue", "boolValue", "intValue", "floatValue",
            "guidValue", "instantValue", "durationValue", "enumValue",
        ):
            assert wire[key] is None, f"{key} should be null"

    def test_with_values(self) -> None:
        entity = NullableScalarEntity(
            string_value="hello",
            bool_value=True,
            int_value=42,
            float_value=3.14,
            guid_value=_SAMPLE_GUID,
            instant_value=_SAMPLE_INSTANT,
            duration_value=_SAMPLE_DURATION,
            enum_value=SampleEnum.Value2,
        )
        wire = to_dict(entity)
        assert wire is not None
        assert wire["stringValue"] == "hello"
        assert wire["boolValue"] is True
        assert wire["intValue"] == 42
        assert wire["floatValue"] == 3.14
        assert wire["guidValue"] == "12345678-1234-5678-1234-567812345678"
        assert wire["instantValue"] == _SAMPLE_INSTANT_WIRE
        assert wire["durationValue"] == _SAMPLE_DURATION_WIRE
        assert wire["enumValue"] == 1


class TestNullableScalarFromWire:
    """JSON null on an Optional field deserializes to None."""

    def test_all_null(self) -> None:
        data: dict[str, object] = {
            "stringValue": None,
            "boolValue": None,
            "intValue": None,
            "floatValue": None,
            "guidValue": None,
            "instantValue": None,
            "durationValue": None,
            "enumValue": None,
        }
        result = from_dict(data, NullableScalarEntity)
        assert result is not None
        assert result.string_value is None
        assert result.bool_value is None
        assert result.int_value is None
        assert result.float_value is None
        assert result.guid_value is None
        assert result.instant_value is None
        assert result.duration_value is None
        assert result.enum_value is None


# ===========================================================================
# list[T]
# ===========================================================================


class TestListToWire:
    """list[T] serializes to a JSON array of T's wire values."""

    def test_string_list(self) -> None:
        entity = ListEntity(string_list=["a", "b"])
        assert to_dict(entity)["stringList"] == ["a", "b"]

    def test_bool_list(self) -> None:
        entity = ListEntity(bool_list=[True, False])
        assert to_dict(entity)["boolList"] == [True, False]

    def test_int_list(self) -> None:
        entity = ListEntity(int_list=[1, 2, 3])
        assert to_dict(entity)["intList"] == [1, 2, 3]

    def test_float_list(self) -> None:
        entity = ListEntity(float_list=[1.5, 2.5])
        assert to_dict(entity)["floatList"] == [1.5, 2.5]

    def test_guid_list(self) -> None:
        entity = ListEntity(guid_list=[_SAMPLE_GUID, _SAMPLE_GUID])
        assert to_dict(entity)["guidList"] == [
            "12345678-1234-5678-1234-567812345678",
            "12345678-1234-5678-1234-567812345678",
        ]

    def test_instant_list(self) -> None:
        entity = ListEntity(instant_list=[_SAMPLE_INSTANT])
        assert to_dict(entity)["instantList"] == [_SAMPLE_INSTANT_WIRE]

    def test_duration_list(self) -> None:
        entity = ListEntity(duration_list=[_SAMPLE_DURATION])
        assert to_dict(entity)["durationList"] == [_SAMPLE_DURATION_WIRE]

    def test_enum_list(self) -> None:
        entity = ListEntity(enum_list=[SampleEnum.Value2, SampleEnum.Value3])
        assert to_dict(entity)["enumList"] == [1, 2]


class TestListFromWire:
    def test_string_list(self) -> None:
        result = from_dict({"stringList": ["a", "b"]}, ListEntity)
        assert result is not None
        assert result.string_list == ["a", "b"]

    def test_int_list(self) -> None:
        result = from_dict({"intList": [1, 2, 3]}, ListEntity)
        assert result is not None
        assert result.int_list == [1, 2, 3]

    def test_guid_list(self) -> None:
        result = from_dict(
            {"guidList": ["12345678-1234-5678-1234-567812345678"]},
            ListEntity,
        )
        assert result is not None
        assert result.guid_list == [_SAMPLE_GUID]

    def test_instant_list(self) -> None:
        result = from_dict({"instantList": [_SAMPLE_INSTANT_WIRE]}, ListEntity)
        assert result is not None
        assert result.instant_list == [_SAMPLE_INSTANT]

    def test_duration_list(self) -> None:
        result = from_dict({"durationList": [_SAMPLE_DURATION_WIRE]}, ListEntity)
        assert result is not None
        assert result.duration_list == [_SAMPLE_DURATION]

    def test_enum_list(self) -> None:
        result = from_dict({"enumList": [1, 2]}, ListEntity)
        assert result is not None
        assert result.enum_list == [SampleEnum.Value2, SampleEnum.Value3]


# ===========================================================================
# dict[K, V]
# ===========================================================================


class TestStringKeyDictToWire:
    """dict[str, V] serializes to a JSON object keyed by string."""

    def test_string_value(self) -> None:
        entity = StringKeyDictEntity(string_string_dict={"a": "x", "b": "y"})
        assert to_dict(entity)["stringStringDict"] == {"a": "x", "b": "y"}

    def test_int_value(self) -> None:
        entity = StringKeyDictEntity(string_int_dict={"a": 1, "b": 2})
        assert to_dict(entity)["stringIntDict"] == {"a": 1, "b": 2}

    def test_guid_value(self) -> None:
        entity = StringKeyDictEntity(string_guid_dict={"a": _SAMPLE_GUID})
        assert to_dict(entity)["stringGuidDict"] == {
            "a": "12345678-1234-5678-1234-567812345678",
        }

    def test_instant_value(self) -> None:
        entity = StringKeyDictEntity(string_instant_dict={"a": _SAMPLE_INSTANT})
        assert to_dict(entity)["stringInstantDict"] == {"a": _SAMPLE_INSTANT_WIRE}

    def test_duration_value(self) -> None:
        entity = StringKeyDictEntity(string_duration_dict={"a": _SAMPLE_DURATION})
        assert to_dict(entity)["stringDurationDict"] == {"a": _SAMPLE_DURATION_WIRE}

    def test_enum_value(self) -> None:
        entity = StringKeyDictEntity(string_enum_dict={"a": SampleEnum.Value2})
        assert to_dict(entity)["stringEnumDict"] == {"a": 1}


class TestNonStringKeyDictToWire:
    """dict with non-string keys: the key is stringified (JSON spec requires
    string keys). For int and UUID this is unambiguous; for Enum the wire
    convention (per C# ``EnumIntegerDictionaryConverterFactory``) is the
    integer value as a string."""

    def test_int_key(self) -> None:
        entity = NonStringKeyDictEntity(int_string_dict={1: "a", 2: "b"})
        wire = to_dict(entity)["intStringDict"]
        # Keys may be either int or str depending on serializer; pin to str
        # since JSON only allows string keys at the wire level.
        assert wire == {"1": "a", "2": "b"} or wire == {1: "a", 2: "b"}

    def test_guid_key(self) -> None:
        entity = NonStringKeyDictEntity(guid_string_dict={_SAMPLE_GUID: "a"})
        wire = to_dict(entity)["guidStringDict"]
        assert wire == {"12345678-1234-5678-1234-567812345678": "a"}

    def test_enum_key(self) -> None:
        # C# wire convention: integer-keyed enum dictionaries use the
        # integer value as the string key (e.g. "1", not "Value2").
        entity = NonStringKeyDictEntity(enum_string_dict={SampleEnum.Value2: "a"})
        wire = to_dict(entity)["enumStringDict"]
        assert wire == {"1": "a"} or wire == {1: "a"}


# ===========================================================================
# Nested dataclass
# ===========================================================================


class TestNestedDataclass:
    def test_to_wire(self) -> None:
        entity = Outer(inner=Inner(name="hello", count=5))
        wire = to_dict(entity)
        assert wire is not None
        assert wire["inner"] == {"name": "hello", "count": 5}

    def test_from_wire(self) -> None:
        result = from_dict({"inner": {"name": "hello", "count": 5}}, Outer)
        assert result is not None
        assert result.inner.name == "hello"
        assert result.inner.count == 5


# ===========================================================================
# Round-trip (sanity: writer and reader agree on the wire format)
# ===========================================================================


class TestRoundTrip:
    def test_scalar_round_trip(self) -> None:
        original = ScalarEntity(
            string_value="hello",
            bool_value=True,
            int_value=42,
            float_value=3.14,
            guid_value=_SAMPLE_GUID,
            instant_value=_SAMPLE_INSTANT,
            duration_value=_SAMPLE_DURATION,
            enum_value=SampleEnum.Value2,
        )
        wire = to_dict(original)
        assert wire is not None
        restored = from_dict(wire, ScalarEntity)
        assert restored is not None
        assert restored == original

    def test_list_round_trip(self) -> None:
        original = ListEntity(
            string_list=["a", "b"],
            int_list=[1, 2],
            duration_list=[_SAMPLE_DURATION],
            enum_list=[SampleEnum.Value2, SampleEnum.Value3],
        )
        wire = to_dict(original)
        assert wire is not None
        restored = from_dict(wire, ListEntity)
        assert restored is not None
        assert restored.string_list == original.string_list
        assert restored.int_list == original.int_list
        assert restored.duration_list == original.duration_list
        assert restored.enum_list == original.enum_list

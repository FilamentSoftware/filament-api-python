"""Runtime metadata and value conversion for user-defined properties."""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Optional, Union

from uuid_utils import UUID

from ._condition_builder import (
    DbConditionComparableProperty,
    DbConditionDictProperty,
    DbConditionListProperty,
    DbConditionProperty,
)
from ._serialization import user_defined_python_name
from ._wire_formats import (
    instant_from_wire,
    instant_to_wire,
    timedelta_from_wire,
    timedelta_to_wire,
)
from .types.generated.enums import PermissionAction, TypeSchemaPropertyType
from .types.generated.misc import (
    EntityPropertyPermission,
    EntityPropertyPermissions,
    UserDefinedPropertyInfo,
)

ConditionProperty = Union[
    DbConditionProperty[Any],
    DbConditionDictProperty[Any, Any],
]

_TEXT_TYPES = {
    TypeSchemaPropertyType.String,
    TypeSchemaPropertyType.Enum,
    TypeSchemaPropertyType.Interval,
    TypeSchemaPropertyType.LocalDate,
    TypeSchemaPropertyType.LocalTime,
    TypeSchemaPropertyType.LocalDateTime,
    TypeSchemaPropertyType.DateTimeZone,
}
_INTEGER_TYPES = {
    TypeSchemaPropertyType.Short,
    TypeSchemaPropertyType.Int,
    TypeSchemaPropertyType.Long,
}
_FLOAT_TYPES = {
    TypeSchemaPropertyType.Float,
    TypeSchemaPropertyType.Double,
}
# Ordering operators are refused by the backend's user-defined condition policy
# for Bool and Interval, and Enum is restricted to equality and In.  Every other
# scalar type -- including LocalDate, LocalTime, LocalDateTime and DateTimeZone,
# which are ordered domains despite serializing as text -- supports ordering.
_NON_COMPARABLE_TYPES = {
    TypeSchemaPropertyType.Bool,
    TypeSchemaPropertyType.Enum,
    TypeSchemaPropertyType.Interval,
}
_COLLECTION_TYPES = {
    TypeSchemaPropertyType.List,
    TypeSchemaPropertyType.Array,
    TypeSchemaPropertyType.Dictionary,
}
_DICT_KEY_TYPES = {
    TypeSchemaPropertyType.String,
    TypeSchemaPropertyType.Enum,
    TypeSchemaPropertyType.Int,
    TypeSchemaPropertyType.Guid,
}


def _validate_scalar_type(property_type: TypeSchemaPropertyType) -> None:
    if property_type in _COLLECTION_TYPES or property_type is TypeSchemaPropertyType.Invalid:
        raise ValueError(f"{property_type.name} is not a supported scalar UDP type")


@dataclass(frozen=True)
class UserDefinedValueAdapter:
    """Convert one definition's values between Python and backend wire shapes."""

    definition: UserDefinedPropertyInfo

    def __post_init__(self) -> None:
        property_type = self.definition.property_type
        sub_types = self.definition.sub_property_types
        if property_type in (TypeSchemaPropertyType.List, TypeSchemaPropertyType.Array):
            if len(sub_types) != 1:
                raise ValueError(f"{property_type.name} requires one element type")
            _validate_scalar_type(sub_types[0])
        elif property_type is TypeSchemaPropertyType.Dictionary:
            if len(sub_types) != 2:
                raise ValueError("Dictionary requires key and value types")
            if sub_types[0] not in _DICT_KEY_TYPES:
                raise ValueError(f"Unsupported dictionary key type {sub_types[0].name}")
            _validate_scalar_type(sub_types[1])
        else:
            if sub_types:
                raise ValueError(f"{property_type.name} must not have sub-property types")
            _validate_scalar_type(property_type)

    def from_wire(self, value: Any) -> Any:
        """Convert a JSON-compatible backend value to its Python representation."""
        if value is None:
            return None
        property_type = self.definition.property_type
        if property_type in (TypeSchemaPropertyType.List, TypeSchemaPropertyType.Array):
            if not isinstance(value, (list, tuple)):
                raise TypeError(f"Expected a list for {self.definition.name}")
            return [self._scalar_from_wire(item, self.definition.sub_property_types[0]) for item in value]
        if property_type is TypeSchemaPropertyType.Dictionary:
            if not isinstance(value, dict):
                raise TypeError(f"Expected a dictionary for {self.definition.name}")
            key_type, value_type = self.definition.sub_property_types
            return {
                self._scalar_from_wire(key, key_type): self._scalar_from_wire(item, value_type)
                for key, item in value.items()
            }
        return self._scalar_from_wire(value, property_type)

    def to_wire(self, value: Any) -> Any:
        """Convert a Python value to a JSON-compatible backend representation."""
        if value is None:
            return None
        property_type = self.definition.property_type
        if property_type in (TypeSchemaPropertyType.List, TypeSchemaPropertyType.Array):
            if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
                raise TypeError(f"Expected a list for {self.definition.name}")
            return [self._scalar_to_wire(item, self.definition.sub_property_types[0]) for item in value]
        if property_type is TypeSchemaPropertyType.Dictionary:
            if not isinstance(value, dict):
                raise TypeError(f"Expected a dictionary for {self.definition.name}")
            key_type, value_type = self.definition.sub_property_types
            return {
                str(self._scalar_to_wire(key, key_type)): self._scalar_to_wire(item, value_type)
                for key, item in value.items()
            }
        return self._scalar_to_wire(value, property_type)

    def condition_to_wire(self, value: Any) -> Optional[str]:
        """Convert a scalar condition operand to the backend string representation."""
        if value is None:
            return None
        converted = self.to_wire(value)
        if isinstance(converted, (dict, list)):
            raise TypeError("Collection values cannot be scalar condition operands")
        return str(converted)

    def key_condition_to_wire(self, value: Any) -> Optional[str]:
        """Convert a dictionary key condition operand."""
        if value is None:
            return None
        if self.definition.property_type is not TypeSchemaPropertyType.Dictionary:
            return self.condition_to_wire(value)
        return str(self._scalar_to_wire(value, self.definition.sub_property_types[0]))

    def value_condition_to_wire(self, value: Any) -> Optional[str]:
        """Convert a collection element/dictionary value condition operand."""
        if value is None:
            return None
        property_type = self.definition.property_type
        if property_type in (TypeSchemaPropertyType.List, TypeSchemaPropertyType.Array):
            scalar_type = self.definition.sub_property_types[0]
        elif property_type is TypeSchemaPropertyType.Dictionary:
            scalar_type = self.definition.sub_property_types[1]
        else:
            scalar_type = property_type
        return str(self._scalar_to_wire(value, scalar_type))

    @staticmethod
    def _scalar_from_wire(value: Any, property_type: TypeSchemaPropertyType) -> Any:
        if value is None:
            raise TypeError("Collection values cannot contain null")
        if isinstance(value, (dict, list, tuple)):
            raise TypeError("Nested UDP collection values are not supported")
        if property_type in _TEXT_TYPES:
            return value if isinstance(value, str) else str(value)
        if property_type is TypeSchemaPropertyType.Bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                return value.lower() == "true"
            if value in (0, 1):
                return bool(value)
            raise TypeError(f"Cannot convert {value!r} to bool")
        if property_type in _INTEGER_TYPES:
            return int(value)
        if property_type in _FLOAT_TYPES:
            return float(value)
        if property_type is TypeSchemaPropertyType.Decimal:
            return value if isinstance(value, Decimal) else Decimal(str(value))
        if property_type is TypeSchemaPropertyType.Guid:
            return value if isinstance(value, UUID) else UUID(str(value))
        if property_type is TypeSchemaPropertyType.Instant:
            return value if isinstance(value, datetime.datetime) else instant_from_wire(str(value))
        if property_type is TypeSchemaPropertyType.Duration:
            return value if isinstance(value, datetime.timedelta) else timedelta_from_wire(str(value))
        raise TypeError(f"Unsupported UDP scalar type {property_type.name}")

    @staticmethod
    def _scalar_to_wire(value: Any, property_type: TypeSchemaPropertyType) -> Any:
        if value is None:
            raise TypeError("Collection values cannot contain null")
        if isinstance(value, (dict, list, tuple)):
            raise TypeError("Nested UDP collection values are not supported")
        if property_type in _TEXT_TYPES:
            if not isinstance(value, str):
                raise TypeError(f"Expected str, got {type(value).__name__}")
            return value
        if property_type is TypeSchemaPropertyType.Bool:
            if not isinstance(value, bool):
                raise TypeError(f"Expected bool, got {type(value).__name__}")
            return value
        if property_type in _INTEGER_TYPES:
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"Expected int, got {type(value).__name__}")
            return value
        if property_type in _FLOAT_TYPES:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"Expected float, got {type(value).__name__}")
            return float(value)
        if property_type is TypeSchemaPropertyType.Decimal:
            if not isinstance(value, Decimal):
                raise TypeError(f"Expected Decimal, got {type(value).__name__}")
            return str(value)
        if property_type is TypeSchemaPropertyType.Guid:
            if not isinstance(value, UUID):
                raise TypeError(f"Expected UUID, got {type(value).__name__}")
            return str(value)
        if property_type is TypeSchemaPropertyType.Instant:
            if not isinstance(value, datetime.datetime):
                raise TypeError(f"Expected datetime, got {type(value).__name__}")
            return instant_to_wire(value)
        if property_type is TypeSchemaPropertyType.Duration:
            if not isinstance(value, datetime.timedelta):
                raise TypeError(f"Expected timedelta, got {type(value).__name__}")
            return timedelta_to_wire(value)
        raise TypeError(f"Unsupported UDP scalar type {property_type.name}")


@dataclass(frozen=True)
class UserDefinedProperty:
    """A discoverable UDP definition with permission and conversion metadata."""

    entity_type_name: str
    python_name: str
    wire_name: str
    permission: EntityPropertyPermission
    definition: UserDefinedPropertyInfo
    adapter: UserDefinedValueAdapter

    @property
    def actions(self) -> PermissionAction:
        """The unconditional property actions granted to the current user."""
        return self.permission.actions

    @property
    def label(self) -> str:
        return self.definition.label

    @property
    def description(self) -> Optional[str]:
        return self.definition.description

    @property
    def nullable(self) -> bool:
        return self.definition.nullable

    @property
    def property_type(self) -> TypeSchemaPropertyType:
        return self.definition.property_type

    @property
    def sub_property_types(self) -> tuple[TypeSchemaPropertyType, ...]:
        return tuple(self.definition.sub_property_types)

    def condition(self) -> ConditionProperty:
        """Create the condition wrapper appropriate for this definition's type."""
        property_type = self.definition.property_type
        if property_type in (TypeSchemaPropertyType.List, TypeSchemaPropertyType.Array):
            return DbConditionListProperty[Any](
                self.wire_name, _value_converter=self.adapter.value_condition_to_wire,
            )
        if property_type is TypeSchemaPropertyType.Dictionary:
            return DbConditionDictProperty[Any, Any](
                self.wire_name,
                _key_converter=self.adapter.key_condition_to_wire,
                _value_converter=self.adapter.value_condition_to_wire,
            )
        if property_type not in _NON_COMPARABLE_TYPES:
            return DbConditionComparableProperty[Any](
                self.wire_name, _value_converter=self.adapter.condition_to_wire,
            )
        return DbConditionProperty[Any](
            self.wire_name, _value_converter=self.adapter.condition_to_wire,
        )


def build_user_defined_properties(
    entity_type_name: str,
    permissions: EntityPropertyPermissions,
) -> Mapping[str, UserDefinedProperty]:
    """Build an immutable, Python-name-keyed UDP registry from permissions."""
    result: dict[str, UserDefinedProperty] = {}
    for permission in permissions.properties.values():
        definition = permission.user_defined_data
        if definition is None:
            continue
        python_name = user_defined_python_name(definition.name)
        if python_name is None:
            raise ValueError(
                f"Backend returned invalid user-defined property name {definition.name!r}"
            )
        existing = result.get(python_name)
        if existing is not None:
            raise ValueError(
                f"User-defined properties {existing.definition.name!r} and "
                f"{definition.name!r} both map to {python_name!r}"
            )
        result[python_name] = UserDefinedProperty(
            entity_type_name=entity_type_name,
            python_name=python_name,
            # The backend's own name, camelCased like every other wire key.  Do
            # not rebuild it from python_name: that round trip is lossy (str.title
            # capitalises after a digit, so UdpFoo2bar would come back udpFoo2Bar).
            wire_name=definition.name[:1].lower() + definition.name[1:],
            permission=permission,
            definition=definition,
            adapter=UserDefinedValueAdapter(definition),
        )
    return MappingProxyType(result)


def hydrate_user_defined_properties(
    entity: Any, properties: Mapping[str, UserDefinedProperty],
) -> set[str]:
    """Hydrate UDP attributes on *entity* and return unknown UDP names."""
    unknown: set[str] = set()
    for name, value in vars(entity).items():
        if not name.startswith("udp_"):
            continue
        udp_property = properties.get(name)
        if udp_property is None:
            unknown.add(name)
            continue
        object.__setattr__(entity, name, udp_property.adapter.from_wire(value))
    return unknown


__all__ = [
    "UserDefinedProperty",
    "UserDefinedValueAdapter",
    "build_user_defined_properties",
]

"""Base class for tracking which properties were explicitly specified."""
# NOTE: this module deliberately does NOT use ``from __future__ import
# annotations``.  The ``InitVar`` pseudo-field below must stay a real
# ``InitVar`` object rather than the string ``"InitVar[...]"``: on Python 3.10
# ``typing.get_type_hints`` evaluates such a string and rejects the result with
# ``TypeError: Forward references must evaluate to types``.  Python 3.11
# relaxed that check, so re-adding the import here breaks 3.10 only.  Forward
# references in this module must therefore be quoted explicitly.

from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from typing import Any, Optional


@dataclass(kw_only=True)
class SpecifiedPropertiesBase:
    """Base class that tracks which properties were explicitly passed to __init__.

    Works with ``@dataclass(kw_only=True)`` subclasses.  Only keyword arguments
    explicitly provided by the caller are recorded in ``specified_properties``.
    ``id`` is always included (client generates UUIDs the server needs).

    ``to_dict()`` in ``_serialization.py`` already checks for the
    ``specified_properties`` attribute and limits serialization to those fields.
    """

    specified_properties: Optional[list[str]] = field(
        init=False, repr=False, compare=False,
    )
    _user_defined_properties: InitVar[Optional[Mapping[str, Any]]] = None

    def __new__(cls, **kwargs: Any) -> "SpecifiedPropertiesBase":
        instance = super().__new__(cls)
        specified = [
            name for name in kwargs if name != "_user_defined_properties"
        ]
        if "id" not in specified:
            specified.insert(0, "id")
        object.__setattr__(instance, "specified_properties", specified)
        return instance

    def __post_init__(
        self, _user_defined_properties: Optional[Mapping[str, Any]],
    ) -> None:
        """Install constructor-only user-defined values as dynamic attributes."""
        if _user_defined_properties is None:
            return
        if not isinstance(_user_defined_properties, Mapping):
            raise TypeError("_user_defined_properties must be a mapping")
        for name, value in _user_defined_properties.items():
            if not isinstance(name, str) or not name.startswith("udp_"):
                raise ValueError(
                    "User-defined property names must be strings beginning with 'udp_'"
                )
            setattr(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        """Track dynamic ``udp_*`` assignments like generated dataclass fields."""
        if name == "_user_defined_properties":
            raise AttributeError(
                "_user_defined_properties is a constructor-only input"
            )
        object.__setattr__(self, name, value)
        if name.startswith("udp_"):
            specified = getattr(self, "specified_properties", None)
            if specified is not None and name not in specified:
                specified.append(name)

    def __getattribute__(self, name: str) -> Any:
        """Keep the constructor-only UDP mapping unreadable after initialization."""
        if name == "_user_defined_properties":
            raise AttributeError(
                "_user_defined_properties is a constructor-only input"
            )
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        """Provide the mypy plugin a hook point while preserving normal runtime errors."""
        if name == "_user_defined_properties":
            raise AttributeError(
                "_user_defined_properties is a constructor-only input"
            )
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

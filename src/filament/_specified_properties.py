"""Base class for tracking which properties were explicitly specified."""
from __future__ import annotations

from dataclasses import dataclass, field
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

    def __new__(cls, **kwargs: Any) -> SpecifiedPropertiesBase:
        instance = super().__new__(cls)
        specified = list(kwargs.keys())
        if "id" not in specified:
            specified.insert(0, "id")
        object.__setattr__(instance, "specified_properties", specified)
        return instance

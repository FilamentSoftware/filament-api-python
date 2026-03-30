__version__ = "0.0.0-dev"

from typing import Optional, Type, TypeVar

from filament._client import FilamentClient
from filament._condition_builder import Condition
from filament._exceptions import (
    FilamentAuthenticationError,
    FilamentConnectionError,
    FilamentError,
)
from filament.types.generated.base.base import EntityBase

_E = TypeVar("_E", bound=EntityBase)


def as_entity(entity: EntityBase, entity_type: Type[_E]) -> Optional[_E]:
    """Check that *entity* is an instance of *entity_type* and return it, or ``None``."""
    if isinstance(entity, entity_type):
        return entity
    return None


__all__ = [
    "as_entity",
    "Condition",
    "FilamentClient",
    "FilamentAuthenticationError",
    "FilamentConnectionError",
    "FilamentError",
]

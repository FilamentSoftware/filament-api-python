"""Filament Python API — async client for the Filament platform over SignalR."""
__version__ = "0.0.0-dev"

from typing import Optional, Type, TypeVar

from ._client import FilamentClient
from ._condition_builder import Condition
from ._exceptions import (
    FilamentAuthenticationError,
    FilamentConnectionError,
    FilamentError,
)
from ._specified_properties import SpecifiedPropertiesBase
from .types.generated.base.base import EntityBase

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
    "SpecifiedPropertiesBase",
]

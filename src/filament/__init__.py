"""Filament Python API — async client for the Filament platform over SignalR."""
__version__ = "0.0.0-dev"

import json
from typing import Optional, Type, TypeVar

from ._client import FilamentClient
from ._condition_builder import Condition
from ._exceptions import (
    FilamentAuthenticationError,
    FilamentConnectionError,
    FilamentError,
    ResponseTooLargeError,
)
from ._file_upload import build_upload_request, compute_checksum
from ._serialization import from_dict
from ._specified_properties import SpecifiedPropertiesBase
from .user_defined_properties import UserDefinedProperty, UserDefinedValueAdapter
from .types.generated.base.base import EntityBase
from .types.generated.shared import FileReference

_E = TypeVar("_E", bound=EntityBase)


def as_entity(entity: EntityBase, entity_type: Type[_E]) -> Optional[_E]:
    """Check that *entity* is an instance of *entity_type* and return it, or ``None``."""
    if isinstance(entity, entity_type):
        return entity
    return None


def file_reference_from_json(value: str) -> FileReference:
    """Deserialize a ``[FileDataProperty]`` read projection into a :class:`FileReference`.

    Properties such as ``UserEntity.image_data`` come back as a JSON-encoded
    ``FileReference`` string when a read sets ``populate_file_data`` and requests
    the property.  The returned reference already carries a signed ``url``, so the
    bytes can be fetched with :meth:`FilamentClient.download_file_reference`
    without a separate ``get_file_urls`` call.

    :raises ValueError: if *value* is not a JSON object encoding a
        ``FileReference`` (e.g. a JSON array, scalar, or ``null``).
    """
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError(
            f"Expected a JSON object for FileReference, got {type(decoded).__name__}"
        )
    result = from_dict(decoded, FileReference)
    if result is None:
        raise ValueError("Could not deserialize FileReference from projection")
    return result


__all__ = [
    "as_entity",
    "build_upload_request",
    "compute_checksum",
    "file_reference_from_json",
    "Condition",
    "FilamentClient",
    "FilamentAuthenticationError",
    "FilamentConnectionError",
    "FilamentError",
    "ResponseTooLargeError",
    "SpecifiedPropertiesBase",
    "UserDefinedProperty",
    "UserDefinedValueAdapter",
]

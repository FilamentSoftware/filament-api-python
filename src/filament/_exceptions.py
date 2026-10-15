"""Exception hierarchy for the Filament client."""
from __future__ import annotations


class FilamentError(Exception):
    """Base exception for the Filament client."""


class FilamentAuthenticationError(FilamentError):
    """Raised when the server rejects authentication."""


class FilamentConnectionError(FilamentError):
    """Raised when the SignalR connection fails."""


class ResponseTooLargeError(FilamentError):
    """Raised when a read response is too large to be paged any smaller.

    :meth:`~filament.FilamentClient.read_data` automatically splits a response
    the server rejects as too large into smaller pages.  When even a single
    entity's response still exceeds the client's negotiated limit there is
    nothing left to split, so this error is raised instead.

    :ivar entity_bytes: Serialised size, in bytes, of the single-entity page
        that could not be made to fit.
    :ivar full_request_bytes: Serialised size, in bytes, the original
        (un-paged) request would have produced.
    :ivar limit_bytes: The maximum message size, in bytes, the client
        negotiated for the connection.
    """

    def __init__(
        self,
        entity_bytes: int,
        full_request_bytes: int,
        limit_bytes: int,
    ) -> None:
        self.entity_bytes = entity_bytes
        self.full_request_bytes = full_request_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"A single entity's response is {entity_bytes:,} bytes, which still "
            f"exceeds the client limit of {limit_bytes:,} bytes; the full request "
            f"would have been {full_request_bytes:,} bytes."
        )


class CompressedPropertyError(FilamentError, ValueError):
    """Raised when a compressed property value cannot be decoded.

    Fields whose dataclass metadata carries ``compression`` / ``encoding``
    keys hold either plain text or ``<compression>+<encoding>:<payload>``.
    The value's header decides how it is decoded.  An unknown compression or
    encoding token, or a known header whose payload fails to decode, raises
    this error rather than returning the raw (still encoded) string.

    It is also a :class:`ValueError`, so code that already guards
    deserialization with ``except ValueError`` keeps handling it.

    :ivar owner: Name of the class that owns the field (may be empty).
    :ivar field_name: Name of the field being decoded (may be empty).
    :ivar header: The value's header, e.g. ``"zstd+base64:"``.
    """

    def __init__(
        self,
        message: str,
        *,
        owner: str = "",
        field_name: str = "",
        header: str = "",
    ) -> None:
        self.owner = owner
        self.field_name = field_name
        self.header = header
        super().__init__(message)

"""Exception hierarchy for the Filament client."""
from __future__ import annotations


class FilamentError(Exception):
    """Base exception for the Filament client."""


class FilamentAuthenticationError(FilamentError):
    """Raised when the server rejects authentication."""


class FilamentConnectionError(FilamentError):
    """Raised when the SignalR connection fails."""

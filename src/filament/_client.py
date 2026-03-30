"""Async Filament API client over SignalR."""
from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Optional, Type, Union

from signalr_async.netcore import Hub, Client
from signalr_async.netcore.protocols import JsonProtocol
from uuid_utils import UUID

from filament._exceptions import FilamentAuthenticationError
from filament._serialization import from_dict, to_dict
from filament.types.generated.misc import (
    EntityPermissions,
    EntityPropertyPermissions,
    ModifyDataRequest,
    ProcessingStatus,
    ReadDataRequest,
    RequestId,
    ServiceDataRead,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionId,
    SuccessOrErrorPublic,
    ValueOrErrorPublic_1,
)
from filament.types.generated.enums import ErrorCodes

logger = logging.getLogger(__name__)

# Type alias for user callbacks — may be sync or async.
Callback = Union[Callable[..., Any], Callable[..., Awaitable[Any]]]

_DEFAULT_SERVER = "https://backend.filament-software.com"
_DEFAULT_PORT = 7076
_DEFAULT_PATH = "api/v0"


class FilamentClient:
    """Async client for the Filament SignalR API.

    Usage::

        async with FilamentClient(
            organization_id="org-id", api_key_name="my-key", api_key="secret",
        ) as f:
            result = await f.read_data(request)
    """

    def __init__(
        self,
        organization_id: str,
        api_key_name: str,
        api_key: str,
        server: str = _DEFAULT_SERVER,
        port: int = _DEFAULT_PORT,
        path: str = _DEFAULT_PATH,
    ) -> None:
        self._api_key_name = api_key_name
        self._authentication_failed = False
        self._auth_message: Optional[str] = None

        # Subscription routing: subscription UUID -> user callback
        self._subscriptions: dict[UUID, Callback] = {}

        # User-registered event callbacks
        self._on_processing_started_callbacks: list[Callback] = []
        self._on_processing_completed_callbacks: list[Callback] = []
        self._on_processing_failed_callbacks: list[Callback] = []

        # Build SignalR connection
        url = f"{server}:{port}"
        self._hub: Hub = Hub(path)

        # Register internal event handlers
        self._hub.on("AuthenticationFailed", self._handle_authentication_failed)
        self._hub.on("DataUpdated", self._handle_data_updated)
        self._hub.on("ProcessingStarted", self._handle_processing_started)
        self._hub.on("ProcessingCompleted", self._handle_processing_completed)
        self._hub.on("ProcessingFailed", self._handle_processing_failed)
        self._hub.on("KeepAlive", self._handle_keep_alive)

        headers = {
            "X-Api-Key-Name": api_key_name,
            "X-Api-Key": api_key,
            "X-Organization-Id": organization_id,
        }
        self._client: Client = Client(
            url,
            self._hub,
            keepalive_interval=3600,
            connection_options={
                "protocol": JsonProtocol(),
                "http_client_options": {"headers": headers},
                "ws_client_options": {"headers": headers, "timeout": 1.0},
            },
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> FilamentClient:
        await self._client.__aenter__()
        if self._authentication_failed:
            await self._client.__aexit__(None, None, None)
            raise FilamentAuthenticationError(
                self._auth_message or "Authentication failed"
            )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)

    # ------------------------------------------------------------------
    # Internal event handlers
    # ------------------------------------------------------------------

    async def _handle_authentication_failed(self, message: str = "") -> None:
        logger.error("Authentication failed: %s", message)
        self._authentication_failed = True
        self._auth_message = message

    async def _handle_data_updated(self, data: Any) -> None:
        update = from_dict(data, ServiceDataUpdate)
        if update is None:
            logger.warning("Failed to deserialize DataUpdated payload")
            return

        callback = self._subscriptions.get(update.subscription_id.id)
        if callback is not None:
            try:
                await _invoke_callback(callback, update)
            except Exception:
                logger.exception(
                    "Error in DataUpdated callback for subscription %s",
                    update.subscription_id.id,
                )

    async def _handle_processing_started(self, data: Any) -> None:
        status = from_dict(data, ProcessingStatus)
        if status is None:
            return
        for cb in self._on_processing_started_callbacks:
            try:
                await _invoke_callback(cb, status)
            except Exception:
                logger.exception("Error in ProcessingStarted callback")

    async def _handle_processing_completed(self, data: Any) -> None:
        status = from_dict(data, ProcessingStatus)
        if status is None:
            return
        for cb in self._on_processing_completed_callbacks:
            try:
                await _invoke_callback(cb, status)
            except Exception:
                logger.exception("Error in ProcessingCompleted callback")

    async def _handle_processing_failed(self, data: Any) -> None:
        status = from_dict(data, ProcessingStatus)
        if status is None:
            return
        for cb in self._on_processing_failed_callbacks:
            try:
                await _invoke_callback(cb, status)
            except Exception:
                logger.exception("Error in ProcessingFailed callback")

    async def _handle_keep_alive(self, data: Any) -> None:
        # Handled internally — not exposed to users.
        pass

    # ------------------------------------------------------------------
    # Event callback registration
    # ------------------------------------------------------------------

    def on_processing_started(self, callback: Callback) -> None:
        """Register a callback for ``ProcessingStarted`` events."""
        self._on_processing_started_callbacks.append(callback)

    def on_processing_completed(self, callback: Callback) -> None:
        """Register a callback for ``ProcessingCompleted`` events."""
        self._on_processing_completed_callbacks.append(callback)

    def on_processing_failed(self, callback: Callback) -> None:
        """Register a callback for ``ProcessingFailed`` events."""
        self._on_processing_failed_callbacks.append(callback)

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    async def get_entity_permissions(
        self,
    ) -> ValueOrErrorPublic_1[EntityPermissions]:
        """Retrieve entity-level permissions for the authenticated user."""
        result_dict: Any = await self._hub.invoke("GetEntityPermissions")
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[EntityPermissions])
        except (TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def get_entity_property_permissions(
        self,
        entity_type_name: str,
    ) -> ValueOrErrorPublic_1[EntityPropertyPermissions]:
        """Retrieve property-level permissions for a given entity type."""
        result_dict: Any = await self._hub.invoke(
            "GetEntityPropertyPermissions", entity_type_name
        )
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[EntityPropertyPermissions])
        except (TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def read_data(
        self,
        request: ReadDataRequest,
    ) -> ValueOrErrorPublic_1[ServiceDataRead]:
        """Read entity data from the server."""
        request_dict = to_dict(request)
        result_dict: Any = await self._hub.invoke("ReadData", request_dict)
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[ServiceDataRead])
        except (TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def modify_data(
        self,
        request: ModifyDataRequest,
    ) -> ValueOrErrorPublic_1[RequestId]:
        """Create, update, or delete entity data."""
        request_dict = to_dict(request)
        result_dict: Any = await self._hub.invoke("ModifyData", request_dict)
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[RequestId])
        except (TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def subscribe(
        self,
        subscription: SubscriptionConfig,
        callback: Callback,
    ) -> ValueOrErrorPublic_1[SubscriptionId]:
        """Subscribe to data updates and register a callback.

        The *callback* is called with a :class:`ServiceDataUpdate` argument
        whenever the server sends a ``DataUpdated`` event for this subscription.
        It may be a sync or async callable.
        """
        subscription_dict = to_dict(subscription)
        result_dict: Any = await self._hub.invoke("Subscribe", subscription_dict)
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[SubscriptionId])
        except (TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is None:
            return _deserialization_error()

        if result.success and result.data is not None:
            self._subscriptions[result.data.id] = callback

        return result

    async def unsubscribe(
        self,
        subscription_id: SubscriptionId,
    ) -> SuccessOrErrorPublic:
        """Cancel a subscription."""
        subscription_dict = to_dict(subscription_id)
        result_dict = await self._hub.invoke("Unsubscribe", subscription_dict)
        self._subscriptions.pop(subscription_id.id, None)
        result = from_dict(result_dict, SuccessOrErrorPublic)
        if result is not None:
            return result
        return _error_result_public("Failed to deserialize Unsubscribe response")

    async def unsubscribe_all(self) -> SuccessOrErrorPublic:
        """Cancel all active subscriptions."""
        result_dict = await self._hub.invoke("UnsubscribeAll")
        self._subscriptions.clear()
        result = from_dict(result_dict, SuccessOrErrorPublic)
        if result is not None:
            return result
        return _error_result_public("Failed to deserialize UnsubscribeAll response")

def _deserialization_error() -> ValueOrErrorPublic_1[Any]:
    return ValueOrErrorPublic_1(
        success=False,
        message="Failed to deserialize server response",
        error_code=ErrorCodes.InternalError,
        data=None,
    )


def _error_result_public(message: str) -> SuccessOrErrorPublic:
    return SuccessOrErrorPublic(
        success=False,
        message=message,
        error_code=ErrorCodes.InternalError,
    )


async def _invoke_callback(callback: Callback, *args: Any) -> None:
    """Invoke a callback that may be sync or async."""
    if inspect.iscoroutinefunction(callback):
        await callback(*args)
    else:
        callback(*args)

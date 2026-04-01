"""Async Filament API client over SignalR."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Optional, Type, Union

from pysignalr.client import SignalRClient
from pysignalr.messages import CompletionMessage, Message
from uuid_utils import UUID, uuid7

from filament._exceptions import FilamentAuthenticationError, FilamentConnectionError
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
_DEFAULT_CONNECTION_TIMEOUT = 10.0


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
        connection_timeout: float = _DEFAULT_CONNECTION_TIMEOUT,
    ) -> None:
        """Initialise the client.

        :param organization_id: The Filament organization to connect to.
        :param api_key_name: Name of the API key used for authentication.
        :param api_key: Secret value of the API key.
        :param server: Base URL of the Filament backend server.
        :param port: Port number the backend listens on.
        :param path: URL path prefix for the SignalR hub endpoint.
        :param connection_timeout: Timeout in seconds for the initial
            connection to the server (default 10).
        """
        self._api_key_name = api_key_name
        self._authentication_failed = False
        self._auth_message: Optional[str] = None
        self._connection_timeout = connection_timeout

        # Subscription routing — supports multiple callbacks per backend
        # subscription.  The backend returns the same subscription ID when
        # the same type+permissions are subscribed to more than once.  We
        # generate a unique *client* subscription ID for each ``subscribe``
        # call so callers can unsubscribe independently.
        #
        # backend sub ID  →  set of client sub IDs
        self._backend_to_client: dict[UUID, set[UUID]] = {}
        # client sub ID  →  callback
        self._client_callbacks: dict[UUID, Callback] = {}
        # client sub ID  →  backend sub ID  (reverse lookup for unsubscribe)
        self._client_to_backend: dict[UUID, UUID] = {}

        # User-registered event callbacks
        self._on_processing_started_callbacks: list[Callback] = []
        self._on_processing_completed_callbacks: list[Callback] = []
        self._on_processing_failed_callbacks: list[Callback] = []

        # Build SignalR connection
        self._url = f"{server}:{port}/{path}"

        token_raw = f"{api_key_name}:{organization_id}:{api_key}"
        token = base64.b64encode(token_raw.encode()).decode()
        headers = {
            "Authorization": f"ApiKey {token}",
        }
        self._signalr_client: SignalRClient = SignalRClient(
            url=self._url,
            headers=headers,
            connection_timeout=int(connection_timeout),
            ping_interval=3600,
        )

        # Register internal event handlers.  pysignalr passes
        # message.arguments (a list) to on() callbacks, so we wrap
        # each handler to unpack the arguments list.
        self._signalr_client.on("AuthenticationFailed", self._wrap_handler(self._handle_authentication_failed))
        self._signalr_client.on("DataUpdated", self._wrap_handler(self._handle_data_updated))
        self._signalr_client.on("ProcessingStarted", self._wrap_handler(self._handle_processing_started))
        self._signalr_client.on("ProcessingCompleted", self._wrap_handler(self._handle_processing_completed))
        self._signalr_client.on("ProcessingFailed", self._wrap_handler(self._handle_processing_failed))
        self._signalr_client.on("KeepAlive", self._wrap_handler(self._handle_keep_alive))

        # pysignalr requires an error callback to avoid RuntimeError
        # when a server invocation returns an error.
        self._signalr_client.on_error(self._handle_signalr_error)

        # Background task for pysignalr's run() loop
        self._run_task: Optional[asyncio.Task[None]] = None
        self._connected: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> FilamentClient:
        """Open the SignalR connection.

        Raises :class:`FilamentConnectionError` if the server cannot be
        reached, and :class:`FilamentAuthenticationError` if the server
        rejects the API key during the connection handshake.
        """
        self._connected = asyncio.Event()
        self._signalr_client.on_open(self._on_open)

        self._run_task = asyncio.create_task(self._signalr_client.run())

        # Wait for either: connection established, run() failing, or timeout
        wait_connected = asyncio.ensure_future(self._connected.wait())
        done, pending = await asyncio.wait(
            {wait_connected, self._run_task},
            timeout=self._connection_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel the wait future if still pending
        for fut in pending:
            if fut is wait_connected:
                fut.cancel()

        if wait_connected not in done:
            # Either run_task failed or we timed out
            self._run_task.cancel()
            try:
                await self._run_task
            except Exception as exc:
                raise FilamentConnectionError(
                    f"Failed to connect to {self._url}: {exc}"
                ) from None
            except asyncio.CancelledError:
                pass

            raise FilamentConnectionError(
                f"Failed to connect to {self._url}: connection timed out"
            ) from None

        if self._authentication_failed:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
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
        """Close the SignalR connection."""
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task

    # ------------------------------------------------------------------
    # SignalR invoke helper
    # ------------------------------------------------------------------

    async def _invoke(self, method: str, *args: Any) -> Any:
        """Send a hub invocation and await the server's response.

        Wraps pysignalr's fire-and-forget ``send()`` with a
        :class:`asyncio.Future` so callers can ``await`` the result.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        async def on_completion(message: Message) -> None:
            if future.done():
                return
            if isinstance(message, CompletionMessage):
                if message.error:
                    future.set_exception(Exception(message.error))
                else:
                    future.set_result(message.result)

        await self._signalr_client.send(method, list(args), on_invocation=on_completion)
        return await future

    # ------------------------------------------------------------------
    # Internal event handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_handler(
        handler: Callable[..., Awaitable[Any]],
    ) -> Callable[[Any], Awaitable[None]]:
        """Wrap an event handler to unpack pysignalr's arguments list."""
        async def wrapper(arguments: Any) -> None:
            if isinstance(arguments, list):
                await handler(*arguments)
            else:
                await handler(arguments)
        return wrapper

    async def _on_open(self) -> None:
        """Called when the SignalR connection is established."""
        # Brief yield to let immediate server messages (e.g.
        # AuthenticationFailed) arrive before we signal ready.
        await asyncio.sleep(0.1)
        if self._connected is not None:
            self._connected.set()

    async def _handle_signalr_error(self, message: CompletionMessage) -> None:
        """Handle invocation errors from the server."""
        logger.error("SignalR invocation error: %s", message.error)

    async def _handle_authentication_failed(self, message: str = "") -> None:
        """Record an ``AuthenticationFailed`` event from the server."""
        logger.error("Authentication failed: %s", message)
        self._authentication_failed = True
        self._auth_message = message
        # Signal connected so __aenter__ can check the flag and raise
        if self._connected is not None:
            self._connected.set()

    async def _handle_data_updated(self, data: Any) -> None:
        """Deserialize a ``DataUpdated`` event and fan out to all matching callbacks."""
        try:
            update = from_dict(data, ServiceDataUpdate)
        except (TypeError, ValueError, KeyError):
            logger.exception("Failed to deserialize DataUpdated payload")
            return
        if update is None:
            logger.warning("Failed to deserialize DataUpdated payload")
            return

        backend_id = update.subscription_id.id
        client_ids = self._backend_to_client.get(backend_id)
        if client_ids is not None:
            for client_id in list(client_ids):
                callback = self._client_callbacks.get(client_id)
                if callback is not None:
                    try:
                        await _invoke_callback(callback, update)
                    except Exception:
                        logger.exception(
                            "Error in DataUpdated callback for subscription %s"
                            " (client %s)",
                            backend_id,
                            client_id,
                        )

    async def _handle_processing_started(self, data: Any) -> None:
        """Deserialize a ``ProcessingStarted`` event and invoke registered callbacks."""
        try:
            status = from_dict(data, ProcessingStatus)
        except (TypeError, ValueError, KeyError):
            logger.exception("Failed to deserialize %s payload", "ProcessingStarted")
            return
        if status is None:
            return
        for cb in self._on_processing_started_callbacks:
            try:
                await _invoke_callback(cb, status)
            except Exception:
                logger.exception("Error in ProcessingStarted callback")

    async def _handle_processing_completed(self, data: Any) -> None:
        """Deserialize a ``ProcessingCompleted`` event and invoke registered callbacks."""
        try:
            status = from_dict(data, ProcessingStatus)
        except (TypeError, ValueError, KeyError):
            logger.exception("Failed to deserialize %s payload", "ProcessingCompleted")
            return
        if status is None:
            return
        for cb in self._on_processing_completed_callbacks:
            try:
                await _invoke_callback(cb, status)
            except Exception:
                logger.exception("Error in ProcessingCompleted callback")

    async def _handle_processing_failed(self, data: Any) -> None:
        """Deserialize a ``ProcessingFailed`` event and invoke registered callbacks."""
        try:
            status = from_dict(data, ProcessingStatus)
        except (TypeError, ValueError, KeyError):
            logger.exception("Failed to deserialize %s payload", "ProcessingFailed")
            return
        if status is None:
            return
        for cb in self._on_processing_failed_callbacks:
            try:
                await _invoke_callback(cb, status)
            except Exception:
                logger.exception("Error in ProcessingFailed callback")

    async def _handle_keep_alive(self, data: Any) -> None:
        """Handle a ``KeepAlive`` heartbeat from the server (no-op)."""

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
        result_dict: Any = await self._invoke("GetEntityPermissions")
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
        result_dict: Any = await self._invoke(
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
        result_dict: Any = await self._invoke("ReadData", request_dict)
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
        result_dict: Any = await self._invoke("ModifyData", request_dict)
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

        Returns a client-generated :class:`SubscriptionId`.  Use this ID with
        :meth:`unsubscribe` to remove the callback.  The backend is always
        called and may return the same subscription ID for duplicate
        type/permission combinations.  The backend ``Unsubscribe`` is only
        sent when the last client subscription sharing a backend ID is removed.
        """
        subscription_dict = to_dict(subscription)
        result_dict: Any = await self._invoke("Subscribe", subscription_dict)
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[SubscriptionId])
        except (TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is None:
            return _deserialization_error()

        if result.success and result.data is not None:
            backend_id = result.data.id
            client_id = uuid7()

            # Track the mapping
            if backend_id not in self._backend_to_client:
                self._backend_to_client[backend_id] = set()
            self._backend_to_client[backend_id].add(client_id)
            self._client_callbacks[client_id] = callback
            self._client_to_backend[client_id] = backend_id

            # Return the client-generated subscription ID
            return ValueOrErrorPublic_1(
                success=result.success,
                message=result.message,
                error_code=result.error_code,
                error_id=None,
                data=SubscriptionId(id=client_id),
            )

        return result

    async def unsubscribe(
        self,
        subscription_id: SubscriptionId,
    ) -> SuccessOrErrorPublic:
        """Cancel a subscription by its client-generated ID.

        The backend ``Unsubscribe`` call is only made when the last client
        subscription sharing the same backend subscription ID is removed.
        """
        client_id = subscription_id.id
        backend_id = self._client_to_backend.pop(client_id, None)
        self._client_callbacks.pop(client_id, None)

        if backend_id is not None:
            client_ids = self._backend_to_client.get(backend_id)
            if client_ids is not None:
                client_ids.discard(client_id)
                if not client_ids:
                    # Last client for this backend subscription — tell backend
                    del self._backend_to_client[backend_id]
                    backend_sub = SubscriptionId(id=backend_id)
                    result_dict = await self._invoke(
                        "Unsubscribe", to_dict(backend_sub),
                    )
                    result = from_dict(result_dict, SuccessOrErrorPublic)
                    if result is not None:
                        return result
                    return _error_result_public(
                        "Failed to deserialize Unsubscribe response",
                    )

        # No backend call needed (or unknown client ID) — return success
        return SuccessOrErrorPublic(
            success=True,
            message="",
            error_code=ErrorCodes.None_,
            error_id=None,
        )

    async def unsubscribe_all(self) -> SuccessOrErrorPublic:
        """Cancel all active subscriptions."""
        result_dict = await self._invoke("UnsubscribeAll")
        self._backend_to_client.clear()
        self._client_callbacks.clear()
        self._client_to_backend.clear()
        result = from_dict(result_dict, SuccessOrErrorPublic)
        if result is not None:
            return result
        return _error_result_public("Failed to deserialize UnsubscribeAll response")

def _deserialization_error() -> ValueOrErrorPublic_1[Any]:
    """Return a failed ``ValueOrErrorPublic_1`` for deserialization failures."""
    return ValueOrErrorPublic_1(
        success=False,
        message="Failed to deserialize server response",
        error_code=ErrorCodes.InternalError,
        error_id=None,
        data=None,
    )


def _error_result_public(message: str) -> SuccessOrErrorPublic:
    """Return a failed ``SuccessOrErrorPublic`` with the given message."""
    return SuccessOrErrorPublic(
        success=False,
        message=message,
        error_code=ErrorCodes.InternalError,
        error_id=None,
    )


async def _invoke_callback(callback: Callback, *args: Any) -> None:
    """Invoke a callback that may be sync or async."""
    if inspect.iscoroutinefunction(callback):
        await callback(*args)
    else:
        callback(*args)

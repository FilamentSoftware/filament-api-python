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

from filament._exceptions import FilamentAuthenticationError, FilamentConnectionError, FilamentError
from filament._serialization import from_dict, to_dict
from filament.types.generated.misc import (
    ClientHelloRequest,
    DataUpdateRejectedNotification,
    EntityPermissions,
    EntityPropertyPermissions,
    EntityServiceDataUpdate,
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
_DEFAULT_PORT = 443
_DEFAULT_PATH = "api/v0"
_DEFAULT_CONNECTION_TIMEOUT = 10.0
# Matches the websockets-library default that pysignalr uses; pass ``None`` for unlimited.
_DEFAULT_MAX_SIZE = 2**20  # 1 MiB


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
        max_size: Optional[int] = _DEFAULT_MAX_SIZE,
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
        :param max_size: Maximum size in bytes of an incoming SignalR
            message (default 1 MiB).  Pass ``None`` to remove the limit.
            When the server sends a larger message the websocket is
            closed with code 1009 and any in-flight invocation fails
            with :class:`FilamentConnectionError`.
        """
        self._api_key_name = api_key_name
        self._authentication_failed = False
        self._auth_message: Optional[str] = None
        self._connection_timeout = connection_timeout
        self._max_size = max_size

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
        # client sub ID  →  on_error callback for DataUpdateRejected
        self._client_error_callbacks: dict[UUID, Callback] = {}
        # client sub ID  →  backend sub ID  (reverse lookup for unsubscribe)
        self._client_to_backend: dict[UUID, UUID] = {}

        # Reassembly buffers for chunked DataUpdated events.
        # Key: (backend_subscription_id, sequence_idx) — the logical update being reassembled.
        # Value: dict mapping part_idx → ServiceDataUpdate part.
        self._part_buffers: dict[tuple[UUID, int], dict[int, ServiceDataUpdate]] = {}
        # Keys whose multi-part reassembly is permanently poisoned: an inconsistent
        # part_count was observed for the (backend_sub, sequence_idx).  Any further
        # frames for these keys are dropped with a warning rather than starting a
        # fresh buffer (we cannot trust later parts of a sequence the backend has
        # already proven inconsistent for).  Cleared on close / unsubscribe.
        self._poisoned_part_buffer_keys: set[tuple[UUID, int]] = set()

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
            max_size=max_size,
        )

        # Register internal event handlers.  pysignalr passes
        # message.arguments (a list) to on() callbacks, so we wrap
        # each handler to unpack the arguments list.
        self._signalr_client.on("AuthenticationFailed", self._wrap_handler(self._handle_authentication_failed))
        self._signalr_client.on("DataUpdated", self._wrap_handler(self._handle_data_updated))
        self._signalr_client.on("DataUpdateRejected", self._wrap_handler(self._handle_data_update_rejected))
        self._signalr_client.on("ProcessingStarted", self._wrap_handler(self._handle_processing_started))
        self._signalr_client.on("ProcessingCompleted", self._wrap_handler(self._handle_processing_completed))
        self._signalr_client.on("ProcessingFailed", self._wrap_handler(self._handle_processing_failed))
        self._signalr_client.on("KeepAlive", self._wrap_handler(self._handle_keep_alive))

        # pysignalr requires an error callback to avoid RuntimeError
        # when a server invocation returns an error.
        self._signalr_client.on_error(self._handle_signalr_error)

        # In-flight invocation futures, tracked so they can be failed
        # collectively if the SignalR connection drops before the
        # server response arrives (e.g. websocket close 1009 when a
        # response exceeds the client's max frame size — otherwise
        # the awaiting caller would hang indefinitely).
        self._pending_invocations: set[asyncio.Future[Any]] = set()
        self._signalr_client.on_close(self._handle_close)

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

        try:
            await self._send_client_hello()
        except BaseException:
            # Mirror the auth-failure / timeout exit paths: cancel the background
            # run loop before propagating, otherwise __aexit__ never runs and the
            # task is orphaned with a "Task was destroyed but pending" warning.
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
            raise
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
        If the SignalR connection drops before the server replies,
        the future is failed with :class:`FilamentConnectionError`
        so the caller sees an exception instead of hanging.
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

        self._pending_invocations.add(future)
        future.add_done_callback(self._pending_invocations.discard)

        try:
            await self._signalr_client.send(method, list(args), on_invocation=on_completion)
        except BaseException:
            if not future.done():
                future.cancel()
            raise
        return await future

    async def _send_client_hello(self) -> None:
        """Advertise the negotiated maximum SignalR message size to the server.

        Sent once per connection.  Older backends that don't implement the
        ``ClientHello`` hub method return an invocation error; we log it and
        continue, since the client-side ``max_size`` cap still applies.
        """
        request = ClientHelloRequest(max_message_size_bytes=self._max_size)
        try:
            result_dict: Any = await self._invoke("ClientHello", to_dict(request))
        except FilamentError:
            # Connection / authentication / other lifecycle errors bubble — only
            # the "older server doesn't implement ClientHello" case gets swallowed.
            raise
        except Exception as exc:
            logger.warning(
                "ClientHello invocation failed (older server?): %s. "
                "Continuing with client-side max_size enforcement only.",
                exc,
            )
            return

        try:
            result = from_dict(result_dict, SuccessOrErrorPublic)
        except (TypeError, ValueError, KeyError):
            logger.warning(
                "Failed to deserialize ClientHello response — continuing.",
            )
            return
        if result is None or not result.success:
            logger.warning(
                "Server rejected ClientHello: %s",
                result.message if result is not None else "no result",
            )

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

    async def _handle_close(self) -> None:
        """Tidy up in-flight state when the SignalR connection drops.

        - Fails any awaiting :meth:`_invoke` futures with
          :class:`FilamentConnectionError`, so callers see an exception
          instead of hanging when the websocket closes (e.g. close code
          1009 — message too big).
        - Discards any in-progress multi-part :class:`ServiceDataUpdate`
          reassemblies and any poisoned-sequence markers — the server has
          no idea what we'd buffered and cannot resume across a reconnect.
        """
        if self._pending_invocations:
            error = FilamentConnectionError(
                "SignalR connection closed before the server response arrived"
            )
            for future in list(self._pending_invocations):
                if not future.done():
                    future.set_exception(error)

        if self._part_buffers or self._poisoned_part_buffer_keys:
            logger.info(
                "Closing with %d in-flight partial reassemblies and %d poisoned sequence(s)",
                len(self._part_buffers),
                len(self._poisoned_part_buffer_keys),
            )
            for key, buffer in list(self._part_buffers.items()):
                part_count = next(iter(buffer.values())).part_count if buffer else 0
                logger.warning(
                    "Discarding %d/%d buffered parts for subscription %s seq %d (connection closed)",
                    len(buffer),
                    part_count,
                    key[0],
                    key[1],
                )
        # Cleanup runs unconditionally — clearing empty containers is a no-op,
        # and decoupling it from the logging guard prevents a future edit to the
        # `if` block from accidentally dropping the clears.
        self._part_buffers.clear()
        self._poisoned_part_buffer_keys.clear()

    async def _handle_authentication_failed(self, message: str = "") -> None:
        """Record an ``AuthenticationFailed`` event from the server."""
        logger.error("Authentication failed: %s", message)
        self._authentication_failed = True
        self._auth_message = message
        # Signal connected so __aenter__ can check the flag and raise
        if self._connected is not None:
            self._connected.set()

    async def _handle_data_updated(self, data: Any) -> None:
        """Deserialize a ``DataUpdated`` event and fan out to all matching callbacks.

        Multi-part updates are buffered by ``(backend_sub, sequence_idx)`` and only
        delivered to callbacks once every part has arrived.  Single-part updates
        (``part_count == 1``) bypass buffering entirely.  Malformed metadata is dropped
        with a WARNING before either path can mis-handle it.
        """
        try:
            update = from_dict(data, ServiceDataUpdate)
        except (TypeError, ValueError, KeyError):
            logger.exception("Failed to deserialize DataUpdated payload")
            return
        if update is None:
            logger.warning("Failed to deserialize DataUpdated payload")
            return

        backend_id = update.subscription_id.id

        # 1. Drop updates for unknown subscriptions before any other work.
        # Prevents post-unsubscribe parts from leaking into _part_buffers.
        if backend_id not in self._backend_to_client:
            return

        # 2. Reject malformed chunk metadata.  A non-positive part_count is a backend
        # bug; the fast path and the buffer would both mis-treat zero/negative values.
        if update.part_count < 1:
            logger.warning(
                "Dropping DataUpdated with non-positive part_count %d for "
                "subscription %s seq %d",
                update.part_count, backend_id, update.sequence_idx,
            )
            return

        # 3. Drop any frame — including single-part ones — for a poisoned (sub, seq).
        # Once we've forfeited a sequence we never deliver a callback for it.
        key = (backend_id, update.sequence_idx)
        if key in self._poisoned_part_buffer_keys:
            logger.warning(
                "Dropping DataUpdated (part_idx=%d, part_count=%d) for poisoned "
                "subscription %s seq %d",
                update.part_idx, update.part_count, backend_id, update.sequence_idx,
            )
            return

        # 4. Fast path: unchunked update, no buffering.
        if update.part_count == 1:
            await self._dispatch_data_update(backend_id, update)
            return

        # 5. Multi-part: buffer until all parts arrive.  Multiple sequence_idx values
        # may be buffered concurrently for the same subscription; SignalR does not
        # guarantee that parts of one logical update arrive contiguously across
        # sequence_idx boundaries.
        merged = self._buffer_part(backend_id, update)
        if merged is not None:
            await self._dispatch_data_update(backend_id, merged)

    async def _dispatch_data_update(
        self, backend_id: UUID, update: ServiceDataUpdate,
    ) -> None:
        """Fan out a (possibly merged) ``ServiceDataUpdate`` to every client callback."""
        client_ids = self._backend_to_client.get(backend_id)
        if client_ids is None:
            return
        for client_id in list(client_ids):
            callback = self._client_callbacks.get(client_id)
            if callback is None:
                continue
            try:
                await _invoke_callback(callback, update)
            except Exception:
                logger.exception(
                    "Error in DataUpdated callback for subscription %s (client %s)",
                    backend_id,
                    client_id,
                )

    def _buffer_part(
        self, backend_id: UUID, part: ServiceDataUpdate,
    ) -> Optional[ServiceDataUpdate]:
        """Insert *part* into the reassembly buffer.

        Returns the merged ``ServiceDataUpdate`` when all parts have arrived; otherwise None.
        Validates malformed metadata up-front:

        - Out-of-range ``part_idx`` (``< 0`` or ``>= part_count``) is dropped with a WARNING.
        - A new part whose ``part_count`` disagrees with the buffered ones poisons the
          ``(backend_id, sequence_idx)`` key and discards the buffer.  All later frames for
          the poisoned key are dropped by the caller's pre-check in
          :meth:`_handle_data_updated`.
        - Completeness requires both ``len(buffer) == part_count`` and the buffered keys to
          form exactly ``{0, 1, ..., part_count - 1}``, so indexing during merge is safe.
        """
        if part.part_idx < 0 or part.part_idx >= part.part_count:
            logger.warning(
                "Dropping part with invalid part_idx %d (part_count %d) for "
                "subscription %s seq %d",
                part.part_idx, part.part_count, backend_id, part.sequence_idx,
            )
            return None

        key = (backend_id, part.sequence_idx)
        # The poison check lives in _handle_data_updated (ahead of both the fast path and
        # this method), so by the time we get here the key is guaranteed non-poisoned.

        buffer = self._part_buffers.get(key)

        if buffer is not None and buffer:
            # Validate part_count agrees with what's already buffered.  Any one stored
            # part's part_count is representative because we enforce agreement on insert.
            existing_part_count = next(iter(buffer.values())).part_count
            if existing_part_count != part.part_count:
                logger.warning(
                    "Inconsistent part_count for subscription %s seq %d "
                    "(buffer has part_count=%d, new part has part_count=%d) "
                    "— discarding %d/%d buffered parts and poisoning the sequence; "
                    "all further frames for this (sub, seq) will be dropped",
                    backend_id, part.sequence_idx,
                    existing_part_count, part.part_count,
                    len(buffer), existing_part_count,
                )
                del self._part_buffers[key]
                self._poisoned_part_buffer_keys.add(key)
                return None

        if buffer is None:
            buffer = {}
            self._part_buffers[key] = buffer

        if part.part_idx in buffer:
            logger.warning(
                "Duplicate part_idx %d for subscription %s seq %d — overwriting",
                part.part_idx, backend_id, part.sequence_idx,
            )
        buffer[part.part_idx] = part

        if len(buffer) != part.part_count:
            return None
        if set(buffer) != set(range(part.part_count)):
            # Right count but a gap somewhere — keep waiting.
            return None

        merged = self._merge_parts(buffer, part.part_count)
        del self._part_buffers[key]
        return merged

    @staticmethod
    def _merge_parts(
        buffer: dict[int, ServiceDataUpdate], part_count: int,
    ) -> ServiceDataUpdate:
        """Merge buffered parts into one logical :class:`ServiceDataUpdate`.

        Envelope fields are taken from ``buffer[0]`` (they are identical across parts by
        contract).  For ``modified_entities`` the parts may share entity-type keys (the
        backend can split a long ``created``/``updated``/``deleted`` list across parts), so
        on collision the inner lists are extended rather than overwritten.  The merged
        result is rewritten with ``part_idx=0`` / ``part_count=1`` so the user-visible
        delivery looks like a single un-chunked update.
        """
        parts = [buffer[i] for i in range(part_count)]
        first = parts[0]
        merged_entities: dict[str, EntityServiceDataUpdate] = {}
        for part in parts:
            for type_name, entity_update in part.modified_entities.items():
                existing = merged_entities.get(type_name)
                if existing is None:
                    merged_entities[type_name] = EntityServiceDataUpdate(
                        created=list(entity_update.created),
                        updated=list(entity_update.updated),
                        deleted=list(entity_update.deleted),
                    )
                else:
                    existing.created.extend(entity_update.created)
                    existing.updated.extend(entity_update.updated)
                    existing.deleted.extend(entity_update.deleted)

        return ServiceDataUpdate(
            scenario_id=first.scenario_id,
            status=first.status,
            request_ids=first.request_ids,
            subscription_id=first.subscription_id,
            modified_entities=merged_entities,
            progress=first.progress,
            progress_amount=first.progress_amount,
            sequence_idx=first.sequence_idx,
            part_idx=0,
            part_count=1,
        )

    async def _handle_data_update_rejected(self, data: Any) -> None:
        """Route a ``DataUpdateRejected`` event to the subscription's ``on_error`` callback.

        Sent by the server in place of ``DataUpdated`` when the update's serialised size
        would exceed the limit negotiated via :meth:`_send_client_hello`.  If no
        ``on_error`` was registered for the subscription the rejection is logged so the
        dropped update is at least visible.
        """
        try:
            notification = from_dict(data, DataUpdateRejectedNotification)
        except (TypeError, ValueError, KeyError):
            logger.exception("Failed to deserialize DataUpdateRejected payload")
            return
        if notification is None:
            logger.warning("Failed to deserialize DataUpdateRejected payload")
            return

        backend_id = notification.subscription_id.id
        client_ids = self._backend_to_client.get(backend_id)
        if not client_ids:
            logger.warning(
                "DataUpdateRejected for unknown subscription %s "
                "(%d bytes, limit %d): %s",
                backend_id,
                notification.actual_bytes,
                notification.limit_bytes,
                notification.reason,
            )
            return

        for client_id in list(client_ids):
            on_error = self._client_error_callbacks.get(client_id)
            if on_error is not None:
                try:
                    await _invoke_callback(on_error, notification)
                except Exception:
                    logger.exception(
                        "Error in DataUpdateRejected callback for subscription %s"
                        " (client %s)",
                        backend_id,
                        client_id,
                    )
            else:
                logger.warning(
                    "DataUpdateRejected for subscription %s (client %s) — "
                    "no on_error callback registered "
                    "(%d bytes, limit %d): %s",
                    backend_id,
                    client_id,
                    notification.actual_bytes,
                    notification.limit_bytes,
                    notification.reason,
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
        on_error: Optional[Callback] = None,
    ) -> ValueOrErrorPublic_1[SubscriptionId]:
        """Subscribe to data updates and register a callback.

        The *callback* is called with a :class:`ServiceDataUpdate` argument
        whenever the server sends a ``DataUpdated`` event for this subscription.
        It may be a sync or async callable.

        The optional *on_error* callback is called with a
        :class:`DataUpdateRejectedNotification` when the server drops an
        update for this subscription because it would exceed the
        client's negotiated ``max_size``.  If omitted, rejections are
        logged at WARNING level.

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
            if on_error is not None:
                self._client_error_callbacks[client_id] = on_error
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
        self._client_error_callbacks.pop(client_id, None)

        if backend_id is not None:
            client_ids = self._backend_to_client.get(backend_id)
            if client_ids is not None:
                client_ids.discard(client_id)
                if not client_ids:
                    # Last client for this backend subscription — tell backend
                    del self._backend_to_client[backend_id]
                    # Drop any in-flight partial reassemblies / poisoned
                    # sequences for this backend sub — nothing left to
                    # dispatch them to.
                    stale_buffer_keys = [
                        k for k in self._part_buffers if k[0] == backend_id
                    ]
                    for k in stale_buffer_keys:
                        del self._part_buffers[k]
                    self._poisoned_part_buffer_keys = {
                        k for k in self._poisoned_part_buffer_keys if k[0] != backend_id
                    }
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
        self._client_error_callbacks.clear()
        self._client_to_backend.clear()
        self._part_buffers.clear()
        self._poisoned_part_buffer_keys.clear()
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

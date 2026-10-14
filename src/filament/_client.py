"""Async Filament API client over SignalR."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import inspect
import logging
import math
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from types import TracebackType
from typing import Any, Optional, Type, Union

import httpx
from pysignalr.client import SignalRClient
from pysignalr.exceptions import HubError
from pysignalr.messages import CompletionMessage, Message
from uuid_utils import UUID, uuid7

from ._exceptions import (
    CompressedPropertyError,
    FilamentAuthenticationError,
    FilamentConnectionError,
    FilamentError,
    ResponseTooLargeError,
)
from ._condition_builder import Condition
from ._file_upload import build_upload_request, compute_checksum
from ._serialization import from_dict, to_dict
from .user_defined_properties import (
    UserDefinedProperty,
    build_user_defined_properties,
    hydrate_user_defined_properties,
)
from .types.generated.entities.core import FileEntity, UserDefinedPropertyEntity
from .types.generated.misc import (
    ClientHelloRequest,
    CreateUserDefinedPropertyRequest,
    DataUpdateRejectedNotification,
    EntityPermissions,
    EntityPropertyPermissions,
    EntityServiceDataUpdate,
    FileUploadCompletion,
    FileUploadRequest,
    GetFileUrlsRequest,
    ModifyDataRequest,
    ProcessingStatus,
    ReadConnectedDataRequest,
    ReadDataRequest,
    RequestId,
    ServerCapabilities,
    ServiceDataRead,
    ServiceDataReadConnected,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionId,
    SuccessOrErrorPublic,
    UpdateUserDefinedPropertyRequest,
    UploadGrant,
    UserDefinedPropertyInfo,
    ValueOrErrorPublic_1,
    WrappedEntity,
)
from .types.generated.enums import (
    DataProcessingStatus,
    DataRequestAction,
    ErrorCodes,
    FileChecksumAlgorithm,
    ReadMode,
    SubscriptionMode,
)
from .types.generated.shared import FileReference

logger = logging.getLogger(__name__)

# Type alias for user callbacks — may be sync or async.
Callback = Union[Callable[..., Any], Callable[..., Awaitable[Any]]]

_DEFAULT_SERVER = "https://backend.filament-software.com"
_DEFAULT_PORT = 443
_DEFAULT_PATH = "api/v0"
_DEFAULT_CONNECTION_TIMEOUT = 10.0
# Matches the websockets-library default that pysignalr uses; pass ``None`` for unlimited.
_DEFAULT_MAX_SIZE = 2**20  # 1 MiB
# Backoff, in seconds, before retrying a rejected UDP definition-change ``Subscribe``
# from a read.  Doubles per consecutive rejection up to the maximum.
_UDP_SUBSCRIBE_RETRY_INITIAL = 5.0
_UDP_SUBSCRIBE_RETRY_MAX = 300.0

# Parses the byte counts out of a ``ResponseTooLarge`` error message.  The backend
# formats it as ``"Response would be {actual:N0} bytes; client limit is {limit:N0}
# bytes."`` where ``:N0`` inserts thousands separators (e.g. ``1,234,567``), so the
# captured groups may contain commas which are stripped before ``int`` conversion.
_RESPONSE_TOO_LARGE_RE = re.compile(
    r"would be ([\d,]+) bytes.*?limit is ([\d,]+) bytes"
)


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

        # Capabilities the server advertises during ClientHello (e.g. the file
        # store's checksum algorithm).  Populated on connect; stays None against
        # older servers that don't return them.
        self._server_capabilities: Optional[ServerCapabilities] = None

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

        self._user_defined_properties: dict[str, Mapping[str, UserDefinedProperty]] = {}
        self._user_defined_property_tasks: dict[
            str, asyncio.Task[Mapping[str, UserDefinedProperty]]
        ] = {}
        self._all_user_defined_property_tasks: set[
            asyncio.Task[Mapping[str, UserDefinedProperty]]
        ] = set()
        self._user_defined_property_generations: dict[str, int] = {}
        self._user_defined_subscription_id: Optional[UUID] = None
        self._user_defined_subscription_types: frozenset[str] = frozenset()
        self._user_defined_subscription_lock = asyncio.Lock()
        self._user_defined_subscription_failures = 0
        self._user_defined_subscription_retry_at = 0.0

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
        # Complete updates awaiting dispatch, drained in order by one worker task.
        # Dispatch can await hub invocations (UDP discovery and hydration), whose
        # completions arrive on the same receive loop that runs the DataUpdated
        # handler, so it must never run inside the handler itself.  Each entry
        # carries the client subscriptions that existed when the update arrived,
        # and whether it arrived on the private UDP-definition subscription.
        self._data_update_queue: deque[
            tuple[UUID, ServiceDataUpdate, frozenset[UUID], bool]
        ] = deque()
        self._data_update_worker: Optional[asyncio.Task[None]] = None

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
            # Do NOT lower this to make dropped connections surface sooner. It is the
            # *websockets* ping interval, and websockets enforces its own pong timeout on
            # the same schedule: on a loop that is starved (a long synchronous pass on a
            # shared loop), the pong would arrive late and websockets would tear the
            # connection down from a second direction, earlier than the server would have.
            # Keeping the transport off a busy loop is what actually fixes starvation;
            # this number only decides how loudly it fails.
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
        # The websocket connection object from the most recent open, kept only so a close
        # can be *named* — see _close_detail.  pysignalr clears its own reference before
        # firing on_close, so this is the only place the close code survives.
        self._last_connection: Any = None
        # Bumped on every connection-ending event (close callback, run loop ending).
        # pysignalr only fires on_close on a state change, so opens and closes
        # alternate; _invoke compares this across send() to tell whether the socket
        # its request was written to has since gone.
        self._close_count = 0
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
        self._run_task.add_done_callback(self._handle_run_task_done)

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
        """Close the SignalR connection.

        A transport already broken at close time can surface its own error from the
        cancelled run task (e.g. pysignalr's ``ConnectionError``). Closing must never
        re-raise it: the caller is done with the connection, and pysignalr's exception
        classes are frozen dataclasses that Python cannot even re-raise through a
        ``@contextmanager`` (assigning ``__traceback__`` hits the frozen guard). Log
        and swallow everything except the cancellation we requested.
        """
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("SignalR run loop ended with an error at close", exc_info=True)
        self._data_update_queue.clear()
        worker = self._data_update_worker
        self._data_update_worker = None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._clear_user_defined_property_state(cancel_tasks=True)

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
                    # Name the method: the server's error text alone ("Sequence contains no
                    # elements") says nothing about which invocation produced it. The class
                    # stays a bare ``Exception`` on purpose — ``_send_client_hello``
                    # distinguishes it from ``FilamentError`` to tolerate older servers.
                    future.set_exception(Exception(f"{method}: {message.error}"))
                else:
                    future.set_result(message.result)

        close_count = self._close_count
        try:
            await self._signalr_client.send(method, list(args), on_invocation=on_completion)
        except (HubError, OSError, RuntimeError) as exc:
            # Translate transport failures to this package's exception family. Besides
            # giving callers one family to catch, this is load-bearing: pysignalr's
            # exception classes are frozen dataclasses, and Python cannot re-raise a
            # frozen exception through a ``@contextmanager`` (assigning __traceback__
            # hits the frozen guard and masks the real error with FrozenInstanceError).
            # RuntimeError is in the tuple because pysignalr's transport raises a bare
            # ``RuntimeError('Connection is closed')`` for a send on a dead connection —
            # left untranslated it slips past callers that reconnect on
            # FilamentConnectionError, so one dropped websocket turns into an endless
            # string of failed operations that look like generic, retryable errors.
            if not future.done():
                future.cancel()
            raise FilamentConnectionError(
                f"SignalR send failed: {exc} ({self._close_detail()})"
            ) from exc
        except BaseException:
            if not future.done():
                future.cancel()
            raise

        # Register only once send() has returned, i.e. the invocation is on a socket.
        # pysignalr's send() waits for a connection while reconnecting and wakes just
        # before on_open fires; registering earlier would let _on_open fail a future
        # whose request then goes out on the new socket, so the caller would see an
        # error for a write the server received (and a retry would duplicate it).
        #
        # The flip side: a close can land *during* send() (e.g. while it waits on
        # flow control) and send() may still return normally.  The close handler has
        # then already failed the pending set, so registering would leave the caller
        # waiting forever.  A close during send() always means the socket the request
        # went out on is gone: a send started while connected writes to that socket,
        # and a send queued while disconnected starts after the preceding close was
        # counted, so the next close is the one ending the socket it was written to.
        # There is no await between this check and registering, so no close can slip
        # in between.
        if not future.done() and self._close_count != close_count:
            future.cancel()
            raise FilamentConnectionError(
                "SignalR connection closed before the server response arrived "
                f"({self._close_detail()})"
            )
        self._pending_invocations.add(future)
        future.add_done_callback(self._pending_invocations.discard)
        return await future

    async def _send_client_hello(self) -> None:
        """Advertise the negotiated maximum SignalR message size to the server.

        Sent once per connection.  Older backends that don't implement the
        ``ClientHello`` hub method return an invocation error; we log it and
        continue, since the client-side ``max_size`` cap still applies.

        The invocation is bounded by ``connection_timeout``: if the server
        accepts the socket but never sends a completion for ``ClientHello``,
        we fail the connect with :class:`FilamentConnectionError` rather than
        hang ``__aenter__`` indefinitely.
        """
        # Drop anything the previous connection advertised: every path out of
        # this method below the invocation returns without setting them, so a
        # reused client would otherwise carry stale capabilities (and a stale
        # checksum algorithm) into a handshake that was rejected or unanswered.
        self._server_capabilities = None
        request = ClientHelloRequest(max_message_size_bytes=self._max_size)
        try:
            result_dict: Any = await asyncio.wait_for(
                self._invoke("ClientHello", to_dict(request)),
                timeout=self._connection_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise FilamentConnectionError(
                f"ClientHello did not complete within {self._connection_timeout}s"
            ) from exc
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
            result = from_dict(result_dict, ValueOrErrorPublic_1[ServerCapabilities])
        except (AttributeError, TypeError, ValueError, KeyError):
            logger.warning(
                "Failed to deserialize ClientHello response — continuing.",
            )
            return
        if result is None or not result.success:
            logger.warning(
                "Server rejected ClientHello: %s",
                result.message if result is not None else "no result",
            )
            return

        # Capture advertised capabilities (e.g. the file store's checksum
        # algorithm) so upload_file and callers can consult them without another
        # round trip.  ``data`` may be None on older servers that accept
        # ClientHello but return no capabilities.
        self._server_capabilities = result.data

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

    def _fail_pending_invocations(self, reason: str) -> None:
        """Fail every in-flight :meth:`_invoke` future with :class:`FilamentConnectionError`.

        An invocation cannot survive its connection: the server ties completions to
        the socket they were requested on, so once that socket is gone the reply can
        never arrive and the awaiting caller would hang forever.  Only invocations
        already written to a socket are pending; one still waiting in ``send()`` for a
        connection is untouched and goes out on whichever socket comes up.  Every connection
        lifecycle edge that ends a socket's usefulness must call this — the close
        callback, a successful *re*-connect (new socket, old invocations orphaned),
        and the transport run loop ending without pysignalr firing ``on_close``.
        """
        if not self._pending_invocations:
            return
        error = FilamentConnectionError(reason)
        for future in list(self._pending_invocations):
            if not future.done():
                future.set_exception(error)

    def _handle_run_task_done(self, task: asyncio.Task[None]) -> None:
        """Fail in-flight invocations when the transport's run loop ends.

        pysignalr's ``run()`` can die with an exception (or be cancelled) without the
        ``on_close`` callback ever firing — in that state no completion, close, or
        reconnect event will ever arrive, and an awaiting :meth:`_invoke` hangs
        indefinitely.  Retrieving the task's exception here also keeps asyncio from
        logging "Task exception was never retrieved" at teardown.
        """
        self._close_count += 1
        if task.cancelled():
            detail = "the connection was closed"
        else:
            exc = task.exception()
            detail = f"the transport loop ended: {exc}" if exc else "the transport loop ended"
        self._fail_pending_invocations(
            f"SignalR connection lost before the server response arrived ({detail})"
        )
        # As in _handle_close: nothing will reconnect, so a queued update needing
        # UDP hydration would wait on a hub reply that can never arrive.
        if self._data_update_queue:
            logger.warning(
                "Discarding %d queued DataUpdated event(s) (%s)",
                len(self._data_update_queue),
                detail,
            )
            self._data_update_queue.clear()

    async def _on_open(self) -> None:
        """Called when the SignalR connection is established (first connect and reconnects)."""
        # Stash the live websocket while it is still reachable.  pysignalr sets its
        # transport's ``_ws`` back to None *before* firing the close callback, so by
        # the time _handle_close runs there is nothing left to ask for the close code.
        # getattr-guarded because this reaches into pysignalr internals: a future
        # version that renames the attribute must degrade to "close code unavailable",
        # never break the connection lifecycle.
        self._last_connection = getattr(
            getattr(self._signalr_client, "_transport", None), "_ws", None
        )

        # A successful *re*-connect strands any invocation sent on the previous
        # socket — the new connection knows nothing of it, so its completion will
        # never arrive.  Fail those callers now rather than letting them hang.
        # On the first open the pending set is empty and this is a no-op.
        self._fail_pending_invocations(
            "SignalR connection was re-established before the server response arrived"
        )

        # Brief yield to let immediate server messages (e.g.
        # AuthenticationFailed) arrive before we signal ready.
        await asyncio.sleep(0.1)
        if self._connected is not None:
            self._connected.set()

    async def _handle_signalr_error(self, message: CompletionMessage) -> None:
        """Handle invocation errors from the server."""
        logger.error("SignalR invocation error: %s", message.error)

    def _close_detail(self) -> str:
        """Describe the websocket close that just happened, for diagnostics.

        The close code is the single most diagnostic fact about a dropped Filament
        connection: 1006 (no close frame) is the keep-alive-starvation signature, 1009 is
        the client's own frame cap, 1011 is a server error and 1000 an orderly shutdown.
        Without it every drop reads the same in the logs.

        Every lookup is guarded and the failure mode is a fixed string: this runs on the
        failure path, where an AttributeError would mask the failure it is describing.
        """
        connection = getattr(self, "_last_connection", None)
        code = getattr(connection, "close_code", None) if connection is not None else None
        if code is None:
            return "close code unavailable"
        reason = getattr(connection, "close_reason", None)
        return f"close {code} ({reason})" if reason else f"close {code}"

    async def _handle_close(self) -> None:
        """Tidy up in-flight state when the SignalR connection drops.

        - Fails any awaiting :meth:`_invoke` futures with
          :class:`FilamentConnectionError`, so callers see an exception
          instead of hanging when the websocket closes (e.g. close code
          1009 — message too big).  The close code travels in the failure
          message (see :meth:`_close_detail`) so the caller's log names
          *why* the socket went, not just that it did.
        - Discards any in-progress multi-part :class:`ServiceDataUpdate`
          reassemblies and any poisoned-sequence markers — the server has
          no idea what we'd buffered and cannot resume across a reconnect.
        - Drops complete updates still queued for dispatch, for the same reason.
          An update already being dispatched finishes (or fails) on its own.
        """
        self._close_count += 1
        detail = self._close_detail()
        logger.warning("SignalR connection closed (%s)", detail)
        self._fail_pending_invocations(
            f"SignalR connection closed before the server response arrived ({detail})"
        )

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
        if self._data_update_queue:
            logger.warning(
                "Discarding %d queued DataUpdated event(s) (connection closed)",
                len(self._data_update_queue),
            )
            self._data_update_queue.clear()
        self._clear_user_defined_property_state()

    def _clear_user_defined_property_state(self, *, cancel_tasks: bool = False) -> None:
        """Discard UDP metadata and private-subscription state without I/O.

        In-flight loaders are cancelled only when *cancel_tasks* is set (shutdown).
        On a connection close their ``_invoke`` already fails with
        :class:`FilamentConnectionError`; cancelling them as well would race that
        error and hand their awaiting callers a ``CancelledError`` instead.
        """
        if cancel_tasks:
            for task in self._all_user_defined_property_tasks:
                task.cancel()
            self._all_user_defined_property_tasks.clear()
        # Bump rather than reset the generations: a loader left running whose reply
        # landed just before the close must not publish into the cleared cache.
        for name in set(self._user_defined_property_generations) | set(
            self._user_defined_property_tasks
        ):
            self._user_defined_property_generations[name] = (
                self._user_defined_property_generations.get(name, 0) + 1
            )
        self._user_defined_property_tasks.clear()
        self._user_defined_properties.clear()
        self._user_defined_subscription_id = None
        self._user_defined_subscription_types = frozenset()
        self._user_defined_subscription_failures = 0
        self._user_defined_subscription_retry_at = 0.0

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
        except (AttributeError, TypeError, ValueError, KeyError):
            logger.exception("Failed to deserialize DataUpdated payload")
            return
        if update is None:
            logger.warning("Failed to deserialize DataUpdated payload")
            return

        backend_id = update.subscription_id.id

        # 1. Drop updates for unknown subscriptions before any other work.
        # Prevents post-unsubscribe parts from leaking into _part_buffers.
        if (
            backend_id not in self._backend_to_client
            and backend_id != self._user_defined_subscription_id
        ):
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
            self._enqueue_data_update(backend_id, update)
            return

        # 5. Multi-part: buffer until all parts arrive.  Multiple sequence_idx values
        # may be buffered concurrently for the same subscription; SignalR does not
        # guarantee that parts of one logical update arrive contiguously across
        # sequence_idx boundaries.
        merged = self._buffer_part(backend_id, update)
        if merged is not None:
            self._enqueue_data_update(backend_id, merged)

    def _enqueue_data_update(self, backend_id: UUID, update: ServiceDataUpdate) -> None:
        """Queue a complete update for dispatch, starting the worker if it is idle.

        pysignalr awaits each handler before reading the next frame, so awaiting a
        hub invocation inside the handler would wait for a completion that can never
        be read.  One worker drains the queue, which keeps delivery in arrival order.

        The recipients are fixed now, as inline dispatch would have fixed them: a
        client that subscribes while the update waits must not receive it.  So is
        whether this is a UDP definition push: reconciliation may replace the private
        subscription ID before the worker reaches it.
        """
        client_ids = frozenset(self._backend_to_client.get(backend_id, ()))
        from_udp = backend_id == self._user_defined_subscription_id
        self._data_update_queue.append((backend_id, update, client_ids, from_udp))
        if self._data_update_worker is None or self._data_update_worker.done():
            self._data_update_worker = asyncio.create_task(self._drain_data_updates())

    async def _drain_data_updates(self) -> None:
        """Dispatch queued updates in order; exit once the queue is empty."""
        while self._data_update_queue:
            backend_id, update, client_ids, from_udp = self._data_update_queue.popleft()
            try:
                await self._dispatch_data_update(
                    backend_id, update, client_ids,
                    from_user_defined_subscription=from_udp,
                )
            except Exception:
                # E.g. FilamentConnectionError from UDP discovery when the connection
                # drops mid-dispatch.  Losing one update must not stop the worker.
                logger.exception(
                    "Failed to dispatch DataUpdated for subscription %s", backend_id,
                )

    async def _dispatch_data_update(
        self,
        backend_id: UUID,
        update: ServiceDataUpdate,
        client_ids: frozenset[UUID],
        *,
        from_user_defined_subscription: bool,
    ) -> None:
        """Fan out a (possibly merged) ``ServiceDataUpdate`` to its client callbacks.

        *client_ids* are the subscriptions that existed when the update arrived; any
        that have since been unsubscribed are skipped.
        *from_user_defined_subscription* marks a UDP definition push, which refreshes
        the cached registries before any client callbacks run.
        """
        if from_user_defined_subscription:
            await self._handle_user_defined_property_update(update)
            # The backend returns the same subscription ID for duplicate
            # type/permission combinations, so a caller who subscribes to
            # UserDefinedPropertyEntity with a matching config shares this ID.
            # Keep dispatching so their callback still runs.
            if not client_ids:
                return
        if not await self._hydrate_data_update(update):
            logger.error(
                "Dropping DataUpdated for subscription %s because UDP hydration failed",
                backend_id,
            )
            return
        for client_id in client_ids:
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

    async def _hydrate_entities_for_type(
        self, entity_type_name: str, entities: list[Any],
    ) -> bool:
        """Hydrate a group of entities, refreshing once for unknown UDP names."""
        try:
            properties = await self._ensure_user_defined_properties(entity_type_name)
            unknown: set[str] = set()
            for entity in entities:
                if entity is not None:
                    unknown.update(
                        hydrate_user_defined_properties(entity, properties)
                    )
            if not unknown:
                return True

            self._evict_user_defined_properties({entity_type_name})
            properties = await self._ensure_user_defined_properties(entity_type_name)
            still_unknown: set[str] = set()
            for entity in entities:
                if entity is not None:
                    still_unknown.update(
                        hydrate_user_defined_properties(entity, properties)
                    )
            if still_unknown:
                logger.error(
                    "Backend returned unknown UDP properties for %s after refresh: %s",
                    entity_type_name,
                    ", ".join(sorted(still_unknown)),
                )
                return False
            return True
        except (TypeError, ValueError, _UserDefinedPropertyLoadError):
            logger.exception(
                "Could not hydrate UDP properties for %s", entity_type_name
            )
            return False

    async def _hydrate_data_update(self, update: ServiceDataUpdate) -> bool:
        for entity_type_name, changes in update.modified_entities.items():
            entities: list[Any] = list(changes.created) + list(changes.deleted)
            entities.extend(item.entity for item in changes.updated if item.entity is not None)
            if not await self._hydrate_entities_for_type(entity_type_name, entities):
                return False
        return True

    async def _handle_user_defined_property_update(
        self, update: ServiceDataUpdate,
    ) -> None:
        """Refresh cached type registries affected by a definition push."""
        affected: set[str] = set()
        for type_name, changes in update.modified_entities.items():
            if type_name.rsplit(".", 1)[-1] != "UserDefinedPropertyEntity":
                continue
            entities: list[Any] = list(changes.created) + list(changes.deleted)
            entities.extend(item.entity for item in changes.updated if item.entity is not None)
            for entity in entities:
                if isinstance(entity, UserDefinedPropertyEntity) and entity.entity_type_name:
                    affected.add(entity.entity_type_name)
        cached = affected.intersection(self._user_defined_properties)
        if cached:
            cached_names = sorted(cached)
            self._evict_user_defined_properties(cached)
            results = await asyncio.gather(
                *(
                    self._ensure_user_defined_properties(name, reconcile=False)
                    for name in cached_names
                ),
                return_exceptions=True,
            )
            for name, result in zip(cached_names, results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(
                        "Could not refresh UDP definitions for %s: %s", name, result,
                    )
            await self._reconcile_user_defined_property_subscription_or_warn(
                ", ".join(cached_names),
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
        except (AttributeError, TypeError, ValueError, KeyError):
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
        except (AttributeError, TypeError, ValueError, KeyError):
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
        except (AttributeError, TypeError, ValueError, KeyError):
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
        except (AttributeError, TypeError, ValueError, KeyError):
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
    # User-defined property metadata
    # ------------------------------------------------------------------

    async def _get_entity_property_permissions_uncached(
        self, entity_type_name: str,
    ) -> ValueOrErrorPublic_1[EntityPropertyPermissions]:
        result_dict: Any = await self._invoke(
            "GetEntityPropertyPermissions", entity_type_name
        )
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[EntityPropertyPermissions])
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        return result if result is not None else _deserialization_error()

    async def _load_user_defined_properties(
        self, entity_type_name: str, generation: int,
    ) -> Mapping[str, UserDefinedProperty]:
        permissions = await self._get_entity_property_permissions_uncached(entity_type_name)
        if not permissions.success or permissions.data is None:
            raise _UserDefinedPropertyLoadError(
                permissions.message or f"Could not load permissions for {entity_type_name}",
                permissions.error_code,
            )
        try:
            properties = build_user_defined_properties(
                entity_type_name, permissions.data
            )
        except (TypeError, ValueError) as exc:
            raise _UserDefinedPropertyLoadError(
                f"Invalid UDP definitions for {entity_type_name}: {exc}",
                ErrorCodes.InternalError,
            ) from exc
        if self._user_defined_property_generations.get(entity_type_name, 0) == generation:
            self._user_defined_properties[entity_type_name] = properties
        return properties

    async def _ensure_user_defined_properties(
        self, entity_type_name: str, *, reconcile: bool = True,
    ) -> Mapping[str, UserDefinedProperty]:
        """Load and cache one type's UDP definitions.

        Pass ``reconcile=False`` when loading a batch of types: each reconcile
        issues its own ``Subscribe`` plus an ``Unsubscribe`` for the superseded
        ID, so reconciling per type costs 2N hub calls and leaves every
        intermediate subscription covering only part of the desired set.  The
        caller is then responsible for reconciling once after the batch.
        """
        cached = self._user_defined_properties.get(entity_type_name)
        if cached is not None:
            if reconcile and (
                entity_type_name not in self._user_defined_subscription_types
                or self._user_defined_subscription_id is None
            ):
                await self._reconcile_user_defined_property_subscription_or_warn(
                    entity_type_name,
                )
            return cached
        task = self._user_defined_property_tasks.get(entity_type_name)
        if task is None:
            generation = self._user_defined_property_generations.get(entity_type_name, 0)
            task = asyncio.create_task(
                self._load_user_defined_properties(entity_type_name, generation)
            )
            self._user_defined_property_tasks[entity_type_name] = task
            self._all_user_defined_property_tasks.add(task)
            task.add_done_callback(
                functools.partial(self._forget_user_defined_property_task, entity_type_name)
            )
        try:
            # The loader is shared by every caller for this type, so one caller being
            # cancelled must not cancel it for the others.  wait() neither cancels the
            # task nor raises its outcome, so a CancelledError here is always this
            # caller's own cancellation and propagates as-is.
            await asyncio.wait({task})
            if task.cancelled():
                # Only client shutdown cancels a loader.  Its callers must see the
                # dead connection, not a CancelledError they did not ask for.
                raise FilamentConnectionError(
                    f"Client closed while loading UDP definitions for {entity_type_name}"
                )
            task.result()
        finally:
            # A cancelled waiter leaves the loader running for the other callers, so
            # only a finished loader is deregistered here.  The done callback also
            # deregisters it, but runs later than this waiter can retry.
            if task.done() and self._user_defined_property_tasks.get(entity_type_name) is task:
                del self._user_defined_property_tasks[entity_type_name]
        cached = self._user_defined_properties.get(entity_type_name)
        if cached is None:
            # The load was invalidated while in flight; retry under the new generation.
            return await self._ensure_user_defined_properties(
                entity_type_name, reconcile=reconcile,
            )
        if reconcile:
            await self._reconcile_user_defined_property_subscription_or_warn(
                entity_type_name,
            )
        return cached

    async def _reconcile_user_defined_property_subscription_or_warn(
        self, entity_type_names: str,
    ) -> None:
        """Reconcile, keeping loaded definitions usable if the subscribe fails.

        A failed reconcile leaves the subscription state untouched, so the type
        stays uncovered and a later ``_ensure_user_defined_properties`` call
        retries it once the rejection backoff has elapsed.  *entity_type_names*
        only labels the warning.
        """
        try:
            await self._reconcile_user_defined_property_subscription(
                respect_backoff=True,
            )
        except _UserDefinedPropertyLoadError as exc:
            logger.warning(
                "Could not subscribe to UDP definition changes for %s: %s",
                entity_type_names,
                exc.message,
            )

    def _forget_user_defined_property_task(
        self,
        entity_type_name: str,
        task: asyncio.Task[Mapping[str, UserDefinedProperty]],
    ) -> None:
        self._all_user_defined_property_tasks.discard(task)
        # Identity check: an eviction may already have registered a replacement.
        if self._user_defined_property_tasks.get(entity_type_name) is task:
            del self._user_defined_property_tasks[entity_type_name]
        # Every waiter re-raises the loader's failure itself; retrieving it here
        # keeps a loader whose waiters were all cancelled from logging "Task
        # exception was never retrieved".
        if not task.cancelled():
            task.exception()

    async def _reconcile_user_defined_property_subscription(
        self, *, respect_backoff: bool = False,
    ) -> None:
        """Subscribe privately to definition changes for every cached type.

        With *respect_backoff*, skip the attempt while a previous rejection's
        backoff is still running, so repeated or concurrent reads don't each
        re-issue a ``Subscribe`` the backend keeps rejecting.
        """
        async with self._user_defined_subscription_lock:
            # Include active refreshes so a temporarily evicted map does not
            # shrink the still-needed maintenance subscription mid-refresh.
            desired = frozenset(
                set(self._user_defined_properties) | set(self._user_defined_property_tasks)
            )
            old_id = self._user_defined_subscription_id
            if desired == self._user_defined_subscription_types and (
                old_id is not None or not desired
            ):
                return
            if (
                respect_backoff
                and asyncio.get_running_loop().time()
                < self._user_defined_subscription_retry_at
            ):
                return

            new_id: Optional[UUID] = None
            if desired:
                config = SubscriptionConfig(
                    scenario_id=UUID("00000000-0000-0000-0000-000000000000"),
                    status=DataProcessingStatus.All,
                    type_name="UserDefinedPropertyEntity",
                    condition=Condition(UserDefinedPropertyEntity).prop(
                        "entity_type_name"
                    ).in_(sorted(desired)),
                    mode=SubscriptionMode.Data,
                )
                response_dict: Any = await self._invoke("Subscribe", to_dict(config))
                response: Optional[ValueOrErrorPublic_1[SubscriptionId]]
                try:
                    response = from_dict(
                        response_dict, ValueOrErrorPublic_1[SubscriptionId]
                    )
                except (AttributeError, TypeError, ValueError, KeyError):
                    response = None
                if response is None or not response.success or response.data is None:
                    self._user_defined_subscription_failures += 1
                    self._user_defined_subscription_retry_at = (
                        asyncio.get_running_loop().time()
                        + min(
                            _UDP_SUBSCRIBE_RETRY_INITIAL
                            # Capped so a long run of rejections can't overflow the float.
                            * 2 ** min(self._user_defined_subscription_failures - 1, 16),
                            _UDP_SUBSCRIBE_RETRY_MAX,
                        )
                    )
                    raise _UserDefinedPropertyLoadError(
                        response.message if response is not None else (
                            "Invalid response while subscribing to UDP definition changes"
                        ),
                        response.error_code if response is not None else (
                            ErrorCodes.InternalError
                        ),
                    )
                new_id = response.data.id

            self._user_defined_subscription_id = new_id
            self._user_defined_subscription_types = desired
            self._user_defined_subscription_failures = 0
            self._user_defined_subscription_retry_at = 0.0
            # A user subscription may share the superseded backend ID (the backend
            # dedupes identical subscriptions); unsubscribe() sends the backend
            # Unsubscribe once its last client is removed, so leave it alone here.
            # Known gap: subscribe() doesn't hold _user_defined_subscription_lock and
            # only registers in _backend_to_client after its Subscribe returns, so a
            # concurrent identical user subscribe can still be deduped onto old_id
            # and then cancelled by the Unsubscribe below.
            if (
                old_id is not None
                and old_id != new_id
                and old_id not in self._backend_to_client
            ):
                response_dict = await self._invoke(
                    "Unsubscribe", to_dict(SubscriptionId(id=old_id)),
                )
                unsubscribe_response = from_dict(
                    response_dict, SuccessOrErrorPublic
                )
                if unsubscribe_response is None or not unsubscribe_response.success:
                    logger.warning(
                        "Could not remove superseded UDP definition subscription %s",
                        old_id,
                    )

    async def invalidate_user_defined_property_cache(
        self, entity_type_name: Optional[str] = None,
    ) -> None:
        """Invalidate one type's cached UDP definitions, or every cached type."""
        names = (
            {entity_type_name}
            if entity_type_name is not None
            else set(self._user_defined_properties) | set(self._user_defined_property_tasks)
        )
        self._evict_user_defined_properties(names)
        await self._reconcile_user_defined_property_subscription()

    def _evict_user_defined_properties(self, names: set[str]) -> None:
        """Evict maps without changing the still-covering private subscription."""
        for name in names:
            self._user_defined_property_generations[name] = (
                self._user_defined_property_generations.get(name, 0) + 1
            )
            self._user_defined_properties.pop(name, None)
            # Detach rather than cancel: current callers may still be awaiting
            # the old generation. Its loader is forbidden from publishing and
            # those callers will transparently retry under the new generation.
            self._user_defined_property_tasks.pop(name, None)

    async def get_user_defined_properties(
        self, entity_type_name: str,
    ) -> ValueOrErrorPublic_1[Mapping[str, UserDefinedProperty]]:
        """Return the cached/discovered UDP properties for an entity type."""
        try:
            properties = await self._ensure_user_defined_properties(entity_type_name)
        except _UserDefinedPropertyLoadError as exc:
            return _value_error(exc.message, exc.error_code)
        return ValueOrErrorPublic_1(
            success=True,
            message="",
            error_code=ErrorCodes.None_,
            error_id=None,
            data=properties,
        )

    # Properties
    # ------------------------------------------------------------------

    @property
    def server_capabilities(self) -> Optional[ServerCapabilities]:
        """Capabilities the server advertised during the ``ClientHello`` handshake.

        ``None`` until the connection is established, or when connected to an
        older server that returns no capabilities.  ``server_capabilities.files``
        (when present) carries the file store's checksum algorithm and
        server-side-checksum flag, which :meth:`upload_file` consults to decide
        whether and how to checksum before requesting an upload grant.
        """
        return self._server_capabilities

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
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def get_entity_property_permissions(
        self,
        entity_type_name: str,
    ) -> ValueOrErrorPublic_1[EntityPropertyPermissions]:
        """Retrieve permissions and refresh this type's UDP metadata cache."""
        result = await self._get_entity_property_permissions_uncached(entity_type_name)
        if result.success and result.data is not None:
            self._user_defined_property_generations[entity_type_name] = (
                self._user_defined_property_generations.get(entity_type_name, 0) + 1
            )
            try:
                self._user_defined_properties[entity_type_name] = (
                    build_user_defined_properties(entity_type_name, result.data)
                )
            except (TypeError, ValueError) as exc:
                return _value_error(
                    f"Invalid UDP definitions for {entity_type_name}: {exc}"
                )
            await self._reconcile_user_defined_property_subscription_or_warn(
                entity_type_name,
            )
        return result

    async def read_data(
        self,
        request: ReadDataRequest,
    ) -> ValueOrErrorPublic_1[ServiceDataRead]:
        """Read entity data from the server.

        If the server rejects the response as too large
        (:attr:`ErrorCodes.ResponseTooLarge` — returned when the serialised
        result would exceed the ``max_size`` negotiated at connect time),
        the request is automatically split into smaller pages.  The number
        of pages is derived from the byte counts reported in the error
        message, and each page is read recursively so that a page which is
        *itself* too large (because the data is not spread evenly across the
        entities) is split again.  The pages are then merged into a single
        :class:`ServiceDataRead`.

        Recursion stops when a request for a single entity is still too
        large: in that case no further requests are made and a
        :class:`ResponseTooLargeError` is raised, carrying the size of the
        offending single-entity response and the size the full request would
        have produced.
        """
        entity_type_name = request.request.type_name
        try:
            await self._ensure_user_defined_properties(entity_type_name)
        except _UserDefinedPropertyLoadError as exc:
            return _value_error(exc.message, exc.error_code)
        result = await self._read_data_paged(request)
        if result.success and result.data is not None:
            entities = [wrapped.entity for wrapped in result.data.data]
            if not await self._hydrate_entities_for_type(entity_type_name, entities):
                return _value_error(
                    f"Could not hydrate user-defined properties for {entity_type_name}"
                )
        return result

    async def _read_data_once(
        self,
        request: ReadDataRequest,
    ) -> ValueOrErrorPublic_1[ServiceDataRead]:
        """Issue a single ``ReadData`` invocation without any paging."""
        request_dict = to_dict(request)
        result_dict: Any = await self._invoke("ReadData", request_dict)
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[ServiceDataRead])
        except CompressedPropertyError as exc:
            # Name the field and header rather than the opaque generic error.
            return _value_error(str(exc), ErrorCodes.SerializationError)
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def _read_data_paged(
        self,
        request: ReadDataRequest,
        full_request_bytes: Optional[int] = None,
    ) -> ValueOrErrorPublic_1[ServiceDataRead]:
        """Read data, splitting into smaller pages on ``ResponseTooLarge``.

        See :meth:`read_data` for the user-facing contract.

        :param full_request_bytes: Serialised size the original (top-level)
            request would have produced.  Threaded through the recursion so a
            single-entity failure can report it; ``None`` at the top level,
            where it is taken from the first too-large response.
        """
        result = await self._read_data_once(request)

        # Only a too-large response triggers paging; everything else (success
        # or any other error) is returned to the caller untouched.
        if result.success or result.error_code != ErrorCodes.ResponseTooLarge:
            return result

        match = _RESPONSE_TOO_LARGE_RE.search(result.message)
        if match is None:
            # The error code says too large but the message isn't in the
            # expected format, so we can't size the pages — surface as-is.
            return result
        would_be = int(match.group(1).replace(",", ""))
        limit_bytes = int(match.group(2).replace(",", ""))
        # The first too-large response we see is the full, un-paged request.
        if full_request_bytes is None:
            full_request_bytes = would_be
        if limit_bytes <= 0:
            return result

        type_request = request.request

        # Resolve an unlimited request (``limit <= 0`` means "all matching
        # records") to a concrete entity count so the range can be divided
        # into pages.  Only meaningful for a plain data read.
        limit = type_request.limit
        if limit <= 0:
            if type_request.mode != ReadMode.Data:
                return result
            count = await self._read_entity_count(request)
            if count is None:
                return result
            limit = count - type_request.offset

        if limit <= 0:
            # No records remain in range — e.g. the count probe on an unlimited
            # read found ``offset >= entity_count`` and the response is all
            # envelope overhead.  There is no single entity to blame, so surface
            # the original error rather than misreporting an overflowing entity.
            return result

        if limit == 1:
            # A single entity is still too large — nothing left to split, so
            # stop making requests and report the failure.
            raise ResponseTooLargeError(
                entity_bytes=would_be,
                full_request_bytes=full_request_bytes,
                limit_bytes=limit_bytes,
            )

        # Split the [offset, offset + limit) range into ``num_pages`` pages,
        # sized so each page is expected to fit within the byte limit.  Always
        # at least two pages so the per-page limit is strictly smaller than the
        # current one, guaranteeing the recursion makes progress towards the
        # single-entity base case.
        num_pages = max(2, math.ceil(would_be / limit_bytes))
        page_size = max(1, math.ceil(limit / num_pages))

        accumulated: list[WrappedEntity] = []
        template: Optional[ServiceDataRead] = None
        # Pending-request maps are keyed by entity ID, so each page contributes
        # its own entries — merge them rather than keeping only the first page's.
        pending_update_remove: dict[UUID, RequestId] = {}
        pending_delete: dict[UUID, RequestId] = {}
        saw_pending_update_remove = False
        saw_pending_delete = False
        offset = type_request.offset
        end = type_request.offset + limit
        more_data = False

        while offset < end:
            page_limit = min(page_size, end - offset)
            page_request = replace(
                request,
                request=replace(type_request, offset=offset, limit=page_limit),
            )
            # A single-entity page that is still too large raises
            # ResponseTooLargeError, which propagates out without reading any
            # further pages.
            page_result = await self._read_data_paged(page_request, full_request_bytes)
            if not page_result.success or page_result.data is None:
                # Some other error (e.g. a deserialization failure) — surface it.
                return page_result
            page_data = page_result.data
            if template is None:
                template = page_data
            accumulated.extend(page_data.data)
            if page_data.pending_update_remove_request_ids is not None:
                pending_update_remove.update(page_data.pending_update_remove_request_ids)
                saw_pending_update_remove = True
            if page_data.pending_delete_request_ids is not None:
                pending_delete.update(page_data.pending_delete_request_ids)
                saw_pending_delete = True
            more_data = page_data.more_data
            offset += page_limit
            if not page_data.more_data:
                # No further records available within the requested range.
                break

        if template is None:  # pragma: no cover - unreachable while limit > 1
            return result

        merged = ServiceDataRead(
            type_name=template.type_name,
            permissions=template.permissions,
            data=accumulated,
            pending_update_remove_request_ids=(
                pending_update_remove if saw_pending_update_remove else None
            ),
            pending_delete_request_ids=(
                pending_delete if saw_pending_delete else None
            ),
            entity_count=template.entity_count,
            more_data=more_data,
        )
        return ValueOrErrorPublic_1(
            success=True,
            message="",
            error_code=ErrorCodes.None_,
            error_id=None,
            data=merged,
        )

    async def _read_entity_count(
        self,
        request: ReadDataRequest,
    ) -> Optional[int]:
        """Read the total entity count for *request* using ``ReadMode.Count``.

        Used to give an unlimited (``limit <= 0``) read a finite range to
        page over.  Returns ``None`` if the count read fails.
        """
        count_request = replace(
            request,
            request=replace(request.request, mode=ReadMode.Count),
        )
        result = await self._read_data_once(count_request)
        if result.success and result.data is not None:
            return result.data.entity_count
        return None

    async def modify_data(
        self,
        request: ModifyDataRequest,
    ) -> ValueOrErrorPublic_1[RequestId]:
        """Create, update, or delete entity data."""
        try:
            properties_by_type = (
                dict(zip(
                    request.data,
                    await asyncio.gather(*(
                        self._ensure_user_defined_properties(name) for name in request.data
                    )),
                ))
                if request.action in (DataRequestAction.Create, DataRequestAction.Update)
                else {}
            )
        except _UserDefinedPropertyLoadError as exc:
            return _value_error(exc.message, exc.error_code)
        request_dict = to_dict(request)
        if request_dict is None:
            return _deserialization_error()
        request_dict["data"] = {
            type_name: [
                to_dict(entity, user_defined_properties=properties_by_type.get(type_name))
                for entity in entities
            ]
            for type_name, entities in request.data.items()
        }
        result_dict: Any = await self._invoke("ModifyData", request_dict)
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[RequestId])
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def read_connected_data(
        self,
        request: ReadConnectedDataRequest,
    ) -> ValueOrErrorPublic_1[ServiceDataReadConnected]:
        """Read connected data and hydrate both connection and entity UDPs."""
        connection_type = request.request.connection_type_name
        entity_type = request.request.entity_type_name
        try:
            await asyncio.gather(
                self._ensure_user_defined_properties(connection_type),
                self._ensure_user_defined_properties(entity_type),
            )
        except _UserDefinedPropertyLoadError as exc:
            return _value_error(exc.message, exc.error_code)
        result_dict: Any = await self._invoke("ReadConnectedData", to_dict(request))
        try:
            result = from_dict(
                result_dict, ValueOrErrorPublic_1[ServiceDataReadConnected]
            )
        except CompressedPropertyError as exc:
            # Name the field and header rather than the opaque generic error.
            return _value_error(str(exc), ErrorCodes.SerializationError)
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is None:
            return _deserialization_error()
        if result.success and result.data is not None:
            connections = [item.connection.entity for item in result.data.data]
            entities = [item.entity.entity for item in result.data.data]
            if not await self._hydrate_entities_for_type(connection_type, connections):
                return _value_error(
                    f"Could not hydrate user-defined properties for {connection_type}"
                )
            if not await self._hydrate_entities_for_type(entity_type, entities):
                return _value_error(
                    f"Could not hydrate user-defined properties for {entity_type}"
                )
        return result

    async def create_user_defined_property(
        self,
        request: CreateUserDefinedPropertyRequest,
    ) -> ValueOrErrorPublic_1[UserDefinedPropertyInfo]:
        """Create a UDP definition and refresh its entity type's cache."""
        result_dict: Any = await self._invoke(
            "CreateUserDefinedProperty", to_dict(request),
        )
        try:
            result = from_dict(
                result_dict, ValueOrErrorPublic_1[UserDefinedPropertyInfo]
            )
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is None:
            return _deserialization_error()
        if result.success:
            self._evict_user_defined_properties({request.entity_type_name})
            try:
                await self._ensure_user_defined_properties(request.entity_type_name)
            except _UserDefinedPropertyLoadError as exc:
                return _value_error(exc.message, exc.error_code)
        return result

    async def update_user_defined_property(
        self,
        request: UpdateUserDefinedPropertyRequest,
    ) -> ValueOrErrorPublic_1[UserDefinedPropertyInfo]:
        """Update a UDP definition and clear all caches on success."""
        result_dict: Any = await self._invoke(
            "UpdateUserDefinedProperty", to_dict(request),
        )
        try:
            result = from_dict(
                result_dict, ValueOrErrorPublic_1[UserDefinedPropertyInfo]
            )
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is None:
            return _deserialization_error()
        if result.success:
            await self.invalidate_user_defined_property_cache()
        return result

    # ------------------------------------------------------------------
    # File methods
    # ------------------------------------------------------------------
    #
    # These are thin wrappers over the file-store hub methods.  The bytes
    # themselves never pass through the hub: ``request_file_upload`` returns an
    # :class:`UploadGrant` locating a storage endpoint, the client POSTs the
    # bytes there directly (see :mod:`filament._file_upload`), then
    # ``complete_file_upload`` confirms arrival.  A future ``upload_file``
    # helper will orchestrate the whole sequence.

    async def request_file_upload(
        self,
        request: FileUploadRequest,
    ) -> ValueOrErrorPublic_1[UploadGrant]:
        """Request a grant to upload one file into one slot.

        The returned :class:`UploadGrant` carries the storage URL, method, and
        policy fields the client submits the bytes with; it does **not** move any
        bytes itself.  If the store advertises a checksum algorithm (see
        :attr:`server_capabilities`), set :attr:`FileUploadRequest.checksum`
        before calling so the upload is verified at completion.
        """
        result_dict: Any = await self._invoke("RequestFileUpload", to_dict(request))
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[UploadGrant])
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def refresh_file_upload_grant(
        self,
        file_id: UUID,
    ) -> ValueOrErrorPublic_1[UploadGrant]:
        """Re-sign the grant for an in-progress upload.

        Use when a grant expires mid-transfer.  Unlike
        :meth:`request_file_upload` this does **not** take a second reservation,
        so it is safe to call repeatedly for the same *file_id*.
        """
        result_dict: Any = await self._invoke("RefreshFileUploadGrant", str(file_id))
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[UploadGrant])
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def complete_file_upload(
        self,
        file_id: UUID,
    ) -> ValueOrErrorPublic_1[FileEntity]:
        """Confirm an upload's bytes arrived, returning the completed file.

        The server verifies the stored object against what was declared (size,
        content type, and checksum where both sides have one).  A checksum
        mismatch returns :attr:`ErrorCodes.ChecksumMismatch`; the file is marked
        failed and the caller should request a fresh upload rather than retry.
        """
        request = FileUploadCompletion(file_id=file_id)
        result_dict: Any = await self._invoke("CompleteFileUpload", to_dict(request))
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[FileEntity])
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def cancel_file_upload(
        self,
        file_id: UUID,
    ) -> SuccessOrErrorPublic:
        """Abandon an upload, releasing its reservation immediately.

        Idempotent — safe to fire on abort without knowing whether the upload
        already completed.
        """
        result_dict: Any = await self._invoke("CancelFileUpload", str(file_id))
        try:
            result = from_dict(result_dict, SuccessOrErrorPublic)
        except (AttributeError, TypeError, ValueError, KeyError):
            return _error_result_public(
                "Failed to deserialize CancelFileUpload response"
            )
        if result is not None:
            return result
        return _error_result_public("Failed to deserialize CancelFileUpload response")

    async def get_file_urls(
        self,
        file_ids: Sequence[UUID],
    ) -> ValueOrErrorPublic_1[dict[UUID, FileReference]]:
        """Mint read URLs for a batch of files.

        Returns one :class:`FileReference` per requested id, keyed by file id.
        Results are per-file: a stale or unreadable id yields a failed
        :class:`FileReference` without failing the others.  A file the caller
        cannot read and one that does not exist are indistinguishable, by design.
        """
        request = GetFileUrlsRequest(file_ids=list(file_ids))
        result_dict: Any = await self._invoke("GetFileUrls", to_dict(request))
        try:
            result = from_dict(
                result_dict, ValueOrErrorPublic_1[dict[UUID, FileReference]]
            )
        except (AttributeError, TypeError, ValueError, KeyError):
            return _deserialization_error()
        if result is not None:
            return result
        return _deserialization_error()

    async def upload_file(
        self,
        *,
        name: str,
        content_type: str,
        data: bytes,
        reference_type: str,
        reference_id: UUID,
        reference_property: str,
        description: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> ValueOrErrorPublic_1[FileEntity]:
        """Upload one file end-to-end and return the completed :class:`FileEntity`.

        Orchestrates the full flow: compute whatever checksum the store requires
        (from :attr:`server_capabilities`), request an upload grant, POST the
        bytes directly to storage, then confirm completion.  If the transfer
        fails the grant is refreshed once and retried (a likely cause is expiry,
        and refresh takes no new reservation); if it still fails the reservation
        is cancelled and a failed result is returned.

        :param data: The file bytes.  ``size`` and any checksum are derived from
            these, so the whole file must be in memory (streaming is a planned
            follow-up).
        :param http_client: Optional httpx client to reuse for the storage
            transfer.  When omitted, a short-lived client is created and closed.
        """
        # 1. Checksum in the store's declared algorithm (None_ → no checksum).
        algorithm = FileChecksumAlgorithm.None_
        caps = self._server_capabilities
        if caps is not None and caps.files is not None:
            algorithm = caps.files.checksum_algorithm
        try:
            checksum = compute_checksum(data, algorithm)
        except (RuntimeError, ValueError) as exc:
            # Crc32cBase64 without the optional google-crc32c dependency, or an
            # algorithm this client does not know.  Either way the upload cannot
            # be attempted — surface it as a result, not an exception, since no
            # reservation has been taken yet.
            return _value_error(str(exc), ErrorCodes.NotSupported)

        # 2. Request the grant.
        grant_result = await self.request_file_upload(
            FileUploadRequest(
                name=name,
                content_type=content_type,
                size=len(data),
                reference_type=reference_type,
                reference_id=reference_id,
                reference_property=reference_property,
                description=description,
                checksum=checksum,
            )
        )
        if not grant_result.success or grant_result.data is None:
            return _propagate_failure(grant_result)
        grant = grant_result.data
        file_id = grant.file_id

        # 3. Transfer the bytes, refreshing the grant once on failure.
        owns_client = http_client is None
        client = http_client if http_client is not None else httpx.AsyncClient()
        try:
            ok, message, error_code = await self._submit_upload(
                client, grant, data, content_type, name
            )
            if not ok:
                refresh = await self.refresh_file_upload_grant(file_id)
                if refresh.success and refresh.data is not None:
                    grant = refresh.data
                    ok, message, error_code = await self._submit_upload(
                        client, grant, data, content_type, name
                    )
            if not ok:
                await self._release_upload_reservation(file_id)
                return _value_error(message, error_code)

            # 4. Confirm arrival; the server verifies size / type / checksum.
            #    Inside the try so a cancellation or hub failure while completion
            #    is in flight still releases the reservation below.
            return await self.complete_file_upload(file_id)
        except asyncio.CancelledError:
            # The reservation outlives the cancelled task and keeps consuming
            # quota, so release it before unwinding.  Shielded so the in-flight
            # hub call is not itself cancelled.
            await self._release_upload_reservation(file_id, shielded=True)
            raise
        except Exception:
            # The grant refresh or the completion call raised rather than
            # returning a failed result — a dropped hub connection surfaces this
            # way.  Same leak, but no shield needed: this task is not being
            # cancelled, so the cleanup call can run normally.
            await self._release_upload_reservation(file_id)
            raise
        finally:
            if owns_client:
                await client.aclose()

    async def _release_upload_reservation(
        self,
        file_id: UUID,
        *,
        shielded: bool = False,
    ) -> None:
        """Best-effort release of an upload reservation.

        Never raises: a failure here must not mask the error that prompted the
        release.  Safe to call even when the upload may already have completed
        server-side, since :meth:`cancel_file_upload` is idempotent.

        :param shielded: Protect the hub call from a cancellation already being
            delivered to this task, so the reservation is still released.
        """
        call = self.cancel_file_upload(file_id)
        try:
            await (asyncio.shield(call) if shielded else call)
        except Exception:
            logger.warning(
                "Failed to cancel upload reservation for %s",
                file_id,
                exc_info=True,
            )

    async def _submit_upload(
        self,
        http_client: httpx.AsyncClient,
        grant: UploadGrant,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> tuple[bool, str, ErrorCodes]:
        """POST *data* to the storage endpoint described by *grant*.

        Returns ``(ok, message, error_code)``; ``message`` and ``error_code``
        are only meaningful when ``ok`` is False.
        """
        request = build_upload_request(
            url=grant.url,
            method=grant.method,
            fields=grant.fields,
            headers=grant.headers,
            data=data,
            content_type=content_type,
            filename=filename,
        )
        try:
            response = await http_client.send(request)
        except httpx.HTTPError as exc:
            return False, f"Upload transfer failed: {exc}", ErrorCodes.ConnectionError
        if response.is_success:
            return True, "", ErrorCodes.None_
        body = response.text[:500]
        return (
            False,
            f"Storage rejected upload ({response.status_code}): {body}",
            ErrorCodes.RequestError,
        )

    async def download_file(
        self,
        file_id: UUID,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> ValueOrErrorPublic_1[bytes]:
        """Fetch one file's bytes via a freshly minted read URL.

        Resolves a signed URL with :meth:`get_file_urls`, then GETs the bytes
        from storage.  Returns the bytes on success, or a failed result carrying
        the reason the file could not be resolved or fetched.  A file the caller
        cannot read and one that does not exist are indistinguishable, by design.
        """
        urls = await self.get_file_urls([file_id])
        if not urls.success or urls.data is None:
            return _propagate_failure(urls)
        reference = urls.data.get(file_id)
        if reference is None:
            return _value_error(
                f"No result returned for file {file_id}", ErrorCodes.NoData
            )
        return await self.download_file_reference(reference, http_client=http_client)

    async def download_file_reference(
        self,
        reference: FileReference,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> ValueOrErrorPublic_1[bytes]:
        """Fetch the bytes for an already-resolved :class:`FileReference`.

        Use this when you already hold a reference — e.g. ``UserEntity.image``,
        which is populated automatically from its ``[FileDataProperty]`` read
        projection (a raw projection string can also be parsed with
        :func:`filament.file_reference_from_json`) — to download directly,
        skipping the extra :meth:`get_file_urls` round trip.  Returns the bytes,
        or a failed result if the reference did not resolve or the transfer
        failed.
        """
        if not reference.success or reference.url is None:
            return _value_error(
                reference.message or "File could not be resolved",
                reference.error_code,
            )

        owns_client = http_client is None
        client = http_client if http_client is not None else httpx.AsyncClient()
        try:
            response = await client.get(reference.url)
        except httpx.HTTPError as exc:
            return _value_error(
                f"Download transfer failed: {exc}", ErrorCodes.ConnectionError
            )
        finally:
            if owns_client:
                await client.aclose()

        if not response.is_success:
            return _value_error(
                f"Storage rejected download ({response.status_code})",
                ErrorCodes.RequestError,
            )
        return ValueOrErrorPublic_1(
            success=True,
            message="",
            error_code=ErrorCodes.None_,
            error_id=None,
            data=response.content,
        )

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
        try:
            await self._ensure_user_defined_properties(subscription.type_name)
        except _UserDefinedPropertyLoadError as exc:
            return _value_error(exc.message, exc.error_code)
        subscription_dict = to_dict(subscription)
        result_dict: Any = await self._invoke("Subscribe", subscription_dict)
        try:
            result = from_dict(result_dict, ValueOrErrorPublic_1[SubscriptionId])
        except (AttributeError, TypeError, ValueError, KeyError):
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
                    if backend_id == self._user_defined_subscription_id:
                        # The private UDP-definition subscription shares this
                        # backend ID.  Sending Unsubscribe would cancel it while
                        # _user_defined_subscription_id still points at it, and
                        # _reconcile_user_defined_property_subscription would
                        # then early-return and never restore it.  Leave the
                        # backend subscription — and its in-flight buffers —
                        # alone; once reconcile moves off this ID it sends the
                        # deferred Unsubscribe itself.
                        return SuccessOrErrorPublic(
                            success=True,
                            message="",
                            error_code=ErrorCodes.None_,
                            error_id=None,
                        )
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
                    # Likewise complete updates still queued for dispatch: hydrating
                    # them would cost hub calls for a callback that no longer exists.
                    # UDP definition pushes stay: this ID may be a private subscription
                    # that reconciliation has since replaced, and the backend does not
                    # resend the push on the new one.
                    queued = [
                        e for e in self._data_update_queue if e[0] != backend_id or e[3]
                    ]
                    self._data_update_queue.clear()
                    self._data_update_queue.extend(queued)
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
        cached_type_names = list(self._user_defined_properties)
        result_dict = await self._invoke("UnsubscribeAll")
        self._backend_to_client.clear()
        self._client_callbacks.clear()
        self._client_error_callbacks.clear()
        self._client_to_backend.clear()
        self._part_buffers.clear()
        self._poisoned_part_buffer_keys.clear()
        self._data_update_queue.clear()
        self._user_defined_subscription_id = None
        self._user_defined_subscription_types = frozenset()
        result = from_dict(result_dict, SuccessOrErrorPublic)
        if result is None:
            return _error_result_public("Failed to deserialize UnsubscribeAll response")
        if result.success and cached_type_names:
            await self.invalidate_user_defined_property_cache()
            refreshes = await asyncio.gather(
                *(
                    self._ensure_user_defined_properties(name, reconcile=False)
                    for name in cached_type_names
                ),
                return_exceptions=True,
            )
            for name, refresh in zip(cached_type_names, refreshes, strict=True):
                if isinstance(refresh, BaseException):
                    logger.error(
                        "Could not restore UDP metadata subscription for %s: %s",
                        name,
                        refresh,
                    )
            try:
                await self._reconcile_user_defined_property_subscription()
            except _UserDefinedPropertyLoadError:
                logger.exception(
                    "Could not restore UDP definition subscription after "
                    "UnsubscribeAll"
                )
        return result


class _UserDefinedPropertyLoadError(Exception):
    """Internal error carrying the backend envelope details for a cache load."""

    def __init__(self, message: str, error_code: ErrorCodes) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code

def _deserialization_error() -> ValueOrErrorPublic_1[Any]:
    """Return a failed ``ValueOrErrorPublic_1`` for deserialization failures."""
    return ValueOrErrorPublic_1(
        success=False,
        message="Failed to deserialize server response",
        error_code=ErrorCodes.InternalError,
        error_id=None,
        data=None,
    )


def _value_error(
    message: str,
    error_code: ErrorCodes = ErrorCodes.InternalError,
) -> ValueOrErrorPublic_1[Any]:
    """Return a failed generic value envelope."""
    return ValueOrErrorPublic_1(
        success=False,
        message=message,
        error_code=error_code,
        error_id=None,
        data=None,
    )


def _propagate_failure(
    source: ValueOrErrorPublic_1[Any],
) -> ValueOrErrorPublic_1[Any]:
    """Re-wrap a failed result under a different data type, dropping its payload."""
    return ValueOrErrorPublic_1(
        success=False,
        message=source.message,
        error_code=source.error_code,
        error_id=source.error_id,
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

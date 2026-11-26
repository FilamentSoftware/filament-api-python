"""FilamentClientSync — a blocking client for the Filament SignalR API.

The SignalR connection needs an event loop that keeps running between calls
(keep-alives, completions, close detection), so the blocking client does not
drive a loop per call.  It owns a daemon thread running one private loop, keeps
a real :class:`FilamentClient` entirely on that loop, and each public method
schedules the matching coroutine there and blocks until it finishes.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import functools
import inspect
import logging
import threading
from collections.abc import Callable, Coroutine, Mapping, Sequence
from types import TracebackType
from typing import Any, Generic, Optional, Type, TypeVar

from uuid_utils import UUID

from ._client import (
    _DEFAULT_CONNECTION_TIMEOUT,
    _DEFAULT_MAX_SIZE,
    _DEFAULT_PATH,
    _DEFAULT_PORT,
    _DEFAULT_SERVER,
    FilamentClient,
)
from ._exceptions import FilamentConnectionError, FilamentError
from .types.generated.entities.core import FileEntity
from .types.generated.misc import (
    CreateUserDefinedPropertyRequest,
    EntityPermissions,
    EntityPropertyPermissions,
    FileUploadRequest,
    ModifyDataRequest,
    ReadConnectedDataRequest,
    ReadDataRequest,
    RequestId,
    ServerCapabilities,
    ServiceDataRead,
    ServiceDataReadConnected,
    SuccessOrErrorPublic,
    UpdateUserDefinedPropertyRequest,
    UploadGrant,
    UserDefinedPropertyInfo,
    ValueOrErrorPublic_1,
)
from .types.generated.shared import FileReference
from .user_defined_properties import UserDefinedProperty

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_THREAD_NAME = "filament-sync-client"
# How long a blocked caller sleeps between checks for a pending signal.  A bare
# blocking wait is not reliably interruptible by Ctrl+C on Windows; waking this
# often lets KeyboardInterrupt through.  It does not delay results — the wait
# returns as soon as the call completes.
_POLL_SECONDS = 0.1
# How long close() lets calls that are still in flight finish their cancellation
# cleanup (e.g. releasing an interrupted upload's reservation) before closing the
# connection regardless.  Hub invocations have no response timeout, so without a
# cap a server that never answers would hang close() forever.
_CLOSE_GRACE_SECONDS = 5.0

_NOT_CONNECTED = "FilamentClientSync is not connected"
_CLOSED_WHILE_CONNECTING = "FilamentClientSync was closed while connecting"
_CLOSED_DURING_CALL = "FilamentClientSync was closed while the call was in progress"


class _State(enum.Enum):
    CLOSED = "closed"
    CONNECTING = "connecting"
    OPEN = "open"
    CLOSING = "closing"


class _Operation(Generic[_T]):
    """One submitted call.

    ``future`` is what the calling thread blocks on.  ``task`` and
    ``cancel_requested`` are touched only on the loop thread.
    """

    __slots__ = ("future", "task", "cancel_requested")

    def __init__(self) -> None:
        self.future: concurrent.futures.Future[_T] = concurrent.futures.Future()
        self.task: Optional[asyncio.Task[_T]] = None
        self.cancel_requested = False


class _Attempt:
    """Everything belonging to one ``connect()``: its thread, loop and client.

    Each ``connect()`` gets a fresh attempt, and every lifecycle step checks it is
    still acting on its own attempt, so a thread that was delayed can never reopen
    a client another thread closed, or disturb a later connection.

    ``loop``, ``stop_event`` and ``stop_requested`` are guarded by the client's
    lock.  ``client``, ``operations`` and ``shutting_down`` are written only on
    the loop thread.  ``finished`` is set by the worker thread as its last act.
    """

    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.stop_requested = False
        # Completed once by the loop thread: None for a successful connect,
        # otherwise the exception that ended the attempt.
        self.outcome: concurrent.futures.Future[None] = concurrent.futures.Future()
        # Whether the worker is done is read from this, never from the thread's
        # liveness: on CPython before 3.13 a Ctrl+C that lands inside
        # Thread.join() or is_alive() can mark a still-running thread as stopped.
        self.finished = threading.Event()
        self.client: Optional[FilamentClient] = None
        self.operations: set[_Operation[Any]] = set()
        self.shutting_down = False

    def publish(self, exc: Optional[BaseException] = None) -> None:
        """Give the connecting thread its outcome, if it has not had one yet."""
        if self.outcome.done():
            return
        if exc is None:
            self.outcome.set_result(None)
        else:
            self.outcome.set_exception(exc)


class FilamentClientSync:
    """Blocking client for the Filament SignalR API.

    For code that does not use :mod:`asyncio`.  It offers the request/response
    methods of :class:`FilamentClient` as ordinary blocking calls; subscriptions
    and event callbacks are not available — use :class:`FilamentClient` for those.

    Usage::

        with FilamentClientSync(
            organization_id="org-id", api_key_name="my-key", api_key="secret",
        ) as f:
            result = f.read_data(request)

    or, without a ``with`` block::

        f = FilamentClientSync(organization_id="org-id", api_key_name="my-key", api_key="secret")
        f.connect()
        try:
            result = f.read_data(request)
        finally:
            f.close()

    While connected, the client runs a background thread that owns the
    connection.  It may be shared between threads: calls made from different
    threads run concurrently on the one connection and may complete in any
    order.  Do not carry a connected client across ``fork()``; connect in the
    child instead.  Calling it from inside a running event loop works but blocks
    that loop for the duration of each call.
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
        """Initialise the client.  Nothing is connected until :meth:`connect`.

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
            closed with code 1009 and any in-flight call fails
            with :class:`FilamentConnectionError`.
        """
        self._client_kwargs: dict[str, Any] = {
            "organization_id": organization_id,
            "api_key_name": api_key_name,
            "api_key": api_key,
            "server": server,
            "port": port,
            "path": path,
            "connection_timeout": connection_timeout,
            "max_size": max_size,
        }
        # Guards _state, _attempt, and each attempt's loop / stop fields.  Held only
        # for short non-blocking sections, never while waiting on a result.
        self._lock = threading.Lock()
        self._state = _State.CLOSED
        self._attempt: Optional[_Attempt] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the connection, blocking until it is ready.

        Raises :class:`FilamentConnectionError` if the server cannot be
        reached (or if another thread calls :meth:`close` while this is
        connecting), :class:`FilamentAuthenticationError` if the server
        rejects the API key, and :class:`FilamentError` if the client is
        already connected or connecting.

        A failed ``connect()`` leaves nothing running and needs no matching
        :meth:`close`.  A closed client can be connected again.
        """
        attempt = _Attempt()
        thread = threading.Thread(
            target=self._run_thread, args=(attempt,), name=_THREAD_NAME, daemon=True
        )
        attempt.thread = thread
        # Everything from registering the attempt to declaring it open sits in one
        # try, so an error or a Ctrl+C at any point takes the same way out and a
        # connect() that does not return normally leaves nothing running.
        try:
            with self._lock:
                if self._state is _State.CLOSING:
                    raise FilamentError("FilamentClientSync is still closing")
                if self._state is not _State.CLOSED:
                    raise FilamentError("FilamentClientSync is already connected")
                # Registered before the worker exists, and the attempt before the
                # state, so there is no moment at which a worker is running that
                # close() or the cleanup below cannot find.  The worker's first act
                # is to take this lock, so it cannot get ahead of the registration.
                self._attempt = attempt
                self._state = _State.CONNECTING
                thread.start()

            self._wait_for_outcome(attempt)
            attempt.outcome.result()

            with self._lock:
                # Another thread may have closed the client between the worker
                # publishing success and this thread reading it.
                accepted = self._state is _State.CONNECTING and self._attempt is attempt
                if accepted:
                    self._state = _State.OPEN
            if not accepted:
                raise FilamentConnectionError(_CLOSED_WHILE_CONNECTING)
        except BaseException:
            self._abandon(attempt)
            raise

    def close(self) -> None:
        """Close the connection and stop the background thread.

        Safe to call more than once, on a client that never connected, and from
        a different thread than the one that connected.  Calls still in flight
        are cancelled and fail with :class:`FilamentConnectionError`; a
        :meth:`connect` in progress on another thread is cancelled the same way.

        Never raises for a connection that is already broken.
        """
        with self._lock:
            attempt = self._attempt
            if attempt is None:
                return
            self._state = _State.CLOSING
            # Sent on every call, not only the one that moved the state: an
            # earlier close() may have been interrupted (Ctrl+C) before it got
            # this far, and the request is idempotent.
            self._request_stop_locked(attempt)
        self._wait_finished(attempt)
        self._release(attempt)

    def __enter__(self) -> FilamentClientSync:
        """Open the connection.  See :meth:`connect`."""
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        """Close the connection.  See :meth:`close`."""
        self.close()

    def _abandon(self, attempt: _Attempt) -> None:
        """Stop *attempt*'s worker, wait for it, and return to CLOSED.

        The one way out of a connect() that did not succeed.  Safe at any stage:
        for an attempt that was never registered or whose thread never started
        there is nothing to stop or wait for, and the identity check in
        :meth:`_release` keeps it from touching a connection that is not its own.
        """
        with self._lock:
            self._request_stop_locked(attempt)
        self._wait_finished(attempt)
        self._release(attempt)

    def _wait_finished(self, attempt: _Attempt) -> None:
        """Block until *attempt*'s worker thread has finished all its work."""
        thread = attempt.thread
        if thread is None or thread.ident is None:
            # Never started.  (A thread interrupted in the instant between being
            # spawned and recording its ident also lands here; the stop request
            # already on the attempt makes it exit without connecting.)
            return
        # Polled so Ctrl+C stays deliverable on Windows.  If this is interrupted,
        # a retry waits again: the event is only ever set by the worker itself.
        while not attempt.finished.wait(_POLL_SECONDS):
            pass
        # The worker's work is done; this only waits out the thread's last few
        # instructions so it is really gone when the caller returns.
        thread.join()

    def _release(self, attempt: _Attempt) -> None:
        """Return to CLOSED, but only if *attempt* is still the current one."""
        with self._lock:
            if self._attempt is attempt:
                self._attempt = None
                self._state = _State.CLOSED

    def _request_stop_locked(self, attempt: _Attempt) -> None:
        """Ask *attempt*'s loop to shut down.  Caller holds the lock.

        Idempotent, and valid before the worker has a loop: the flag is recorded
        on the attempt and the worker honours it when it publishes its loop.
        """
        attempt.stop_requested = True
        loop, stop_event = attempt.loop, attempt.stop_event
        if loop is None or stop_event is None:
            return
        try:
            loop.call_soon_threadsafe(stop_event.set)
        except RuntimeError:
            # The loop has already closed; there is nothing left to stop.
            pass

    @staticmethod
    def _wait_for_outcome(attempt: _Attempt) -> None:
        """Block until the worker has reported how the connect went."""
        while True:
            done, _ = concurrent.futures.wait([attempt.outcome], timeout=_POLL_SECONDS)
            if done:
                return
            if attempt.finished.is_set():
                if attempt.outcome.done():
                    return
                raise FilamentConnectionError(
                    "FilamentClientSync's connection thread exited before connecting"
                )

    # ------------------------------------------------------------------
    # Loop thread
    # ------------------------------------------------------------------

    def _run_thread(self, attempt: _Attempt) -> None:
        """Thread target: run the loop, then signal completion whatever happened."""
        try:
            self._thread_main(attempt)
        finally:
            attempt.finished.set()

    def _thread_main(self, attempt: _Attempt) -> None:
        main = self._main(attempt)
        try:
            asyncio.run(main)
        except BaseException as exc:
            attempt.publish(exc)
            logger.debug("FilamentClientSync loop thread ended with an error", exc_info=True)
        finally:
            if inspect.getcoroutinestate(main) == inspect.CORO_CREATED:
                # asyncio.run() failed before it could start the coroutine (the
                # event loop could not be created).  Close it so it is not
                # reported as "never awaited" when it is collected.
                main.close()
            with self._lock:
                attempt.loop = None
                attempt.stop_event = None
            attempt.publish(
                FilamentConnectionError(
                    "FilamentClientSync's connection thread exited before connecting"
                )
            )

    async def _main(self, attempt: _Attempt) -> None:
        """Connect, serve calls until asked to stop, then shut down.

        Stopping never cancels this coroutine: close() and an interrupted
        connect() both set the stop event and this reacts to it, so the shutdown
        in ``finally`` always runs as ordinary code rather than mid-cancellation.
        """
        stop = asyncio.Event()
        with self._lock:
            attempt.loop = asyncio.get_running_loop()
            attempt.stop_event = stop
            if attempt.stop_requested:
                # close() landed before this loop existed.
                stop.set()

        try:
            if stop.is_set():
                attempt.publish(FilamentConnectionError(_CLOSED_WHILE_CONNECTING))
                return
            # Built here, on the loop thread, so every asyncio primitive the
            # client creates belongs to this loop.
            client = FilamentClient(**self._client_kwargs)
            attempt.client = client

            connect_task = asyncio.ensure_future(client.connect())
            stop_task = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait(
                    {connect_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                stop_task.cancel()
            if not connect_task.done():
                connect_task.cancel()
                try:
                    await connect_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("Connect ended with an error while closing", exc_info=True)
                attempt.publish(FilamentConnectionError(_CLOSED_WHILE_CONNECTING))
                return
            connect_task.result()

            attempt.publish()
            await stop.wait()
        except BaseException as exc:
            attempt.publish(exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            await self._shutdown(attempt)

    async def _shutdown(self, attempt: _Attempt) -> None:
        """Settle in-flight calls, then close the connection.

        Order matters: calls are cancelled and given time to finish their
        cancellation cleanup *while the transport is still open*, because that
        cleanup can itself be a hub call (an interrupted upload releases its
        reservation).  Only then is the client closed.
        """
        attempt.shutting_down = True
        operations = list(attempt.operations)
        for operation in operations:
            self._cancel_operation(operation)
        tasks = [operation.task for operation in operations if operation.task is not None]
        if tasks:
            # asyncio.wait, not wait_for(gather(...)): on timeout wait_for would
            # cancel the tasks a second time, which is exactly what
            # _cancel_operation exists to prevent.
            _, pending = await asyncio.wait(tasks, timeout=_CLOSE_GRACE_SECONDS)
            if pending:
                logger.warning(
                    "FilamentClientSync closed with %d call(s) still cleaning up after "
                    "%.1f s; abandoning them",
                    len(pending),
                    _CLOSE_GRACE_SECONDS,
                )
                # Don't leave their callers blocked until loop finalisation.
                for operation in operations:
                    if operation.task in pending and not operation.future.done():
                        operation.future.set_exception(
                            FilamentConnectionError(_CLOSED_DURING_CALL)
                        )

        client = attempt.client
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("Closing the inner FilamentClient failed", exc_info=True)
        # Anything still running is force-cancelled by asyncio.run() as it
        # finalises the loop — the one place a call can be cancelled twice.

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    def _run(self, factory: Callable[[FilamentClient], Coroutine[Any, Any, _T]]) -> _T:
        """Run one call on the loop thread and block for its result.

        *factory* builds the coroutine from the connected client.  It is invoked
        on the loop thread, so the coroutine always belongs to the loop and
        client it runs on, and a rejected call never creates a coroutine at all.
        """
        operation: _Operation[_T] = _Operation()
        future = operation.future
        # The loop the call was (or was about to be) queued on.  Set immediately
        # before queueing, so the handler below knows a cancellation is needed
        # even if the interrupt lands as the queueing call returns.
        submitted_to: Optional[asyncio.AbstractEventLoop] = None
        try:
            with self._lock:
                attempt = self._attempt
                loop = attempt.loop if attempt is not None else None
                if self._state is not _State.OPEN or attempt is None or loop is None:
                    raise FilamentConnectionError(_NOT_CONNECTED)
                if threading.current_thread() is attempt.thread:
                    raise RuntimeError(
                        "FilamentClientSync cannot be called from its own connection thread"
                    )
                # Scheduled under the lock: close() flips the state and sends its
                # stop signal under this same lock, so this call is either rejected
                # above or queued on the loop strictly ahead of the stop signal.
                submitted_to = loop
                try:
                    loop.call_soon_threadsafe(self._start_operation, attempt, operation, factory)
                except RuntimeError:
                    submitted_to = None
                    raise FilamentConnectionError(_NOT_CONNECTED) from None

            # Poll until done, then read the result once, outside the loop.  Never
            # catch TimeoutError around result(): on Python 3.11+ the polling
            # timeout and a call that itself raised TimeoutError are the same
            # class, and retrying on it would spin forever.
            while not future.done():
                concurrent.futures.wait([future], timeout=_POLL_SECONDS)
        except BaseException:
            # Interrupted (Ctrl+C), at any point from queueing the call onwards.
            # Cancel it and leave immediately; its cleanup carries on on the loop
            # and close() will wait for it.  Without this a call the caller has
            # stopped waiting for could still go on to run.  A call rejected
            # before it was queued has nothing to cancel.
            if submitted_to is not None:
                try:
                    submitted_to.call_soon_threadsafe(self._cancel_operation, operation)
                except RuntimeError:
                    pass
            raise
        return future.result()

    def _start_operation(
        self,
        attempt: _Attempt,
        operation: _Operation[_T],
        factory: Callable[[FilamentClient], Coroutine[Any, Any, _T]],
    ) -> None:
        """Create and register the task for *operation*.  Loop thread only.

        Creating and registering happen in this one callback, and loop callbacks
        run in the order they were queued, so every call queued ahead of the
        stop signal is in ``attempt.operations`` before shutdown looks at it.
        """
        client = attempt.client
        if attempt.shutting_down or client is None:
            operation.future.set_exception(FilamentConnectionError(_NOT_CONNECTED))
            return
        if operation.cancel_requested:
            # Its caller was interrupted before this ran; never start it.
            operation.future.set_exception(
                FilamentConnectionError("The FilamentClientSync call was cancelled")
            )
            return
        try:
            task = asyncio.get_running_loop().create_task(factory(client))
        except Exception as exc:
            operation.future.set_exception(exc)
            return
        operation.task = task
        attempt.operations.add(operation)
        # Delivered by a done-callback rather than a try/finally inside a wrapper
        # coroutine: a task cancelled before its first step never runs its body.
        task.add_done_callback(functools.partial(self._finish_operation, attempt, operation))

    @staticmethod
    def _cancel_operation(operation: _Operation[Any]) -> None:
        """Cancel *operation*'s task, at most once.  Loop thread only.

        The only place a call is cancelled.  A second ``cancel()`` on a task that
        is already awaiting its shielded cleanup would interrupt that await and
        detach the cleanup from the task, so waiting for the task would no longer
        mean the cleanup had finished.
        """
        if operation.cancel_requested:
            return
        operation.cancel_requested = True
        if operation.task is not None:
            operation.task.cancel()

    @staticmethod
    def _finish_operation(
        attempt: _Attempt, operation: _Operation[_T], task: asyncio.Task[_T]
    ) -> None:
        """Hand *task*'s outcome to the blocked caller.  Loop thread only."""
        attempt.operations.discard(operation)
        future = operation.future
        if future.done():
            # Already failed as abandoned when close()'s grace period ran out.
            if not task.cancelled():
                task.exception()  # mark retrieved
            return
        if task.cancelled():
            future.set_exception(
                FilamentConnectionError(
                    _CLOSED_DURING_CALL
                    if attempt.shutting_down
                    else "The FilamentClientSync call was cancelled"
                )
            )
            return
        exc = task.exception()
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(task.result())

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    @property
    def server_capabilities(self) -> Optional[ServerCapabilities]:
        """Capabilities the server advertised during the ``ClientHello`` handshake.

        ``None`` while not connected, or when connected to an older server that
        returns no capabilities.  ``server_capabilities.files`` (when present)
        carries the file store's checksum algorithm and server-side-checksum
        flag, which :meth:`upload_file` consults to decide whether and how to
        checksum before requesting an upload grant.
        """
        with self._lock:
            attempt = self._attempt
            if self._state is not _State.OPEN or attempt is None:
                return None
            client = attempt.client
        return client.server_capabilities if client is not None else None

    def invalidate_user_defined_property_cache(
        self, entity_type_name: Optional[str] = None,
    ) -> None:
        """Invalidate one type's cached UDP definitions, or every cached type."""
        self._run(
            lambda client: client.invalidate_user_defined_property_cache(entity_type_name)
        )

    def get_user_defined_properties(
        self, entity_type_name: str,
    ) -> ValueOrErrorPublic_1[Mapping[str, UserDefinedProperty]]:
        """Return the cached/discovered UDP properties for an entity type."""
        return self._run(lambda client: client.get_user_defined_properties(entity_type_name))

    def get_entity_permissions(
        self,
    ) -> ValueOrErrorPublic_1[EntityPermissions]:
        """Retrieve entity-level permissions for the authenticated user."""
        return self._run(lambda client: client.get_entity_permissions())

    def get_entity_property_permissions(
        self,
        entity_type_name: str,
    ) -> ValueOrErrorPublic_1[EntityPropertyPermissions]:
        """Retrieve permissions and refresh this type's UDP metadata cache."""
        return self._run(
            lambda client: client.get_entity_property_permissions(entity_type_name)
        )

    def read_data(
        self,
        request: ReadDataRequest,
    ) -> ValueOrErrorPublic_1[ServiceDataRead]:
        """Read entity data from the server.

        A response the server rejects as too large for the negotiated
        ``max_size`` is automatically split into smaller pages and merged, as
        described on :meth:`FilamentClient.read_data`.  If a request for a
        single entity is still too large, :class:`ResponseTooLargeError` is
        raised.
        """
        return self._run(lambda client: client.read_data(request))

    def modify_data(
        self,
        request: ModifyDataRequest,
    ) -> ValueOrErrorPublic_1[RequestId]:
        """Create, update, or delete entity data.

        On an ``Update`` with ``update_properties`` set, an entity that was
        read back from the server (rather than constructed by the caller) is
        sent as its ``id`` plus the named properties only, since those are all
        the backend writes. Entities the caller constructed are sent as given.
        """
        return self._run(lambda client: client.modify_data(request))

    def read_connected_data(
        self,
        request: ReadConnectedDataRequest,
    ) -> ValueOrErrorPublic_1[ServiceDataReadConnected]:
        """Read connected data and hydrate both connection and entity UDPs."""
        return self._run(lambda client: client.read_connected_data(request))

    def create_user_defined_property(
        self,
        request: CreateUserDefinedPropertyRequest,
    ) -> ValueOrErrorPublic_1[UserDefinedPropertyInfo]:
        """Create a UDP definition and refresh its entity type's cache."""
        return self._run(lambda client: client.create_user_defined_property(request))

    def update_user_defined_property(
        self,
        request: UpdateUserDefinedPropertyRequest,
    ) -> ValueOrErrorPublic_1[UserDefinedPropertyInfo]:
        """Update a UDP definition and clear all caches on success."""
        return self._run(lambda client: client.update_user_defined_property(request))

    def request_file_upload(
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
        return self._run(lambda client: client.request_file_upload(request))

    def refresh_file_upload_grant(
        self,
        file_id: UUID,
    ) -> ValueOrErrorPublic_1[UploadGrant]:
        """Re-sign the grant for an in-progress upload.

        Use when a grant expires mid-transfer.  Unlike
        :meth:`request_file_upload` this does **not** take a second reservation,
        so it is safe to call repeatedly for the same *file_id*.
        """
        return self._run(lambda client: client.refresh_file_upload_grant(file_id))

    def complete_file_upload(
        self,
        file_id: UUID,
    ) -> ValueOrErrorPublic_1[FileEntity]:
        """Confirm an upload's bytes arrived, returning the completed file.

        The server verifies the stored object against what was declared (size,
        content type, and checksum where both sides have one).  A checksum
        mismatch returns :attr:`ErrorCodes.ChecksumMismatch`; the file is marked
        failed and the caller should request a fresh upload rather than retry.
        """
        return self._run(lambda client: client.complete_file_upload(file_id))

    def cancel_file_upload(
        self,
        file_id: UUID,
    ) -> SuccessOrErrorPublic:
        """Abandon an upload, releasing its reservation immediately.

        Idempotent — safe to fire on abort without knowing whether the upload
        already completed.
        """
        return self._run(lambda client: client.cancel_file_upload(file_id))

    def get_file_urls(
        self,
        file_ids: Sequence[UUID],
    ) -> ValueOrErrorPublic_1[dict[UUID, FileReference]]:
        """Mint read URLs for a batch of files.

        Returns one :class:`FileReference` per requested id, keyed by file id.
        Results are per-file: a stale or unreadable id yields a failed
        :class:`FileReference` without failing the others.  A file the caller
        cannot read and one that does not exist are indistinguishable, by design.
        """
        return self._run(lambda client: client.get_file_urls(file_ids))

    def upload_file(
        self,
        *,
        name: str,
        content_type: str,
        data: bytes,
        reference_type: str,
        reference_id: UUID,
        reference_property: str,
        description: Optional[str] = None,
    ) -> ValueOrErrorPublic_1[FileEntity]:
        """Upload one file end-to-end and return the completed :class:`FileEntity`.

        Orchestrates the full flow: compute whatever checksum the store requires
        (from :attr:`server_capabilities`), request an upload grant, POST the
        bytes directly to storage, then confirm completion.  If the transfer
        fails the grant is refreshed once and retried (a likely cause is expiry,
        and refresh takes no new reservation); if it still fails the reservation
        is cancelled and a failed result is returned.

        Unlike :meth:`FilamentClient.upload_file` there is no ``http_client``
        parameter: an ``httpx.AsyncClient`` belongs to the event loop it is used
        on, and this client's loop is private.

        :param data: The file bytes.  ``size`` and any checksum are derived from
            these, so the whole file must be in memory.
        """
        return self._run(
            lambda client: client.upload_file(
                name=name,
                content_type=content_type,
                data=data,
                reference_type=reference_type,
                reference_id=reference_id,
                reference_property=reference_property,
                description=description,
            )
        )

    def download_file(
        self,
        file_id: UUID,
    ) -> ValueOrErrorPublic_1[bytes]:
        """Fetch one file's bytes via a freshly minted read URL.

        Resolves a signed URL with :meth:`get_file_urls`, then GETs the bytes
        from storage.  Returns the bytes on success, or a failed result carrying
        the reason the file could not be resolved or fetched.  A file the caller
        cannot read and one that does not exist are indistinguishable, by design.
        """
        return self._run(lambda client: client.download_file(file_id))

    def download_file_reference(
        self,
        reference: FileReference,
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
        return self._run(lambda client: client.download_file_reference(reference))

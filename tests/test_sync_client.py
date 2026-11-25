"""Tests for FilamentClientSync, the blocking client.

Most tests replace the inner :class:`FilamentClient` with a fake whose async
methods are mocks, so they exercise only what the sync client adds: the loop
thread, its lifecycle state machine, and the hand-off of calls and results
between threads.  ``TestWithRealInnerClient`` at the end runs the real
``FilamentClient`` with only the SignalR and HTTP boundaries faked, to check
that shutdown really does let transport-level cleanup finish.

Tests are synchronised with events, not sleeps: a fake coroutine signals a
``threading.Event`` when it reaches the point of interest, and the test waits on
that.  The few short sleeps left are in negative assertions ("this is still
blocked"), which have no event to wait for.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import inspect
import logging
import threading
import time
import warnings
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pysignalr.messages import CompletionMessage
from uuid_utils import uuid7

from filament import FilamentClientSync
from filament import _sync_client
from filament._client import FilamentClient
from filament._exceptions import (
    FilamentAuthenticationError,
    FilamentConnectionError,
    FilamentError,
)
from filament._sync_client import _Attempt, _Operation, _State
from tests.test_upload_orchestration import _grant, _ok

_THREAD_NAME = "filament-sync-client"
_TIMEOUT = 5.0

# Every blocking API method, with arguments to call it with.  The parity test
# below fails if a method is added to either client without appearing here.
_UPLOAD_KWARGS: dict[str, Any] = {
    "name": "photo.png",
    "content_type": "image/png",
    "data": b"bytes",
    "reference_type": "UserEntity",
    "reference_id": "ref-id",
    "reference_property": "Image",
    "description": "a photo",
}
_API_CALLS: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {
    "invalidate_user_defined_property_cache": (("UserEntity",), {}),
    "get_user_defined_properties": (("UserEntity",), {}),
    "get_entity_permissions": ((), {}),
    "get_entity_property_permissions": (("UserEntity",), {}),
    "read_data": (("read-request",), {}),
    "modify_data": (("modify-request",), {}),
    "read_connected_data": (("connected-request",), {}),
    "create_user_defined_property": (("create-request",), {}),
    "update_user_defined_property": (("update-request",), {}),
    "request_file_upload": (("upload-request",), {}),
    "refresh_file_upload_grant": (("file-id",), {}),
    "complete_file_upload": (("file-id",), {}),
    "cancel_file_upload": (("file-id",), {}),
    "get_file_urls": ((["file-id"],), {}),
    "upload_file": ((), _UPLOAD_KWARGS),
    "download_file": (("file-id",), {}),
    "download_file_reference": (("reference",), {}),
}
# Async-client members the sync client must not offer.
_ASYNC_ONLY = {
    "subscribe",
    "unsubscribe",
    "unsubscribe_all",
    "on_processing_started",
    "on_processing_completed",
    "on_processing_failed",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Every client and every gate a test creates, so teardown can release and close
# them even when the test failed before reaching its own close().
_clients: list[FilamentClientSync] = []
_gates: list["_Held"] = []


@pytest.fixture(autouse=True)
def _no_leaked_connections() -> Iterator[None]:
    """Close whatever a test left open, then check no connection thread remains.

    Without this a single failed assertion skips the test's ``close()``, the
    worker thread lives on, and every later test fails its own leak check.
    Autouse fixtures are torn down last, so this runs after ``fakes`` / ``hub``.
    """
    yield
    for held in _gates:
        held.release.set()
        held.cleanup_release.set()
    closers = [_Call(client.close) for client in _clients]
    for closer in closers:
        closer.join(2 * _TIMEOUT)
    _gates.clear()
    _clients.clear()
    assert not _worker_threads(), "a test left a connection thread running"


def _worker_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == _THREAD_NAME]


def _wait_until(predicate: Callable[[], bool], timeout: float = _TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not reached in time")
        time.sleep(0.005)


async def _until(event: threading.Event) -> None:
    """Wait, on the loop, for an event set from a test thread."""
    while not event.is_set():
        await asyncio.sleep(0.005)


class _Call(threading.Thread):
    """Run a blocking call on another thread, keeping its result or exception."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._fn = fn
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.start()

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            self.error = exc

    def finish(self) -> _Call:
        self.join(_TIMEOUT)
        assert not self.is_alive(), "the call is still blocked"
        return self


class _Held:
    """An inner coroutine that blocks until released, recording its cancellation.

    On cancellation it runs a "cleanup" phase, which can itself be held open
    (``hold_cleanup=True``) to stand in for cleanup that needs the connection,
    such as an interrupted upload releasing its reservation.
    """

    def __init__(self, *, hold_cleanup: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.cleanup_release = threading.Event()
        self.cleanup_finished = threading.Event()
        self.cancels = 0
        if not hold_cleanup:
            self.cleanup_release.set()
        _gates.append(self)

    async def run(self, *_args: Any, **_kwargs: Any) -> str:
        self.started.set()
        try:
            await _until(self.release)
            return "released"
        except asyncio.CancelledError:
            self.cancels += 1
            self.cancelled.set()
            try:
                await _until(self.cleanup_release)
            except asyncio.CancelledError:
                self.cancels += 1
                raise
            self.cleanup_finished.set()
            raise


class _FakeClient:
    """Stands in for the inner FilamentClient."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.created_on = threading.current_thread().name
        self.server_capabilities: Any = None
        self.connect = AsyncMock()
        self.close = AsyncMock()
        for name in _API_CALLS:
            setattr(self, name, AsyncMock(return_value=f"{name}:result"))


class _Fakes:
    """Factory patched in for ``FilamentClient``; records every instance made."""

    def __init__(self) -> None:
        self.instances: list[_FakeClient] = []
        # Called with each new instance before it is connected.
        self.configure: Optional[Callable[[_FakeClient], None]] = None

    def __call__(self, **kwargs: Any) -> _FakeClient:
        instance = _FakeClient(**kwargs)
        if self.configure is not None:
            self.configure(instance)
        self.instances.append(instance)
        return instance

    @property
    def last(self) -> _FakeClient:
        return self.instances[-1]


@pytest.fixture
def fakes() -> Iterator[_Fakes]:
    fakes = _Fakes()
    with patch("filament._sync_client.FilamentClient", fakes):
        yield fakes


def _make_sync(**kwargs: Any) -> FilamentClientSync:
    sync = FilamentClientSync(
        organization_id="test-org", api_key_name="test-key", api_key="test-secret", **kwargs
    )
    _clients.append(sync)
    return sync


@contextmanager
def _interrupt_wait_when(condition: Callable[[], bool]) -> Iterator[None]:
    """Raise KeyboardInterrupt in the thread blocked on a call, once *condition* holds.

    A real Ctrl+C surfaces in the blocked thread at its next wake-up from the
    polling wait; this raises at exactly that point.
    """
    real_wait = concurrent.futures.wait

    def wait(futures: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        if condition():
            raise KeyboardInterrupt
        return real_wait(futures, timeout=timeout, **kwargs)

    with patch("filament._sync_client.concurrent.futures.wait", side_effect=wait):
        yield


def _attempt_of(sync: FilamentClientSync) -> _Attempt:
    attempt = sync._attempt
    assert attempt is not None
    return attempt


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------

def _public_members(cls: type) -> set[str]:
    return {name for name in vars(cls) if not name.startswith("_")}


def _parameters(fn: Any, *, drop: frozenset[str] = frozenset()) -> list[tuple[Any, ...]]:
    return [
        (param.name, param.kind, param.default, param.annotation)
        for param in inspect.signature(fn).parameters.values()
        if param.name not in drop
    ]


class TestParityWithAsyncClient:
    """The sync client mirrors the async one, minus subscriptions and callbacks."""

    def test_sync_client_offers_everything_except_subscriptions_and_callbacks(self) -> None:
        assert _public_members(FilamentClient) - _public_members(FilamentClientSync) == _ASYNC_ONLY
        assert _public_members(FilamentClientSync) <= _public_members(FilamentClient)

    def test_every_api_method_is_covered_by_these_tests(self) -> None:
        methods = _public_members(FilamentClientSync) - {"connect", "close", "server_capabilities"}
        assert methods == set(_API_CALLS)

    @pytest.mark.parametrize(
        "name", sorted(_public_members(FilamentClientSync) - {"server_capabilities"})
    )
    def test_method_signature_matches(self, name: str) -> None:
        async_method = vars(FilamentClient)[name]
        sync_method = vars(FilamentClientSync)[name]

        assert inspect.iscoroutinefunction(async_method)
        assert not inspect.iscoroutinefunction(sync_method)
        # http_client is an httpx.AsyncClient, which belongs to the loop it is used
        # on; the sync client's loop is private, so it deliberately has no such
        # parameter.
        assert _parameters(sync_method) == _parameters(
            async_method, drop=frozenset({"http_client"})
        )
        assert "http_client" not in inspect.signature(sync_method).parameters
        assert (
            inspect.signature(sync_method).return_annotation
            == inspect.signature(async_method).return_annotation
        )

    def test_constructor_signature_matches(self) -> None:
        assert _parameters(FilamentClientSync.__init__) == _parameters(FilamentClient.__init__)

    def test_server_capabilities_is_a_property_on_both(self) -> None:
        assert isinstance(vars(FilamentClient)["server_capabilities"], property)
        assert isinstance(vars(FilamentClientSync)["server_capabilities"], property)

    @pytest.mark.parametrize("name", sorted(_ASYNC_ONLY))
    def test_subscriptions_and_callbacks_are_absent(self, name: str) -> None:
        assert not hasattr(FilamentClientSync, name)
        assert not hasattr(_make_sync(), name)


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

class TestCalls:
    @pytest.mark.parametrize("name", sorted(_API_CALLS))
    def test_method_delegates_and_returns_the_result(self, fakes: _Fakes, name: str) -> None:
        args, kwargs = _API_CALLS[name]

        with _make_sync() as sync:
            result = getattr(sync, name)(*args, **kwargs)

        getattr(fakes.last, name).assert_awaited_once_with(*args, **kwargs)
        if name == "invalidate_user_defined_property_cache":
            assert result is None
        else:
            assert result == f"{name}:result"

    def test_constructor_arguments_reach_the_inner_client(self, fakes: _Fakes) -> None:
        sync = FilamentClientSync(
            organization_id="org",
            api_key_name="key",
            api_key="secret",
            server="https://example.test",
            port=8443,
            path="api/v9",
            connection_timeout=3.5,
            max_size=None,
        )
        with sync:
            pass

        assert fakes.last.kwargs == {
            "organization_id": "org",
            "api_key_name": "key",
            "api_key": "secret",
            "server": "https://example.test",
            "port": 8443,
            "path": "api/v9",
            "connection_timeout": 3.5,
            "max_size": None,
        }

    def test_exception_from_the_call_propagates_unchanged(self, fakes: _Fakes) -> None:
        error = FilamentConnectionError("socket dropped")
        with _make_sync() as sync:
            fakes.last.read_data.side_effect = error

            with pytest.raises(FilamentConnectionError) as raised:
                sync.read_data("request")  # type: ignore[arg-type]

        assert raised.value is error

    def test_call_that_raises_timeout_error_surfaces_it_once(self, fakes: _Fakes) -> None:
        """Regression: on 3.11+ the polling timeout and a call's own TimeoutError are
        the same class, so a wait that retried on it would spin forever."""
        with _make_sync() as sync:
            fakes.last.read_data.side_effect = TimeoutError("server took too long")
            caller = _Call(lambda: sync.read_data("request"))  # type: ignore[arg-type]
            caller.finish()

        assert isinstance(caller.error, TimeoutError)
        assert str(caller.error) == "server took too long"
        fakes.last.read_data.assert_awaited_once()

    def test_factory_that_raises_surfaces_the_exception(self, fakes: _Fakes) -> None:
        def factory(_client: Any) -> Any:
            raise ValueError("bad factory")

        with _make_sync() as sync:
            with pytest.raises(ValueError, match="bad factory"):
                sync._run(factory)
            # The client is still usable afterwards.
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_client_and_calls_run_on_the_connection_thread(self, fakes: _Fakes) -> None:
        seen: list[str] = []

        async def record(*_args: Any) -> str:
            seen.append(threading.current_thread().name)
            return "ok"

        with _make_sync() as sync:
            fakes.last.read_data.side_effect = record
            sync.read_data("request")  # type: ignore[arg-type]

        assert fakes.last.created_on == _THREAD_NAME
        assert seen == [_THREAD_NAME]
        assert threading.current_thread().name != _THREAD_NAME

    def test_works_when_called_from_inside_a_running_event_loop(self, fakes: _Fakes) -> None:
        async def main() -> Any:
            with _make_sync() as sync:
                return sync.get_entity_permissions()

        assert asyncio.run(main()) == "get_entity_permissions:result"

    def test_calls_from_several_threads_all_get_their_own_result(self, fakes: _Fakes) -> None:
        async def echo(name: str) -> str:
            await asyncio.sleep(0.01)
            return f"echo:{name}"

        with _make_sync() as sync:
            fakes.last.get_user_defined_properties.side_effect = echo
            callers = [
                _Call(lambda i=i: sync.get_user_defined_properties(f"type-{i}"))  # type: ignore[misc]
                for i in range(8)
            ]
            for caller in callers:
                caller.finish()

        assert [caller.error for caller in callers] == [None] * 8
        assert [caller.result for caller in callers] == [f"echo:type-{i}" for i in range(8)]

    def test_server_capabilities_passes_through_while_connected(self, fakes: _Fakes) -> None:
        sync = _make_sync()
        assert sync.server_capabilities is None

        sync.connect()
        fakes.last.server_capabilities = "capabilities"
        assert sync.server_capabilities == "capabilities"

        sync.close()
        assert sync.server_capabilities is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_with_block_connects_and_closes(self, fakes: _Fakes) -> None:
        sync = _make_sync()

        with sync as entered:
            assert entered is sync
            fakes.last.connect.assert_awaited_once_with()
            fakes.last.close.assert_not_awaited()
            assert len(_worker_threads()) == 1

        fakes.last.close.assert_awaited_once_with()
        assert not _worker_threads()

    def test_with_block_closes_and_propagates_when_the_body_raises(self, fakes: _Fakes) -> None:
        with pytest.raises(ValueError, match="body failed"):
            with _make_sync():
                raise ValueError("body failed")

        fakes.last.close.assert_awaited_once_with()
        assert not _worker_threads()

    def test_explicit_connect_and_close(self, fakes: _Fakes) -> None:
        sync = _make_sync()

        assert sync.connect() is None  # type: ignore[func-returns-value]
        assert sync.get_entity_permissions() == "get_entity_permissions:result"
        assert sync.close() is None  # type: ignore[func-returns-value]

        fakes.last.connect.assert_awaited_once_with()
        fakes.last.close.assert_awaited_once_with()
        assert not _worker_threads()

    def test_nothing_is_started_until_connect(self, fakes: _Fakes) -> None:
        _make_sync()

        assert fakes.instances == []
        assert not _worker_threads()

    def test_close_is_idempotent(self, fakes: _Fakes) -> None:
        sync = _make_sync()
        sync.connect()

        sync.close()
        sync.close()

        fakes.last.close.assert_awaited_once_with()

    def test_close_without_connect_is_a_no_op(self, fakes: _Fakes) -> None:
        _make_sync().close()

        assert fakes.instances == []

    def test_connect_twice_raises_and_keeps_the_connection(self, fakes: _Fakes) -> None:
        with _make_sync() as sync:
            with pytest.raises(FilamentError, match="already connected"):
                sync.connect()

            assert len(fakes.instances) == 1
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_reconnect_after_close_uses_a_new_inner_client(self, fakes: _Fakes) -> None:
        sync = _make_sync()
        sync.connect()
        sync.read_data("first")  # type: ignore[arg-type]
        sync.close()

        sync.connect()
        sync.read_data("second")  # type: ignore[arg-type]
        sync.close()

        first, second = fakes.instances
        assert first is not second
        first.read_data.assert_awaited_once_with("first")
        second.read_data.assert_awaited_once_with("second")
        first.close.assert_awaited_once_with()
        second.close.assert_awaited_once_with()

    @pytest.mark.parametrize("name", sorted(_API_CALLS))
    def test_methods_raise_before_connect_and_after_close(self, fakes: _Fakes, name: str) -> None:
        args, kwargs = _API_CALLS[name]
        sync = _make_sync()

        with pytest.raises(FilamentConnectionError, match="not connected"):
            getattr(sync, name)(*args, **kwargs)

        sync.connect()
        sync.close()

        with pytest.raises(FilamentConnectionError, match="not connected"):
            getattr(sync, name)(*args, **kwargs)
        getattr(fakes.last, name).assert_not_awaited()

    @pytest.mark.parametrize(
        "error",
        [
            FilamentConnectionError("Failed to connect: no route to host"),
            FilamentAuthenticationError("bad key"),
        ],
    )
    def test_connect_failure_propagates_and_leaves_nothing_running(
        self, fakes: _Fakes, error: Exception
    ) -> None:
        fakes.configure = lambda client: setattr(client.connect, "side_effect", error)
        sync = _make_sync()

        with pytest.raises(type(error)) as raised:
            sync.connect()

        assert raised.value is error
        assert not _worker_threads()
        assert sync._state is _State.CLOSED
        # A failed connect never opened anything, but closing the inner client is
        # harmless and keeps one cleanup path.
        with pytest.raises(FilamentConnectionError, match="not connected"):
            sync.get_entity_permissions()

        # The client is reusable once the cause is fixed.
        fakes.configure = None
        with sync:
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_inner_client_constructor_failure_propagates(self) -> None:
        def explode(**_kwargs: Any) -> Any:
            raise ValueError("bad argument")

        sync = _make_sync()
        with patch("filament._sync_client.FilamentClient", explode):
            with pytest.raises(ValueError, match="bad argument"):
                sync.connect()

        assert not _worker_threads()
        assert sync._state is _State.CLOSED


class TestClosingWithCallsInFlight:
    def test_close_fails_a_blocked_caller_and_rejects_new_calls(self, fakes: _Fakes) -> None:
        held = _Held(hold_cleanup=True)
        order: list[str] = []
        sync = _make_sync()
        sync.connect()
        fakes.last.read_data.side_effect = held.run

        async def inner_close() -> None:
            order.append(
                "close after cleanup" if held.cleanup_finished.is_set() else "close too early"
            )

        fakes.last.close.side_effect = inner_close

        caller = _Call(lambda: sync.read_data("request"))  # type: ignore[arg-type]
        assert held.started.wait(_TIMEOUT)
        closer = _Call(sync.close)
        assert held.cancelled.wait(_TIMEOUT)

        # close() has begun (it is waiting on the call's cleanup): new calls are
        # rejected rather than queued behind the shutdown.
        with pytest.raises(FilamentConnectionError, match="not connected"):
            sync.get_entity_permissions()
        with pytest.raises(FilamentError, match="still closing"):
            sync.connect()
        assert closer.is_alive()
        assert order == []

        held.cleanup_release.set()
        closer.finish()
        caller.finish()

        assert closer.error is None
        assert isinstance(caller.error, FilamentConnectionError)
        assert "closed while the call was in progress" in str(caller.error)
        # The call's cancellation cleanup finished before the connection was closed.
        assert order == ["close after cleanup"]
        assert held.cancels == 1
        fakes.last.get_entity_permissions.assert_not_awaited()

    def test_concurrent_close_calls_both_wait_for_the_shutdown(self, fakes: _Fakes) -> None:
        held = _Held(hold_cleanup=True)
        sync = _make_sync()
        sync.connect()
        fakes.last.read_data.side_effect = held.run
        caller = _Call(lambda: sync.read_data("request"))  # type: ignore[arg-type]
        assert held.started.wait(_TIMEOUT)

        # Signalled each time a close() reaches the point where it waits for the
        # worker, so the test knows both callers are really inside close().
        waiting = threading.Semaphore(0)
        wait_finished = FilamentClientSync._wait_finished

        def tracked(self: FilamentClientSync, attempt: _Attempt) -> None:
            waiting.release()
            wait_finished(self, attempt)

        with patch.object(FilamentClientSync, "_wait_finished", tracked):
            first = _Call(sync.close)
            second = _Call(sync.close)
            assert waiting.acquire(timeout=_TIMEOUT)
            assert waiting.acquire(timeout=_TIMEOUT)

            # The worker cannot finish while the call's cleanup is held, so neither
            # close() may have returned.
            assert first.is_alive()
            assert second.is_alive()
            fakes.last.close.assert_not_awaited()

            held.cleanup_release.set()
            for call in (first, second, caller):
                call.finish()

        assert first.error is None
        assert second.error is None
        fakes.last.close.assert_awaited_once_with()
        assert sync._state is _State.CLOSED

    def test_interrupted_caller_cancels_its_call(self, fakes: _Fakes) -> None:
        held = _Held()
        with _make_sync() as sync:
            fakes.last.read_data.side_effect = held.run

            with _interrupt_wait_when(held.started.is_set):
                with pytest.raises(KeyboardInterrupt):
                    sync.read_data("request")  # type: ignore[arg-type]

            assert held.cancelled.wait(_TIMEOUT)
            # Other calls are unaffected.
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

        assert held.cancels == 1

    def test_interrupt_as_the_call_is_queued_still_cancels_it(self, fakes: _Fakes) -> None:
        """Ctrl+C lands after the call was handed to the loop but before the caller
        began waiting for it; the call must not be left to run unattended."""
        held = _Held()
        with _make_sync() as sync:
            fakes.last.read_data.side_effect = held.run
            attempt = _attempt_of(sync)
            loop = attempt.loop
            assert loop is not None
            real_call_soon = loop.call_soon_threadsafe
            calls: list[Any] = []

            def interrupt_after_queueing(callback: Any, *args: Any) -> Any:
                calls.append(callback)
                handle = real_call_soon(callback, *args)
                if len(calls) == 1:
                    raise KeyboardInterrupt
                return handle

            with patch.object(loop, "call_soon_threadsafe", interrupt_after_queueing):
                with pytest.raises(KeyboardInterrupt):
                    sync.read_data("request")  # type: ignore[arg-type]

            # The cancellation was queued behind the start, and the call ended
            # without ever being released.
            assert len(calls) == 2
            _wait_until(lambda: not attempt.operations)
            assert not held.release.is_set()
            assert held.cancels == (1 if held.started.is_set() else 0)
            # The client is still usable.
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_rejected_call_queues_nothing(self, fakes: _Fakes) -> None:
        sync = _make_sync()
        sync.connect()
        loop = _attempt_of(sync).loop
        assert loop is not None
        sync.close()

        with patch.object(loop, "call_soon_threadsafe") as call_soon:
            with pytest.raises(FilamentConnectionError, match="not connected"):
                sync.read_data("request")  # type: ignore[arg-type]

        call_soon.assert_not_called()

    def test_close_does_not_cancel_an_interrupted_call_a_second_time(self, fakes: _Fakes) -> None:
        """A second cancel would cut the call's cleanup loose from the task that
        close() is waiting on."""
        held = _Held(hold_cleanup=True)
        sync = _make_sync()
        sync.connect()
        fakes.last.read_data.side_effect = held.run
        attempt = _attempt_of(sync)

        with _interrupt_wait_when(held.started.is_set):
            with pytest.raises(KeyboardInterrupt):
                sync.read_data("request")  # type: ignore[arg-type]
        assert held.cancelled.wait(_TIMEOUT)

        closer = _Call(sync.close)
        _wait_until(lambda: attempt.shutting_down)
        time.sleep(0.1)  # long enough for a wrongly issued second cancel to land
        assert closer.is_alive()
        assert held.cancels == 1
        fakes.last.close.assert_not_awaited()

        held.cleanup_release.set()
        closer.finish()

        assert held.cancels == 1
        assert held.cleanup_finished.is_set()
        fakes.last.close.assert_awaited_once_with()

    def test_close_gives_up_on_cleanup_that_never_finishes(
        self, fakes: _Fakes, caplog: pytest.LogCaptureFixture
    ) -> None:
        held = _Held(hold_cleanup=True)  # never released
        cancels_when_closed: list[int] = []
        sync = _make_sync()
        sync.connect()
        fakes.last.read_data.side_effect = held.run

        async def inner_close() -> None:
            cancels_when_closed.append(held.cancels)

        fakes.last.close.side_effect = inner_close
        caller = _Call(lambda: sync.read_data("request"))  # type: ignore[arg-type]
        assert held.started.wait(_TIMEOUT)

        # Run on another thread so a shutdown that waits forever fails the test
        # instead of hanging it.
        with patch.object(_sync_client, "_CLOSE_GRACE_SECONDS", 0.1), caplog.at_level(
            logging.WARNING, logger="filament._sync_client"
        ):
            closer = _Call(sync.close).finish()
        caller.finish()

        assert closer.error is None
        assert isinstance(caller.error, FilamentConnectionError)
        assert "1 call(s) still cleaning up" in caplog.text
        # The drain itself cancelled the call once only; the connection was then
        # closed regardless, and loop finalisation swept up what was left.
        assert cancels_when_closed == [1]
        assert held.cancels == 2
        assert not held.cleanup_finished.is_set()
        assert not _worker_threads()

    def test_call_cancelled_before_its_first_step_still_completes(self, fakes: _Fakes) -> None:
        ran: list[str] = []

        async def body() -> str:
            ran.append("ran")
            return "done"

        with _make_sync() as sync:
            attempt = _attempt_of(sync)
            loop = attempt.loop
            assert loop is not None
            operation: _Operation[str] = _Operation()

            def start_then_cancel() -> None:
                sync._start_operation(attempt, operation, lambda _client: body())
                sync._cancel_operation(operation)

            loop.call_soon_threadsafe(start_then_cancel)

            with pytest.raises(FilamentConnectionError, match="cancelled"):
                operation.future.result(timeout=_TIMEOUT)
            _wait_until(lambda: not attempt.operations)

        assert ran == []


class TestOverlappingLifecycleCalls:
    def test_connect_in_progress_rejects_other_calls_and_close_cancels_it(
        self, fakes: _Fakes
    ) -> None:
        held = _Held()
        fakes.configure = lambda client: setattr(client.connect, "side_effect", held.run)
        sync = _make_sync()

        connecting = _Call(sync.connect)
        assert held.started.wait(_TIMEOUT)

        with pytest.raises(FilamentError, match="already connected"):
            sync.connect()
        with pytest.raises(FilamentConnectionError, match="not connected"):
            sync.get_entity_permissions()
        assert sync.server_capabilities is None

        sync.close()
        connecting.finish()

        assert isinstance(connecting.error, FilamentConnectionError)
        assert "closed while connecting" in str(connecting.error)
        assert held.cancels == 1
        assert not _worker_threads()
        assert sync._state is _State.CLOSED

        fakes.configure = None
        with sync:
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_interrupted_connect_leaves_nothing_running(self, fakes: _Fakes) -> None:
        held = _Held()
        fakes.configure = lambda client: setattr(client.connect, "side_effect", held.run)
        sync = _make_sync()

        with _interrupt_wait_when(held.started.is_set):
            with pytest.raises(KeyboardInterrupt):
                sync.connect()

        assert held.cancels == 1
        assert not _worker_threads()
        assert sync._state is _State.CLOSED

        fakes.configure = None
        with sync:
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_interrupt_after_the_inner_connect_succeeded_still_closes_it(
        self, fakes: _Fakes
    ) -> None:
        """The interrupt lands after success was published but before connect()
        read it; the connection that did open must not be left behind."""
        sync = _make_sync()
        real_wait = concurrent.futures.wait

        def wait(futures: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
            real_wait(futures, timeout=_TIMEOUT, **kwargs)  # success is now published
            raise KeyboardInterrupt

        with patch("filament._sync_client.concurrent.futures.wait", side_effect=wait):
            with pytest.raises(KeyboardInterrupt):
                sync.connect()

        fakes.last.connect.assert_awaited_once_with()
        fakes.last.close.assert_awaited_once_with()
        assert not _worker_threads()
        assert sync._state is _State.CLOSED

    def test_close_after_the_inner_connect_succeeded_wins(self, fakes: _Fakes) -> None:
        """close() from another thread lands between success being published and
        connect() reading it: the client must end closed, not reopened."""
        sync = _make_sync()
        real_wait = concurrent.futures.wait

        def wait(futures: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
            result = real_wait(futures, timeout=_TIMEOUT, **kwargs)  # success published
            closer = _Call(sync.close).finish()
            assert closer.error is None
            return result

        with patch("filament._sync_client.concurrent.futures.wait", side_effect=wait):
            with pytest.raises(FilamentConnectionError, match="closed while connecting"):
                sync.connect()

        first = fakes.last
        first.close.assert_awaited_once_with()
        assert not _worker_threads()
        assert sync._state is _State.CLOSED

        # A later connection is its own attempt, untouched by the stale one.
        with sync:
            assert sync.get_entity_permissions() == "get_entity_permissions:result"
        assert fakes.last is not first
        first.get_entity_permissions.assert_not_awaited()

    def test_close_before_the_worker_has_a_loop(self, fakes: _Fakes) -> None:
        hold = threading.Event()
        original = FilamentClientSync._thread_main

        def held_thread_main(self: FilamentClientSync, attempt: _Attempt) -> None:
            hold.wait(_TIMEOUT)
            original(self, attempt)

        sync = _make_sync()
        with patch.object(FilamentClientSync, "_thread_main", held_thread_main):
            connecting = _Call(sync.connect)
            _wait_until(lambda: sync._state is _State.CONNECTING)
            attempt = _attempt_of(sync)
            assert attempt.loop is None

            closer = _Call(sync.close)
            _wait_until(lambda: attempt.stop_requested)
            hold.set()
            connecting.finish()
            closer.finish()

        assert isinstance(connecting.error, FilamentConnectionError)
        assert "closed while connecting" in str(connecting.error)
        assert closer.error is None
        # The stop request was honoured before anything was connected.
        assert fakes.instances == []
        assert not _worker_threads()
        assert sync._state is _State.CLOSED


class TestInterruptedLifecycleCalls:
    """Ctrl+C landing inside connect() or close() themselves."""

    def test_interrupt_while_starting_the_worker_leaves_nothing_running(
        self, fakes: _Fakes
    ) -> None:
        """The worker thread exists but connect() never got to wait for it."""

        class InterruptedStart(threading.Thread):
            def start(self) -> None:
                super().start()
                raise KeyboardInterrupt

        sync = _make_sync()
        with patch("filament._sync_client.threading.Thread", InterruptedStart):
            with pytest.raises(KeyboardInterrupt):
                sync.connect()

        assert not _worker_threads()
        assert sync._state is _State.CLOSED
        assert sync._attempt is None
        # Whether or not the worker got as far as creating a client, none is left open.
        for instance in fakes.instances:
            instance.close.assert_awaited_once_with()

        with sync:
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_close_retried_after_an_interrupt_before_the_stop_request(
        self, fakes: _Fakes
    ) -> None:
        """The first close() marked the client closing but never told the worker."""
        sync = _make_sync()
        sync.connect()

        with patch.object(sync, "_request_stop_locked", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                sync.close()
        assert sync._state is _State.CLOSING
        assert len(_worker_threads()) == 1

        retry = _Call(sync.close).finish()

        assert retry.error is None
        fakes.last.close.assert_awaited_once_with()
        assert sync._state is _State.CLOSED
        assert not _worker_threads()

    def test_close_retried_after_an_interrupted_wait_still_waits_for_the_worker(
        self, fakes: _Fakes
    ) -> None:
        held = _Held(hold_cleanup=True)
        sync = _make_sync()
        sync.connect()
        fakes.last.read_data.side_effect = held.run
        attempt = _attempt_of(sync)
        caller = _Call(lambda: sync.read_data("request"))  # type: ignore[arg-type]
        assert held.started.wait(_TIMEOUT)

        with patch.object(attempt.finished, "wait", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                sync.close()
        assert held.cancelled.wait(_TIMEOUT)

        # Signalled when the retry reaches its wait for the worker, so the test
        # knows the retry is really waiting before it lets the worker finish.  A
        # retry that skipped the wait would never signal.
        retry_waiting = threading.Event()
        real_wait = attempt.finished.wait

        def observed_wait(timeout: Optional[float] = None) -> bool:
            retry_waiting.set()
            return real_wait(timeout)

        with patch.object(attempt.finished, "wait", observed_wait):
            retry = _Call(sync.close)
            assert retry_waiting.wait(_TIMEOUT)

            # The worker is still cleaning up, so the retry must not report it done
            # or let the client be connected again.
            assert retry.is_alive()
            assert sync._state is _State.CLOSING
            with pytest.raises(FilamentError, match="still closing"):
                sync.connect()
            fakes.last.close.assert_not_awaited()

            held.cleanup_release.set()
            retry.finish()
        caller.finish()

        assert retry.error is None
        assert held.cancels == 1
        fakes.last.close.assert_awaited_once_with()
        assert sync._state is _State.CLOSED


class TestWorkerStartupFailure:
    def test_thread_that_cannot_start(self, fakes: _Fakes) -> None:
        class Unstartable(threading.Thread):
            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        sync = _make_sync()
        with patch("filament._sync_client.threading.Thread", Unstartable):
            with pytest.raises(RuntimeError, match="can't start new thread"):
                sync.connect()

        assert sync._state is _State.CLOSED
        assert sync._attempt is None
        with sync:
            assert sync.get_entity_permissions() == "get_entity_permissions:result"

    def test_worker_that_exits_without_reporting(self, fakes: _Fakes) -> None:
        sync = _make_sync()

        with patch.object(FilamentClientSync, "_thread_main", lambda self, attempt: None):
            with pytest.raises(FilamentConnectionError, match="exited before connecting"):
                sync.connect()

        assert sync._state is _State.CLOSED
        assert fakes.instances == []

    def test_event_loop_that_cannot_be_created(self, fakes: _Fakes) -> None:
        """asyncio.run() fails before it starts the coroutine it was given; the
        error must surface without that coroutine being reported as never awaited."""
        sync = _make_sync()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with patch(
                "asyncio.events.new_event_loop", side_effect=OSError("too many open files")
            ):
                with pytest.raises(OSError, match="too many open files"):
                    sync.connect()
            gc.collect()

        assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []
        assert sync._state is _State.CLOSED
        assert fakes.instances == []
        assert not _worker_threads()


# ---------------------------------------------------------------------------
# Real inner client, faked transport
# ---------------------------------------------------------------------------

_SUCCESS = {"success": True, "message": "", "errorCode": 0, "errorId": None}


class _Hub:
    """Shared script and record for the fake SignalR transport."""

    def __init__(self) -> None:
        self.opens = True
        self.run_error: Optional[Exception] = None
        # Hub methods answered immediately; anything else stays pending.
        self.replies: dict[str, Any] = {"ClientHello": _SUCCESS}
        self.sent: dict[str, threading.Event] = defaultdict(threading.Event)
        self.pending: dict[str, Any] = {}
        self.run_started = threading.Event()
        self.events: list[str] = []

    def __call__(self, **_kwargs: Any) -> _FakeSignalR:
        return _FakeSignalR(self)


class _FakeSignalR:
    """Stands in for pysignalr's ``SignalRClient``."""

    def __init__(self, hub: _Hub) -> None:
        self._hub = hub
        self._open_callback: Any = None

    def on(self, _event: str, _handler: Any) -> None:
        pass

    def on_error(self, _callback: Any) -> None:
        pass

    def on_close(self, _callback: Any) -> None:
        pass

    def on_open(self, callback: Any) -> None:
        self._open_callback = callback

    async def run(self) -> None:
        self._hub.run_started.set()
        try:
            if self._hub.run_error is not None:
                raise self._hub.run_error
            if self._hub.opens:
                await self._open_callback()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._hub.events.append("transport closed")
            raise

    async def send(self, method: str, _arguments: Any, on_invocation: Any = None) -> None:
        if method in self._hub.replies:
            await on_invocation(
                CompletionMessage(invocation_id="1", result=self._hub.replies[method])
            )
        else:
            self._hub.pending[method] = on_invocation
        self._hub.sent[method].set()


@pytest.fixture
def hub() -> Iterator[_Hub]:
    hub = _Hub()
    with patch("filament._client.SignalRClient", hub):
        yield hub


@contextmanager
def _upload_blocked_in_transfer(hub: _Hub) -> Iterator[threading.Event]:
    """Make ``upload_file`` get its grant, then block in the storage transfer.

    Yields an event set once the transfer has started.  ``hub.events`` records
    when the reservation release finishes and when the HTTP client is closed.
    """
    transfer_started = threading.Event()
    real_http_client = httpx.AsyncClient
    real_cancel = FilamentClient.cancel_file_upload

    async def blocked(_request: httpx.Request) -> httpx.Response:
        transfer_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    class RecordingHttpClient(real_http_client):  # type: ignore[valid-type,misc]
        async def aclose(self) -> None:
            await super().aclose()
            hub.events.append("http client closed")

    def make_http_client() -> httpx.AsyncClient:
        return RecordingHttpClient(transport=httpx.MockTransport(blocked))

    async def cancel_file_upload(self: FilamentClient, file_id: Any) -> Any:
        result = await real_cancel(self, file_id)
        hub.events.append("reservation released")
        return result

    grant = AsyncMock(return_value=_ok(_grant(uuid7())))
    with patch.object(FilamentClient, "request_file_upload", grant), patch.object(
        FilamentClient, "cancel_file_upload", cancel_file_upload
    ), patch("filament._client.httpx.AsyncClient", make_http_client):
        yield transfer_started


def _interrupted_upload(sync: FilamentClientSync, transfer_started: threading.Event) -> None:
    with _interrupt_wait_when(transfer_started.is_set):
        with pytest.raises(KeyboardInterrupt):
            sync.upload_file(
                name="f",
                content_type="text/plain",
                data=b"x",
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Attachment",
            )


class TestWithRealInnerClient:
    """The real ``FilamentClient`` on the loop thread; only the transports are fake."""

    def test_round_trip_through_the_real_client(self, hub: _Hub) -> None:
        hub.replies["CancelFileUpload"] = _SUCCESS
        file_id = uuid7()

        with _make_sync() as sync:
            result = sync.cancel_file_upload(file_id)
            assert len(_worker_threads()) == 1

        assert result.success
        assert hub.sent["ClientHello"].is_set()
        assert hub.sent["CancelFileUpload"].is_set()
        assert hub.events == ["transport closed"]

    def test_close_during_a_pending_hub_invocation(self, hub: _Hub) -> None:
        sync = _make_sync()
        sync.connect()
        caller = _Call(sync.get_entity_permissions)
        assert hub.sent["GetEntityPermissions"].wait(_TIMEOUT)

        sync.close()
        caller.finish()

        assert isinstance(caller.error, FilamentConnectionError)
        assert hub.events == ["transport closed"]

    def test_interrupted_upload_finishes_releasing_its_reservation_before_close(
        self, hub: _Hub
    ) -> None:
        sync = _make_sync()
        sync.connect()
        loop = _attempt_of(sync).loop
        assert loop is not None

        with _upload_blocked_in_transfer(hub) as transfer_started:
            _interrupted_upload(sync, transfer_started)
            # The cancelled upload is now releasing its reservation over the hub,
            # and the server has not answered yet.
            assert hub.sent["CancelFileUpload"].wait(_TIMEOUT)

            closer = _Call(sync.close)
            time.sleep(0.2)
            assert closer.is_alive(), "close() did not wait for the release"
            assert hub.events == []

            def reply() -> None:
                loop.create_task(
                    hub.pending["CancelFileUpload"](
                        CompletionMessage(invocation_id="1", result=_SUCCESS)
                    )
                )

            loop.call_soon_threadsafe(reply)
            closer.finish()

        assert closer.error is None
        assert hub.events == ["reservation released", "http client closed", "transport closed"]

    def test_close_is_not_held_up_by_a_release_that_never_replies(
        self, hub: _Hub, caplog: pytest.LogCaptureFixture
    ) -> None:
        sync = _make_sync()
        sync.connect()

        with _upload_blocked_in_transfer(hub) as transfer_started:
            _interrupted_upload(sync, transfer_started)
            assert hub.sent["CancelFileUpload"].wait(_TIMEOUT)

            with patch.object(_sync_client, "_CLOSE_GRACE_SECONDS", 0.2):
                with caplog.at_level(logging.WARNING, logger="filament._sync_client"):
                    closer = _Call(sync.close).finish()

        assert closer.error is None
        assert "still cleaning up" in caplog.text
        assert "transport closed" in hub.events
        assert "reservation released" not in hub.events
        assert hub.events.index("transport closed") == 0

    def test_failed_connect_leaves_nothing_running(self, hub: _Hub) -> None:
        hub.run_error = RuntimeError("no route to host")
        sync = _make_sync()

        with pytest.raises(FilamentConnectionError, match="no route to host"):
            sync.connect()

        assert not _worker_threads()
        assert sync._state is _State.CLOSED

        hub.run_error = None
        with sync:
            assert len(_worker_threads()) == 1

    def test_authentication_failure_surfaces_from_connect(self, hub: _Hub) -> None:
        sync = _make_sync()
        original = FilamentClient._on_open

        async def rejected_on_open(self: FilamentClient) -> None:
            await self._handle_authentication_failed("bad key")
            await original(self)

        with patch.object(FilamentClient, "_on_open", rejected_on_open):
            with pytest.raises(FilamentAuthenticationError, match="bad key"):
                sync.connect()

        assert hub.events == ["transport closed"]
        assert not _worker_threads()

    def test_interrupted_connect_stops_the_transport(self, hub: _Hub) -> None:
        hub.opens = False
        sync = _make_sync()

        with _interrupt_wait_when(hub.run_started.is_set):
            with pytest.raises(KeyboardInterrupt):
                sync.connect()

        assert hub.events == ["transport closed"]
        assert not _worker_threads()
        assert sync._state is _State.CLOSED

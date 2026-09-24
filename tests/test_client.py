"""Tests for the FilamentClient class.

These tests mock the pysignalr SignalRClient to verify method dispatch,
serialization, and subscription routing without a live server.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pysignalr.exceptions import HubError
from pysignalr.messages import CompletionMessage
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament._exceptions import (
    FilamentAuthenticationError,
    FilamentConnectionError,
    ResponseTooLargeError,
)
from filament._serialization import from_dict
from filament.types.generated.entities.core import FileEntity
from filament.types.generated.enums import (
    DataProcessingStatus,
    ErrorCodes,
    FileChecksumAlgorithm,
    FileState,
    ReadMode,
    SubscriptionMode,
)
from filament.types.generated.misc import (
    DataUpdateRejectedNotification,
    EntityPermissions,
    EntityServiceDataUpdate,
    FileUploadRequest,
    ProcessingStatus,
    ReadDataRequest,
    ReadDataTypeRequest,
    RequestId,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionId,
    UploadGrant,
    ValueOrErrorPublic_1,
)
from filament.types.generated.shared import FileReference


def _make_client(*, mock_invoke: bool = True, **kwargs: Any) -> FilamentClient:
    """Create a FilamentClient with mocked SignalR internals.

    By default also stubs ``_invoke`` with an :class:`AsyncMock` so callers can
    drive API methods without a live hub.  Pass ``mock_invoke=False`` to keep
    the real implementation (needed when the test itself drives ``_invoke``'s
    pending-future machinery).
    """
    with patch("filament._client.SignalRClient"):
        client = FilamentClient(
            organization_id="test-org",
            api_key_name="test-key",
            api_key="test-secret",
            **kwargs,
        )
        if mock_invoke:
            client._invoke = AsyncMock()
        # Existing client tests predate UDP discovery and focus on the hub method
        # under test.  Prime the two fixture types with authoritative empty maps;
        # dedicated UDP tests below exercise discovery and maintenance itself.
        client._user_defined_properties.update({"UserEntity": {}, "TestEntity": {}})
        client._user_defined_subscription_types = frozenset({"UserEntity", "TestEntity"})
        client._user_defined_subscription_id = uuid7()
        return client


async def _deliver(client: FilamentClient, data: Any) -> None:
    """Feed one ``DataUpdated`` frame and wait for the dispatch worker to drain it."""
    await client._handle_data_updated(data)
    worker = client._data_update_worker
    if worker is not None:
        await worker


async def _make_subscription(
    client: FilamentClient,
    backend_id: UUID,
    *,
    callback: Any = None,
    on_error: Any = None,
) -> tuple[UUID, MagicMock]:
    """Register a subscription for *backend_id*; return ``(client_id, callback)``.

    Mocks ``client._invoke`` to return a successful Subscribe response carrying
    *backend_id*.  When *callback* is not provided, a fresh :class:`MagicMock`
    is created so callers that only care about the registration can assert on
    it later.
    """
    client._invoke = AsyncMock(return_value={
        "success": True,
        "message": "",
        "errorCode": 0,
        "data": {"id": str(backend_id)},
    })
    if callback is None:
        callback = MagicMock()
    result = await client.subscribe(
        SubscriptionConfig(
            scenario_id=UUID(int=0),
            status=DataProcessingStatus.Pending,
            type_name="TestEntity",
            mode=SubscriptionMode.Data,
        ),
        callback=callback,
        on_error=on_error,
    )
    assert result.data is not None
    return result.data.id, callback


class TestClientConstruction:
    def test_default_url(self) -> None:
        with patch("filament._client.SignalRClient") as mock_cls:
            FilamentClient(organization_id="org", api_key_name="k", api_key="s")
            call_kwargs = mock_cls.call_args
            assert call_kwargs is not None
            assert call_kwargs[1]["url"] == "https://backend.filament-software.com:443/api/v0"

    def test_custom_url(self) -> None:
        with patch("filament._client.SignalRClient") as mock_cls:
            FilamentClient(
                organization_id="org",
                api_key_name="k",
                api_key="s",
                server="https://dev.example.com",
                port=9999,
                path="api/v1",
            )
            call_kwargs = mock_cls.call_args
            assert call_kwargs is not None
            assert call_kwargs[1]["url"] == "https://dev.example.com:9999/api/v1"

    def test_registers_event_handlers(self) -> None:
        with patch("filament._client.SignalRClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            FilamentClient(organization_id="org", api_key_name="k", api_key="s")
            registered = {call[0][0] for call in mock_client.on.call_args_list}
            assert "DataUpdated" in registered
            assert "DataUpdateRejected" in registered
            assert "ProcessingStarted" in registered
            assert "ProcessingCompleted" in registered
            assert "ProcessingFailed" in registered
            assert "AuthenticationFailed" in registered
            assert "KeepAlive" in registered

    def test_headers_passed(self) -> None:
        with patch("filament._client.SignalRClient") as mock_cls:
            FilamentClient(
                organization_id="my-org",
                api_key_name="my-key",
                api_key="my-secret",
            )
            call_kwargs = mock_cls.call_args
            assert call_kwargs is not None
            headers = call_kwargs[1]["headers"]
            # Single Authorization header with base64-encoded token
            import base64
            expected_token = base64.b64encode(b"my-key:my-org:my-secret").decode()
            assert headers["Authorization"] == f"ApiKey {expected_token}"

    def test_default_max_size_is_1mib(self) -> None:
        with patch("filament._client.SignalRClient") as mock_cls:
            FilamentClient(organization_id="org", api_key_name="k", api_key="s")
            call_kwargs = mock_cls.call_args
            assert call_kwargs is not None
            assert call_kwargs[1]["max_size"] == 2**20

    def test_explicit_max_size_forwarded(self) -> None:
        with patch("filament._client.SignalRClient") as mock_cls:
            FilamentClient(
                organization_id="org",
                api_key_name="k",
                api_key="s",
                max_size=4 * 2**20,
            )
            call_kwargs = mock_cls.call_args
            assert call_kwargs is not None
            assert call_kwargs[1]["max_size"] == 4 * 2**20

    def test_max_size_none_forwarded(self) -> None:
        """Passing None opts out of the limit (unlimited message size)."""
        with patch("filament._client.SignalRClient") as mock_cls:
            FilamentClient(
                organization_id="org",
                api_key_name="k",
                api_key="s",
                max_size=None,
            )
            call_kwargs = mock_cls.call_args
            assert call_kwargs is not None
            assert call_kwargs[1]["max_size"] is None


class TestReadData:
    @pytest.mark.asyncio
    async def test_read_data_invokes_hub(self) -> None:
        client = _make_client()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {
                "typeName": "UserEntity",
                "data": [],
                "entityCount": 0,
                "moreData": False,
            },
        })

        request = ReadDataRequest(
            scenario_id=UUID(int=0),
            request=ReadDataTypeRequest(type_name="UserEntity"),
        )
        result = await client.read_data(request)

        client._invoke.assert_called_once()
        call_args = client._invoke.call_args
        assert call_args is not None
        assert call_args[0][0] == "ReadData"
        assert result.success is True


class TestSubscriptionRouting:
    @pytest.mark.asyncio
    async def test_subscribe_stores_callback(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        callback = MagicMock()
        result = await client.subscribe(
            SubscriptionConfig(
                scenario_id=UUID(int=0),
                status=DataProcessingStatus.Pending,
                type_name="TestEntity",
                mode=SubscriptionMode.Data,
            ),
            callback=callback,
        )

        assert result.success is True
        # The returned ID should be a client-generated ID, not the backend ID
        assert result.data is not None
        client_id = result.data.id
        assert client_id != backend_id
        assert client_id in client._client_callbacks
        assert client._client_callbacks[client_id] is callback
        assert backend_id in client._backend_to_client
        assert client_id in client._backend_to_client[backend_id]

    @pytest.mark.asyncio
    async def test_subscribe_converts_property_names_to_camel_case(self) -> None:
        """The ``properties`` list is sent to the hub in camelCase wire form.

        Callers pass snake_case names matching the Python dataclass fields;
        ``subscribe`` (via ``to_dict``) normalises them to the camelCase names
        the backend expects, matching every other property-name list field.
        """
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        await client.subscribe(
            SubscriptionConfig(
                scenario_id=UUID(int=0),
                status=DataProcessingStatus.Pending,
                type_name="TestEntity",
                mode=SubscriptionMode.Data,
                properties=["display_name", "status", "scenario_id"],
            ),
            callback=MagicMock(),
        )

        method, subscription_dict = client._invoke.call_args[0]
        assert method == "Subscribe"
        assert subscription_dict["properties"] == [
            "displayName",
            "status",
            "scenarioId",
        ]

    @pytest.mark.asyncio
    async def test_subscribe_omits_no_properties_narrowing(self) -> None:
        """A ``None`` (default) property set is sent as ``null`` = all properties."""
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        await client.subscribe(
            SubscriptionConfig(
                scenario_id=UUID(int=0),
                status=DataProcessingStatus.Pending,
                type_name="TestEntity",
                mode=SubscriptionMode.Data,
            ),
            callback=MagicMock(),
        )

        _, subscription_dict = client._invoke.call_args[0]
        assert subscription_dict["properties"] is None

    @pytest.mark.asyncio
    async def test_subscribe_multiple_same_backend_id(self) -> None:
        """Multiple subscribes returning the same backend ID get separate client IDs."""
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        sub_config = SubscriptionConfig(
            scenario_id=UUID(int=0),
            status=DataProcessingStatus.Pending,
            type_name="TestEntity",
            mode=SubscriptionMode.Data,
        )

        cb1 = MagicMock()
        cb2 = MagicMock()
        result1 = await client.subscribe(sub_config, callback=cb1)
        result2 = await client.subscribe(sub_config, callback=cb2)

        assert result1.data is not None
        assert result2.data is not None
        assert result1.data.id != result2.data.id
        assert len(client._backend_to_client[backend_id]) == 2

    @pytest.mark.asyncio
    async def test_data_updated_routes_to_all_callbacks(self) -> None:
        """DataUpdated events are dispatched to all callbacks for that backend ID."""
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        sub_config = SubscriptionConfig(
            scenario_id=UUID(int=0),
            status=DataProcessingStatus.Pending,
            type_name="TestEntity",
            mode=SubscriptionMode.Data,
        )

        cb1 = MagicMock()
        cb2 = MagicMock()
        await client.subscribe(sub_config, callback=cb1)
        await client.subscribe(sub_config, callback=cb2)

        update_data = {
            "scenarioId": str(uuid7()),
            "status": 1,
            "requestIds": [],
            "subscriptionId": {"id": str(backend_id)},
            "modifiedEntities": {},
            "progress": False,
            "progressAmount": 0.0,
            "sequenceIdx": 0,
        }
        await _deliver(client, update_data)

        cb1.assert_called_once()
        cb2.assert_called_once()
        arg = cb1.call_args[0][0]
        assert isinstance(arg, ServiceDataUpdate)
        assert arg.subscription_id.id == backend_id

    @pytest.mark.asyncio
    async def test_data_updated_ignores_unknown_subscription(self) -> None:
        client = _make_client()
        # No subscriptions registered
        update_data = {
            "scenarioId": str(uuid7()),
            "status": 1,
            "requestIds": [],
            "subscriptionId": {"id": str(uuid7())},
            "modifiedEntities": {},
            "progress": False,
            "progressAmount": 0.0,
            "sequenceIdx": 0,
        }
        # Should not raise
        await _deliver(client, update_data)

    @pytest.mark.asyncio
    async def test_unsubscribe_last_client_calls_backend(self) -> None:
        """Unsubscribing the last client for a backend ID calls backend Unsubscribe."""
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        cb = MagicMock()
        result = await client.subscribe(
            SubscriptionConfig(
                scenario_id=UUID(int=0),
                status=DataProcessingStatus.Pending,
                type_name="TestEntity",
                mode=SubscriptionMode.Data,
            ),
            callback=cb,
        )
        assert result.data is not None
        client_id = result.data.id

        # Reset mock to track unsubscribe call
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
        })

        await client.unsubscribe(SubscriptionId(id=client_id))

        # Backend should have been called
        client._invoke.assert_called_once()
        call_args = client._invoke.call_args
        assert call_args is not None
        assert call_args[0][0] == "Unsubscribe"
        assert client_id not in client._client_callbacks
        assert backend_id not in client._backend_to_client

    @pytest.mark.asyncio
    async def test_unsubscribe_non_last_client_skips_backend(self) -> None:
        """Unsubscribing when other clients remain does NOT call backend."""
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        sub_config = SubscriptionConfig(
            scenario_id=UUID(int=0),
            status=DataProcessingStatus.Pending,
            type_name="TestEntity",
            mode=SubscriptionMode.Data,
        )

        cb1 = MagicMock()
        cb2 = MagicMock()
        result1 = await client.subscribe(sub_config, callback=cb1)
        result2 = await client.subscribe(sub_config, callback=cb2)
        assert result1.data is not None
        assert result2.data is not None

        # Reset mock to track unsubscribe
        client._invoke = AsyncMock()

        await client.unsubscribe(SubscriptionId(id=result1.data.id))

        # Backend should NOT have been called
        client._invoke.assert_not_called()
        # Second callback should still be registered
        assert result2.data.id in client._client_callbacks
        assert len(client._backend_to_client[backend_id]) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_all_clears_callbacks(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_id)},
        })

        sub_config = SubscriptionConfig(
            scenario_id=UUID(int=0),
            status=DataProcessingStatus.Pending,
            type_name="TestEntity",
            mode=SubscriptionMode.Data,
        )
        await client.subscribe(sub_config, callback=MagicMock())
        await client.subscribe(sub_config, callback=MagicMock())

        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
        })

        await client.unsubscribe_all()
        assert len(client._client_callbacks) == 0
        assert len(client._backend_to_client) == 0
        assert len(client._client_to_backend) == 0


class TestEventCallbacks:
    @pytest.mark.asyncio
    async def test_processing_started_callback(self) -> None:
        client = _make_client()
        callback = MagicMock()
        client.on_processing_started(callback)

        status_data = {
            "scenarioId": str(uuid7()),
            "requestIds": [],
            "message": "started",
        }
        await client._handle_processing_started(status_data)

        callback.assert_called_once()
        arg = callback.call_args[0][0]
        assert isinstance(arg, ProcessingStatus)
        assert arg.message == "started"

    @pytest.mark.asyncio
    async def test_async_callback(self) -> None:
        client = _make_client()
        async_callback = AsyncMock()
        client.on_processing_completed(async_callback)

        status_data = {
            "scenarioId": str(uuid7()),
            "requestIds": [],
            "message": "done",
        }
        await client._handle_processing_completed(status_data)
        async_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_callbacks(self) -> None:
        client = _make_client()
        cb1 = MagicMock()
        cb2 = MagicMock()
        client.on_processing_failed(cb1)
        client.on_processing_failed(cb2)

        status_data = {
            "scenarioId": str(uuid7()),
            "requestIds": [],
            "message": "error",
        }
        await client._handle_processing_failed(status_data)

        cb1.assert_called_once()
        cb2.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_callable_object(self) -> None:
        """Callable objects with async __call__ are treated as sync by _invoke_callback.

        NOTE: inspect.iscoroutinefunction() returns False for callable objects even when
        their __call__ method is async — it only returns True for async functions and
        coroutine methods checked directly. As a result, _invoke_callback calls the
        object synchronously, which means the returned coroutine is never awaited.

        This test documents the current (limited) behavior: the callable object IS
        called (call_count increments), but the async body does not execute because
        the coroutine is not awaited. Sync callable objects work correctly.
        """
        client = _make_client()
        call_count = 0

        class SyncCallableCallback:
            def __call__(self, status: ProcessingStatus) -> None:
                nonlocal call_count
                call_count += 1

        callback = SyncCallableCallback()
        client.on_processing_started(callback)

        status_data = {
            "scenarioId": str(uuid7()),
            "requestIds": [],
            "message": "test",
        }
        await client._handle_processing_started(status_data)

        # Sync callable objects are handled correctly.
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_callback_exception_isolation(self) -> None:
        """One callback raising should not prevent others from executing."""
        client = _make_client()

        def failing_callback(status: ProcessingStatus) -> None:
            raise ValueError("Intentional test failure")

        successful_callback = MagicMock()

        client.on_processing_started(failing_callback)
        client.on_processing_started(successful_callback)

        status_data = {
            "scenarioId": str(uuid7()),
            "requestIds": [],
            "message": "test",
        }
        await client._handle_processing_started(status_data)
        successful_callback.assert_called_once()


class TestConnectionClose:
    """Connection-drop handling for in-flight invocations."""

    @pytest.mark.asyncio
    async def test_invoke_registers_and_clears_pending_future(self) -> None:
        """_invoke should add its future to the pending set and remove it on completion."""
        client = _make_client(mock_invoke=False)

        captured: dict[str, Any] = {}

        async def fake_send(method: str, args: list[Any], on_invocation: Any) -> None:
            captured["on_invocation"] = on_invocation

        client._signalr_client.send = AsyncMock(side_effect=fake_send)

        async def complete_later() -> None:
            # Yield until _invoke has reached `await future` and registered it.
            for _ in range(10):
                if client._pending_invocations:
                    break
                await asyncio.sleep(0)
            assert len(client._pending_invocations) == 1
            await captured["on_invocation"](
                CompletionMessage(invocation_id="x", result={"ok": True}),
            )

        completer = asyncio.create_task(complete_later())
        result = await client._invoke("SomeMethod", "arg")
        await completer

        assert result == {"ok": True}
        # Future is discarded once completed.
        assert len(client._pending_invocations) == 0

    @pytest.mark.asyncio
    async def test_close_fails_pending_invocations(self) -> None:
        """_handle_close should fail every awaiting _invoke with FilamentConnectionError.

        Simulates the 1009 close case: send() succeeds (the request goes out),
        but the server never replies because the websocket has been torn down.
        Without on_close handling, the caller would hang on `await future`.
        """
        client = _make_client(mock_invoke=False)

        # send() returns immediately (request sent fine) but the completion
        # callback is never invoked, mimicking a dropped websocket.
        client._signalr_client.send = AsyncMock(return_value=None)

        invoke_task = asyncio.create_task(client._invoke("SomeMethod"))
        # Yield until _invoke has reached `await future`.
        for _ in range(10):
            if client._pending_invocations:
                break
            await asyncio.sleep(0)
        assert len(client._pending_invocations) == 1

        await client._handle_close()

        with pytest.raises(FilamentConnectionError):
            await invoke_task
        # Done-callback drains the pending set.
        assert client._pending_invocations == set()

    @pytest.mark.asyncio
    async def test_close_with_no_pending_is_noop(self) -> None:
        """_handle_close is safe to call when no invocations are in flight."""
        client = _make_client()
        # Should not raise.
        await client._handle_close()
        assert client._pending_invocations == set()

    def test_close_handler_registered(self) -> None:
        """FilamentClient should register an on_close handler with SignalRClient."""
        with patch("filament._client.SignalRClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            FilamentClient(organization_id="org", api_key_name="k", api_key="s")
            mock_client.on_close.assert_called_once()

    @staticmethod
    async def _start_pending_invoke(client: FilamentClient) -> asyncio.Task[Any]:
        """Start an ``_invoke`` whose completion never arrives, and wait until it is pending."""
        client._signalr_client.send = AsyncMock(return_value=None)
        invoke_task = asyncio.create_task(client._invoke("SomeMethod"))
        for _ in range(10):
            if client._pending_invocations:
                break
            await asyncio.sleep(0)
        assert len(client._pending_invocations) == 1
        return invoke_task

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("Connection is closed"),
            HubError(),
            ConnectionResetError("reset by peer"),
        ],
    )
    async def test_send_transport_failure_raises_connection_error(
        self, error: BaseException,
    ) -> None:
        """Transport errors from send() surface as FilamentConnectionError, not pysignalr's own."""
        client = _make_client(mock_invoke=False)
        client._signalr_client.send = AsyncMock(side_effect=error)

        with pytest.raises(FilamentConnectionError, match="SignalR send failed") as exc_info:
            await client._invoke("SomeMethod")

        assert exc_info.value.__cause__ is error
        # The cancelled future's done-callback runs on the next loop iteration.
        await asyncio.sleep(0)
        assert client._pending_invocations == set()

    @pytest.mark.asyncio
    async def test_server_error_names_method(self) -> None:
        """A completion error is a bare Exception prefixed with the hub method name."""
        client = _make_client(mock_invoke=False)

        async def fake_send(method: str, args: list[Any], on_invocation: Any) -> None:
            await on_invocation(
                CompletionMessage(invocation_id="x", error="Sequence contains no elements"),
            )

        client._signalr_client.send = AsyncMock(side_effect=fake_send)

        with pytest.raises(Exception) as exc_info:
            await client._invoke("ReadData")

        assert type(exc_info.value) is Exception
        assert str(exc_info.value) == "ReadData: Sequence contains no elements"

    @pytest.mark.asyncio
    async def test_close_error_includes_close_code(self) -> None:
        """The close code and reason from the last connection travel in the failure message."""
        client = _make_client(mock_invoke=False)
        client._last_connection = MagicMock(close_code=1009, close_reason="message too big")
        invoke_task = await self._start_pending_invoke(client)

        await client._handle_close()

        with pytest.raises(FilamentConnectionError, match=r"close 1009 \(message too big\)"):
            await invoke_task

    def test_close_detail_without_connection(self) -> None:
        """With no recorded connection (or no close code), the detail degrades to a fixed string."""
        client = _make_client()
        assert client._close_detail() == "close code unavailable"
        client._last_connection = MagicMock(close_code=None)
        assert client._close_detail() == "close code unavailable"
        client._last_connection = MagicMock(close_code=1000, close_reason="")
        assert client._close_detail() == "close 1000"

    @pytest.mark.asyncio
    async def test_reconnect_fails_pending_invocations(self) -> None:
        """_on_open fails invocations orphaned by a reconnect and records the new websocket."""
        client = _make_client(mock_invoke=False)
        websocket = MagicMock()
        client._signalr_client._transport._ws = websocket
        invoke_task = await self._start_pending_invoke(client)

        await client._on_open()

        with pytest.raises(FilamentConnectionError, match="re-established"):
            await invoke_task
        assert client._last_connection is websocket

    @pytest.mark.asyncio
    async def test_send_waiting_for_connection_survives_reconnect(self) -> None:
        """An invocation queued in send() while disconnected is not failed by the reconnect.

        pysignalr's send() blocks until a connection is available; on reconnect it
        resumes after on_open fires and writes to the new socket, so failing it
        there would report an error for a request the server actually received.
        """
        client = _make_client(mock_invoke=False)
        connected = asyncio.Event()
        captured: dict[str, Any] = {}

        async def send_after_reconnect(method: str, args: list[Any], on_invocation: Any) -> None:
            await connected.wait()
            captured["on_invocation"] = on_invocation

        client._signalr_client.send = AsyncMock(side_effect=send_after_reconnect)
        await client._handle_close()
        invoke_task = asyncio.create_task(client._invoke("SomeMethod"))
        await asyncio.sleep(0)

        connected.set()
        await client._on_open()
        assert not invoke_task.done()

        for _ in range(10):
            if client._pending_invocations:
                break
            await asyncio.sleep(0)
        await captured["on_invocation"](CompletionMessage(invocation_id="x", result={"ok": True}))
        assert await invoke_task == {"ok": True}

    @pytest.mark.asyncio
    async def test_close_during_send_fails_invocation(self) -> None:
        """A close that lands while send() is still running fails the invocation.

        send() can return normally after the socket it wrote to has closed (e.g. it
        was waiting on flow control); the close handler has then already run, so
        registering the future afterwards would leave the caller hanging forever.
        """
        client = _make_client(mock_invoke=False)
        client._last_connection = MagicMock(close_code=1000, close_reason="")

        async def send_then_close(method: str, args: list[Any], on_invocation: Any) -> None:
            await client._handle_close()

        client._signalr_client.send = AsyncMock(side_effect=send_then_close)

        with pytest.raises(FilamentConnectionError, match=r"closed before .*\(close 1000\)"):
            await asyncio.wait_for(client._invoke("SomeMethod"), timeout=1)
        assert client._pending_invocations == set()

    @pytest.mark.asyncio
    async def test_send_failure_includes_close_detail(self) -> None:
        """A send failure after a drop names the close code of the last connection."""
        client = _make_client(mock_invoke=False)
        client._last_connection = MagicMock(close_code=1009, close_reason="message too big")
        client._signalr_client.send = AsyncMock(side_effect=RuntimeError("Connection is closed"))

        with pytest.raises(FilamentConnectionError, match=r"close 1009 \(message too big\)"):
            await client._invoke("SomeMethod")

    @pytest.mark.asyncio
    async def test_run_task_ending_fails_pending_invocations(self) -> None:
        """A run loop that dies without firing on_close still fails in-flight invocations."""
        client = _make_client(mock_invoke=False)
        invoke_task = await self._start_pending_invoke(client)

        async def failing_run() -> None:
            raise RuntimeError("transport exploded")

        run_task: asyncio.Task[None] = asyncio.create_task(failing_run())
        run_task.add_done_callback(client._handle_run_task_done)

        with pytest.raises(FilamentConnectionError, match="transport exploded"):
            await invoke_task

    @pytest.mark.asyncio
    async def test_aexit_swallows_run_loop_error(self) -> None:
        """Closing never re-raises an error from an already-broken transport."""
        client = _make_client()

        async def failing_run() -> None:
            raise HubError()

        client._run_task = asyncio.create_task(failing_run())
        await asyncio.sleep(0)

        # Should not raise.
        await client.__aexit__(None, None, None)


class TestClientHello:
    """ClientHello capability handshake on connect."""

    @pytest.mark.asyncio
    async def test_send_client_hello_uses_configured_max_size(self) -> None:
        """_send_client_hello must invoke ClientHello with the configured max_size."""
        client = _make_client(max_size=4 * 2**20)
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
        })

        await client._send_client_hello()

        client._invoke.assert_called_once()
        method, payload = client._invoke.call_args[0]
        assert method == "ClientHello"
        assert payload == {"maxMessageSizeBytes": 4 * 2**20}

    @pytest.mark.asyncio
    async def test_send_client_hello_passes_none_for_unlimited(self) -> None:
        client = _make_client(max_size=None)
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
        })

        await client._send_client_hello()

        method, payload = client._invoke.call_args[0]
        assert method == "ClientHello"
        assert payload == {"maxMessageSizeBytes": None}

    @pytest.mark.asyncio
    async def test_send_client_hello_swallows_method_not_found(self) -> None:
        """Older backends without ClientHello must not break the connection."""
        client = _make_client()
        client._invoke = AsyncMock(
            side_effect=Exception("Method 'ClientHello' not found")
        )

        # Should not raise — just log and continue.
        await client._send_client_hello()

    @pytest.mark.asyncio
    async def test_send_client_hello_reraises_connection_error(self) -> None:
        """Connection drops during ClientHello must surface, not be swallowed."""
        client = _make_client()
        client._invoke = AsyncMock(side_effect=FilamentConnectionError("dropped"))

        with pytest.raises(FilamentConnectionError):
            await client._send_client_hello()

    @pytest.mark.asyncio
    async def test_send_client_hello_reraises_filament_errors(self) -> None:
        """Any FilamentError subclass during ClientHello must surface, not be swallowed."""
        client = _make_client()
        client._invoke = AsyncMock(side_effect=FilamentAuthenticationError("unauthorised"))

        with pytest.raises(FilamentAuthenticationError):
            await client._send_client_hello()

    @pytest.mark.asyncio
    async def test_send_client_hello_times_out(self) -> None:
        """A ClientHello that never completes raises FilamentConnectionError after timeout."""
        client = _make_client(connection_timeout=0.05)

        async def hang_forever(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(10)

        client._invoke = AsyncMock(side_effect=hang_forever)

        with pytest.raises(FilamentConnectionError, match="ClientHello did not complete"):
            await client._send_client_hello()

    @pytest.mark.asyncio
    async def test_capabilities_default_none_before_connect(self) -> None:
        client = _make_client()
        assert client.server_capabilities is None

    @pytest.mark.asyncio
    async def test_send_client_hello_captures_server_capabilities(self) -> None:
        client = _make_client()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {
                "files": {
                    "checksumAlgorithm": 1,
                    "serverSideChecksum": True,
                },
            },
        })

        await client._send_client_hello()

        caps = client.server_capabilities
        assert caps is not None
        assert caps.files is not None
        assert caps.files.checksum_algorithm == FileChecksumAlgorithm.Sha256Hex
        assert caps.files.server_side_checksum is True

    @pytest.mark.asyncio
    async def test_send_client_hello_no_file_store_leaves_files_none(self) -> None:
        client = _make_client()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {"files": None},
        })

        await client._send_client_hello()

        caps = client.server_capabilities
        assert caps is not None
        assert caps.files is None

    @pytest.mark.asyncio
    async def test_send_client_hello_rejected_leaves_capabilities_none(self) -> None:
        client = _make_client()
        client._invoke = AsyncMock(return_value={
            "success": False,
            "message": "nope",
            "errorCode": 0,
            "errorId": None,
            "data": None,
        })

        await client._send_client_hello()

        assert client.server_capabilities is None

    @pytest.mark.asyncio
    async def test_send_client_hello_survives_a_scalar_response(self) -> None:
        # A bare scalar reaches from_dict's data.get(...) and raises
        # AttributeError.  ClientHello is optional, so this must log and
        # continue — if it escaped, __aenter__ would fail the whole connection.
        client = _make_client()
        client._invoke = AsyncMock(return_value="success")

        await client._send_client_hello()

        assert client.server_capabilities is None

    @pytest.mark.asyncio
    async def test_send_client_hello_older_server_leaves_capabilities_none(self) -> None:
        client = _make_client()
        client._invoke = AsyncMock(
            side_effect=Exception("Method 'ClientHello' not found")
        )

        await client._send_client_hello()

        assert client.server_capabilities is None

    @pytest.mark.asyncio
    async def test_reconnect_clears_stale_capabilities(self) -> None:
        # A reused client must not carry the previous connection's capabilities
        # into a handshake that was rejected — upload_file would otherwise
        # checksum with an algorithm this server never advertised.
        client = _make_client()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {
                "files": {
                    "checksumAlgorithm": 1,
                    "serverSideChecksum": True,
                },
            },
        })

        await client._send_client_hello()
        assert client.server_capabilities is not None

        client._invoke = AsyncMock(return_value={
            "success": False,
            "message": "nope",
            "errorCode": 0,
            "errorId": None,
            "data": None,
        })

        await client._send_client_hello()

        assert client.server_capabilities is None

    @pytest.mark.asyncio
    async def test_reconnect_to_older_server_clears_stale_capabilities(self) -> None:
        # Same again for the swallowed "older server has no ClientHello" path,
        # which returns before ever looking at a response.
        client = _make_client()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {
                "files": {
                    "checksumAlgorithm": 1,
                    "serverSideChecksum": True,
                },
            },
        })

        await client._send_client_hello()
        assert client.server_capabilities is not None

        client._invoke = AsyncMock(
            side_effect=Exception("Method 'ClientHello' not found")
        )

        await client._send_client_hello()

        assert client.server_capabilities is None


class TestFileMethods:
    """Thin wrappers over the file-store hub methods."""

    @pytest.mark.asyncio
    async def test_request_file_upload_invokes_hub(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {
                "fileId": str(file_id),
                "url": "https://storage.example.com/bucket",
                "method": "POST",
                "expiresAt": "2026-08-10T17:00:00.00000000Z",
                "maxBytes": 1024,
                "fields": {"key": "k", "Content-Type": "image/png"},
                "checksumAlgorithm": 1,
            },
        })

        request = FileUploadRequest(
            name="photo.png",
            content_type="image/png",
            size=1024,
            reference_type="UserEntity",
            reference_id=uuid7(),
            reference_property="Avatar",
            checksum="abc",
        )
        result = await client.request_file_upload(request)

        method, payload = client._invoke.call_args[0]
        assert method == "RequestFileUpload"
        assert payload["name"] == "photo.png"
        assert payload["contentType"] == "image/png"
        assert payload["referenceProperty"] == "Avatar"
        assert result.success is True
        assert result.data is not None
        assert result.data.file_id == file_id
        assert result.data.url == "https://storage.example.com/bucket"
        assert result.data.checksum_algorithm == FileChecksumAlgorithm.Sha256Hex

    @pytest.mark.asyncio
    async def test_refresh_file_upload_grant_passes_uuid_string(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {
                "fileId": str(file_id),
                "url": "https://s/x",
                "method": "POST",
                "expiresAt": "2026-08-10T17:00:00.00000000Z",
                "maxBytes": 10,
            },
        })

        result = await client.refresh_file_upload_grant(file_id)

        method, payload = client._invoke.call_args[0]
        assert method == "RefreshFileUploadGrant"
        assert payload == str(file_id)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_complete_file_upload_sends_completion(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {"id": str(file_id), "name": "photo.png", "state": 1},
        })

        result = await client.complete_file_upload(file_id)

        method, payload = client._invoke.call_args[0]
        assert method == "CompleteFileUpload"
        assert payload == {"fileId": str(file_id)}
        assert result.success is True
        assert result.data is not None
        assert isinstance(result.data, FileEntity)
        assert result.data.id == file_id
        assert result.data.state == FileState.Available

    @pytest.mark.asyncio
    async def test_cancel_file_upload_returns_success_or_error(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
        })

        result = await client.cancel_file_upload(file_id)

        method, payload = client._invoke.call_args[0]
        assert method == "CancelFileUpload"
        assert payload == str(file_id)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_cancel_file_upload_deserialization_error(self) -> None:
        # A malformed hub response must be turned into a failed result rather
        # than leaking a deserialization exception.
        client = _make_client()
        client._invoke = AsyncMock(return_value={"errorCode": "NotAValidCode"})

        result = await client.cancel_file_upload(uuid7())

        assert result.success is False
        assert result.error_code == ErrorCodes.InternalError

    @pytest.mark.asyncio
    async def test_get_file_urls_returns_uuid_keyed_dict(self) -> None:
        client = _make_client()
        id1, id2 = uuid7(), uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {
                str(id1): {
                    "success": True,
                    "message": "",
                    "errorCode": 0,
                    "url": "https://s/1",
                    "refreshable": True,
                    "metadata": {
                        "contentType": "image/png",
                        "size": 10,
                        "width": 4,
                        "height": 4,
                    },
                },
                str(id2): {
                    "success": False,
                    "message": "gone",
                    "errorCode": 0,
                },
            },
        })

        result = await client.get_file_urls([id1, id2])

        method, payload = client._invoke.call_args[0]
        assert method == "GetFileUrls"
        assert payload == {"fileIds": [str(id1), str(id2)]}
        assert result.success is True
        assert result.data is not None
        # Wire string keys are converted back to UUID.
        assert set(result.data.keys()) == {id1, id2}
        assert isinstance(result.data[id1], FileReference)
        assert result.data[id1].url == "https://s/1"
        assert result.data[id1].metadata is not None
        assert result.data[id1].metadata.content_type == "image/png"
        assert result.data[id2].success is False

    @pytest.mark.asyncio
    async def test_request_file_upload_deserialization_error(self) -> None:
        client = _make_client()
        # An empty grant dict is missing UploadGrant's required fields, so
        # deserialization raises and the wrapper returns a failed result.
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {},
        })

        result = await client.request_file_upload(
            FileUploadRequest(
                name="x",
                content_type="text/plain",
                size=1,
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Avatar",
            )
        )

        assert result.success is False
        assert result.error_code == ErrorCodes.InternalError

    @pytest.mark.asyncio
    async def test_file_wrappers_survive_a_scalar_hub_response(self) -> None:
        # A hub that answers with a bare scalar instead of an envelope object
        # reaches ``from_dict``'s ``data.get(...)`` and raises AttributeError.
        # Every file wrapper must absorb that into a failed result.
        client = _make_client()
        client._invoke = AsyncMock(return_value="success")
        file_id = uuid7()

        request = FileUploadRequest(
            name="x",
            content_type="text/plain",
            size=1,
            reference_type="UserEntity",
            reference_id=uuid7(),
            reference_property="Avatar",
        )
        results = [
            await client.request_file_upload(request),
            await client.refresh_file_upload_grant(file_id),
            await client.complete_file_upload(file_id),
            await client.cancel_file_upload(file_id),
            await client.get_file_urls([file_id]),
        ]

        for result in results:
            assert result.success is False
            assert result.error_code == ErrorCodes.InternalError


class TestDataUpdateRejected:
    """DataUpdateRejected event routing to subscription on_error callbacks."""

    @staticmethod
    def _rejection_payload(backend_id: UUID) -> dict[str, Any]:
        return {
            "subscriptionId": {"id": str(backend_id)},
            "actualBytes": 4_194_304,
            "limitBytes": 1_048_576,
            "reason": "ServiceDataUpdate exceeds negotiated limit",
        }

    @pytest.mark.asyncio
    async def test_rejection_routes_to_on_error(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        on_error = MagicMock()
        await _make_subscription(client, backend_id, on_error=on_error)

        await client._handle_data_update_rejected(self._rejection_payload(backend_id))

        on_error.assert_called_once()
        arg = on_error.call_args[0][0]
        assert isinstance(arg, DataUpdateRejectedNotification)
        assert arg.subscription_id.id == backend_id
        assert arg.actual_bytes == 4_194_304
        assert arg.limit_bytes == 1_048_576

    @pytest.mark.asyncio
    async def test_rejection_without_on_error_is_a_warning(self) -> None:
        """A rejection on a subscription with no on_error must not raise."""
        client = _make_client()
        backend_id = uuid7()
        await _make_subscription(client, backend_id, on_error=None)

        # Should log a warning and return cleanly.
        await client._handle_data_update_rejected(self._rejection_payload(backend_id))

    @pytest.mark.asyncio
    async def test_rejection_for_unknown_subscription_is_a_warning(self) -> None:
        client = _make_client()
        await client._handle_data_update_rejected(self._rejection_payload(uuid7()))

    @pytest.mark.asyncio
    async def test_unsubscribe_clears_on_error(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        on_error = MagicMock()
        client_id, _ = await _make_subscription(client, backend_id, on_error=on_error)
        assert client_id in client._client_error_callbacks

        # Mock the Unsubscribe call.
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
        })
        await client.unsubscribe(SubscriptionId(id=client_id))

        assert client_id not in client._client_error_callbacks

    @pytest.mark.asyncio
    async def test_unsubscribe_all_clears_on_error(self) -> None:
        client = _make_client()
        on_error = MagicMock()
        await _make_subscription(client, uuid7(), on_error=on_error)
        assert client._client_error_callbacks

        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
        })
        await client.unsubscribe_all()
        assert not client._client_error_callbacks


class TestPartReassembly:
    """Reassembly of chunked ``ServiceDataUpdate`` parts in ``_handle_data_updated``."""

    @staticmethod
    def _part_payload(
        *,
        backend_id: UUID,
        sequence_idx: int = 0,
        part_idx: int = 0,
        part_count: int = 1,
        modified_entities: Any = None,
    ) -> dict[str, Any]:
        """Build a wire-format ``DataUpdated`` dict for one part."""
        return {
            "scenarioId": str(UUID(int=0)),
            "status": 1,
            "requestIds": [],
            "subscriptionId": {"id": str(backend_id)},
            "modifiedEntities": modified_entities if modified_entities is not None else {},
            "progress": False,
            "progressAmount": 0.0,
            "sequenceIdx": sequence_idx,
            "partIdx": part_idx,
            "partCount": part_count,
        }

    # ------------------------------------------------------------------
    # Fast path / single-part
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_single_part_update_fast_path(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_count=1),
        )

        callback.assert_called_once()
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_updates_are_dispatched_off_the_handler_in_order(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, sequence_idx=1),
        )
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, sequence_idx=2),
        )
        # The handler only queues; dispatch happens on the worker.
        callback.assert_not_called()

        assert client._data_update_worker is not None
        await client._data_update_worker
        assert [call.args[0].sequence_idx for call in callback.call_args_list] == [1, 2]

    @pytest.mark.asyncio
    async def test_close_discards_queued_updates(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        await client._handle_data_updated(self._part_payload(backend_id=backend_id))
        await client._handle_close()
        assert client._data_update_worker is not None
        await client._data_update_worker

        callback.assert_not_called()
        assert not client._data_update_queue

    @pytest.mark.asyncio
    async def test_failed_dispatch_does_not_stop_the_worker(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)
        real_dispatch = client._dispatch_data_update
        calls = 0

        async def dispatch(backend: UUID, update: ServiceDataUpdate) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FilamentConnectionError("dropped mid-dispatch")
            await real_dispatch(backend, update)

        client._dispatch_data_update = dispatch
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, sequence_idx=1),
        )
        await _deliver(client, self._part_payload(backend_id=backend_id, sequence_idx=2))

        callback.assert_called_once()
        assert callback.call_args.args[0].sequence_idx == 2

    @pytest.mark.asyncio
    async def test_aexit_cancels_the_dispatch_worker(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        started = asyncio.Event()

        async def slow_callback(update: ServiceDataUpdate) -> None:
            started.set()
            await asyncio.Event().wait()

        await _make_subscription(client, backend_id, callback=slow_callback)
        await client._handle_data_updated(self._part_payload(backend_id=backend_id))
        worker = client._data_update_worker
        await started.wait()

        await client.__aexit__(None, None, None)

        assert worker is not None and worker.cancelled()
        assert client._data_update_worker is None

    # ------------------------------------------------------------------
    # Reassembly happy paths
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_two_part_update_merges(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
        )
        callback.assert_not_called()

        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=1, part_count=2),
        )
        callback.assert_called_once()
        # Buffer is cleared after delivery.
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_out_of_order_parts_merge(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        # Fire idx 1 first, then idx 0.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=1, part_count=2),
        )
        callback.assert_not_called()

        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
        )
        callback.assert_called_once()
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_partial_buffer_not_dispatched(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        # Only 1 of 3 parts.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=3),
        )
        callback.assert_not_called()
        assert (backend_id, 0) in client._part_buffers

    # ------------------------------------------------------------------
    # Merge correctness — _merge_parts directly
    # ------------------------------------------------------------------

    def test_merge_unions_distinct_keys(self) -> None:
        """Merged dict contains the union of modified_entities keys across parts."""
        part0 = self._build_update(
            sequence_idx=7, part_idx=0, part_count=2,
            modified_entities={"UserEntity": EntityServiceDataUpdate()},
        )
        part1 = self._build_update(
            sequence_idx=7, part_idx=1, part_count=2,
            modified_entities={"TaskEntity": EntityServiceDataUpdate()},
        )
        merged = FilamentClient._merge_parts({0: part0, 1: part1}, 2)
        assert set(merged.modified_entities.keys()) == {"UserEntity", "TaskEntity"}

    def test_merge_extends_collision_lists(self) -> None:
        """On a same-entity-type key collision, the created/updated/deleted lists are extended."""
        sentinel_a = object()
        sentinel_b = object()
        sentinel_c = object()
        sentinel_d = object()
        part0 = self._build_update(
            sequence_idx=1, part_idx=0, part_count=2,
            modified_entities={"UserEntity": EntityServiceDataUpdate(
                created=[sentinel_a],  # type: ignore[list-item]
                updated=[],
                deleted=[sentinel_b],  # type: ignore[list-item]
            )},
        )
        part1 = self._build_update(
            sequence_idx=1, part_idx=1, part_count=2,
            modified_entities={"UserEntity": EntityServiceDataUpdate(
                created=[sentinel_c],  # type: ignore[list-item]
                updated=[sentinel_d],  # type: ignore[list-item]
                deleted=[],
            )},
        )
        merged = FilamentClient._merge_parts({0: part0, 1: part1}, 2)
        user_update = merged.modified_entities["UserEntity"]
        assert user_update.created == [sentinel_a, sentinel_c]
        assert user_update.updated == [sentinel_d]
        assert user_update.deleted == [sentinel_b]

    def test_merged_update_has_part_count_one(self) -> None:
        """Merged result hides chunking from user code (part_idx=0, part_count=1)."""
        part0 = self._build_update(sequence_idx=5, part_idx=0, part_count=2)
        part1 = self._build_update(sequence_idx=5, part_idx=1, part_count=2)
        merged = FilamentClient._merge_parts({0: part0, 1: part1}, 2)
        assert merged.part_idx == 0
        assert merged.part_count == 1
        assert merged.sequence_idx == 5

    @staticmethod
    def _build_update(
        *,
        sequence_idx: int,
        part_idx: int,
        part_count: int,
        modified_entities: Any = None,
    ) -> ServiceDataUpdate:
        """Construct a ``ServiceDataUpdate`` directly (no wire-format round trip)."""
        return ServiceDataUpdate(
            scenario_id=UUID(int=0),
            subscription_id=SubscriptionId(id=UUID(int=42)),
            modified_entities=modified_entities or {},
            sequence_idx=sequence_idx,
            part_idx=part_idx,
            part_count=part_count,
        )

    # ------------------------------------------------------------------
    # Concurrent sequences and subscriptions
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_interleaved_sequences_complete_independently(self) -> None:
        """Parts of seq=1 and seq=2 on the same sub may interleave; each completes alone."""
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        # Interleave: seq=1 idx=0, seq=2 idx=0, seq=1 idx=1, seq=2 idx=1.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, sequence_idx=1, part_idx=0, part_count=2),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, sequence_idx=2, part_idx=0, part_count=2),
        )
        callback.assert_not_called()
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, sequence_idx=1, part_idx=1, part_count=2),
        )
        assert callback.call_count == 1
        first_delivery: ServiceDataUpdate = callback.call_args_list[0][0][0]
        assert first_delivery.sequence_idx == 1

        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, sequence_idx=2, part_idx=1, part_count=2),
        )
        assert callback.call_count == 2
        second_delivery: ServiceDataUpdate = callback.call_args_list[1][0][0]
        assert second_delivery.sequence_idx == 2
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_concurrent_subscriptions_dont_cross_contaminate(self) -> None:
        client = _make_client()
        backend_a = uuid7()
        backend_b = uuid7()
        _, cb_a = await _make_subscription(client, backend_a)
        _, cb_b = await _make_subscription(client, backend_b)

        # Interleave 2-part updates for both subs.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_a, part_idx=0, part_count=2),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_b, part_idx=0, part_count=2),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_a, part_idx=1, part_count=2),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_b, part_idx=1, part_count=2),
        )

        cb_a.assert_called_once()
        cb_b.assert_called_once()
        assert cb_a.call_args[0][0].subscription_id.id == backend_a
        assert cb_b.call_args[0][0].subscription_id.id == backend_b

    # ------------------------------------------------------------------
    # Cleanup: close / unsubscribe / unsubscribe_all
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_close_clears_part_buffers(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        await _make_subscription(client, backend_id)
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
        )
        assert client._part_buffers

        await client._handle_close()
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_close_clears_buffers_with_no_pending_invocations(self) -> None:
        """Regression: the prior _handle_close early-returned when no RPCs were in flight."""
        client = _make_client()
        backend_id = uuid7()
        await _make_subscription(client, backend_id)
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
        )
        assert client._part_buffers
        assert not client._pending_invocations

        await client._handle_close()
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_unsubscribe_clears_subscription_buffer(self) -> None:
        client = _make_client()
        backend_a = uuid7()
        backend_b = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_a)},
        })
        sub_a_result = await client.subscribe(
            SubscriptionConfig(
                scenario_id=UUID(int=0),
                status=DataProcessingStatus.Pending,
                type_name="TestEntity",
                mode=SubscriptionMode.Data,
            ),
            callback=MagicMock(),
        )
        await _make_subscription(client, backend_b)

        # Buffer partials for both subs.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_a, part_idx=0, part_count=2),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_b, part_idx=0, part_count=2),
        )
        assert (backend_a, 0) in client._part_buffers
        assert (backend_b, 0) in client._part_buffers

        # Mock the Unsubscribe call and unsubscribe sub A.
        client._invoke = AsyncMock(return_value={
            "success": True, "message": "", "errorCode": 0, "errorId": None,
        })
        assert sub_a_result.data is not None
        await client.unsubscribe(sub_a_result.data)

        assert (backend_a, 0) not in client._part_buffers
        assert (backend_b, 0) in client._part_buffers

    @pytest.mark.asyncio
    async def test_unsubscribe_all_clears_all_buffers(self) -> None:
        client = _make_client()
        backend_a = uuid7()
        backend_b = uuid7()
        await _make_subscription(client, backend_a)
        await _make_subscription(client, backend_b)
        await _deliver(
            client,
            self._part_payload(backend_id=backend_a, part_idx=0, part_count=2),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_b, part_idx=0, part_count=2),
        )
        assert len(client._part_buffers) == 2

        client._invoke = AsyncMock(return_value={
            "success": True, "message": "", "errorCode": 0, "errorId": None,
        })
        await client.unsubscribe_all()
        assert client._part_buffers == {}

    # ------------------------------------------------------------------
    # Malformed metadata
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_duplicate_part_idx_overwrites_with_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        with caplog.at_level("WARNING"):
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
            )
            await _deliver(
                client,
                # Duplicate idx=0 — should overwrite and log a warning.
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
            )

        assert any("Duplicate part_idx" in r.message for r in caplog.records)
        callback.assert_not_called()
        # Buffer still has just one slot (after duplicate overwrite) — needs idx=1 to complete.
        assert len(client._part_buffers[(backend_id, 0)]) == 1

    @pytest.mark.asyncio
    async def test_unknown_subscription_part_dropped_before_buffering(self) -> None:
        client = _make_client()
        # No subscription registered.
        await _deliver(
            client,
            self._part_payload(backend_id=uuid7(), part_idx=0, part_count=2),
        )
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_part_idx_out_of_range_dropped(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        with caplog.at_level("WARNING"):
            # part_idx == part_count
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=2, part_count=2),
            )
            # negative
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=-1, part_count=2),
            )

        assert sum("invalid part_idx" in r.message for r in caplog.records) == 2
        callback.assert_not_called()
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_inconsistent_part_count_poisons_sequence(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        # Buffer part 0 of a 3-part update.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=3),
        )
        assert (backend_id, 0) in client._part_buffers

        # Send a part with conflicting part_count=2.
        with caplog.at_level("WARNING"):
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=1, part_count=2),
            )

        assert any("Inconsistent part_count" in r.message for r in caplog.records)
        assert (backend_id, 0) not in client._part_buffers
        assert (backend_id, 0) in client._poisoned_part_buffer_keys
        callback.assert_not_called()

        # A follow-up "consistent" batch under the new part_count=2 must still be dropped.
        with caplog.at_level("WARNING"):
            caplog.clear()
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
            )
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=1, part_count=2),
            )

        assert sum("poisoned" in r.message for r in caplog.records) == 2
        callback.assert_not_called()
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_missing_middle_index_blocks_completion(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        # Fire parts 0 and 2 of a 3-part update.  Index set {0, 2} != {0, 1, 2}.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=3),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=2, part_count=3),
        )
        callback.assert_not_called()
        assert len(client._part_buffers[(backend_id, 0)]) == 2

        # Fire the missing part 1 — now {0, 1, 2} is complete and callback fires.
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, part_idx=1, part_count=3),
        )
        callback.assert_called_once()
        assert client._part_buffers == {}

    @pytest.mark.asyncio
    async def test_non_positive_part_count_dropped(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        with caplog.at_level("WARNING"):
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=0),
            )
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=-1),
            )

        assert sum("non-positive part_count" in r.message for r in caplog.records) == 2
        callback.assert_not_called()
        assert client._part_buffers == {}

    # ------------------------------------------------------------------
    # Poisoned-key lifecycle
    # ------------------------------------------------------------------

    async def _poison_sequence(self, client: FilamentClient, backend_id: UUID, seq: int = 0) -> None:
        """Trigger a part_count inconsistency to poison ``(backend_id, seq)``."""
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, sequence_idx=seq, part_idx=0, part_count=3),
        )
        await _deliver(
            client,
            self._part_payload(backend_id=backend_id, sequence_idx=seq, part_idx=1, part_count=2),
        )
        assert (backend_id, seq) in client._poisoned_part_buffer_keys

    @pytest.mark.asyncio
    async def test_poisoned_keys_cleared_on_close(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        await _make_subscription(client, backend_id)
        await self._poison_sequence(client, backend_id)

        await client._handle_close()
        assert client._poisoned_part_buffer_keys == set()

    @pytest.mark.asyncio
    async def test_poisoned_keys_cleared_on_unsubscribe(self) -> None:
        client = _make_client()
        backend_a = uuid7()
        backend_b = uuid7()
        client._invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(backend_a)},
        })
        sub_a_result = await client.subscribe(
            SubscriptionConfig(
                scenario_id=UUID(int=0),
                status=DataProcessingStatus.Pending,
                type_name="TestEntity",
                mode=SubscriptionMode.Data,
            ),
            callback=MagicMock(),
        )
        await _make_subscription(client, backend_b)

        await self._poison_sequence(client, backend_a)
        await self._poison_sequence(client, backend_b)
        assert (backend_a, 0) in client._poisoned_part_buffer_keys
        assert (backend_b, 0) in client._poisoned_part_buffer_keys

        client._invoke = AsyncMock(return_value={
            "success": True, "message": "", "errorCode": 0, "errorId": None,
        })
        assert sub_a_result.data is not None
        await client.unsubscribe(sub_a_result.data)

        assert (backend_a, 0) not in client._poisoned_part_buffer_keys
        assert (backend_b, 0) in client._poisoned_part_buffer_keys

    @pytest.mark.asyncio
    async def test_poisoned_keys_cleared_on_unsubscribe_all(self) -> None:
        client = _make_client()
        backend_a = uuid7()
        backend_b = uuid7()
        await _make_subscription(client, backend_a)
        await _make_subscription(client, backend_b)
        await self._poison_sequence(client, backend_a)
        await self._poison_sequence(client, backend_b)
        assert len(client._poisoned_part_buffer_keys) == 2

        client._invoke = AsyncMock(return_value={
            "success": True, "message": "", "errorCode": 0, "errorId": None,
        })
        await client.unsubscribe_all()
        assert client._poisoned_part_buffer_keys == set()

    @pytest.mark.asyncio
    async def test_single_part_for_poisoned_sequence_is_dropped(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Regression: the fast path used to bypass the poison check."""
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)
        await self._poison_sequence(client, backend_id)
        callback.reset_mock()  # ignore any noise from the poison-trigger calls

        with caplog.at_level("WARNING"):
            await _deliver(
                client,
                self._part_payload(backend_id=backend_id, sequence_idx=0,
                                    part_idx=0, part_count=1),
            )

        assert any("poisoned" in r.message for r in caplog.records)
        callback.assert_not_called()


class _FakeReadServer:
    """A minimal stand-in for the backend's ``ReadData`` endpoint.

    Models a single entity type with ``len(entity_sizes)`` records, each of a
    notional serialised size.  A page ``[offset, offset + limit)`` is rejected
    with :attr:`ErrorCodes.ResponseTooLarge` when the summed size of its records
    exceeds ``max_bytes`` — mirroring the server-side response-size filter — so
    the client's recursive paging can be driven without a live hub.  ``limit <= 0``
    means "all records" and ``ReadMode.Count`` returns just the total count.
    """

    def __init__(
        self,
        entity_sizes: list[int],
        max_bytes: int,
        *,
        base_bytes: int = 0,
        pending_delete: Optional[dict[int, str]] = None,
    ) -> None:
        self.entity_sizes = entity_sizes
        self.max_bytes = max_bytes
        self.base_bytes = base_bytes
        # Maps an entity index to the request id of a pending delete on it.
        self.pending_delete = pending_delete or {}
        self.entities = [
            {"entity": {"$type": "UserEntity", "id": str(uuid7()),
                        "givenName": f"User{i}"}}
            for i in range(len(entity_sizes))
        ]
        # Records (offset, limit, mode) for every invocation, in order.
        self.calls: list[tuple[int, int, ReadMode]] = []

    async def invoke(self, method: str, request_dict: Any) -> Any:
        assert method == "ReadData"
        request = from_dict(request_dict, ReadDataRequest)
        assert request is not None
        type_request = request.request
        self.calls.append(
            (type_request.offset, type_request.limit, type_request.mode)
        )

        total = len(self.entity_sizes)
        if type_request.mode == ReadMode.Count:
            return self._success(data=[], more_data=False, entity_count=total)

        limit = type_request.limit if type_request.limit > 0 else total
        start = type_request.offset
        stop = min(total, start + limit)
        would_be = self.base_bytes + sum(self.entity_sizes[start:stop])
        if would_be > self.max_bytes:
            return {
                "success": False,
                "message": (
                    f"Response would be {would_be:,} bytes; "
                    f"client limit is {self.max_bytes:,} bytes."
                ),
                "errorCode": ErrorCodes.ResponseTooLarge.value,
                "errorId": None,
                "data": None,
            }
        return self._success(
            data=self.entities[start:stop],
            more_data=stop < total,
            entity_count=0,
            pending_delete=self._pending_delete_for(start, stop),
        )

    def _pending_delete_for(self, start: int, stop: int) -> Optional[dict[str, Any]]:
        """Pending-delete entries (entity id -> request id) for a page slice."""
        page = {
            self.entities[i]["entity"]["id"]: {"id": rid}
            for i, rid in self.pending_delete.items()
            if start <= i < stop
        }
        return page or None

    @staticmethod
    def _success(
        *,
        data: list[Any],
        more_data: bool,
        entity_count: int,
        pending_delete: Optional[dict[str, Any]] = None,
    ) -> Any:
        return {
            "success": True,
            "message": "",
            "errorCode": 0,
            "errorId": None,
            "data": {
                "typeName": "UserEntity",
                "data": data,
                "entityCount": entity_count,
                "moreData": more_data,
                "pendingDeleteRequestIds": pending_delete,
            },
        }


def _read_request(*, limit: int, offset: int = 0) -> ReadDataRequest:
    return ReadDataRequest(
        scenario_id=UUID(int=0),
        request=ReadDataTypeRequest(
            type_name="UserEntity", limit=limit, offset=offset,
        ),
    )


class TestReadDataPaging:
    @pytest.mark.asyncio
    async def test_success_passes_through_without_paging(self) -> None:
        """A response that fits is returned directly with a single invocation."""
        server = _FakeReadServer([100] * 3, max_bytes=1000)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=3))

        assert result.success is True
        assert result.data is not None
        assert len(result.data.data) == 3
        assert len(server.calls) == 1

    @pytest.mark.asyncio
    async def test_non_too_large_error_passes_through(self) -> None:
        """Errors other than ResponseTooLarge are surfaced without paging."""
        client = _make_client()
        client._invoke = AsyncMock(return_value={
            "success": False,
            "message": "nope",
            "errorCode": ErrorCodes.PermissionDenied.value,
            "errorId": None,
            "data": None,
        })

        result = await client.read_data(_read_request(limit=10))

        assert result.success is False
        assert result.error_code == ErrorCodes.PermissionDenied
        client._invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_splits_into_pages_and_merges(self) -> None:
        """An evenly-sized too-large response is paged and merged in full."""
        server = _FakeReadServer([100] * 10, max_bytes=250)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=10))

        assert result.success is True
        assert result.data is not None
        # Every entity is returned exactly once, in order.
        expected_ids = [w["entity"]["id"] for w in server.entities]
        got_ids = [str(w.entity.id) for w in result.data.data]
        assert got_ids == expected_ids
        assert result.data.more_data is False

    @pytest.mark.asyncio
    async def test_recurses_on_unevenly_distributed_pages(self) -> None:
        """A page that is still too large after the first split is split again."""
        # First split yields a [0, 2) page (400 bytes) that itself exceeds the
        # 250-byte limit and must be paged a second time.
        server = _FakeReadServer([200, 200, 40, 40], max_bytes=250)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=4))

        assert result.success is True
        assert result.data is not None
        assert len(result.data.data) == 4
        assert result.data.more_data is False
        # A page of size 1 was needed to get the two 200-byte records through.
        assert any(limit == 1 for _, limit, _ in server.calls)

    @pytest.mark.asyncio
    async def test_single_entity_still_too_large_raises(self) -> None:
        """When one entity alone exceeds the limit, a ResponseTooLargeError is
        raised carrying both sizes, and no further requests are made."""
        # Entity 1 is 5000 bytes on its own; the full request is 5200 bytes.
        server = _FakeReadServer([100, 5000, 100], max_bytes=250)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        with pytest.raises(ResponseTooLargeError) as exc_info:
            await client.read_data(_read_request(limit=3))

        error = exc_info.value
        assert error.entity_bytes == 5000
        assert error.full_request_bytes == 5200
        assert error.limit_bytes == 250
        # Terminates: a single-entity page is never split further.
        assert any(limit == 1 for _, limit, _ in server.calls)
        assert len(server.calls) < 30

    @pytest.mark.asyncio
    async def test_top_level_single_entity_request_raises(self) -> None:
        """A user request for a single oversized entity raises immediately,
        with the entity and full-request sizes equal."""
        server = _FakeReadServer([5000], max_bytes=250)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        with pytest.raises(ResponseTooLargeError) as exc_info:
            await client.read_data(_read_request(limit=1))

        error = exc_info.value
        assert error.entity_bytes == 5000
        assert error.full_request_bytes == 5000
        assert error.limit_bytes == 250
        assert len(server.calls) == 1

    @pytest.mark.asyncio
    async def test_more_data_preserved_when_limit_below_total(self) -> None:
        """Paging within a user-supplied limit keeps ``more_data`` set when the
        backend has further records beyond that limit."""
        server = _FakeReadServer([100] * 10, max_bytes=250)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=4))

        assert result.success is True
        assert result.data is not None
        assert len(result.data.data) == 4
        assert result.data.more_data is True

    @pytest.mark.asyncio
    async def test_unlimited_request_resolves_count_then_pages(self) -> None:
        """An unlimited (limit=0) read first reads the count, then pages it."""
        server = _FakeReadServer([100] * 6, max_bytes=250)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=0))

        assert result.success is True
        assert result.data is not None
        assert len(result.data.data) == 6
        assert result.data.more_data is False
        # A Count-mode probe was used to bound the otherwise-unlimited read.
        assert any(mode == ReadMode.Count for _, _, mode in server.calls)

    @pytest.mark.asyncio
    async def test_unlimited_request_with_no_remaining_rows_does_not_raise(self) -> None:
        """When an unlimited read's count probe leaves no in-range rows (the
        offset is at/after the total) and the response is pure envelope
        overhead, the original error is surfaced rather than a misleading
        single-entity ResponseTooLargeError."""
        # base_bytes alone exceeds max_bytes, so even an empty page is rejected.
        server = _FakeReadServer([100, 100], max_bytes=50, base_bytes=100)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=0, offset=2))

        assert result.success is False
        assert result.error_code == ErrorCodes.ResponseTooLarge

    @pytest.mark.asyncio
    async def test_pending_request_ids_merged_across_pages(self) -> None:
        """Per-entity pending maps from every page survive the merge."""
        rid_1 = str(uuid7())
        rid_8 = str(uuid7())
        # Entities 1 and 8 land in different pages once the read is split.
        server = _FakeReadServer(
            [100] * 10, max_bytes=250,
            pending_delete={1: rid_1, 8: rid_8},
        )
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=10))

        assert result.success is True
        assert result.data is not None
        pending = result.data.pending_delete_request_ids
        assert pending is not None
        got = {str(eid): str(req.id) for eid, req in pending.items()}
        assert got == {
            server.entities[1]["entity"]["id"]: rid_1,
            server.entities[8]["entity"]["id"]: rid_8,
        }

    @pytest.mark.asyncio
    async def test_pages_preserve_request_offset(self) -> None:
        """Paging honours a non-zero starting offset."""
        server = _FakeReadServer([100] * 10, max_bytes=250)
        client = _make_client()
        client._invoke = AsyncMock(side_effect=server.invoke)

        result = await client.read_data(_read_request(limit=4, offset=2))

        assert result.success is True
        assert result.data is not None
        # Records 2..5 (the 3rd through 6th entities) are returned.
        expected_ids = [w["entity"]["id"] for w in server.entities[2:6]]
        got_ids = [w.entity.id for w in result.data.data]
        assert [str(i) for i in got_ids] == expected_ids
        assert all(offset >= 2 for offset, _, _ in server.calls)

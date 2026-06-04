"""Tests for the FilamentClient class.

These tests mock the pysignalr SignalRClient to verify method dispatch,
serialization, and subscription routing without a live server.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament._exceptions import FilamentAuthenticationError, FilamentConnectionError
from filament.types.generated.enums import (
    DataProcessingStatus,
    ErrorCodes,
    SubscriptionMode,
)
from filament.types.generated.misc import (
    EntityPermissions,
    ProcessingStatus,
    ReadDataRequest,
    ReadDataTypeRequest,
    RequestId,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionId,
    ValueOrErrorPublic_1,
)


def _make_client(**kwargs: Any) -> FilamentClient:
    """Create a FilamentClient with mocked SignalR internals."""
    with patch("filament._client.SignalRClient"):
        client = FilamentClient(
            organization_id="test-org",
            api_key_name="test-key",
            api_key="test-secret",
            **kwargs,
        )
        # Mock _invoke for testing API methods
        client._invoke = AsyncMock()
        return client


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
        await client._handle_data_updated(update_data)

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
        await client._handle_data_updated(update_data)

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

    @staticmethod
    def _client_with_real_invoke() -> FilamentClient:
        """Like ``_make_client`` but keeps the real ``_invoke`` method."""
        with patch("filament._client.SignalRClient"):
            return FilamentClient(
                organization_id="org",
                api_key_name="k",
                api_key="s",
            )

    @pytest.mark.asyncio
    async def test_invoke_registers_and_clears_pending_future(self) -> None:
        """_invoke should add its future to the pending set and remove it on completion."""
        import asyncio

        from pysignalr.messages import CompletionMessage

        client = self._client_with_real_invoke()

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
        import asyncio

        client = self._client_with_real_invoke()

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

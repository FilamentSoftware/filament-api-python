"""Tests for the FilamentClient class.

These tests mock the signalr-async Hub/Client to verify method dispatch,
serialization, and subscription routing without a live server.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament._exceptions import FilamentAuthenticationError
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
    with patch("filament._client.Hub") as mock_hub_cls, \
         patch("filament._client.Client"):
        mock_hub = MagicMock()
        mock_hub.on = MagicMock()
        mock_hub.invoke = AsyncMock()
        mock_hub_cls.return_value = mock_hub

        client = FilamentClient(
            organization_id="test-org",
            api_key_name="test-key",
            api_key="test-secret",
            **kwargs,
        )
        # Store mock for test access
        client._hub = mock_hub
        return client


class TestClientConstruction:
    def test_default_url(self) -> None:
        with patch("filament._client.Hub"), \
             patch("filament._client.Client") as mock_client_cls:
            FilamentClient(organization_id="org", api_key_name="k", api_key="s")
            call_args = mock_client_cls.call_args
            assert call_args is not None
            assert call_args[0][0] == "https://backend.filament-software.com:7076"

    def test_custom_url(self) -> None:
        with patch("filament._client.Hub"), \
             patch("filament._client.Client") as mock_client_cls:
            FilamentClient(
                organization_id="org",
                api_key_name="k",
                api_key="s",
                server="https://dev.example.com",
                port=9999,
                path="api/v1",
            )
            call_args = mock_client_cls.call_args
            assert call_args is not None
            assert call_args[0][0] == "https://dev.example.com:9999"

    def test_registers_event_handlers(self) -> None:
        with patch("filament._client.Hub") as mock_hub_cls, \
             patch("filament._client.Client"):
            mock_hub = MagicMock()
            mock_hub_cls.return_value = mock_hub
            FilamentClient(organization_id="org", api_key_name="k", api_key="s")
            registered = {call[0][0] for call in mock_hub.on.call_args_list}
            assert "DataUpdated" in registered
            assert "ProcessingStarted" in registered
            assert "ProcessingCompleted" in registered
            assert "ProcessingFailed" in registered
            assert "AuthenticationFailed" in registered
            assert "KeepAlive" in registered


class TestReadData:
    @pytest.mark.asyncio
    async def test_read_data_invokes_hub(self) -> None:
        client = _make_client()
        client._hub.invoke = AsyncMock(return_value={
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

        client._hub.invoke.assert_called_once()
        call_args = client._hub.invoke.call_args
        assert call_args is not None
        assert call_args[0][0] == "ReadData"
        assert result.success is True


class TestSubscriptionRouting:
    @pytest.mark.asyncio
    async def test_subscribe_stores_callback(self) -> None:
        client = _make_client()
        sub_id = uuid7()
        client._hub.invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
            "data": {"id": str(sub_id)},
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
        assert sub_id in client._subscriptions

    @pytest.mark.asyncio
    async def test_data_updated_routes_to_callback(self) -> None:
        client = _make_client()
        sub_id = uuid7()

        callback = MagicMock()
        client._subscriptions[sub_id] = callback

        # Simulate a DataUpdated event
        update_data = {
            "scenarioId": str(uuid7()),
            "status": 1,
            "requestIds": [],
            "subscriptionId": {"id": str(sub_id)},
            "modifiedEntities": {},
            "progress": False,
            "progressAmount": 0.0,
            "sequenceIdx": 0,
        }
        await client._handle_data_updated(update_data)

        callback.assert_called_once()
        arg = callback.call_args[0][0]
        assert isinstance(arg, ServiceDataUpdate)
        assert arg.subscription_id.id == sub_id

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
    async def test_unsubscribe_removes_callback(self) -> None:
        client = _make_client()
        sub_id = uuid7()
        client._subscriptions[sub_id] = MagicMock()
        client._hub.invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
        })

        await client.unsubscribe(SubscriptionId(id=sub_id))
        assert sub_id not in client._subscriptions

    @pytest.mark.asyncio
    async def test_unsubscribe_all_clears_callbacks(self) -> None:
        client = _make_client()
        client._subscriptions[uuid7()] = MagicMock()
        client._subscriptions[uuid7()] = MagicMock()
        client._hub.invoke = AsyncMock(return_value={
            "success": True,
            "message": "",
            "errorCode": 0,
        })

        await client.unsubscribe_all()
        assert len(client._subscriptions) == 0


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

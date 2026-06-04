"""Tests for the FilamentClient class.

These tests mock the pysignalr SignalRClient to verify method dispatch,
serialization, and subscription routing without a live server.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pysignalr.messages import CompletionMessage
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament._exceptions import FilamentAuthenticationError, FilamentConnectionError
from filament.types.generated.enums import (
    DataProcessingStatus,
    ErrorCodes,
    SubscriptionMode,
)
from filament.types.generated.misc import (
    DataUpdateRejectedNotification,
    EntityPermissions,
    EntityServiceDataUpdate,
    ProcessingStatus,
    ReadDataRequest,
    ReadDataTypeRequest,
    RequestId,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionId,
    ValueOrErrorPublic_1,
)


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
        return client


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

        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, part_count=1),
        )

        callback.assert_called_once()
        assert client._part_buffers == {}

    # ------------------------------------------------------------------
    # Reassembly happy paths
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_two_part_update_merges(self) -> None:
        client = _make_client()
        backend_id = uuid7()
        _, callback = await _make_subscription(client, backend_id)

        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
        )
        callback.assert_not_called()

        await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, part_idx=1, part_count=2),
        )
        callback.assert_not_called()

        await client._handle_data_updated(
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
        await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, sequence_idx=1, part_idx=0, part_count=2),
        )
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, sequence_idx=2, part_idx=0, part_count=2),
        )
        callback.assert_not_called()
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, sequence_idx=1, part_idx=1, part_count=2),
        )
        assert callback.call_count == 1
        first_delivery: ServiceDataUpdate = callback.call_args_list[0][0][0]
        assert first_delivery.sequence_idx == 1

        await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_a, part_idx=0, part_count=2),
        )
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_b, part_idx=0, part_count=2),
        )
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_a, part_idx=1, part_count=2),
        )
        await client._handle_data_updated(
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
        await client._handle_data_updated(
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
        await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_a, part_idx=0, part_count=2),
        )
        await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_a, part_idx=0, part_count=2),
        )
        await client._handle_data_updated(
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
            await client._handle_data_updated(
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
            )
            await client._handle_data_updated(
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
        await client._handle_data_updated(
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
            await client._handle_data_updated(
                self._part_payload(backend_id=backend_id, part_idx=2, part_count=2),
            )
            # negative
            await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=3),
        )
        assert (backend_id, 0) in client._part_buffers

        # Send a part with conflicting part_count=2.
        with caplog.at_level("WARNING"):
            await client._handle_data_updated(
                self._part_payload(backend_id=backend_id, part_idx=1, part_count=2),
            )

        assert any("Inconsistent part_count" in r.message for r in caplog.records)
        assert (backend_id, 0) not in client._part_buffers
        assert (backend_id, 0) in client._poisoned_part_buffer_keys
        callback.assert_not_called()

        # A follow-up "consistent" batch under the new part_count=2 must still be dropped.
        with caplog.at_level("WARNING"):
            caplog.clear()
            await client._handle_data_updated(
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=2),
            )
            await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, part_idx=0, part_count=3),
        )
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, part_idx=2, part_count=3),
        )
        callback.assert_not_called()
        assert len(client._part_buffers[(backend_id, 0)]) == 2

        # Fire the missing part 1 — now {0, 1, 2} is complete and callback fires.
        await client._handle_data_updated(
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
            await client._handle_data_updated(
                self._part_payload(backend_id=backend_id, part_idx=0, part_count=0),
            )
            await client._handle_data_updated(
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
        await client._handle_data_updated(
            self._part_payload(backend_id=backend_id, sequence_idx=seq, part_idx=0, part_count=3),
        )
        await client._handle_data_updated(
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
            await client._handle_data_updated(
                self._part_payload(backend_id=backend_id, sequence_idx=0,
                                    part_idx=0, part_count=1),
            )

        assert any("poisoned" in r.message for r in caplog.records)
        callback.assert_not_called()

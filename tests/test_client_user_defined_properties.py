"""Client integration tests for UDP registry use across hub operations."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pysignalr.messages import CompletionMessage
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament._exceptions import FilamentConnectionError
from filament.types.generated.connections.task_service import TaskDependencyConnection
from filament.types.generated.entities.task_service import TaskEntity
from filament.types.generated.enums import DataRequestAction, TypeSchemaPropertyType
from filament.types.generated.misc import (
    CreateUserDefinedPropertyRequest,
    ServiceDataUpdate,
    SubscriptionId,
    EntityPropertyPermission,
    EntityPropertyPermissions,
    ModifyDataRequest,
    ReadConnectedDataRequest,
    ReadConnectedDataTypeRequest,
    ReadDataRequest,
    ReadDataTypeRequest,
    UserDefinedPropertyInfo,
    UpdateUserDefinedPropertyRequest,
)
from filament.user_defined_properties import build_user_defined_properties


def _client() -> FilamentClient:
    with patch("filament._client.SignalRClient"):
        return FilamentClient(
            organization_id="org", api_key_name="key", api_key="secret",
        )


def _permissions_response() -> dict[str, Any]:
    return {
        "success": True,
        "message": "",
        "errorCode": 0,
        "data": {
            "properties": {
                "udpBudget": {
                    "actions": 7,
                    "conditionalActions": {},
                    "userDefinedData": {
                        "id": str(uuid7()),
                        "name": "UdpBudget",
                        "label": "Budget",
                        "propertyType": TypeSchemaPropertyType.Decimal.value,
                        "subPropertyTypes": [],
                        "nullable": False,
                    },
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_concurrent_discovery_is_shared_and_warm_cache_is_reused() -> None:
    client = _client()
    backend_subscription_id = uuid7()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            await asyncio.sleep(0)
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(backend_subscription_id)},
            }
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    first, second = await asyncio.gather(
        client.get_user_defined_properties("TaskEntity"),
        client.get_user_defined_properties("TaskEntity"),
    )
    third = await client.get_user_defined_properties("TaskEntity")

    assert first.success and second.success and third.success
    assert first.data is second.data is third.data
    methods = [call.args[0] for call in client._invoke.await_args_list]
    assert methods.count("GetEntityPropertyPermissions") == 1
    assert methods.count("Subscribe") == 1
    subscribe_payload = next(
        call.args[1] for call in client._invoke.await_args_list
        if call.args[0] == "Subscribe"
    )
    assert subscribe_payload["scenarioId"] == "00000000-0000-0000-0000-000000000000"
    assert subscribe_payload["typeName"] == "UserDefinedPropertyEntity"


@pytest.mark.asyncio
async def test_invalidated_in_flight_discovery_cannot_publish_stale_properties() -> None:
    client = _client()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    permission_calls = 0

    async def invoke(method: str, *args: Any) -> Any:
        nonlocal permission_calls
        if method == "GetEntityPropertyPermissions":
            permission_calls += 1
            if permission_calls == 1:
                first_started.set()
                await release_first.wait()
                response = _permissions_response()
                response["data"]["properties"]["udpBudget"]["userDefinedData"]["label"] = "Stale"
                return response
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(uuid7())},
            }
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    pending = asyncio.create_task(client.get_user_defined_properties("TaskEntity"))
    await first_started.wait()
    await client.invalidate_user_defined_property_cache("TaskEntity")
    release_first.set()
    result = await pending

    assert result.success and result.data is not None
    assert result.data["udp_budget"].label == "Budget"
    assert permission_calls == 2


@pytest.mark.asyncio
async def test_read_data_hydrates_udp_value_from_cached_definition() -> None:
    client = _client()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(uuid7())},
            }
        if method == "ReadData":
            return {
                "success": True,
                "message": "",
                "errorCode": 0,
                "data": {
                    "typeName": "TaskEntity",
                    "data": [{
                        "entity": {
                            "$type": "TaskEntity",
                            "id": str(uuid7()),
                            "udpBudget": "12.50",
                        },
                    }],
                    "entityCount": 1,
                    "moreData": False,
                },
            }
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    result = await client.read_data(ReadDataRequest(
        scenario_id=UUID(int=0),
        request=ReadDataTypeRequest(type_name="TaskEntity"),
    ))

    assert result.success and result.data is not None
    entity = result.data.data[0].entity
    assert isinstance(entity, TaskEntity)
    assert entity.udp_budget == Decimal("12.50")


@pytest.mark.asyncio
async def test_modify_data_uses_definition_adapter_before_invocation() -> None:
    client = _client()
    definition = UserDefinedPropertyInfo(
        id=uuid7(),
        name="UdpBudget",
        label="Budget",
        property_type=TypeSchemaPropertyType.Decimal,
    )
    properties = build_user_defined_properties(
        "TaskEntity",
        EntityPropertyPermissions(properties={
            "udpBudget": EntityPropertyPermission(user_defined_data=definition),
        }),
    )
    private_id = uuid7()
    client._user_defined_properties["TaskEntity"] = properties
    client._user_defined_subscription_types = frozenset({"TaskEntity"})
    client._user_defined_subscription_id = private_id
    client._invoke = AsyncMock(return_value={
        "success": True,
        "message": "",
        "errorCode": 0,
        "data": {"id": str(uuid7())},
    })
    task = TaskEntity(
        _user_defined_properties={"udp_budget": Decimal("22.75")}
    )

    result = await client.modify_data(ModifyDataRequest(
        scenario_id=UUID(int=0),
        action=DataRequestAction.Create,
        data={"TaskEntity": [task]},
    ))

    assert result.success
    client._invoke.assert_awaited_once()
    method, payload = client._invoke.await_args.args
    assert method == "ModifyData"
    assert payload["data"]["TaskEntity"][0]["udpBudget"] == "22.75"


@pytest.mark.asyncio
async def test_connected_read_hydrates_connection_and_entity_sides() -> None:
    client = _client()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(uuid7())},
            }
        if method == "Unsubscribe":
            return {"success": True, "message": "", "errorCode": 0}
        if method == "ReadConnectedData":
            return {
                "success": True,
                "message": "",
                "errorCode": 0,
                "data": {
                    "connectionTypeName": "TaskDependencyConnection",
                    "entityTypeName": "TaskEntity",
                    "data": [{
                        "connection": {"entity": {
                            "$type": "TaskDependencyConnection",
                            "id": str(uuid7()),
                            "udpBudget": "1.25",
                        }},
                        "entity": {"entity": {
                            "$type": "TaskEntity",
                            "id": str(uuid7()),
                            "udpBudget": "9.50",
                        }},
                    }],
                },
            }
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    result = await client.read_connected_data(ReadConnectedDataRequest(
        scenario_id=UUID(int=0),
        request=ReadConnectedDataTypeRequest(
            connection_type_name="TaskDependencyConnection",
            entity_type_name="TaskEntity",
        ),
    ))

    assert result.success and result.data is not None
    row = result.data.data[0]
    assert isinstance(row.connection.entity, TaskDependencyConnection)
    assert row.connection.entity.udp_budget == Decimal("1.25")
    assert isinstance(row.entity.entity, TaskEntity)
    assert row.entity.entity.udp_budget == Decimal("9.50")


@pytest.mark.asyncio
async def test_create_definition_refreshes_requested_type() -> None:
    client = _client()
    methods: list[str] = []

    async def invoke(method: str, *args: Any) -> Any:
        methods.append(method)
        if method == "CreateUserDefinedProperty":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {
                    "id": str(uuid7()), "name": "UdpBudget", "label": "Budget",
                    "propertyType": TypeSchemaPropertyType.Decimal.value,
                    "subPropertyTypes": [], "nullable": False,
                },
            }
        if method == "GetEntityPropertyPermissions":
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(uuid7())},
            }
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    result = await client.create_user_defined_property(
        CreateUserDefinedPropertyRequest(
            entity_type_name="TaskEntity",
            name="UdpBudget",
            label="Budget",
            property_type=TypeSchemaPropertyType.Decimal,
        )
    )

    assert result.success
    assert methods == [
        "CreateUserDefinedProperty", "GetEntityPropertyPermissions", "Subscribe",
    ]
    assert "udp_budget" in client._user_defined_properties["TaskEntity"]


@pytest.mark.asyncio
async def test_update_definition_clears_every_cached_type() -> None:
    client = _client()
    client._user_defined_properties.update({"TaskEntity": {}, "UserEntity": {}})
    client._user_defined_subscription_types = frozenset(client._user_defined_properties)
    client._user_defined_subscription_id = uuid7()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "UpdateUserDefinedProperty":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {
                    "id": str(uuid7()), "name": "UdpBudget", "label": "Budget",
                    "propertyType": TypeSchemaPropertyType.Decimal.value,
                    "subPropertyTypes": [], "nullable": False,
                },
            }
        if method == "Unsubscribe":
            return {"success": True, "message": "", "errorCode": 0}
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    result = await client.update_user_defined_property(
        UpdateUserDefinedPropertyRequest(id=uuid7(), label="New budget")
    )

    assert result.success
    assert client._user_defined_properties == {}
    assert client._user_defined_subscription_id is None


@pytest.mark.asyncio
async def test_shared_backend_id_still_reaches_the_user_callback() -> None:
    """A user subscription sharing the private UDP backend ID must still fire.

    The backend returns the same subscription ID for duplicate type/permission
    combinations, so a caller subscribing to ``UserDefinedPropertyEntity`` can
    collide with the client's own private definition-maintenance subscription.
    """
    client = _client()
    shared_id = uuid7()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(shared_id)},
            }
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    await client.get_user_defined_properties("TaskEntity")
    assert client._user_defined_subscription_id == shared_id

    received: list[Any] = []
    # Register a user callback against the very same backend ID.
    client_id = uuid7()
    client._backend_to_client[shared_id] = {client_id}
    client._client_callbacks[client_id] = received.append
    client._client_to_backend[client_id] = shared_id

    update = ServiceDataUpdate(
        subscription_id=SubscriptionId(id=shared_id),
        scenario_id=UUID("00000000-0000-0000-0000-000000000000"),
        modified_entities={},
    )
    await client._dispatch_data_update(shared_id, update)

    assert len(received) == 1, "user callback was swallowed by the metadata handler"


@pytest.mark.asyncio
async def test_unsubscribe_does_not_cancel_the_shared_private_subscription() -> None:
    """Unsubscribing a colliding user subscription must not kill the private one."""
    client = _client()
    shared_id = uuid7()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(shared_id)},
            }
        if method == "Unsubscribe":
            return {"success": True, "message": "", "errorCode": 0}
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    await client.get_user_defined_properties("TaskEntity")

    client_id = uuid7()
    client._backend_to_client[shared_id] = {client_id}
    client._client_callbacks[client_id] = lambda _update: None
    client._client_to_backend[client_id] = shared_id

    result = await client.unsubscribe(SubscriptionId(id=client_id))

    assert result.success
    methods = [call.args[0] for call in client._invoke.await_args_list]
    assert "Unsubscribe" not in methods, (
        "Unsubscribe cancelled the still-needed private UDP subscription"
    )
    assert client._user_defined_subscription_id == shared_id


@pytest.mark.asyncio
async def test_unsubscribe_all_restores_a_subscription_covering_every_type() -> None:
    """``unsubscribe_all`` must leave the private subscription covering all types.

    The per-type reconcile that used to run inside the restoration gather could
    early-return once ``desired`` already matched, so the batch relies on a
    single explicit reconcile afterwards to guarantee the final subscription
    covers everything that was restored.
    """
    client = _client()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(uuid7())},
            }
        if method in ("Unsubscribe", "UnsubscribeAll"):
            return {"success": True, "message": "", "errorCode": 0}
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    type_names = ("TaskEntity", "UserEntity", "ScopeEntity")
    for type_name in type_names:
        await client.get_user_defined_properties(type_name)

    await client.unsubscribe_all()

    assert client._user_defined_subscription_types == frozenset(type_names)
    assert client._user_defined_subscription_id is not None


@pytest.mark.asyncio
async def test_close_during_discovery_raises_connection_error_not_cancelled() -> None:
    """A close landing while discovery is in flight surfaces as a connection error.

    ``_handle_close`` fails the loader's pending invocation and then clears the
    UDP state in the same step, before the loader resumes.  Cancelling the loader
    there replaced its connection error with ``CancelledError``, which reached a
    caller that was never cancelled.
    """
    client = _client()
    client._signalr_client.send = AsyncMock(return_value=None)

    caller = asyncio.create_task(client.get_user_defined_properties("TaskEntity"))
    for _ in range(10):
        if client._pending_invocations:
            break
        await asyncio.sleep(0)
    assert len(client._pending_invocations) == 1

    await client._handle_close()

    with pytest.raises(FilamentConnectionError):
        await caller


@pytest.mark.asyncio
async def test_discovery_reply_landing_before_close_is_not_published() -> None:
    """A loader left running across a close must not repopulate the cleared cache."""
    client = _client()
    captured: dict[str, Any] = {}

    async def send(method: str, args: list[Any], on_invocation: Any) -> None:
        captured["on_invocation"] = on_invocation

    client._signalr_client.send = AsyncMock(side_effect=send)
    caller = asyncio.create_task(client.get_user_defined_properties("TaskEntity"))
    for _ in range(10):
        if client._pending_invocations:
            break
        await asyncio.sleep(0)

    # The reply is delivered, then the socket closes before the loader resumes.
    await captured["on_invocation"](
        CompletionMessage(invocation_id="x", result=_permissions_response()),
    )
    await client._handle_close()
    for _ in range(5):
        await asyncio.sleep(0)

    assert "TaskEntity" not in client._user_defined_properties
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_cancelling_one_caller_does_not_cancel_shared_discovery() -> None:
    client = _client()
    release = asyncio.Event()

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            await release.wait()
            return _permissions_response()
        if method == "Subscribe":
            return {
                "success": True, "message": "", "errorCode": 0,
                "data": {"id": str(uuid7())},
            }
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    first = asyncio.create_task(client.get_user_defined_properties("TaskEntity"))
    second = asyncio.create_task(client.get_user_defined_properties("TaskEntity"))
    for _ in range(5):
        await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await second

    assert result.success and result.data is not None
    assert "udp_budget" in result.data


@pytest.mark.asyncio
async def test_shutdown_during_discovery_raises_connection_error_not_cancelled() -> None:
    """``__aexit__`` cancels loaders; callers waiting on them see a connection error."""
    client = _client()
    never = asyncio.Event()

    async def invoke(method: str, *args: Any) -> Any:
        await never.wait()
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    caller = asyncio.create_task(client.get_user_defined_properties("TaskEntity"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert client._all_user_defined_property_tasks

    await client.__aexit__(None, None, None)

    with pytest.raises(FilamentConnectionError):
        await caller


@pytest.mark.asyncio
async def test_data_update_needing_discovery_does_not_block_the_receive_loop() -> None:
    """Discovery triggered by a ``DataUpdated`` must not deadlock the transport.

    Like pysignalr, the fake transport reads one frame at a time and awaits its
    handler before reading the next, and the server's replies to discovery
    arrive as later frames.  Hydrating inside the handler would wait forever for
    a completion the loop can no longer read.
    """
    client = _client()
    frames: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
    responses: dict[str, Any] = {
        "GetEntityPropertyPermissions": _permissions_response(),
        "Subscribe": {
            "success": True, "message": "", "errorCode": 0,
            "data": {"id": str(uuid7())},
        },
    }

    async def send(method: str, args: list[Any], on_invocation: Any) -> None:
        message = CompletionMessage(invocation_id=method, result=responses[method])

        async def reply() -> None:
            await on_invocation(message)

        frames.put_nowait(reply)

    client._signalr_client.send = AsyncMock(side_effect=send)

    async def receive_loop() -> None:
        while True:
            frame = await frames.get()
            await frame()

    backend_id = uuid7()
    client_id = uuid7()
    received: list[ServiceDataUpdate] = []
    client._backend_to_client[backend_id] = {client_id}
    client._client_callbacks[client_id] = received.append
    client._client_to_backend[client_id] = backend_id
    payload = {
        "scenarioId": str(UUID(int=0)),
        "status": 1,
        "requestIds": [],
        "subscriptionId": {"id": str(backend_id)},
        "modifiedEntities": {
            "TaskEntity": {
                "created": [
                    {"$type": "TaskEntity", "id": str(uuid7()), "udpBudget": "12.50"},
                ],
                "updated": [],
                "deleted": [],
            },
        },
        "progress": False,
        "progressAmount": 0.0,
        "sequenceIdx": 0,
        "partIdx": 0,
        "partCount": 1,
    }

    async def data_updated() -> None:
        await client._handle_data_updated(payload)

    frames.put_nowait(data_updated)
    loop_task = asyncio.create_task(receive_loop())
    try:
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0)
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task

    assert len(received) == 1
    entity = received[0].modified_entities["TaskEntity"].created[0]
    assert isinstance(entity, TaskEntity)
    assert entity.udp_budget == Decimal("12.50")


@pytest.mark.asyncio
async def test_colliding_udp_names_surface_as_a_load_error() -> None:
    client = _client()
    response = _permissions_response()
    duplicate = dict(response["data"]["properties"]["udpBudget"])
    duplicate["userDefinedData"] = {
        **duplicate["userDefinedData"], "id": str(uuid7()), "name": "UDPBudget",
    }
    response["data"]["properties"]["UDPBudget"] = duplicate

    async def invoke(method: str, *args: Any) -> Any:
        if method == "GetEntityPropertyPermissions":
            return response
        raise AssertionError(method)

    client._invoke = AsyncMock(side_effect=invoke)
    result = await client.get_user_defined_properties("TaskEntity")

    assert not result.success
    assert "UdpBudget" in result.message and "UDPBudget" in result.message
    assert "TaskEntity" not in client._user_defined_properties

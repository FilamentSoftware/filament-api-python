"""Client integration tests for UDP registry use across hub operations."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
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

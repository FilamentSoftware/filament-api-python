"""modify_data: what an Update of a read-back entity puts on the wire."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament._serialization import from_dict
from filament.types.generated.entities.permissions_service import PermissionEntity
from filament.types.generated.base.base import EntityBase
from filament.types.generated.enums import (
    DataRequestAction,
    PermissionAction,
    TypeSchemaPropertyType,
)
from filament.types.generated.misc import EntityPropertyPermissions, ModifyDataRequest
from filament.user_defined_properties import build_user_defined_properties

_PERMISSION_ID = "01a1f478-a944-7858-a6d1-309fa8455af9"
_SOURCE_ID = "01a1f478-a944-7858-a6d1-309fa8455afa"


def _client(
    properties: Optional[dict[str, Any]] = None,
) -> tuple[FilamentClient, AsyncMock]:
    with patch("filament._client.SignalRClient"):
        client = FilamentClient(
            organization_id="org", api_key_name="key", api_key="secret",
        )
    # Pre-load the user-defined property map (empty unless *properties* is
    # given, in wire form) so modify_data does not fetch one.
    permissions = from_dict({"properties": properties or {}}, EntityPropertyPermissions)
    assert isinstance(permissions, EntityPropertyPermissions)
    client._user_defined_properties["PermissionEntity"] = build_user_defined_properties(
        "PermissionEntity", permissions,
    )
    client._user_defined_subscription_types = frozenset({"PermissionEntity"})
    client._user_defined_subscription_id = uuid7()
    invoke = AsyncMock(return_value={
        "success": True, "message": "", "errorCode": 0, "data": {"id": str(uuid7())},
    })
    client._invoke = invoke  # type: ignore[method-assign]
    return client, invoke


def _read_back_permission() -> PermissionEntity:
    # As the hub returns it on a composed read: transient grantOrigin and
    # sourcePermissionId are populated.
    entity = from_dict(
        {
            "$type": "PermissionEntity",
            "id": _PERMISSION_ID,
            "typeName": "TaskEntity",
            "action": 2,
            "description": "Read tasks",
            "sourcePermissionId": _SOURCE_ID,
            "grantOrigin": "Core",
        },
        PermissionEntity,
    )
    assert isinstance(entity, PermissionEntity)
    assert entity.specified_properties is None
    return entity


async def _sent_entities(
    entities: list[EntityBase],
    action: DataRequestAction,
    update_properties: Optional[list[str]] = None,
    properties: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    client, invoke = _client(properties)
    result = await client.modify_data(ModifyDataRequest(
        scenario_id=UUID(int=0),
        action=action,
        update_properties=update_properties,
        data={"PermissionEntity": entities},
    ))
    assert result.success
    assert invoke.await_args is not None
    method, payload = invoke.await_args.args
    assert method == "ModifyData"
    sent: list[dict[str, Any]] = payload["data"]["PermissionEntity"]
    return sent


async def _sent_entity(
    entity: PermissionEntity,
    action: DataRequestAction,
    update_properties: Optional[list[str]] = None,
    properties: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return (await _sent_entities([entity], action, update_properties, properties))[0]


@pytest.mark.asyncio
async def test_read_back_update_sends_only_id_and_update_properties() -> None:
    entity = _read_back_permission()
    entity.description = "Read all tasks"

    sent = await _sent_entity(entity, DataRequestAction.Update, ["description"])

    assert sent == {
        "$type": "PermissionEntity",
        "id": _PERMISSION_ID,
        "description": "Read all tasks",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["include_properties", "includeProperties", "IncludeProperties"])
async def test_read_back_update_matches_any_property_name_case(name: str) -> None:
    entity = _read_back_permission()
    entity.include_properties = ["name"]

    sent = await _sent_entity(entity, DataRequestAction.Update, [name])

    assert set(sent) == {"$type", "id", "includeProperties"}
    assert sent["includeProperties"] == ["name"]


@pytest.mark.asyncio
async def test_constructed_update_is_not_narrowed() -> None:
    # A constructed entity already sends only what it was given, even beyond
    # update_properties.
    entity = PermissionEntity(
        id=UUID(_PERMISSION_ID), description="x", action=PermissionAction.Read,
    )

    sent = await _sent_entity(entity, DataRequestAction.Update, ["description"])

    assert set(sent) == {"$type", "id", "description", "action"}


@pytest.mark.asyncio
async def test_read_back_create_is_not_narrowed() -> None:
    sent = await _sent_entity(_read_back_permission(), DataRequestAction.Create)

    assert sent["sourcePermissionId"] == _SOURCE_ID
    assert "grantOrigin" in sent


@pytest.mark.asyncio
async def test_read_back_delete_is_not_narrowed() -> None:
    sent = await _sent_entity(_read_back_permission(), DataRequestAction.Delete)

    assert sent["sourcePermissionId"] == _SOURCE_ID
    assert "grantOrigin" in sent


@pytest.mark.asyncio
async def test_read_back_update_without_update_properties_is_not_narrowed() -> None:
    # The backend treats a null UpdateProperties as "every property in the
    # body", so there is nothing to narrow to.
    sent = await _sent_entity(_read_back_permission(), DataRequestAction.Update)

    assert sent["description"] == "Read tasks"
    assert sent["sourcePermissionId"] == _SOURCE_ID


@pytest.mark.asyncio
async def test_read_back_update_with_no_update_properties_sends_only_id() -> None:
    # An empty list names nothing to write, which is all the backend would
    # have written from a full body.
    sent = await _sent_entity(_read_back_permission(), DataRequestAction.Update, [])

    assert sent == {"$type": "PermissionEntity", "id": _PERMISSION_ID}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "PermissionEntity.description",
    "permissionentity.Description",
    " PermissionEntity . description ",
])
async def test_read_back_update_keeps_type_qualified_property(name: str) -> None:
    sent = await _sent_entity(_read_back_permission(), DataRequestAction.Update, [name])

    assert set(sent) == {"$type", "id", "description"}


@pytest.mark.asyncio
async def test_read_back_update_keeps_type_qualified_snake_case_property() -> None:
    entity = _read_back_permission()
    entity.include_properties = ["name"]

    sent = await _sent_entity(
        entity, DataRequestAction.Update, ["PermissionEntity.include_properties"],
    )

    assert set(sent) == {"$type", "id", "includeProperties"}


@pytest.mark.asyncio
async def test_read_back_update_drops_property_qualified_for_another_type() -> None:
    sent = await _sent_entity(
        _read_back_permission(), DataRequestAction.Update, ["TaskEntity.description"],
    )

    assert sent == {"$type": "PermissionEntity", "id": _PERMISSION_ID}


@pytest.mark.asyncio
async def test_update_narrows_read_back_entities_only_in_a_mixed_request() -> None:
    constructed = PermissionEntity(
        id=uuid7(), description="x", action=PermissionAction.Read,
    )

    read_back, built = await _sent_entities(
        [_read_back_permission(), constructed],
        DataRequestAction.Update,
        ["description"],
    )

    assert set(read_back) == {"$type", "id", "description"}
    assert set(built) == {"$type", "id", "description", "action"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["udp_budget", "udpBudget", "UdpBudget"])
async def test_read_back_update_keeps_user_defined_property(name: str) -> None:
    entity = _read_back_permission()
    setattr(entity, "udp_budget", Decimal("5"))

    sent = await _sent_entity(
        entity,
        DataRequestAction.Update,
        [name],
        properties={
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
    )

    assert set(sent) == {"$type", "id", "udpBudget"}

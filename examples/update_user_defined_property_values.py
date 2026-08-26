"""Partially update several UDP values on an existing user.

This example expects a user named ``jsmith`` and the ``UdpEmployeeNumber``
(String) and ``UdpHourlyRate`` (Decimal) definitions on ``UserEntity``.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import filament
from filament import Condition
from filament.types import (
    DataRequestAction,
    ModifyDataRequest,
    ReadDataRequest,
    ReadDataTypeRequest,
    UserEntity,
)


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        read_result = await f.read_data(
            ReadDataRequest(
                request=ReadDataTypeRequest(
                    type_name="UserEntity",
                    condition=(
                        Condition(UserEntity).prop("user_name").equal_to("jsmith")
                    ),
                ),
            ),
        )
        if (
            not read_result.success
            or read_result.data is None
            or not read_result.data.data
        ):
            print(f"Could not find jsmith: {read_result.message}")
            return

        existing = filament.as_entity(
            read_result.data.data[0].entity, UserEntity
        )
        if existing is None:
            print("The backend did not return a UserEntity")
            return

        # A minimal update entity contains only the identity and values that
        # should change. Explicit None would clear a nullable UDP.
        update = UserEntity(
            id=existing.id,
            _user_defined_properties={
                "udp_employee_number": "EMP-0100",
                "udp_hourly_rate": Decimal("57.25"),
            },
        )
        update_result = await f.modify_data(
            ModifyDataRequest(
                action=DataRequestAction.Update,
                data={"UserEntity": [update]},
                update_properties=[
                    "udp_employee_number",
                    "udp_hourly_rate",
                ],
            ),
        )

        if update_result.success:
            print(f"Updated UDP values (request id: {update_result.data})")
        else:
            print(f"Update error: {update_result.message}")


if __name__ == "__main__":
    asyncio.run(main())

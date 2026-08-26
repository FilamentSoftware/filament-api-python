"""Create a user while setting several user-defined properties at once.

This example expects ``UdpEmployeeNumber`` (String) and ``UdpHourlyRate``
(Decimal) to be defined on ``UserEntity``. An administrator can create them by
running ``create_user_defined_properties.py`` first.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import filament
from filament.types import (
    DataRequestAction,
    ModifyDataRequest,
    UserEntity,
    UserStatusEnum,
    UserTypeEnum,
)


async def main() -> None:
    user = UserEntity(
        given_name="Alex",
        family_name="Morgan",
        user_name="amorgan",
        email_address="alex.morgan@example.com",
        schedule_status=UserStatusEnum.Enabled,
        user_type=UserTypeEnum.Internal,
        # This constructor-only mapping makes it possible to set several
        # tenant-specific UDPs without adding them to the generated dataclass.
        _user_defined_properties={
            "udp_employee_number": "EMP-0042",
            "udp_hourly_rate": Decimal("52.50"),
        },
    )

    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        result = await f.modify_data(
            ModifyDataRequest(
                action=DataRequestAction.Create,
                data={"UserEntity": [user]},
            ),
        )

        if result.success:
            print(f"Created user with UDP values (request id: {result.data})")
        else:
            print(f"Create error: {result.message}")


if __name__ == "__main__":
    asyncio.run(main())

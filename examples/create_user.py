"""Create a new UserEntity."""
from __future__ import annotations

import asyncio

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
        given_name="Jane",
        family_name="Smith",
        user_name="jsmith",
        email_address="jane.smith@example.com",
        schedule_status=UserStatusEnum.Enabled,
        user_type=UserTypeEnum.Internal,
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
            print(f"Created user (request id: {result.data})")
        else:
            print(f"Error: {result.message}")


if __name__ == "__main__":
    asyncio.run(main())

"""Find a user by username and update their schedule status to Disabled."""
from __future__ import annotations

import asyncio

import filament
from filament import Condition
from filament.types import (
    DataRequestAction,
    ModifyDataRequest,
    ReadDataRequest,
    ReadDataTypeRequest,
    ServiceDataRead,
    UserEntity,
    UserStatusEnum,
)


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        # Step 1: Read the user created by create_user.py
        condition = Condition(UserEntity).prop("user_name").equal_to("jsmith")

        read_result = await f.read_data(
            ReadDataRequest(
                request=ReadDataTypeRequest(
                    type_name="UserEntity",
                    condition=condition,
                ),
            ),
        )

        if not read_result.success:
            print(f"Read error: {read_result.message}")
            return

        if read_result.data is None or not read_result.data.data:
            print("User 'jsmith' not found")
            return

        data: ServiceDataRead = read_result.data
        user = filament.as_entity(data.data[0].entity, UserEntity)
        if user is None:
            print("Failed to parse user entity")
            return

        print(f"Found user: {user.given_name} {user.family_name} "
              f"(status: {user.schedule_status.name})")

        # Step 2: Create a new entity with just the id and updated field
        updated_user = UserEntity(
            id=user.id,
            schedule_status=UserStatusEnum.Disabled,
        )

        update_result = await f.modify_data(
            ModifyDataRequest(
                action=DataRequestAction.Update,
                update_properties=["scheduleStatus"],
                data={"UserEntity": [updated_user]},
            ),
        )

        if update_result.success:
            print(f"Updated user schedule status to Disabled "
                  f"(request id: {update_result.data})")
        else:
            print(f"Update error: {update_result.message}")


if __name__ == "__main__":
    asyncio.run(main())

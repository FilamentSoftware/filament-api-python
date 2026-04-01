"""Find a user by username and delete them."""
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

        print(f"Found user: {user.given_name} {user.family_name}")

        # Step 2: Delete the user by passing an entity with just the id
        delete_user = UserEntity(id=user.id)

        delete_result = await f.modify_data(
            ModifyDataRequest(
                action=DataRequestAction.Delete,
                data={"UserEntity": [delete_user]},
            ),
        )

        if delete_result.success:
            print(f"Deleted user (request id: {delete_result.data})")
        else:
            print(f"Delete error: {delete_result.message}")


if __name__ == "__main__":
    asyncio.run(main())

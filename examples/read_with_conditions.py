"""Read users matching a condition using the typed Condition builder and operators."""
from __future__ import annotations

import asyncio

import filament
from filament import Condition
from filament.types import (
    ReadDataRequest,
    ReadDataTypeRequest,
    ServiceDataRead,
    UserEntity,
    UserStatusEnum,
)


async def main() -> None:
    # Build a condition: enabled users whose name starts with "A"
    condition = (
        Condition(UserEntity).prop("schedule_status").equal_to(UserStatusEnum.Enabled)
        & Condition(UserEntity).prop("given_name").like("A%")
    )

    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        result = await f.read_data(
            ReadDataRequest(
                request=ReadDataTypeRequest(
                    type_name="UserEntity",
                    properties=[
                        "givenName",
                        "familyName",
                        "userName",
                        "scheduleStatus",
                    ],
                    condition=condition,
                ),
            ),
        )

        if not result.success:
            print(f"Error: {result.message}")
            return

        if result.data is None:
            print("No data returned")
            return

        data: ServiceDataRead = result.data
        print(f"Found {data.entity_count} matching user(s)\n")
        print(f"{'Name':<30} {'Username':<20} {'Status'}")
        print("-" * 70)

        for wrapped in data.data:
            user = filament.as_entity(wrapped.entity, UserEntity)
            if user is None:
                continue
            name = f"{user.given_name or ''} {user.family_name or ''}".strip()
            print(f"{name:<30} {user.user_name:<20} {user.schedule_status}")


if __name__ == "__main__":
    asyncio.run(main())

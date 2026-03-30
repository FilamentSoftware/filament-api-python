"""List all users with their names, usernames, and email addresses."""
from __future__ import annotations

import asyncio

import filament
from filament.types import (
    ReadDataRequest,
    ReadDataTypeRequest,
    ServiceDataRead,
    UserEntity,
)


async def main() -> None:
    async with filament.FilamentClient(
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
                        "emailAddress",
                    ],
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
        print(f"{'Name':<30} {'Username':<20} {'Email'}")
        print("-" * 80)

        for wrapped in data.data:
            user = filament.as_entity(wrapped.entity, UserEntity)
            if user is None:
                continue
            name = f"{user.given_name} {user.family_name}".strip()
            print(f"{name:<30} {user.user_name:<20} {user.email_address}")


if __name__ == "__main__":
    asyncio.run(main())

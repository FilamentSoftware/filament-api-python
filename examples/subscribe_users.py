"""Subscribe to real-time changes on UserEntity."""
from __future__ import annotations

import asyncio

import filament
from filament.types import (
    DataProcessingStatus,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionMode,
    UserEntity,
)


def on_user_updated(update: ServiceDataUpdate) -> None:
    print(f"  Status: {update.status.name}")
    for changes in update.modified_entities.values():
        for entity in changes.created:
            user = filament.as_entity(entity, UserEntity)
            if user is None:
                continue
            print(f"  + Created: {user.given_name} {user.family_name} ({user.user_name})")

        for updated in changes.updated:
            if updated.entity is None:
                continue
            user = filament.as_entity(updated.entity, UserEntity)
            if user is None:
                continue
            props = ", ".join(updated.updated_properties)
            print(f"  ~ Updated: {user.given_name} {user.family_name} [{props}]")

        for entity in changes.deleted:
            user = filament.as_entity(entity, UserEntity)
            if user is None:
                continue
            print(f"  - Deleted: {user.given_name} {user.family_name} ({user.user_name})")


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        result = await f.subscribe(
            SubscriptionConfig(
                status=DataProcessingStatus.All,
                type_name="UserEntity",
                mode=SubscriptionMode.Data,
            ),
            callback=on_user_updated,
        )

        if not result.success:
            print(f"Failed to subscribe: {result.message}")
            return

        print("Subscribed to UserEntity changes. Listening...")

        # Keep running until interrupted
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            print("\nUnsubscribing...")
            if result.data is not None:
                await asyncio.shield(f.unsubscribe(result.data))
            raise


if __name__ == "__main__":
    asyncio.run(main())

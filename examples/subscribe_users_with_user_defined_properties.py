"""Subscribe to users and access converted UDP values in update callbacks.

This example expects ``UdpEmployeeNumber`` (String) and ``UdpHourlyRate``
(Decimal) to be defined on ``UserEntity``. The client loads and caches the
definitions before subscribing, then privately tracks accessible definition
changes so later callbacks continue to use current conversions.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import filament
from filament.types import (
    DataProcessingStatus,
    EntityBase,
    ServiceDataUpdate,
    SubscriptionConfig,
    SubscriptionMode,
    UserEntity,
)


def describe_user(entity: EntityBase) -> Optional[str]:
    user = filament.as_entity(entity, UserEntity)
    if user is None:
        return None
    return (
        f"{user.given_name} {user.family_name}: "
        f"employee={user.udp_employee_number!r}, rate={user.udp_hourly_rate!r}"
    )


def on_user_updated(update: ServiceDataUpdate) -> None:
    print(f"Status: {update.status.name}")
    for changes in update.modified_entities.values():
        for entity in changes.created:
            if (description := describe_user(entity)) is not None:
                print(f"+ {description}")

        for updated in changes.updated:
            if updated.entity is None:
                continue
            if (description := describe_user(updated.entity)) is not None:
                properties = ", ".join(updated.updated_properties)
                print(f"~ {description} [{properties}]")

        for entity in changes.deleted:
            if (description := describe_user(entity)) is not None:
                print(f"- {description}")


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
                properties=[
                    "given_name",
                    "family_name",
                    "udp_employee_number",
                    "udp_hourly_rate",
                ],
            ),
            callback=on_user_updated,
        )

        if not result.success:
            print(f"Failed to subscribe: {result.message}")
            return

        print("Subscribed to UserEntity changes. Listening...")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if result.data is not None:
                await asyncio.shield(f.unsubscribe(result.data))
            raise


if __name__ == "__main__":
    asyncio.run(main())

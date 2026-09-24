"""Discover UDP definitions, build a condition, and read converted values.

This example expects ``UdpEmployeeNumber`` (String) and ``UdpHourlyRate``
(Decimal) to be defined on ``UserEntity``. An administrator can create them by
running ``create_user_defined_properties.py`` first.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import filament
from filament.types import ReadDataRequest, ReadDataTypeRequest, UserEntity


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        properties_result = await f.get_user_defined_properties("UserEntity")
        if not properties_result.success or properties_result.data is None:
            print(f"Could not discover UDPs: {properties_result.message}")
            return

        print("User-defined properties visible to this API key:")
        for name, prop in sorted(properties_result.data.items()):
            print(
                f"  {name}: {prop.label} "
                f"({prop.property_type.name}, actions={prop.actions})"
            )

        hourly_rate = properties_result.data.get("udp_hourly_rate")
        if hourly_rate is None:
            print("udp_hourly_rate is not defined or is not visible to this API key")
            return

        # The definition supplies the correct condition wrapper and converts the
        # Decimal to the backend's wire representation.
        # A UDP's condition capabilities depend on its runtime definition, so
        # the wrapper is intentionally dynamic at type-check time.
        condition_property: Any = hourly_rate.condition()
        condition = condition_property.equal_to(Decimal("52.50"))
        result = await f.read_data(
            ReadDataRequest(
                request=ReadDataTypeRequest(
                    type_name="UserEntity",
                    properties=[
                        "given_name",
                        "family_name",
                        "udp_employee_number",
                        "udp_hourly_rate",
                    ],
                    condition=condition,
                ),
            ),
        )

        if not result.success or result.data is None:
            print(f"Read error: {result.message}")
            return

        for wrapped in result.data.data:
            user = filament.as_entity(wrapped.entity, UserEntity)
            if user is None:
                continue
            # UDPs are flat attributes. The Decimal definition causes
            # udp_hourly_rate to be converted to decimal.Decimal on reads.
            print(
                user.given_name,
                user.family_name,
                user.udp_employee_number,
                user.udp_hourly_rate,
                type(user.udp_hourly_rate).__name__,
            )


if __name__ == "__main__":
    asyncio.run(main())

"""Create the user-defined property definitions used by the UDP examples.

This example requires an administrator API key. Run it once for an organisation
before running the other user-defined property examples. If either definition
already exists, the backend will return an error for that definition and the
script will continue with the next one.
"""
from __future__ import annotations

import asyncio

import filament
from filament.types import (
    CreateUserDefinedPropertyRequest,
    TypeSchemaPropertyType,
)


DEFINITIONS = [
    CreateUserDefinedPropertyRequest(
        entity_type_name="UserEntity",
        name="UdpEmployeeNumber",
        label="Employee number",
        description="The organisation's employee reference.",
        property_type=TypeSchemaPropertyType.String,
        nullable=False,
        default_value="",
    ),
    CreateUserDefinedPropertyRequest(
        entity_type_name="UserEntity",
        name="UdpHourlyRate",
        label="Hourly rate",
        description="The user's hourly rate.",
        property_type=TypeSchemaPropertyType.Decimal,
        nullable=True,
    ),
]


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        for request in DEFINITIONS:
            result = await f.create_user_defined_property(request)
            if result.success and result.data is not None:
                print(f"Created {result.data.name} ({result.data.id})")
            else:
                print(f"Could not create {request.name}: {result.message}")


if __name__ == "__main__":
    asyncio.run(main())

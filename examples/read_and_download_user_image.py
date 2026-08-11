"""Read a UserEntity's profile image projection and download it directly.

Requesting the ``imageData`` ``[FileDataProperty]`` with ``populate_file_data=True``
makes the hub embed a ready-to-fetch ``FileReference`` (as a JSON string).  The
client deserializes it automatically into the ``image`` field, so we can download
the bytes straight from its signed URL — no ``get_file_urls()`` call, and no
manual parsing needed.
"""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

import filament
from filament.types import (
    ReadDataRequest,
    ReadDataTypeRequest,
    UserEntity,
)


async def main() -> None:
    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        result = await f.read_data(
            ReadDataRequest(
                request=ReadDataTypeRequest(
                    type_name="UserEntity",
                    properties=["givenName", "familyName", "imageData"],
                    # Populate the [FileDataProperty] projections requested above.
                    populate_file_data=True,
                ),
            ),
        )

        if not result.success or result.data is None:
            print(f"Error: {result.message}")
            return

        for wrapped in result.data.data:
            user = filament.as_entity(wrapped.entity, UserEntity)
            # ``image`` is deserialized automatically from the imageData
            # projection and already carries a signed, ready-to-fetch URL.
            if user is None or user.image is None:
                continue

            name = f"{user.given_name or ''} {user.family_name or ''}".strip()
            if not user.image.success:
                print(f"{name}: image unavailable ({user.image.message})")
                continue

            # Download straight from the reference — no get_file_urls() call.
            download = await f.download_file_reference(user.image)
            if not download.success or download.data is None:
                print(f"{name}: download failed ({download.message})")
                continue

            # Keep the uploaded file's extension: prefer the original filename
            # the reference carries, falling back to one guessed from the
            # content type.
            suffix = Path(user.image.name).suffix if user.image.name else ""
            if not suffix and user.image.metadata is not None:
                suffix = (
                    mimetypes.guess_extension(user.image.metadata.content_type) or ""
                )
            destination = Path(f"{user.id}{suffix}")
            destination.write_bytes(download.data)
            print(f"{name}: wrote {len(download.data)} bytes to {destination}")


if __name__ == "__main__":
    asyncio.run(main())

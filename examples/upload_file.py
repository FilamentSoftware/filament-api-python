"""Upload a local file and attach it to an entity's file slot.

``upload_file`` runs the whole flow for you: it requests an upload grant,
computes whatever checksum the server's file store requires (learned during the
connection handshake), POSTs the bytes directly to storage, and confirms
completion — refreshing the grant once if it expires mid-transfer.
"""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from uuid_utils import UUID

import filament
from filament.types import (
    DataRequestAction,
    ModifyDataRequest,
    UserEntity,
)


async def main() -> None:
    # The local file to upload.
    path = Path("photo.png")
    data = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    # The entity and file slot that will own the upload.  ``reference_property``
    # is the file-link property declared on the entity type — the permission
    # anchor that gates who may attach a file here.
    reference_type = "UserEntity"
    reference_id = UUID("00000000-0000-0000-0000-000000000000")
    reference_property = "ImageId"

    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        result = await f.upload_file(
            name=path.name,
            content_type=content_type,
            data=data,
            reference_type=reference_type,
            reference_id=reference_id,
            reference_property=reference_property,
        )

        if not result.success or result.data is None:
            print(f"Upload failed: {result.message}")
            return

        file = result.data
        print(
            f"Uploaded {path.name} as file {file.id} "
            f"({file.state.name}, {file.size} bytes)"
        )

        # Attach the uploaded file to the user by writing its id into the
        # ImageId slot (a partial update — only imageId is touched).
        link_result = await f.modify_data(
            ModifyDataRequest(
                action=DataRequestAction.Update,
                update_properties=["imageId"],
                data={"UserEntity": [UserEntity(id=reference_id, image_id=file.id)]},
            ),
        )
        if link_result.success:
            print(f"Linked file to {reference_type} {reference_id}.ImageId")
        else:
            print(f"Link failed: {link_result.message}")


if __name__ == "__main__":
    asyncio.run(main())

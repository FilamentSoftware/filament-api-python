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
    reference_property = "Avatar"

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


if __name__ == "__main__":
    asyncio.run(main())

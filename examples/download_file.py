"""Download a file by its id and write the bytes to disk.

``download_file`` mints a fresh, signed read URL for the file and fetches the
bytes from storage, returning them (or the reason the file could not be
resolved — a file you cannot read and one that does not exist look the same).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from uuid_utils import UUID

import filament


async def main() -> None:
    file_id = UUID("00000000-0000-0000-0000-000000000000")
    destination = Path("downloaded.bin")

    async with filament.FilamentClient(
        organization_id="your-organization-id",
        api_key_name="your-api-key-name",
        api_key="your-api-key",
    ) as f:
        result = await f.download_file(file_id)

        if not result.success or result.data is None:
            print(f"Download failed: {result.message}")
            return

        destination.write_bytes(result.data)
        print(f"Wrote {len(result.data)} bytes to {destination}")


if __name__ == "__main__":
    asyncio.run(main())

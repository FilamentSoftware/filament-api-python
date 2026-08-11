"""Tests for the end-to-end upload/download orchestration on FilamentClient.

These drive the real ``upload_file`` / ``download_file`` logic against a
simulated storage backend (``httpx.MockTransport``) while mocking the SignalR
hub wrappers, so the grant→POST→complete sequence (and its refresh/cancel
branches) is exercised without a live server or bucket.  The multipart body
itself is covered separately in ``test_file_upload``.
"""
from __future__ import annotations

import datetime
import hashlib
from typing import Any, Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament.types.generated.entities.core import FileEntity
from filament.types.generated.enums import (
    ErrorCodes,
    FileChecksumAlgorithm,
    FileState,
)
from filament.types.generated.misc import (
    FileCapabilities,
    FileReference,
    ServerCapabilities,
    UploadGrant,
    ValueOrErrorPublic_1,
)


def _make_client() -> FilamentClient:
    with patch("filament._client.SignalRClient"):
        return FilamentClient(organization_id="o", api_key_name="k", api_key="s")


def _grant(file_id: UUID, url: str = "https://storage.example.com/bucket") -> UploadGrant:
    return UploadGrant(
        file_id=file_id,
        url=url,
        method="POST",
        expires_at=datetime.datetime(2026, 8, 10, 17, 0, tzinfo=datetime.timezone.utc),
        max_bytes=1024,
        fields={"key": "k", "Content-Type": "image/png"},
        headers=None,
        checksum_algorithm=FileChecksumAlgorithm.Sha256Hex,
    )


def _ok(data: Any) -> ValueOrErrorPublic_1[Any]:
    return ValueOrErrorPublic_1(
        success=True, message="", error_code=ErrorCodes.None_, error_id=None, data=data
    )


def _fail(message: str, code: ErrorCodes) -> ValueOrErrorPublic_1[Any]:
    return ValueOrErrorPublic_1(
        success=False, message=message, error_code=code, error_id=None, data=None
    )


def _mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

class TestUploadFile:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = ServerCapabilities(
            files=FileCapabilities(
                checksum_algorithm=FileChecksumAlgorithm.Sha256Hex,
                server_side_checksum=True,
            )
        )
        grant = _grant(file_id)
        client.request_file_upload = AsyncMock(return_value=_ok(grant))  # type: ignore[method-assign]
        completed = FileEntity(id=file_id, name="photo.png", state=FileState.Available)
        client.complete_file_upload = AsyncMock(return_value=_ok(completed))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url)))
            return httpx.Response(204)

        data = b"\x89PNG\r\n\x1a\nsome image bytes"
        async with _mock_transport(handler) as http:
            result = await client.upload_file(
                name="photo.png",
                content_type="image/png",
                data=data,
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Avatar",
                http_client=http,
            )

        assert result.success is True
        assert result.data is completed
        # Checksum computed with the advertised algorithm; size from the bytes.
        request_arg = client.request_file_upload.call_args[0][0]
        assert request_arg.size == len(data)
        assert request_arg.checksum == hashlib.sha256(data).hexdigest()
        # Storage endpoint was POSTed exactly once, at the grant URL.
        assert seen == [("POST", grant.url)]
        client.complete_file_upload.assert_awaited_once_with(file_id)
        client.refresh_file_upload_grant.assert_not_awaited()
        client.cancel_file_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_capabilities_means_no_checksum(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.complete_file_upload = AsyncMock(  # type: ignore[method-assign]
            return_value=_ok(FileEntity(id=file_id, name="f", state=FileState.Available))
        )

        async with _mock_transport(lambda r: httpx.Response(204)) as http:
            result = await client.upload_file(
                name="f",
                content_type="text/plain",
                data=b"hello",
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Attachment",
                http_client=http,
            )

        assert result.success is True
        assert client.request_file_upload.call_args[0][0].checksum is None

    @pytest.mark.asyncio
    async def test_grant_refused_short_circuits(self) -> None:
        client = _make_client()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(  # type: ignore[method-assign]
            return_value=_fail("denied", ErrorCodes.PermissionDenied)
        )
        client.complete_file_upload = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        hit = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hit
            hit = True
            return httpx.Response(204)

        async with _mock_transport(handler) as http:
            result = await client.upload_file(
                name="f",
                content_type="text/plain",
                data=b"x",
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Attachment",
                http_client=http,
            )

        assert result.success is False
        assert result.error_code == ErrorCodes.PermissionDenied
        assert result.message == "denied"
        assert hit is False
        client.complete_file_upload.assert_not_awaited()
        client.cancel_file_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transfer_failure_refreshes_then_succeeds(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock(  # type: ignore[method-assign]
            return_value=_ok(_grant(file_id, url="https://storage.example.com/refreshed"))
        )
        completed = FileEntity(id=file_id, name="f", state=FileState.Available)
        client.complete_file_upload = AsyncMock(return_value=_ok(completed))  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(str(request.url))
            # Fail the first attempt (as if expired), accept the retry.
            return httpx.Response(403 if len(attempts) == 1 else 204)

        async with _mock_transport(handler) as http:
            result = await client.upload_file(
                name="f",
                content_type="text/plain",
                data=b"x",
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Attachment",
                http_client=http,
            )

        assert result.success is True
        assert result.data is completed
        assert attempts == [
            "https://storage.example.com/bucket",
            "https://storage.example.com/refreshed",
        ]
        client.refresh_file_upload_grant.assert_awaited_once_with(file_id)
        client.complete_file_upload.assert_awaited_once_with(file_id)
        client.cancel_file_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transfer_failure_twice_cancels(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.complete_file_upload = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock(return_value=None)  # type: ignore[method-assign]

        async with _mock_transport(lambda r: httpx.Response(403, text="denied")) as http:
            result = await client.upload_file(
                name="f",
                content_type="text/plain",
                data=b"x",
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Attachment",
                http_client=http,
            )

        assert result.success is False
        assert result.error_code == ErrorCodes.RequestError
        assert "Storage rejected upload (403)" in result.message
        client.cancel_file_upload.assert_awaited_once_with(file_id)
        client.complete_file_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_failure_does_not_mask_transfer_error(self) -> None:
        # If the best-effort cancel raises, the original transfer error must
        # still be returned rather than the cancellation exception escaping.
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.complete_file_upload = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("cancel hub failed")
        )

        async with _mock_transport(lambda r: httpx.Response(403, text="denied")) as http:
            result = await client.upload_file(
                name="f",
                content_type="text/plain",
                data=b"x",
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Attachment",
                http_client=http,
            )

        assert result.success is False
        assert result.error_code == ErrorCodes.RequestError
        assert "Storage rejected upload (403)" in result.message
        client.cancel_file_upload.assert_awaited_once_with(file_id)
        client.complete_file_upload.assert_not_awaited()


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------

class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client.get_file_urls = AsyncMock(  # type: ignore[method-assign]
            return_value=_ok(
                {file_id: FileReference(success=True, url="https://storage/read/1")}
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://storage/read/1"
            return httpx.Response(200, content=b"the file bytes")

        async with _mock_transport(handler) as http:
            result = await client.download_file(file_id, http_client=http)

        assert result.success is True
        assert result.data == b"the file bytes"

    @pytest.mark.asyncio
    async def test_reference_resolution_failure(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client.get_file_urls = AsyncMock(  # type: ignore[method-assign]
            return_value=_ok(
                {file_id: FileReference(success=False, message="gone", error_code=ErrorCodes.NoData)}
            )
        )

        hit = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hit
            hit = True
            return httpx.Response(200)

        async with _mock_transport(handler) as http:
            result = await client.download_file(file_id, http_client=http)

        assert result.success is False
        assert result.error_code == ErrorCodes.NoData
        assert result.message == "gone"
        assert hit is False

    @pytest.mark.asyncio
    async def test_get_file_urls_failure_propagates(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client.get_file_urls = AsyncMock(  # type: ignore[method-assign]
            return_value=_fail("boom", ErrorCodes.InternalError)
        )

        async with _mock_transport(lambda r: httpx.Response(200)) as http:
            result = await client.download_file(file_id, http_client=http)

        assert result.success is False
        assert result.message == "boom"
        assert result.error_code == ErrorCodes.InternalError

    @pytest.mark.asyncio
    async def test_storage_rejects_download(self) -> None:
        client = _make_client()
        file_id = uuid7()
        client.get_file_urls = AsyncMock(  # type: ignore[method-assign]
            return_value=_ok(
                {file_id: FileReference(success=True, url="https://storage/read/1")}
            )
        )

        async with _mock_transport(lambda r: httpx.Response(404)) as http:
            result = await client.download_file(file_id, http_client=http)

        assert result.success is False
        assert result.error_code == ErrorCodes.RequestError


class TestDownloadFileReference:
    """Downloading from an already-resolved FileReference (no get_file_urls call)."""

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        client = _make_client()
        reference = FileReference(success=True, url="https://storage/read/direct")

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://storage/read/direct"
            return httpx.Response(200, content=b"direct bytes")

        async with _mock_transport(handler) as http:
            result = await client.download_file_reference(reference, http_client=http)

        assert result.success is True
        assert result.data == b"direct bytes"

    @pytest.mark.asyncio
    async def test_unresolved_reference_is_not_fetched(self) -> None:
        client = _make_client()
        reference = FileReference(
            success=False, message="gone", error_code=ErrorCodes.NoData
        )

        hit = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hit
            hit = True
            return httpx.Response(200)

        async with _mock_transport(handler) as http:
            result = await client.download_file_reference(reference, http_client=http)

        assert result.success is False
        assert result.error_code == ErrorCodes.NoData
        assert result.message == "gone"
        assert hit is False

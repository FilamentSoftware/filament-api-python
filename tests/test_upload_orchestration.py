"""Tests for the end-to-end upload/download orchestration on FilamentClient.

These drive the real ``upload_file`` / ``download_file`` logic against a
simulated storage backend (``httpx.MockTransport``) while mocking the SignalR
hub wrappers, so the grant→POST→complete sequence (and its refresh/cancel
branches) is exercised without a live server or bucket.  The multipart body
itself is covered separately in ``test_file_upload``.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
from typing import Any, Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from uuid_utils import UUID, uuid7

from filament._client import FilamentClient
from filament._exceptions import FilamentConnectionError
from filament.types.generated.entities.core import FileEntity
from filament.types.generated.enums import (
    ErrorCodes,
    FileChecksumAlgorithm,
    FileState,
)
from filament.types.generated.misc import (
    FileCapabilities,
    ServerCapabilities,
    UploadGrant,
    ValueOrErrorPublic_1,
)
from filament.types.generated.shared import FileReference


def _make_client() -> FilamentClient:
    with patch("filament._client.SignalRClient"):
        return FilamentClient(organization_id="o", api_key_name="k", api_key="s")


def _grant(file_id: UUID, url: str = "https://storage.example.com/bucket") -> UploadGrant:
    return UploadGrant(
        file_id=file_id,
        url=url,
        method="POST",
        issued_at=datetime.datetime(2026, 8, 10, 16, 0, tzinfo=datetime.timezone.utc),
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

        async with _mock_transport(lambda _request: httpx.Response(204)) as http:
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

        async with _mock_transport(lambda _request: httpx.Response(403, text="denied")) as http:
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

        async with _mock_transport(lambda _request: httpx.Response(403, text="denied")) as http:
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
    async def test_unavailable_checksum_algorithm_is_a_typed_failure(self) -> None:
        # Crc32cBase64 without the optional google-crc32c dependency raises
        # RuntimeError out of compute_checksum.  upload_file declares a
        # ValueOrErrorPublic_1 result, so that must not escape as an exception —
        # and no reservation should be taken.
        client = _make_client()
        client._server_capabilities = ServerCapabilities(
            files=FileCapabilities(
                checksum_algorithm=FileChecksumAlgorithm.Crc32cBase64,
                server_side_checksum=True,
            )
        )
        client.request_file_upload = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "filament._client.compute_checksum",
            side_effect=RuntimeError(
                "CRC32C checksums require the optional 'google-crc32c' dependency. "
                "Install it with: pip install filament-api[crc32c]"
            ),
        ):
            result = await client.upload_file(
                name="f",
                content_type="text/plain",
                data=b"x",
                reference_type="UserEntity",
                reference_id=uuid7(),
                reference_property="Attachment",
            )

        assert result.success is False
        assert result.error_code == ErrorCodes.NotSupported
        assert "google-crc32c" in result.message
        # Nothing was reserved, so nothing needs releasing.
        client.request_file_upload.assert_not_awaited()
        client.cancel_file_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancellation_releases_the_reservation(self) -> None:
        # Cancelling the upload task after the grant is acquired must release the
        # reservation rather than leaving it pending against the caller's quota,
        # and the cancellation itself must still propagate.
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock()  # type: ignore[method-assign]
        client.complete_file_upload = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        transfer_started = asyncio.Event()

        async def blocked(_request: httpx.Request) -> httpx.Response:
            transfer_started.set()
            await asyncio.Event().wait()  # never completes
            raise AssertionError("unreachable")

        async with _mock_transport(blocked) as http:
            task = asyncio.ensure_future(
                client.upload_file(
                    name="f",
                    content_type="text/plain",
                    data=b"x",
                    reference_type="UserEntity",
                    reference_id=uuid7(),
                    reference_property="Attachment",
                    http_client=http,
                )
            )
            await transfer_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        client.cancel_file_upload.assert_awaited_once_with(file_id)
        client.complete_file_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancellation_during_completion_releases_the_reservation(self) -> None:
        # The transfer has landed but CompleteFileUpload is still in flight.
        # Cancelling there must still release the reservation — cancel is
        # idempotent, so firing it against a completion that may already have
        # succeeded server-side is safe.
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        completing = asyncio.Event()

        async def blocked_completion(_file_id: UUID) -> ValueOrErrorPublic_1[Any]:
            completing.set()
            await asyncio.Event().wait()  # never completes
            raise AssertionError("unreachable")

        client.complete_file_upload = AsyncMock(  # type: ignore[method-assign]
            side_effect=blocked_completion
        )

        async with _mock_transport(lambda _request: httpx.Response(204)) as http:
            task = asyncio.ensure_future(
                client.upload_file(
                    name="f",
                    content_type="text/plain",
                    data=b"x",
                    reference_type="UserEntity",
                    reference_id=uuid7(),
                    reference_property="Attachment",
                    http_client=http,
                )
            )
            await completing.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        client.cancel_file_upload.assert_awaited_once_with(file_id)

    @pytest.mark.asyncio
    async def test_refresh_grant_raising_releases_the_reservation(self) -> None:
        # refresh_file_upload_grant goes through _invoke, which raises when the
        # hub connection drops rather than returning a failed result.  The
        # reservation must still be released and the original error preserved.
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock(  # type: ignore[method-assign]
            side_effect=FilamentConnectionError("hub connection lost")
        )
        client.complete_file_upload = AsyncMock()  # type: ignore[method-assign]
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        async with _mock_transport(lambda _request: httpx.Response(403)) as http:
            with pytest.raises(FilamentConnectionError, match="hub connection lost"):
                await client.upload_file(
                    name="f",
                    content_type="text/plain",
                    data=b"x",
                    reference_type="UserEntity",
                    reference_id=uuid7(),
                    reference_property="Attachment",
                    http_client=http,
                )

        client.cancel_file_upload.assert_awaited_once_with(file_id)
        client.complete_file_upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_completion_raising_releases_the_reservation(self) -> None:
        # Same for a completion call that raises instead of returning a result.
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock()  # type: ignore[method-assign]
        client.complete_file_upload = AsyncMock(  # type: ignore[method-assign]
            side_effect=FilamentConnectionError("hub connection lost")
        )
        client.cancel_file_upload = AsyncMock()  # type: ignore[method-assign]

        async with _mock_transport(lambda _request: httpx.Response(204)) as http:
            with pytest.raises(FilamentConnectionError, match="hub connection lost"):
                await client.upload_file(
                    name="f",
                    content_type="text/plain",
                    data=b"x",
                    reference_type="UserEntity",
                    reference_id=uuid7(),
                    reference_property="Attachment",
                    http_client=http,
                )

        client.cancel_file_upload.assert_awaited_once_with(file_id)

    @pytest.mark.asyncio
    async def test_failing_cleanup_does_not_mask_the_original_exception(self) -> None:
        # The best-effort release must run, and must never replace the error
        # that caused it — even when the release call itself raises.
        client = _make_client()
        file_id = uuid7()
        client._server_capabilities = None
        client.request_file_upload = AsyncMock(return_value=_ok(_grant(file_id)))  # type: ignore[method-assign]
        client.refresh_file_upload_grant = AsyncMock()  # type: ignore[method-assign]
        client.complete_file_upload = AsyncMock(  # type: ignore[method-assign]
            side_effect=FilamentConnectionError("hub connection lost")
        )
        client.cancel_file_upload = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("cancel hub failed")
        )

        async with _mock_transport(lambda _request: httpx.Response(204)) as http:
            with pytest.raises(FilamentConnectionError, match="hub connection lost"):
                await client.upload_file(
                    name="f",
                    content_type="text/plain",
                    data=b"x",
                    reference_type="UserEntity",
                    reference_id=uuid7(),
                    reference_property="Attachment",
                    http_client=http,
                )

        # Without this the test passes even with the release removed, since the
        # completion error propagates on its own.
        client.cancel_file_upload.assert_awaited_once_with(file_id)


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

        async with _mock_transport(lambda _request: httpx.Response(200)) as http:
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

        async with _mock_transport(lambda _request: httpx.Response(404)) as http:
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

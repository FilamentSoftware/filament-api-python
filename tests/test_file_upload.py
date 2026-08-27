"""Tests for the storage-facing file-upload helpers (`filament._file_upload`).

These cover the two pieces that are independent of the SignalR hub: checksum
computation, and the multipart request built from an ``UploadGrant``.  The
multipart assertions pin the provider contract (ordered fields, quoted names,
no per-part Content-Type on fields, file part last) against httpx's real output.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Optional

import pytest

import filament
from filament._file_upload import (
    _CHECKSUM_CRC32C_BASE64,
    _CHECKSUM_MD5_BASE64,
    _CHECKSUM_NONE,
    _CHECKSUM_SHA256_HEX,
    build_upload_request,
    compute_checksum,
)
from filament.types.generated.enums import FileChecksumAlgorithm


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------

class TestComputeChecksum:
    def test_none_returns_none(self) -> None:
        assert compute_checksum(b"anything", FileChecksumAlgorithm.None_) is None

    def test_sha256_hex(self) -> None:
        data = b"hello world"
        result = compute_checksum(data, FileChecksumAlgorithm.Sha256Hex)
        assert result == hashlib.sha256(data).hexdigest()
        # lower-case hex, 64 chars
        assert result is not None
        assert result == result.lower()
        assert len(result) == 64

    def test_md5_base64(self) -> None:
        data = b"hello world"
        result = compute_checksum(data, FileChecksumAlgorithm.Md5Base64)
        assert result == base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
        # canonical padded base64 of a 16-byte digest
        assert result == "XrY7u+Ae7tCTyyK7j1rNww=="

    def test_accepts_raw_int_value(self) -> None:
        data = b"hello world"
        assert compute_checksum(data, _CHECKSUM_SHA256_HEX) == hashlib.sha256(data).hexdigest()

    def test_unknown_algorithm_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_checksum(b"x", 999)

    def test_module_constants_match_enum(self) -> None:
        # The module mirrors the enum's int values to stay import-decoupled from
        # generated code; guard that the mirror stays in step.
        assert _CHECKSUM_NONE == FileChecksumAlgorithm.None_.value
        assert _CHECKSUM_SHA256_HEX == FileChecksumAlgorithm.Sha256Hex.value
        assert _CHECKSUM_MD5_BASE64 == FileChecksumAlgorithm.Md5Base64.value
        assert _CHECKSUM_CRC32C_BASE64 == FileChecksumAlgorithm.Crc32cBase64.value

    def test_crc32c_base64_with_extra(self) -> None:
        google_crc32c = pytest.importorskip("google_crc32c")
        data = b"123456789"
        expected = base64.b64encode(
            google_crc32c.Checksum(data).digest()
        ).decode("ascii")
        assert compute_checksum(data, FileChecksumAlgorithm.Crc32cBase64) == expected

    def test_crc32c_missing_extra_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the optional dependency being absent regardless of the
        # environment, and confirm the guidance error is raised.
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "google_crc32c":
                raise ImportError("simulated missing extra")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(RuntimeError, match="google-crc32c"):
            compute_checksum(b"x", FileChecksumAlgorithm.Crc32cBase64)


# ---------------------------------------------------------------------------
# Multipart request construction
# ---------------------------------------------------------------------------

def _split_parts(body: bytes, boundary: str) -> list[bytes]:
    """Split a multipart body into its individual part blocks (headers+content)."""
    delimiter = b"--" + boundary.encode()
    chunks = body.split(delimiter)
    # First chunk is the preamble (empty), last is the closing "--\r\n".
    return [c for c in chunks[1:-1]]


def _part_name(part: bytes) -> Optional[str]:
    marker = b'name="'
    idx = part.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    end = part.find(b'"', start)
    return part[start:end].decode()


def _header_block(part: bytes) -> bytes:
    # Headers are separated from content by a blank line.
    return part.split(b"\r\n\r\n", 1)[0]


class TestBuildUploadRequest:
    def _build(self) -> tuple[bytes, str]:
        fields = {
            "key": "org/abc/photo.png",
            "policy": "BASE64POLICY==",
            "x-goog-signature": "SIG",
            "Content-Type": "image/png",
        }
        req = build_upload_request(
            url="https://storage.example.com/bucket",
            method="POST",
            fields=fields,
            headers=None,
            data=b"\x89PNG\r\n\x1a\nDATA",
            content_type="image/png",
            filename="photo.png",
        )
        assert req.method == "POST"
        assert str(req.url) == "https://storage.example.com/bucket"
        ctype = req.headers["content-type"]
        assert ctype.startswith("multipart/form-data; boundary=")
        boundary = ctype.split("boundary=", 1)[1]
        # Boundary must NOT be quoted (GCS's multipart parser rejects the
        # RFC-optional quotes).
        assert not boundary.startswith('"')
        return req.read(), boundary

    def test_fields_in_order_then_file_last(self) -> None:
        body, boundary = self._build()
        parts = _split_parts(body, boundary)
        names = [_part_name(p) for p in parts]
        assert names == ["key", "policy", "x-goog-signature", "Content-Type", "file"]

    def test_field_parts_have_no_content_type_header(self) -> None:
        body, boundary = self._build()
        parts = _split_parts(body, boundary)
        for part in parts:
            name = _part_name(part)
            header = _header_block(part).lower()
            if name == "file":
                assert b"content-type:" in header
            else:
                # Policy/field parts must be plain string parts.
                assert b"content-type:" not in header

    def test_file_part_carries_real_content_type(self) -> None:
        body, boundary = self._build()
        parts = _split_parts(body, boundary)
        file_part = next(p for p in parts if _part_name(p) == "file")
        assert b"image/png" in _header_block(file_part).lower()
        assert b"\x89PNG" in file_part

    def test_content_type_field_value_is_the_content_type(self) -> None:
        # The "Content-Type" *field* (a string part) carries the pinned value
        # as its body, not as a header.
        body, boundary = self._build()
        parts = _split_parts(body, boundary)
        ct_field = next(p for p in parts if _part_name(p) == "Content-Type")
        # its content (after the blank line) is the pinned type
        content = ct_field.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n-")
        assert content == b"image/png"

    def test_grant_headers_applied(self) -> None:
        req = build_upload_request(
            url="https://storage.example.com/bucket",
            method="POST",
            fields={"key": "k"},
            headers={"x-custom": "1"},
            data=b"bytes",
            content_type="application/octet-stream",
        )
        assert req.headers["x-custom"] == "1"

    def test_none_fields_is_plain_body_upload(self) -> None:
        # A null fields set means a plain-body upload: raw bytes as the request
        # body, no multipart encoding.
        req = build_upload_request(
            url="https://storage.example.com/bucket",
            method="PUT",
            fields=None,
            headers=None,
            data=b"raw bytes",
            content_type="text/plain",
        )
        assert req.method == "PUT"
        assert req.read() == b"raw bytes"
        assert req.headers["content-type"] == "text/plain"
        assert "multipart" not in req.headers["content-type"]

    def test_plain_body_merges_grant_headers(self) -> None:
        req = build_upload_request(
            url="https://storage.example.com/bucket",
            method="PUT",
            fields=None,
            headers={"x-custom": "1"},
            data=b"raw",
            content_type="application/octet-stream",
        )
        assert req.read() == b"raw"
        assert req.headers["x-custom"] == "1"
        assert req.headers["content-type"] == "application/octet-stream"

    def test_plain_body_grant_content_type_wins_case_insensitively(self) -> None:
        # httpx.Headers keeps "Content-Type" and "content-type" side by side, so
        # a grant that pins the type in lowercase must not end up with two
        # conflicting headers — its value is the only one sent.
        req = build_upload_request(
            url="https://storage.example.com/bucket",
            method="PUT",
            fields=None,
            headers={"content-type": "application/octet-stream"},
            data=b"raw",
            content_type="text/plain",
        )
        assert req.headers.get_list("content-type") == ["application/octet-stream"]

    def test_content_type_field_matched_case_insensitively(self) -> None:
        # A lower-case "content-type" policy field should still drive the file
        # part's content type.
        req = build_upload_request(
            url="https://storage.example.com/bucket",
            method="POST",
            fields={"content-type": "image/jpeg"},
            headers=None,
            data=b"bytes",
            content_type="application/octet-stream",
        )
        ctype = req.headers["content-type"]
        boundary = ctype.split("boundary=", 1)[1]
        file_part = next(
            p for p in _split_parts(req.read(), boundary) if _part_name(p) == "file"
        )
        assert b"image/jpeg" in _header_block(file_part).lower()


# ---------------------------------------------------------------------------
# file_reference_from_json (parsing a [FileDataProperty] projection)
# ---------------------------------------------------------------------------

class TestFileReferenceFromJson:
    def test_roundtrip(self) -> None:
        # A [FileDataProperty] projection (e.g. UserEntity.image_data) is a
        # JSON-encoded FileReference string with camelCase keys.
        payload = {
            "success": True,
            "message": "",
            "errorCode": 0,
            "url": "https://storage/read/1",
            "cacheKey": "abc",
            "expiresAt": "2026-08-10T17:00:00.00000000Z",
            "refreshable": True,
            "metadata": {
                "contentType": "image/png",
                "size": 12,
                "width": 4,
                "height": 4,
            },
        }
        ref = filament.file_reference_from_json(json.dumps(payload))
        assert ref.success is True
        assert ref.url == "https://storage/read/1"
        assert ref.refreshable is True
        assert ref.metadata is not None
        assert ref.metadata.content_type == "image/png"
        assert ref.metadata.width == 4

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError):
            filament.file_reference_from_json("not json")

    @pytest.mark.parametrize("payload", ["null", "[]", '"text"', "123", "true"])
    def test_non_object_json_raises(self, payload: str) -> None:
        # json.loads accepts these, but they are not a FileReference object.
        with pytest.raises(ValueError):
            filament.file_reference_from_json(payload)

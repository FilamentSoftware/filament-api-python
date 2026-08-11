"""File-upload helpers: checksum computation and the storage submission request.

This module owns the provider-facing byte transfer for the file-upload flow: it
turns an ``UploadGrant`` into the exact multipart HTTP request the storage
backend (GCS POST-policy today) will accept, and computes the checksums the
grant may require.

It is deliberately kept free of any *runtime* import from
``filament.types.generated`` — it works in terms of primitives (plain ``dict``
fields, ``bytes``, an integer checksum-algorithm value) so the fiddly,
easy-to-break multipart rules are decoupled from the generated wire types and
can be unit-tested in isolation.  ``_client.py`` adapts ``UploadGrant`` and
``FileChecksumAlgorithm`` onto these functions.
"""
from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from .types.generated.enums import FileChecksumAlgorithm

# Integer values of ``FileChecksumAlgorithm`` (see ``types/generated/enums.py``).
# Mirrored here as plain ints so this module needs no runtime import of the
# generated enum; ``test_file_upload`` asserts these stay in step with it.
_CHECKSUM_NONE = 0
_CHECKSUM_SHA256_HEX = 1
_CHECKSUM_MD5_BASE64 = 2
_CHECKSUM_CRC32C_BASE64 = 3

# The form-field key the POST policy pins the content type under.  Matched
# case-insensitively because the backend compares it that way.
_CONTENT_TYPE_FIELD = "Content-Type"

# Name of the file part.  The backend's reference client uses "file"; the part
# MUST be appended last (the provider ignores anything after it).
_FILE_PART_NAME = "file"


def compute_checksum(
    data: bytes,
    algorithm: "FileChecksumAlgorithm | int",
) -> Optional[str]:
    """Compute *data*'s checksum in the store's declared algorithm/encoding.

    Returns ``None`` when the store computes no checksum
    (:attr:`FileChecksumAlgorithm.None_`), so the result can be handed straight
    to :attr:`FileUploadRequest.checksum`.

    Accepts either the ``FileChecksumAlgorithm`` enum or its raw integer value
    (dispatch is on the value, so no runtime import of the enum is needed).

    :raises ValueError: for an unknown algorithm value.
    :raises RuntimeError: for ``Crc32cBase64`` when the optional
        ``google-crc32c`` dependency is not installed (``filament-api[crc32c]``).
    """
    value = algorithm if isinstance(algorithm, int) else algorithm.value

    if value == _CHECKSUM_NONE:
        return None
    if value == _CHECKSUM_SHA256_HEX:
        return hashlib.sha256(data).hexdigest()
    if value == _CHECKSUM_MD5_BASE64:
        return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
    if value == _CHECKSUM_CRC32C_BASE64:
        return _crc32c_base64(data)
    raise ValueError(f"Unknown checksum algorithm value: {value!r}")


def _crc32c_base64(data: bytes) -> str:
    """CRC32C of *data* as canonical padded base64 (GCS ``crc32c`` form)."""
    try:
        import google_crc32c
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise RuntimeError(
            "CRC32C checksums require the optional 'google-crc32c' dependency. "
            "Install it with: pip install filament-api[crc32c]"
        ) from exc
    digest = google_crc32c.Checksum(data).digest()
    return base64.b64encode(digest).decode("ascii")


def build_upload_request(
    *,
    url: str,
    method: str,
    fields: Optional[dict[str, str]],
    headers: Optional[dict[str, str]],
    data: bytes,
    content_type: str,
    filename: str = "upload.bin",
) -> httpx.Request:
    """Build the storage submission request from an ``UploadGrant``'s primitives.

    Enforces the provider contract exactly:

    - When ``fields`` is ``None`` the grant is a **plain-body** upload: the raw
      bytes are sent as the request body (carrying the content type), not a
      multipart form.  A policy upload is used only when ``fields`` is present.
    - The policy ``fields`` are emitted first, **in order**, as plain form parts
      with a quoted ``name`` and **no per-part ``Content-Type``**.
    - The ``Content-Type`` policy field carries the real content type being
      uploaded (it is pinned by the policy and verified at completion).
    - The file part is appended **last**, named ``file``, with the real content
      type — the provider ignores anything after it.
    - The grant ``headers`` are applied verbatim (normally empty; keeping them
      empty makes a cross-origin POST a CORS *simple* request, avoiding a
      preflight).

    :returns: A ready-to-send :class:`httpx.Request`.
    """
    if fields is None:
        # Plain-body upload: send the raw bytes as the request body with the
        # content type.  ``UploadGrant.fields`` is null precisely for providers
        # that expect a plain body rather than a multipart policy form.
        plain_headers = {"Content-Type": content_type}
        if headers:
            plain_headers.update(headers)
        return httpx.Request(
            method or "POST",
            url,
            content=data,
            headers=plain_headers,
        )

    form_fields = dict(fields)

    # The file part must carry the exact content type the policy pinned, so
    # resolve it case-insensitively from the fields, falling back to the caller
    # supplied value.
    part_content_type = content_type
    for key, val in form_fields.items():
        if key.lower() == _CONTENT_TYPE_FIELD.lower():
            part_content_type = val
            break

    # httpx encodes ``data`` fields (in dict order) before ``files``, so the
    # file part naturally lands last.  String data parts get only a
    # Content-Disposition (no per-part Content-Type), which is what the provider
    # requires.
    files = {_FILE_PART_NAME: (filename, data, part_content_type)}

    return httpx.Request(
        method or "POST",
        url,
        data=form_fields,
        files=files,
        headers=headers or None,
    )

"""Decoding of compressed string property values.

A property marked with ``compression`` / ``encoding`` dataclass field metadata
(generated from the backend's ``[CompressedProperty]`` attribute) holds either
plain text or a value of the form ``<compression>+<encoding>:<payload>``:

* no header (e.g. starts with ``{``, or empty) — plain text, returned as-is;
* ``br+base64:<payload>`` — standard base64 of a Brotli stream of UTF-8 text;
* ``gzip+base64:<payload>`` — standard base64 of a gzip stream of UTF-8 text.

The **header** decides how a value is decoded, never the field metadata: the
metadata only marks a field as one that may carry a header (and names what the
server currently writes).  An unknown token, or a known header whose payload
fails to decode, raises :class:`~filament.CompressedPropertyError`.

Decoding is one-shot and strict: truncated streams and trailing bytes are
rejected for every method, so a corrupt value can never be returned as if it
were complete.  Decoded output is capped at ``_MAX_DECODED_BYTES`` to guard
against decompression bombs; larger values are rejected, never truncated.

Writes are not re-encoded: :func:`~filament._serialization.to_dict` sends the
decoded plain text, which the backend stores as legal header-less plain text.
"""
from __future__ import annotations

import base64
import binascii
import re
import zlib
from collections.abc import Callable, Mapping
from typing import Any, Optional

import brotli

from ._exceptions import CompressedPropertyError

#: The header must end within this many characters of the start of the value.
_MAX_HEADER_LENGTH = 32

#: ``<compression>+<encoding>:`` with lowercase alphanumeric tokens.
_HEADER_RE = re.compile(r"([a-z0-9]+)\+([a-z0-9]+):")

#: Field metadata keys that mark a field as possibly compressed.
_METADATA_KEYS = ("compression", "encoding")

#: Largest decoded (decompressed) value accepted, in bytes.  Guards against
#: decompression bombs; real values are typically a few hundred kilobytes.
_MAX_DECODED_BYTES = 64 * 1024 * 1024

#: Per-call output limit for streaming Brotli decompression.
_BROTLI_CHUNK_BYTES = 1024 * 1024


class _DecodeFailure(Exception):
    """Internal: a payload could not be decoded by a registered method."""


def parse_header(value: str) -> Optional[tuple[str, str, int]]:
    """Parse the ``<compression>+<encoding>:`` header of *value*.

    Returns ``(compression_token, encoding_token, payload_start)``, or ``None``
    when *value* has no header (and is therefore plain text).  Mirrors the
    backend's ``PropertyCompression.TryParseHeader``.
    """
    match = _HEADER_RE.match(value[:_MAX_HEADER_LENGTH])
    if match is None:
        return None
    return match.group(1), match.group(2), match.end()


def _decode_base64(payload: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _DecodeFailure(f"invalid base64 payload: {exc}") from exc


def _too_large() -> _DecodeFailure:
    return _DecodeFailure(
        f"decoded value exceeds the {_MAX_DECODED_BYTES} byte limit"
    )


def _decompress_brotli(data: bytes) -> bytes:
    # Streaming decompression so the output can be bounded.  All input is fed
    # in the first call; later calls pass empty input to drain pending output.
    # Brotli itself rejects trailing bytes and concatenated streams with
    # ``brotli.error``; a stream that never finishes is truncated.
    decompressor = brotli.Decompressor()
    chunks: list[bytes] = []
    total = 0
    pending = data
    try:
        while True:
            limit = min(_BROTLI_CHUNK_BYTES, _MAX_DECODED_BYTES + 1 - total)
            chunk: bytes = decompressor.process(
                pending, output_buffer_limit=limit
            )
            pending = b""
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_DECODED_BYTES:
                raise _too_large()
            if decompressor.is_finished():
                break
            if not chunk:
                # No progress and not finished: the stream needs more input
                # than it was given, so it is truncated.  (Also guarantees the
                # loop cannot spin.)
                raise _DecodeFailure("truncated Brotli stream")
    except brotli.error as exc:
        raise _DecodeFailure(f"invalid Brotli stream: {exc}") from exc
    if not decompressor.can_accept_more_data():
        # Input still buffered after the end of the stream.
        raise _DecodeFailure("trailing bytes after Brotli stream")
    return b"".join(chunks)


def _decompress_gzip(data: bytes) -> bytes:
    # ``gzip.decompress`` accepts trailing NUL padding, concatenated members and
    # empty input, so use a single zlib pass and require a complete, sole member.
    # ``max_length`` bounds the output; hitting it means the value is too large.
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(data, _MAX_DECODED_BYTES + 1)
    except zlib.error as exc:
        raise _DecodeFailure(f"invalid gzip stream: {exc}") from exc
    if len(out) > _MAX_DECODED_BYTES:
        raise _too_large()
    if decompressor.unconsumed_tail:
        # Unreachable below the limit, but never ignore unread input.
        raise _DecodeFailure("gzip stream not fully consumed")
    if not decompressor.eof:
        raise _DecodeFailure("truncated gzip stream")
    if decompressor.unused_data:
        raise _DecodeFailure("trailing bytes after gzip stream")
    return out


#: Encoding token -> decoder from payload text to bytes.
_ENCODINGS: dict[str, Callable[[str], bytes]] = {
    "base64": _decode_base64,
}

#: Compression token -> one-shot strict decompressor.
_COMPRESSIONS: dict[str, Callable[[bytes], bytes]] = {
    "br": _decompress_brotli,
    "gzip": _decompress_gzip,
}


def is_compressed_field(metadata: Mapping[str, Any]) -> bool:
    """Return ``True`` if dataclass field *metadata* marks a compressed field.

    Only the presence of the keys matters; their values are not used to choose
    a decoding method (the value's header does that).
    """
    return any(key in metadata for key in _METADATA_KEYS)


def _describe(owner: str, field_name: str) -> str:
    if owner and field_name:
        return f"{owner}.{field_name}"
    return field_name or owner or "compressed value"


def decode_compressed_value(
    value: str,
    *,
    owner: str = "",
    field_name: str = "",
) -> str:
    """Decode a compressed property *value* to plain text.

    A value without a header is returned unchanged (including ``""``).

    :raises CompressedPropertyError: if the header names an unknown compression
        or encoding, or the payload fails to decode.
    """
    parsed = parse_header(value)
    if parsed is None:
        return value
    compression, encoding, payload_start = parsed
    header = value[:payload_start]
    where = _describe(owner, field_name)

    decode = _ENCODINGS.get(encoding)
    if decode is None:
        raise CompressedPropertyError(
            f"Cannot decode {where}: unsupported encoding {encoding!r} "
            f"in header {header!r}",
            owner=owner, field_name=field_name, header=header,
        )
    decompress = _COMPRESSIONS.get(compression)
    if decompress is None:
        raise CompressedPropertyError(
            f"Cannot decode {where}: unsupported compression {compression!r} "
            f"in header {header!r}",
            owner=owner, field_name=field_name, header=header,
        )

    try:
        raw = decompress(decode(value[payload_start:]))
        return raw.decode("utf-8")
    except (_DecodeFailure, UnicodeDecodeError) as exc:
        raise CompressedPropertyError(
            f"Cannot decode {where} with header {header!r}: {exc}",
            owner=owner, field_name=field_name, header=header,
        ) from exc

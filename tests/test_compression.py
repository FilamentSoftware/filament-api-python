"""Tests for decoding compressed property values (``_compression``) and the
``from_dict`` hook that applies it to fields carrying compression metadata."""
from __future__ import annotations

import base64
import gzip
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

import brotli
import pytest
from uuid_utils import uuid7

import filament
from filament import _compression
from filament._compression import (
    decode_compressed_value,
    is_compressed_field,
    parse_header,
)
from filament._exceptions import CompressedPropertyError, FilamentError
from filament._serialization import from_dict
from filament.types.generated.entities.schedule_service import ScheduleSegmentEntity
from filament.types.generated.misc import (
    ServiceDataRead,
    ServiceDataUpdate,
    ValueOrErrorPublic_1,
)

TEXT = '{"reasons":[{"kind":"blocked","detail":"waiting on café ☃"}]}'


def _brotli(data: bytes) -> bytes:
    out: bytes = brotli.compress(data)
    return out


def _gzip(data: bytes) -> bytes:
    return gzip.compress(data)


COMPRESSORS: dict[str, Callable[[bytes], bytes]] = {"br": _brotli, "gzip": _gzip}


def _encode(text: str, method: str) -> str:
    raw = COMPRESSORS[method](text.encode("utf-8"))
    return f"{method}+base64:" + base64.b64encode(raw).decode("ascii")


def _encode_bytes(raw: bytes, method: str) -> str:
    return f"{method}+base64:" + base64.b64encode(raw).decode("ascii")


METHODS = ["br", "gzip"]


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


class TestParseHeader:
    def test_parses_tokens_and_payload_start(self) -> None:
        assert parse_header("br+base64:abc") == ("br", "base64", 10)
        assert parse_header("gzip+base64:") == ("gzip", "base64", 12)
        assert parse_header("zstd2+b85:x") == ("zstd2", "b85", 10)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            '{"a":1}',
            "Br+base64:x",
            "br+Base64:x",
            "br+:x",
            "+base64:x",
            "br base64:x",
            "br-base64:x",
            "brbase64:x",
            " br+base64:x",
            "br+base64x",
        ],
    )
    def test_no_header(self, value: str) -> None:
        assert parse_header(value) is None

    def test_header_must_end_within_32_characters(self) -> None:
        # 22 + 1 + 8 + 1 = 32 characters: the colon is the 32nd char.
        fits = "a" * 22 + "+" + "b" * 8 + ":payload"
        assert parse_header(fits) == ("a" * 22, "b" * 8, 32)
        # One more character pushes the colon to position 33: plain text.
        too_long = "a" * 23 + "+" + "b" * 8 + ":payload"
        assert parse_header(too_long) is None


# ---------------------------------------------------------------------------
# Value decoding
# ---------------------------------------------------------------------------


class TestDecodeCompressedValue:
    @pytest.mark.parametrize("method", METHODS)
    def test_round_trip(self, method: str) -> None:
        assert decode_compressed_value(_encode(TEXT, method)) == TEXT

    @pytest.mark.parametrize("method", METHODS)
    def test_round_trip_large_value(self, method: str) -> None:
        text = "".join(f'{{"i":{i},"s":"segment reason {i}"}},' for i in range(20000))
        assert decode_compressed_value(_encode(text, method)) == text

    @pytest.mark.parametrize("method", METHODS)
    def test_round_trip_empty_text(self, method: str) -> None:
        assert decode_compressed_value(_encode("", method)) == ""

    @pytest.mark.parametrize(
        "value",
        ["", '{"a":1}', "plain text", "Br+base64:x", "br base64:x", "+base64:x"],
    )
    def test_no_header_passes_through(self, value: str) -> None:
        assert decode_compressed_value(value) == value

    def test_unknown_compression_raises(self) -> None:
        payload = base64.b64encode(b"anything").decode("ascii")
        with pytest.raises(CompressedPropertyError, match="unsupported compression 'zstd'") as info:
            decode_compressed_value(f"zstd+base64:{payload}", owner="Owner", field_name="f")
        assert info.value.header == "zstd+base64:"
        assert info.value.owner == "Owner"
        assert info.value.field_name == "f"

    def test_unknown_encoding_raises(self) -> None:
        with pytest.raises(CompressedPropertyError, match="unsupported encoding 'base85'") as info:
            decode_compressed_value(_encode(TEXT, "br").replace("base64", "base85", 1))
        assert info.value.header == "br+base85:"

    def test_error_is_value_error_and_filament_error(self) -> None:
        with pytest.raises(CompressedPropertyError) as info:
            decode_compressed_value("zstd+base64:AAAA")
        assert isinstance(info.value, ValueError)
        assert isinstance(info.value, FilamentError)

    def test_error_names_owner_and_field(self) -> None:
        with pytest.raises(CompressedPropertyError, match=r"Thing\.notes"):
            decode_compressed_value("br+base64:!!!", owner="Thing", field_name="notes")

    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize(
        "payload",
        ["!!!!", "abc", "YWJj ZA==", "YWJjZA=", "YWJjZA==\n"],
        ids=["bad-alphabet", "bad-padding", "whitespace", "short-padding", "newline"],
    )
    def test_invalid_base64_raises(self, method: str, payload: str) -> None:
        with pytest.raises(CompressedPropertyError, match="invalid base64"):
            decode_compressed_value(f"{method}+base64:{payload}")

    @pytest.mark.parametrize("method", METHODS)
    def test_not_compressed_bytes_raise(self, method: str) -> None:
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(_encode_bytes(TEXT.encode("utf-8"), method))

    @pytest.mark.parametrize("method", METHODS)
    def test_empty_payload_raises(self, method: str) -> None:
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(f"{method}+base64:")

    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize("cut", [1, 4, 10])
    def test_truncated_stream_raises(self, method: str, cut: int) -> None:
        raw = COMPRESSORS[method](TEXT.encode("utf-8") * 50)
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(_encode_bytes(raw[:-cut], method))

    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize("trailing", [b"x", b"\0", b"\0\0\0\0"])
    def test_trailing_bytes_raise(self, method: str, trailing: bytes) -> None:
        raw = COMPRESSORS[method](TEXT.encode("utf-8"))
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(_encode_bytes(raw + trailing, method))

    @pytest.mark.parametrize("method", METHODS)
    def test_concatenated_streams_raise(self, method: str) -> None:
        raw = COMPRESSORS[method](TEXT.encode("utf-8"))
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(_encode_bytes(raw + raw, method))

    def test_gzip_bad_crc_raises(self) -> None:
        raw = bytearray(_gzip(TEXT.encode("utf-8")))
        raw[-8] ^= 0xFF  # first byte of the CRC-32 trailer
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(_encode_bytes(bytes(raw), "gzip"))

    @pytest.mark.parametrize("method", METHODS)
    def test_invalid_utf8_output_raises(self, method: str) -> None:
        raw = COMPRESSORS[method](b"\xff\xfe not utf-8")
        with pytest.raises(CompressedPropertyError, match="utf-8"):
            decode_compressed_value(_encode_bytes(raw, method))

    def test_brotli_header_on_gzip_payload_raises(self) -> None:
        # The header decides the method; a mismatched payload is an error.
        raw = _gzip(TEXT.encode("utf-8"))
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(_encode_bytes(raw, "br"))


def _pseudo_random_text(size: int) -> bytes:
    # Deterministic, poorly compressible ASCII (so Brotli emits many chunks).
    out = bytearray()
    state = 0x12345678
    while len(out) < size:
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out.append(0x21 + (state >> 16) % 94)
    return bytes(out)


def test_default_decoded_size_limit_is_64_mib() -> None:
    assert _compression._MAX_DECODED_BYTES == 64 * 1024 * 1024


class TestDecodedSizeLimit:
    LIMIT = 100_000

    @pytest.fixture(autouse=True)
    def _small_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_compression, "_MAX_DECODED_BYTES", self.LIMIT)
        # Small Brotli chunks so the multi-call draining path is exercised.
        monkeypatch.setattr(_compression, "_BROTLI_CHUNK_BYTES", 4096)

    @pytest.mark.parametrize("method", METHODS)
    def test_compression_bomb_rejected(self, method: str) -> None:
        raw = COMPRESSORS[method](b"a" * (self.LIMIT * 50))
        assert len(raw) < self.LIMIT // 10  # small input, huge output
        with pytest.raises(CompressedPropertyError, match="exceeds"):
            decode_compressed_value(_encode_bytes(raw, method))

    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize("factory", [lambda n: b"a" * n, _pseudo_random_text])
    def test_value_exactly_at_limit_accepted(
        self, method: str, factory: Callable[[int], bytes]
    ) -> None:
        text = factory(self.LIMIT)
        raw = COMPRESSORS[method](text)
        assert decode_compressed_value(_encode_bytes(raw, method)) == text.decode()

    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize("factory", [lambda n: b"a" * n, _pseudo_random_text])
    def test_value_one_byte_over_limit_rejected(
        self, method: str, factory: Callable[[int], bytes]
    ) -> None:
        raw = COMPRESSORS[method](factory(self.LIMIT + 1))
        with pytest.raises(CompressedPropertyError, match="exceeds"):
            decode_compressed_value(_encode_bytes(raw, method))

    @pytest.mark.parametrize("method", METHODS)
    @pytest.mark.parametrize("trailing", [b"x", b"\0"])
    def test_trailing_bytes_after_multi_chunk_stream_raise(
        self, method: str, trailing: bytes
    ) -> None:
        raw = COMPRESSORS[method](_pseudo_random_text(self.LIMIT // 2))
        with pytest.raises(CompressedPropertyError):
            decode_compressed_value(_encode_bytes(raw + trailing, method))

    @pytest.mark.parametrize("method", METHODS)
    def test_truncated_multi_chunk_stream_raises(self, method: str) -> None:
        raw = COMPRESSORS[method](_pseudo_random_text(self.LIMIT // 2))
        with pytest.raises(CompressedPropertyError, match="truncated|invalid"):
            decode_compressed_value(_encode_bytes(raw[:-3], method))


class TestIsCompressedField:
    def test_marked(self) -> None:
        assert is_compressed_field({"compression": "br", "encoding": "base64"})
        assert is_compressed_field({"compression": "gzip"})
        assert is_compressed_field({"encoding": "base64"})

    def test_unmarked(self) -> None:
        assert not is_compressed_field({})
        assert not is_compressed_field({"ignore": True})


def test_compressed_property_error_is_exported() -> None:
    assert filament.CompressedPropertyError is CompressedPropertyError
    assert "CompressedPropertyError" in filament.__all__


# ---------------------------------------------------------------------------
# from_dict integration (generic, metadata-driven)
# ---------------------------------------------------------------------------

_MARK: dict[str, Any] = {"compression": "br", "encoding": "base64"}


@dataclass(kw_only=True)
class _Doc:
    body: str = field(default="", metadata=_MARK)
    maybe: Optional[str] = field(default=None, metadata=_MARK)
    plain: str = ""


@dataclass(kw_only=True)
class _Holder:
    doc: Optional[_Doc] = None
    docs: list[_Doc] = field(default_factory=list)
    by_key: dict[str, _Doc] = field(default_factory=dict)


class TestFromDictDecoding:
    @pytest.mark.parametrize("method", METHODS)
    def test_marked_field_decoded(self, method: str) -> None:
        doc = from_dict({"body": _encode(TEXT, method)}, _Doc)
        assert doc is not None
        assert doc.body == TEXT

    def test_marked_field_without_header_unchanged(self) -> None:
        for value in ("", '{"a":1}'):
            doc = from_dict({"body": value}, _Doc)
            assert doc is not None
            assert doc.body == value

    def test_unmarked_field_left_untouched(self) -> None:
        encoded = _encode(TEXT, "br")
        doc = from_dict({"plain": encoded}, _Doc)
        assert doc is not None
        assert doc.plain == encoded

    def test_unmarked_field_with_unknown_header_does_not_raise(self) -> None:
        doc = from_dict({"plain": "zstd+base64:AAAA"}, _Doc)
        assert doc is not None
        assert doc.plain == "zstd+base64:AAAA"

    def test_optional_marked_field(self) -> None:
        doc = from_dict({"maybe": None}, _Doc)
        assert doc is not None
        assert doc.maybe is None
        doc = from_dict({"maybe": _encode(TEXT, "gzip")}, _Doc)
        assert doc is not None
        assert doc.maybe == TEXT

    def test_nested_list_and_dict(self) -> None:
        holder = from_dict(
            {
                "doc": {"body": _encode("nested", "br")},
                "docs": [{"body": _encode("first", "gzip")}, {"body": "plain"}],
                "byKey": {"k": {"body": _encode("keyed", "br")}},
            },
            _Holder,
        )
        assert holder is not None
        assert holder.doc is not None
        assert holder.doc.body == "nested"
        assert [d.body for d in holder.docs] == ["first", "plain"]
        assert holder.by_key["k"].body == "keyed"

    def test_error_from_from_dict(self) -> None:
        with pytest.raises(CompressedPropertyError, match=r"_Doc\.body") as info:
            from_dict({"body": "zstd+base64:AAAA"}, _Doc)
        assert isinstance(info.value, ValueError)
        assert info.value.owner == "_Doc"
        assert info.value.field_name == "body"

    @pytest.mark.parametrize("method", METHODS)
    def test_generated_entity_field(self, method: str) -> None:
        seg = from_dict(
            {"id": str(uuid7()), "reasonsData": _encode(TEXT, method)},
            ScheduleSegmentEntity,
        )
        assert seg is not None
        assert seg.reasons_data == TEXT

    def test_generated_entity_plain_value_unchanged(self) -> None:
        seg = from_dict({"id": str(uuid7()), "reasonsData": TEXT}, ScheduleSegmentEntity)
        assert seg is not None
        assert seg.reasons_data == TEXT

    def test_generated_entity_nested_parents(self) -> None:
        seg = from_dict(
            {
                "id": str(uuid7()),
                "parents": [{"id": str(uuid7()), "reasonsData": _encode("parent", "br")}],
            },
            ScheduleSegmentEntity,
        )
        assert seg is not None
        assert seg.parents is not None
        assert seg.parents[0].reasons_data == "parent"

    def test_read_envelope_with_type_dispatch(self) -> None:
        envelope = from_dict(
            {
                "success": True,
                "message": "",
                "errorCode": 0,
                "data": {
                    "typeName": "ScheduleSegmentEntity",
                    "data": [
                        {
                            "entity": {
                                "$type": "ScheduleSegmentEntity",
                                "id": str(uuid7()),
                                "reasonsData": _encode(TEXT, "br"),
                            },
                        },
                    ],
                },
            },
            ValueOrErrorPublic_1[ServiceDataRead],
        )
        assert envelope is not None
        assert envelope.data is not None
        entity = envelope.data.data[0].entity
        assert isinstance(entity, ScheduleSegmentEntity)
        assert entity.reasons_data == TEXT

    def test_service_data_update(self) -> None:
        update = from_dict(
            {
                "scenarioId": str(uuid7()),
                "status": 1,
                "requestIds": [],
                "subscriptionId": {"id": str(uuid7())},
                "modifiedEntities": {
                    "ScheduleSegmentEntity": {
                        "created": [
                            {
                                "$type": "ScheduleSegmentEntity",
                                "id": str(uuid7()),
                                "reasonsData": _encode(TEXT, "gzip"),
                            },
                        ],
                    },
                },
                "sequenceIdx": 0,
                "partIdx": 0,
                "partCount": 1,
            },
            ServiceDataUpdate,
        )
        assert update is not None
        created = update.modified_entities["ScheduleSegmentEntity"].created[0]
        assert isinstance(created, ScheduleSegmentEntity)
        assert created.reasons_data == TEXT

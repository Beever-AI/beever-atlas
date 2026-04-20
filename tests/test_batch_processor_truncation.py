"""Tests for the truncation-error predicate in batch_processor."""

import json

from pydantic import BaseModel, ValidationError

from beever_atlas.services.batch_processor import _is_truncation_error


class _Model(BaseModel):
    x: int


class TestIsTruncationError:
    def test_validation_error(self):
        try:
            _Model(x="not-an-int")  # type: ignore[arg-type]
        except ValidationError as exc:
            assert _is_truncation_error(exc) is True

    def test_json_decode_error(self):
        try:
            json.loads("{")
        except json.JSONDecodeError as exc:
            assert _is_truncation_error(exc) is True

    def test_max_tokens_string(self):
        assert _is_truncation_error(RuntimeError("stop_reason=MAX_TOKENS")) is True

    def test_unexpected_eof_string(self):
        assert _is_truncation_error(RuntimeError("unexpected EOF while parsing")) is True

    def test_json_invalid_string(self):
        assert _is_truncation_error(RuntimeError("json_invalid: bad")) is True

    def test_key_error_is_not_truncation(self):
        assert _is_truncation_error(KeyError("missing")) is False

    def test_value_error_without_markers_is_not_truncation(self):
        assert _is_truncation_error(ValueError("something else")) is False

    def test_remote_protocol_error_by_name(self):
        # Simulate httpx.RemoteProtocolError via a class of the same name.
        class RemoteProtocolError(Exception):
            pass

        assert _is_truncation_error(RemoteProtocolError("peer closed")) is True

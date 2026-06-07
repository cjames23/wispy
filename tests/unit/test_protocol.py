"""Unit tests for the JSON-RPC 2.0 codec edge cases.

Covers the boundary behaviours of :func:`wispy.protocol.parse_message`
and :func:`wispy.protocol.serialize_response` that are easier to nail
down with concrete examples than with property tests:

* Notification detection -- absent ``id`` member vs. explicit JSON null.
* Float and non-finite-number ids surfaced as ``invalid request``.
* Empty batch arrays surfaced as ``invalid request``.
* Non-UTF-8 and non-JSON payloads surfaced as ``parse error``.
* Round-trip of a known-good request, and of known-good success and
  error responses, equivalent to the canonical wire form up to JSON
  whitespace.
"""

from __future__ import annotations

import json

import pytest

from wispy.errors import JsonRpcErrorCode
from wispy.protocol import (
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    ParseFailure,
    ParseFailureKind,
    parse_message,
    serialize_response,
)

# ---------------------------------------------------------------------------
# Notification detection: absent ``id`` field vs. explicit JSON null.
# ---------------------------------------------------------------------------


def test_absent_id_is_notification() -> None:
    """A request without an ``id`` member is parsed as a notification."""
    raw = b'{"jsonrpc":"2.0","method":"ping"}'
    parsed = parse_message(raw)

    assert isinstance(parsed, JsonRpcRequest)
    assert parsed.method == "ping"
    assert parsed.is_notification is True
    assert parsed.id is None


def test_explicit_null_id_is_not_a_notification() -> None:
    """An ``id`` member explicitly set to JSON null is not a notification.

    The recovered id is ``None`` (the JSON null value) but
    ``is_notification`` is ``False`` because the wire request did carry
    an ``id`` member.
    """
    raw = b'{"jsonrpc":"2.0","method":"ping","id":null}'
    parsed = parse_message(raw)

    assert isinstance(parsed, JsonRpcRequest)
    assert parsed.method == "ping"
    assert parsed.is_notification is False
    assert parsed.id is None


# ---------------------------------------------------------------------------
# Float and non-finite-number ids -> invalid request.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "id_literal",
    [
        "1.5",
        "0.0",
        "-3.14",
        "1e10",
        "NaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_float_or_non_finite_id_is_invalid_request(id_literal: str) -> None:
    """Float and non-finite-number ids are rejected per the WSP design.

    JSON-RPC 2.0 permits string, integer, or null ids only. Python's
    ``json`` module decodes ``NaN``, ``Infinity``, and ``-Infinity`` to
    ``float``, so they share this code path with finite floats.
    """
    raw = f'{{"jsonrpc":"2.0","method":"ping","id":{id_literal}}}'.encode()
    parsed = parse_message(raw)

    assert isinstance(parsed, ParseFailure)
    assert parsed.kind is ParseFailureKind.INVALID_REQUEST
    assert parsed.code == int(JsonRpcErrorCode.INVALID_REQUEST)
    # The codec deliberately drops the malformed id when surfacing the
    # failure so downstream callers emit ``"id": null`` per the spec.
    assert parsed.id is None


def test_boolean_id_is_invalid_request() -> None:
    """Boolean ids (`true`/`false`) are not valid JSON-RPC ids."""
    raw = b'{"jsonrpc":"2.0","method":"ping","id":true}'
    parsed = parse_message(raw)

    assert isinstance(parsed, ParseFailure)
    assert parsed.kind is ParseFailureKind.INVALID_REQUEST
    assert parsed.id is None


# ---------------------------------------------------------------------------
# Empty batch array -> invalid request.
# ---------------------------------------------------------------------------


def test_empty_batch_is_invalid_request() -> None:
    """An empty JSON array at the top level is an invalid request.

    JSON-RPC 2.0 section 6 specifies that a batch with no entries is
    invalid; the codec surfaces this as a single top-level
    ``ParseFailure`` rather than an empty list.
    """
    parsed = parse_message(b"[]")

    assert isinstance(parsed, ParseFailure)
    assert parsed.kind is ParseFailureKind.INVALID_REQUEST
    assert parsed.code == int(JsonRpcErrorCode.INVALID_REQUEST)


def test_non_empty_batch_returns_list() -> None:
    """A batch with at least one entry is parsed into a list per entry."""
    raw = b'[{"jsonrpc":"2.0","method":"ping","id":1},{"jsonrpc":"2.0","method":"pong"}]'
    parsed = parse_message(raw)

    assert isinstance(parsed, list)
    assert len(parsed) == 2
    first, second = parsed
    assert isinstance(first, JsonRpcRequest)
    assert first.method == "ping"
    assert first.id == 1
    assert first.is_notification is False
    assert isinstance(second, JsonRpcRequest)
    assert second.method == "pong"
    assert second.is_notification is True


# ---------------------------------------------------------------------------
# Non-UTF-8 / not-JSON inputs -> parse error.
# ---------------------------------------------------------------------------


def test_invalid_utf8_is_parse_error() -> None:
    """Bytes that are not valid UTF-8 surface as a parse error."""
    # A lone continuation byte is not a valid UTF-8 sequence.
    parsed = parse_message(b"\xff\xfe\xfd")

    assert isinstance(parsed, ParseFailure)
    assert parsed.kind is ParseFailureKind.PARSE_ERROR
    assert parsed.code == int(JsonRpcErrorCode.PARSE_ERROR)


def test_invalid_json_is_parse_error() -> None:
    """Valid UTF-8 that is not valid JSON surfaces as a parse error."""
    parsed = parse_message(b"{not json}")

    assert isinstance(parsed, ParseFailure)
    assert parsed.kind is ParseFailureKind.PARSE_ERROR
    assert parsed.code == int(JsonRpcErrorCode.PARSE_ERROR)


def test_truncated_json_is_parse_error() -> None:
    """Incomplete JSON documents are surfaced as parse errors."""
    parsed = parse_message(b'{"jsonrpc":"2.0","method":"ping"')

    assert isinstance(parsed, ParseFailure)
    assert parsed.kind is ParseFailureKind.PARSE_ERROR


def test_empty_payload_is_parse_error() -> None:
    """An empty byte string is not valid JSON."""
    parsed = parse_message(b"")

    assert isinstance(parsed, ParseFailure)
    assert parsed.kind is ParseFailureKind.PARSE_ERROR


# ---------------------------------------------------------------------------
# Round-trip: known-good request and known-good response.
# ---------------------------------------------------------------------------


def test_request_round_trip_preserves_fields() -> None:
    """A canonical request decodes to the expected ``JsonRpcRequest``.

    The codec does not provide a request serializer (the server does
    not emit requests), so the round-trip we can assert is "wire bytes
    -> parsed dataclass with the expected field values".
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "environment/get",
        "params": {"id": "env-1"},
        "id": 42,
    }
    raw = json.dumps(payload).encode("utf-8")

    parsed = parse_message(raw)

    assert isinstance(parsed, JsonRpcRequest)
    assert parsed.method == "environment/get"
    assert parsed.params == {"id": "env-1"}
    assert parsed.id == 42
    assert parsed.is_notification is False


def test_success_response_round_trip_matches_canonical_wire_form() -> None:
    """A success response serializes to the canonical JSON object."""
    resp = JsonRpcResponse(id=1, result={"ok": True, "value": 7})
    expected = {
        "jsonrpc": "2.0",
        "result": {"ok": True, "value": 7},
        "id": 1,
    }

    wire = serialize_response(resp)

    # Compare by parsed JSON to be robust to key order, but assert the
    # serializer produces a compact (no-whitespace) document.
    assert json.loads(wire) == expected
    assert b" " not in wire
    assert b"\n" not in wire
    # ``error`` MUST NOT appear in a success response.
    assert b'"error"' not in wire


def test_error_response_round_trip_omits_unset_data() -> None:
    """An error response without ``data`` omits the field on the wire."""
    err = JsonRpcError(
        code=int(JsonRpcErrorCode.METHOD_NOT_FOUND),
        message="method not found",
    )
    resp = JsonRpcResponse(id="abc", error=err)

    wire = serialize_response(resp)
    decoded = json.loads(wire)

    assert decoded == {
        "jsonrpc": "2.0",
        "error": {
            "code": int(JsonRpcErrorCode.METHOD_NOT_FOUND),
            "message": "method not found",
        },
        "id": "abc",
    }
    # ``data`` is unset and therefore must not be emitted.
    assert "data" not in decoded["error"]
    assert b'"data"' not in wire
    # ``result`` MUST NOT appear in an error response.
    assert b'"result"' not in wire


def test_error_response_with_explicit_null_data_is_emitted() -> None:
    """Setting ``data`` to ``None`` keeps it on the wire as JSON null.

    The omission rule keys off the ``_UNSET`` sentinel, not on
    truthiness, so a caller that wants to publish ``"data": null``
    explicitly can still do so.
    """
    err = JsonRpcError(
        code=int(JsonRpcErrorCode.INTERNAL_ERROR),
        message="boom",
        data=None,
    )
    resp = JsonRpcResponse(id=None, error=err)

    decoded = json.loads(serialize_response(resp))

    assert decoded == {
        "jsonrpc": "2.0",
        "error": {
            "code": int(JsonRpcErrorCode.INTERNAL_ERROR),
            "message": "boom",
            "data": None,
        },
        "id": None,
    }


def test_error_response_with_data_round_trips() -> None:
    """A populated ``data`` field is serialized verbatim."""
    err = JsonRpcError(
        code=int(JsonRpcErrorCode.INVALID_PARAMS),
        message="bad params",
        data={"field": "name", "violations": ["name-required"]},
    )
    resp = JsonRpcResponse(id=7, error=err)

    decoded = json.loads(serialize_response(resp))

    assert decoded == {
        "jsonrpc": "2.0",
        "error": {
            "code": int(JsonRpcErrorCode.INVALID_PARAMS),
            "message": "bad params",
            "data": {"field": "name", "violations": ["name-required"]},
        },
        "id": 7,
    }

"""Unit tests for :class:`wispy.errors.WspError` construction invariants.

These tests cover the construction-time validation rules required by the
WSP error model so that any :class:`WspError` instance is guaranteed to
serialize cleanly into a JSON-RPC error response on the wire.
"""

from __future__ import annotations

import pytest

from wispy.errors import WspError, WspErrorCode

# A code known to be inside the WSP-reserved range (-31999..-31000) and
# therefore safe to use in tests that only care about other invariants.
_VALID_CODE = int(WspErrorCode.ENVIRONMENT_NOT_FOUND)

# Single-character ellipsis used by the truncation logic. Defined here as a
# literal rather than imported to keep the test independent of the module's
# private constants.
_ELLIPSIS = "\u2026"

# Required upper bound on the truncated message length.
_MAX_MESSAGE_LENGTH = 500


# ---------------------------------------------------------------------------
# data must be JSON-serializable
# ---------------------------------------------------------------------------


class _NotJsonSerializable:
    """A value json.dumps cannot encode without a custom ``default``."""


def test_non_json_serializable_data_raises_type_error() -> None:
    """A non-JSON-serializable ``data`` payload is rejected with TypeError."""
    with pytest.raises(TypeError):
        WspError(_VALID_CODE, "boom", data=_NotJsonSerializable())


def test_set_data_raises_type_error() -> None:
    """Sets are not JSON-serializable and must be rejected."""
    with pytest.raises(TypeError):
        WspError(_VALID_CODE, "boom", data={1, 2, 3})


def test_json_serializable_data_is_accepted() -> None:
    """Plain JSON-compatible structures pass validation and are stored."""
    payload = {"id": "env-1", "tags": ["a", "b"], "count": 7, "ok": True}
    err = WspError(_VALID_CODE, "boom", data=payload)
    assert err.data == payload


def test_data_default_none_is_allowed() -> None:
    """Omitting ``data`` is valid and leaves ``data`` as ``None``."""
    err = WspError(_VALID_CODE, "boom")
    assert err.data is None


# ---------------------------------------------------------------------------
# message length: long messages truncate with an ellipsis to <= 500 chars
# ---------------------------------------------------------------------------


def test_long_message_is_truncated_with_ellipsis() -> None:
    """Messages over 500 chars are shortened and end with an ellipsis."""
    original = "x" * 600
    err = WspError(_VALID_CODE, original)

    assert len(err.message) <= _MAX_MESSAGE_LENGTH
    assert err.message.endswith(_ELLIPSIS)
    # The retained prefix must come from the original message.
    assert err.message[:-1] == original[: len(err.message) - 1]


def test_message_at_boundary_is_not_truncated() -> None:
    """A message exactly at the limit is preserved verbatim."""
    original = "y" * _MAX_MESSAGE_LENGTH
    err = WspError(_VALID_CODE, original)

    assert err.message == original
    assert not err.message.endswith(_ELLIPSIS)


def test_short_message_is_preserved() -> None:
    """A short message is stored unchanged with no ellipsis appended."""
    err = WspError(_VALID_CODE, "nope")
    assert err.message == "nope"


# ---------------------------------------------------------------------------
# message must be a non-empty string
# ---------------------------------------------------------------------------


def test_empty_message_is_rejected() -> None:
    """Empty messages are rejected with ValueError."""
    with pytest.raises(ValueError, match="non-empty string"):
        WspError(_VALID_CODE, "")


# ---------------------------------------------------------------------------
# code must lie in the WSP-reserved range -31999..-31000 (inclusive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "out_of_range_code",
    [
        -32000,  # one above the WSP upper bound; inside the JSON-RPC range
        -30999,  # one below the WSP lower bound (closer to zero)
        -31000 - 1000,  # -32000, repeated for clarity
        0,
        1,
        -1,
        -100000,
    ],
)
def test_out_of_range_code_is_rejected(out_of_range_code: int) -> None:
    """Codes outside -31999..-31000 are rejected with ValueError."""
    with pytest.raises(ValueError, match="outside the reserved WSP range"):
        WspError(out_of_range_code, "boom")


@pytest.mark.parametrize("boundary_code", [-31999, -31000])
def test_boundary_codes_are_accepted(boundary_code: int) -> None:
    """The inclusive bounds of the reserved range are valid codes."""
    err = WspError(boundary_code, "boom")
    assert err.code == boundary_code

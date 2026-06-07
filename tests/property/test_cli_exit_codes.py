"""Property test for CLI exit-code mapping."""

from __future__ import annotations

import io
from contextlib import redirect_stderr

from hypothesis import given
from hypothesis import strategies as st

from wispy.cli.client import ClientResult, _result_from_error_response
from wispy.cli.main import ExitCode
from wispy.errors import JsonRpcErrorCode

# Any int that is NOT -32601 represents the "other JSON-RPC error" arm
# of the exit-code property. We bound the range loosely; the mapping
# is purely on
# equality with -32601, so the exact spread of values does not matter.
_OTHER_ERROR_CODES = st.integers(min_value=-40000, max_value=40000).filter(
    lambda c: c != int(JsonRpcErrorCode.METHOD_NOT_FOUND)
)


def test_success_result_has_exit_code_zero() -> None:
    """Success result -> ExitCode.SUCCESS (0).

    A :class:`ClientResult` constructed for the success arm of the
    dispatcher reports exit code 0, regardless of the carried value.
    """
    result = ClientResult(exit_code=ExitCode.SUCCESS, value={"any": "value"})
    assert result.exit_code == 0
    assert result.exit_code == ExitCode.SUCCESS


@given(message=st.text(min_size=1, max_size=64))
def test_method_not_found_maps_to_usage_or_unsupported(message: str) -> None:
    """Error code -32601 -> ExitCode.USAGE_OR_UNSUPPORTED (2).

    For any non-empty message text, a JSON-RPC error response carrying
    ``code == -32601`` maps to exit code 2 with a stderr line that
    identifies the method as unsupported.
    """
    resp = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": int(JsonRpcErrorCode.METHOD_NOT_FOUND),
            "message": message,
        },
    }
    # Use ``redirect_stderr`` (rather than the ``capsys`` fixture) so a
    # fresh capture buffer is created per Hypothesis-generated input;
    # function-scoped pytest fixtures are not reset between examples.
    err = io.StringIO()
    with redirect_stderr(err):
        result = _result_from_error_response(resp)
    assert result.exit_code == ExitCode.USAGE_OR_UNSUPPORTED
    assert result.exit_code == 2
    assert "method unsupported" in err.getvalue()


@given(code=_OTHER_ERROR_CODES, message=st.text(min_size=1, max_size=64))
def test_other_error_codes_map_to_generic_error(code: int, message: str) -> None:
    """Error code != -32601 -> ExitCode.GENERIC_ERROR (1).

    For every JSON-RPC error code that is not ``-32601``, the response
    maps to exit code 1, and the rendered error JSON is written to
    stderr.
    """
    resp = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": code, "message": message},
    }
    err = io.StringIO()
    with redirect_stderr(err):
        result = _result_from_error_response(resp)
    assert result.exit_code == ExitCode.GENERIC_ERROR
    assert result.exit_code == 1
    # The full error JSON is echoed to stderr; the numeric code must
    # appear there verbatim so users can grep for it.
    assert str(code) in err.getvalue()

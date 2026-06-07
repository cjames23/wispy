"""Property test for dispatcher handler exception mapping.

For every ``WspError(code, message, data)`` raised by a handler the
dispatcher's response error fields MUST equal ``(code, message, data)``
exactly, with ``data`` omitted from the wire when not provided. For
every exception that is not a ``ProtocolError`` subclass the response
error code MUST be exactly ``-32603`` (Internal error). For every
``ProtocolError`` subclass that is not a ``WspError`` (i.e. carries no
WSP error code) the response error code MUST be the WSP
``execution-failed`` code (``-31004``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from wispy.dispatcher import dispatch
from wispy.endpoints import Capabilities
from wispy.errors import ProtocolError, WspError, WspErrorCode
from wispy.lifecycle import LifecycleManager
from wispy.protocol import (
    _UNSET,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    serialize_response,
)
from wispy.registry import HandlerRegistry

# Method name deliberately not in ``WSP_METHODS`` so the dispatcher
# skips param and result validation and goes straight to the handler.
_METHOD = "crash"


# --------------------------------------------------------------------- #
# Hypothesis strategies.
# --------------------------------------------------------------------- #


# JSON-serializable values, recursive. Excludes NaN/Inf so json.dumps
# never fails (and WspError's data invariant is upheld). Mirrors the
# strategy suggested in the design notes.
_json_value = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=16),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4)
    ),
    max_leaves=6,
)

# For the "data populated" test the value must not be ``None`` at the
# top level: the dispatcher treats ``WspError.data is None`` as "no
# data" and omits it from the wire. Inner ``None``
# values inside lists/dicts are still allowed.
_non_none_data = _json_value.filter(lambda v: v is not None)


# Generic exception classes used to exercise the non-ProtocolError
# branch of the dispatcher's exception mapping.
_GENERIC_EXC_TYPES = st.sampled_from([ValueError, RuntimeError, TypeError, KeyError, ZeroDivisionError])


# --------------------------------------------------------------------- #
# Helpers shared across the property tests.
# --------------------------------------------------------------------- #


def _initialized_lifecycle() -> LifecycleManager:
    """Return a lifecycle manager already advanced to INITIALIZED.

    The dispatcher's lifecycle gate would otherwise reject our
    ``crash`` method with ``-32002`` because it is not ``initialize``.
    Pre-advancing the FSM by hand keeps the test focused on the
    handler exception mapping rather than the lifecycle.
    """
    manager = LifecycleManager()
    manager.on_initialize_success(Capabilities(methods=(), protocol_version="0.1.0"))
    return manager


def _make_request(request_id: int = 1) -> JsonRpcRequest:
    """Build a non-notification request for our throwaway ``crash`` method.

    The request id is positive so the dispatcher emits a response (not
    swallows it as a notification). Bypasses the JSON codec so the
    test exercises only the dispatch-layer exception mapping.
    """
    return JsonRpcRequest(
        method=_METHOD,
        params=None,
        id=request_id,
        is_notification=False,
    )


def _registry_with(handler: Any) -> HandlerRegistry:
    """Return a registry with ``handler`` bound to the ``crash`` method."""
    registry = HandlerRegistry()
    registry.register(_METHOD, handler)
    return registry


def _run_dispatch(handler: Any, log: Any = None) -> JsonRpcResponse:
    """Dispatch ``crash`` against ``handler`` from INITIALIZED state.

    Uses ``asyncio.run`` per the design's "no pytest-asyncio" guidance.
    Returns the single :class:`JsonRpcResponse` produced by the
    dispatcher.
    """
    log_callable = log if log is not None else (lambda _msg: None)
    result = asyncio.run(
        dispatch(
            _make_request(),
            registry=_registry_with(handler),
            lifecycle=_initialized_lifecycle(),
            log=log_callable,
        )
    )
    assert isinstance(result, JsonRpcResponse), f"expected JsonRpcResponse, got {type(result).__name__}"
    return result


# --------------------------------------------------------------------- #
# Test 1: WspError carrying data round-trips through dispatch unchanged.
# --------------------------------------------------------------------- #


@given(
    code=st.sampled_from(list(WspErrorCode)),
    message=st.text(min_size=1, max_size=200),
    data=_non_none_data,
)
def test_wsp_error_with_data_round_trips(code: WspErrorCode, message: str, data: Any) -> None:
    """Handler exception mapping.

    A handler raising ``WspError(code, message, data)`` with all three
    fields populated MUST produce a response whose error fields equal
    ``(code, message, data)`` exactly, with no remapping or coercion.
    """

    async def handler(_params: Any) -> Any:
        raise WspError(int(code), message, data)

    response = _run_dispatch(handler)
    err = response.error
    assert isinstance(err, JsonRpcError)
    assert err.code == int(code)
    assert err.message == message
    # ``data`` round-trips by Python equality. The dispatcher does not
    # serialize/deserialize the value, it simply attaches the original
    # object reference to the JsonRpcError.
    assert err.data == data


# --------------------------------------------------------------------- #
# Test 2: WspError without data omits ``data`` on the wire.
# --------------------------------------------------------------------- #


@given(
    code=st.sampled_from(list(WspErrorCode)),
    message=st.text(min_size=1, max_size=200),
)
def test_wsp_error_without_data_omits_data_on_wire(code: WspErrorCode, message: str) -> None:
    """Handler exception mapping.

    A handler raising ``WspError(code, message)`` (no ``data``
    argument) MUST produce a response whose ``data`` is the ``_UNSET``
    sentinel and whose serialized form omits ``"data"`` from the
    error object entirely.
    """

    async def handler(_params: Any) -> Any:
        raise WspError(int(code), message)

    response = _run_dispatch(handler)
    err = response.error
    assert isinstance(err, JsonRpcError)
    assert err.code == int(code)
    assert err.message == message
    # Dataclass-level check: ``data`` was never assigned, so the
    # default sentinel survives untouched.
    assert err.data is _UNSET

    # Wire form: serializing the response MUST emit an error object
    # without a ``data`` member at all.
    encoded = json.loads(serialize_response(response))
    assert "error" in encoded
    assert "data" not in encoded["error"], f"expected no 'data' key in error object, got {encoded['error']!r}"


# --------------------------------------------------------------------- #
# Test 3: non-ProtocolError exceptions map to -32603 (internal error).
# --------------------------------------------------------------------- #


@given(
    exc_type=_GENERIC_EXC_TYPES,
    arg=st.text(min_size=0, max_size=64),
)
def test_non_protocol_error_maps_to_internal_error(exc_type: type[Exception], arg: str) -> None:
    """Handler exception mapping.

    Any exception that is NOT a ``ProtocolError`` subclass MUST be
    mapped to a ``-32603`` (internal error) response regardless of
    type or carried message. The dispatcher MUST also emit at least
    one log entry so operators can diagnose the failure.
    """

    async def handler(_params: Any) -> Any:
        raise exc_type(arg)

    logs: list[str] = []
    response = _run_dispatch(handler, log=logs.append)
    err = response.error
    assert isinstance(err, JsonRpcError)
    # Hard-coded numeric expectation: -32603 is the JSON-RPC reserved
    # internal-error code, never remapped by the dispatcher.
    assert err.code == -32603, f"expected -32603 for {exc_type.__name__}({arg!r}), got {err.code}"
    # The dispatcher logs the traceback for unexpected exceptions; at
    # least one log entry must have been captured.
    assert len(logs) >= 1, f"expected at least one log entry for {exc_type.__name__}, got {logs!r}"


# --------------------------------------------------------------------- #
# Test 4: ProtocolError without a WSP code maps to execution-failed.
# --------------------------------------------------------------------- #


@given(
    code=st.integers(min_value=-32999, max_value=32999),
    message=st.text(min_size=1, max_size=200),
)
def test_protocol_error_without_wsp_code_maps_to_execution_failed(code: int, message: str) -> None:
    """Handler exception mapping.

    A bare ``ProtocolError`` (not a ``WspError`` subclass, therefore
    carrying no WSP error code) MUST be remapped by the dispatcher to
    the WSP ``execution-failed`` code (-31004), regardless of the
    ``code`` value the exception itself carries.
    """

    async def handler(_params: Any) -> Any:
        raise ProtocolError(code, message)

    response = _run_dispatch(handler)
    err = response.error
    assert isinstance(err, JsonRpcError)
    # The expected code is fixed: every bare ProtocolError maps to
    # execution-failed irrespective of the exception's own ``code``.
    assert err.code == int(WspErrorCode.EXECUTION_FAILED)
    assert err.code == -31004

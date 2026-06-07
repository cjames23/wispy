"""Property tests for the WSP JSON-RPC dispatcher.

This module hosts the wire-form behavior of
:func:`wispy.dispatcher.dispatch` end-to-end through
:func:`wispy.protocol.parse_message`:

* Response well-formedness and id preservation.
* Notifications produce no response.
* Error-code priority order.
* Batch ordering and notification omission.

Conventions used throughout the module:

* The dispatcher is async; each property runs it via
  :func:`asyncio.run` since pytest-asyncio is not a project dependency.
* The lifecycle manager is advanced into ``INITIALIZED`` before
  dispatch in every property except the error-code priority test,
  which deliberately
  keeps an ``INITIALIZED`` lifecycle so the lifecycle gate is not the
  source of any error code observed -- the test is verifying the
  priority order over the codec/dispatcher error
  classification, not over lifecycle gating.
* Identity handlers (``params -> params``) are registered against
  method names that are NOT in :data:`wispy.endpoints.WSP_METHODS`,
  so the dispatcher's per-method validator step is bypassed for the
  notification, well-formedness, and batch-ordering properties.
  The error-code priority property deliberately registers WSP
  methods so the invalid-params branch can be exercised.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from wispy.dispatcher import dispatch
from wispy.endpoints import WSP_METHODS, Capabilities
from wispy.errors import JsonRpcErrorCode
from wispy.lifecycle import LifecycleManager
from wispy.protocol import (
    JsonRpcResponse,
    parse_message,
    serialize_response,
)
from wispy.registry import HandlerRegistry

# --------------------------------------------------------------------- #
# Constants and shared helpers.
# --------------------------------------------------------------------- #


# Identity-handler corpus. None of these names appear in WSP_METHODS,
# so the dispatcher skips its WspMethod.validate_params step and the
# handler receives the raw params verbatim. The assertion below is the
# only invariant the corpus must hold for Properties 1, 2, and 4.
_NON_WSP_METHODS: tuple[str, ...] = (
    "echo",
    "ping",
    "sum",
    "noop",
    "identity",
)
assert all(m not in WSP_METHODS for m in _NON_WSP_METHODS), "_NON_WSP_METHODS must not collide with WSP_METHODS"


# Methods that are reserved for the lifecycle FSM and thus would skew
# error-code expectations (they bypass the registry lookup branch).
_LIFECYCLE_RESERVED = frozenset({"initialize", "exit"})


# Compact aliases for the JSON-RPC error codes the dispatcher emits.
_PARSE_ERROR = int(JsonRpcErrorCode.PARSE_ERROR)
_INVALID_REQUEST = int(JsonRpcErrorCode.INVALID_REQUEST)
_METHOD_NOT_FOUND = int(JsonRpcErrorCode.METHOD_NOT_FOUND)
_INVALID_PARAMS = int(JsonRpcErrorCode.INVALID_PARAMS)


def _identity(params: Any) -> Any:
    """Identity handler: returns its input unchanged."""
    return params


def _noop_log(_msg: str) -> None:
    """Discarding log sink. The dispatcher's "MUST NOT raise" guarantee
    means the log output is incidental to the properties tested here."""


def _initialized_lifecycle() -> LifecycleManager:
    """Build a :class:`LifecycleManager` already in the INITIALIZED state.

    Properties 1, 2, and 4 dispatch arbitrary methods that are not
    ``initialize`` itself; without advancing the FSM into INITIALIZED
    every such request would be rejected ``-32002`` by the lifecycle
    gate, masking the codec/dispatcher behavior actually under test.
    """
    mgr = LifecycleManager()
    mgr.on_initialize_success(Capabilities(methods=(), protocol_version="0.1.0"))
    return mgr


def _make_registry(method_names: tuple[str, ...] | list[str]) -> HandlerRegistry:
    """Build a registry that maps each name to the identity handler."""
    reg = HandlerRegistry()
    for m in method_names:
        reg.register(m, _identity)
    return reg


def _run_dispatch(parsed: Any, registry: HandlerRegistry, lifecycle: LifecycleManager) -> Any:
    """Run ``dispatch`` synchronously via :func:`asyncio.run`."""
    return asyncio.run(
        dispatch(
            parsed,
            registry=registry,
            lifecycle=lifecycle,
            log=_noop_log,
        )
    )


def _ids_equal_with_type(a: Any, b: Any) -> bool:
    """JSON-type-and-value equality for JSON-RPC ids.

    Distinguishes ``str`` from ``int`` ids (so that ``1`` and ``"1"``
    are not considered equal) and rejects booleans entirely. JSON-RPC
    permits only string, integer, or null as the request id, and the
    parser already discards floats and booleans; this helper is the
    cross-check on the response side that the dispatcher round-tripped
    the original JSON type intact.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    if a is None and b is None:
        return True
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    return False


# --------------------------------------------------------------------- #
# Strategies.
# --------------------------------------------------------------------- #


# JSON-RPC ids: string, integer, or null. JSON-RPC 2.0 forbids floats
# (and the parser explicitly rejects booleans), so neither appears here.
json_rpc_ids = st.one_of(
    st.text(max_size=20),
    # Bound integers so json round-trips with the same Python type;
    # arbitrary big ints would still be valid but slow generation.
    st.integers(min_value=-(2**53), max_value=2**53),
    st.none(),
)


# Scalars used inside params payloads. Floats are deliberately excluded
# so ``serialize_response`` (which uses ``allow_nan=False``) cannot
# raise on a NaN/inf the strategy would otherwise have to guard against.
_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**31), max_value=2**31 - 1),
    st.text(max_size=8),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=4), children, max_size=4),
    ),
    max_leaves=8,
)
# ``params`` on the wire is either absent (``None`` here, meaning "do
# not emit the key") or a JSON array or object.
json_params_or_absent = st.one_of(
    st.none(),
    st.lists(_json_values, max_size=4),
    st.dictionaries(st.text(max_size=4), _json_values, max_size=4),
)


known_method_names = st.sampled_from(_NON_WSP_METHODS)


# Free-text method names that won't collide with the lifecycle-reserved
# methods or with our identity-handler corpus. Used by the notification
# property to
# generate notifications that hit the method-not-found branch without
# tripping the lifecycle FSM's special handling of ``exit`` / ``initialize``.
_unknown_method_names = st.text(min_size=1, max_size=20).filter(
    lambda s: (s.strip() != "" and s not in _LIFECYCLE_RESERVED and s not in _NON_WSP_METHODS and "\x00" not in s)
)


def _build_request_obj(
    method: str,
    params: Any,
    *,
    id_value: Any = None,
    id_present: bool,
) -> dict[str, Any]:
    """Construct a wire-form JSON-RPC 2.0 request dict.

    ``id_present=False`` produces a notification (no ``"id"`` key on
    the wire). ``params=None`` means "do not include params". Values
    that are list or dict are emitted verbatim.
    """
    out: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        out["params"] = params
    if id_present:
        out["id"] = id_value
    return out


# --------------------------------------------------------------------- #
# Response well-formedness and id preservation.
# --------------------------------------------------------------------- #


@given(
    method=known_method_names,
    params=json_params_or_absent,
    request_id=json_rpc_ids,
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_1_single_request_response_shape_and_id(method: str, params: Any, request_id: Any) -> None:
    """JSON-RPC response well-formedness and id preservation.

    For every well-formed single request, the dispatcher emits a
    response whose serialized wire form has ``jsonrpc == "2.0"``,
    exactly one of ``result`` / ``error``, and an ``id`` JSON-type-
    and-value-equal to the request id.
    """
    registry = _make_registry(_NON_WSP_METHODS)
    lifecycle = _initialized_lifecycle()

    raw = json.dumps(_build_request_obj(method, params, id_value=request_id, id_present=True)).encode("utf-8")
    parsed = parse_message(raw)
    response = _run_dispatch(parsed, registry, lifecycle)

    # Identity-method requests with a present id always produce one
    # JsonRpcResponse (success or error); never None or a list.
    assert isinstance(response, JsonRpcResponse), f"expected JsonRpcResponse, got {response!r}"

    body = json.loads(serialize_response(response).decode("utf-8"))
    assert body["jsonrpc"] == "2.0", f"missing/wrong jsonrpc: {body!r}"

    has_result = "result" in body
    has_error = "error" in body
    assert has_result ^ has_error, f"response must have exactly one of 'result' / 'error': {body!r}"

    assert "id" in body, f"response missing 'id': {body!r}"
    assert _ids_equal_with_type(body["id"], request_id), (
        f"request id {request_id!r} (type {type(request_id).__name__}) "
        f"does not match response id {body['id']!r} "
        f"(type {type(body['id']).__name__})"
    )


@st.composite
def _id_bearing_batch(draw: st.DrawFn) -> list[tuple[str, Any, Any]]:
    """Build a batch of id-bearing requests.

    Each entry is ``(method, params, id)`` where ``method`` is drawn
    from the identity-handler corpus, ``params`` is JSON-array, JSON-
    object, or absent (``None``), and ``id`` is a permitted JSON-RPC id
    type. The returned list always has at least one entry.
    """
    n = draw(st.integers(min_value=1, max_value=5))
    items: list[tuple[str, Any, Any]] = [
        (
            draw(known_method_names),
            draw(json_params_or_absent),
            draw(json_rpc_ids),
        )
        for _ in range(n)
    ]
    return items


@given(entries=_id_bearing_batch())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_1_batch_response_shape_and_id(
    entries: list[tuple[str, Any, Any]],
) -> None:
    """JSON-RPC response well-formedness and id preservation.

    Batch variant of the single-request test: every response in the
    response array carries ``jsonrpc == "2.0"``, exactly one of
    ``result`` / ``error``, and an ``id`` matching the corresponding
    request's id by JSON type and value.
    """
    registry = _make_registry(_NON_WSP_METHODS)
    lifecycle = _initialized_lifecycle()

    batch_objs = [_build_request_obj(m, p, id_value=i, id_present=True) for (m, p, i) in entries]
    raw = json.dumps(batch_objs).encode("utf-8")
    parsed = parse_message(raw)
    response = _run_dispatch(parsed, registry, lifecycle)

    assert isinstance(response, list), f"expected list response for batch, got {response!r}"
    assert len(response) == len(entries), f"expected {len(entries)} responses, got {len(response)}"

    serialized = json.loads(serialize_response(response).decode("utf-8"))
    assert isinstance(serialized, list)
    assert len(serialized) == len(entries)

    for body, (_method, _params, expected_id) in zip(serialized, entries, strict=False):
        assert body["jsonrpc"] == "2.0"
        has_result = "result" in body
        has_error = "error" in body
        assert has_result ^ has_error, f"response must have exactly one of 'result' / 'error': {body!r}"
        assert "id" in body
        assert _ids_equal_with_type(body["id"], expected_id), (
            f"request id {expected_id!r} (type "
            f"{type(expected_id).__name__}) does not match response id "
            f"{body['id']!r} (type {type(body['id']).__name__})"
        )


# --------------------------------------------------------------------- #
# Notifications produce no response.
# --------------------------------------------------------------------- #


# Method strategy used for notifications: draws either a registered
# identity method name or an arbitrary unknown method name. Both must
# yield ``None`` from the dispatcher when sent as a notification.
_notification_methods = st.one_of(
    known_method_names,
    _unknown_method_names,
)


@given(
    method=_notification_methods,
    params=json_params_or_absent,
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_2_single_notification_returns_none(method: str, params: Any) -> None:
    """Notifications produce no response.

    A single message without an ``id`` field MUST cause the dispatcher
    to return ``None``, regardless of whether the method exists in the
    registry and regardless of whether the params would otherwise be
    accepted.
    """
    registry = _make_registry(_NON_WSP_METHODS)
    lifecycle = _initialized_lifecycle()

    raw = json.dumps(_build_request_obj(method, params, id_present=False)).encode("utf-8")
    parsed = parse_message(raw)
    response = _run_dispatch(parsed, registry, lifecycle)

    assert response is None, f"expected None for notification (method={method!r}); got {response!r}"


@given(
    notifications=st.lists(
        st.tuples(_notification_methods, json_params_or_absent),
        min_size=1,
        max_size=5,
    ),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_2_batch_of_only_notifications_returns_empty_list(
    notifications: list[tuple[str, Any]],
) -> None:
    """Notifications produce no response.

    A batch composed entirely of notifications produces an empty list
    on the wire. Each entry contributes nothing to the response array
    even when its method is unknown or otherwise malformed.
    """
    registry = _make_registry(_NON_WSP_METHODS)
    lifecycle = _initialized_lifecycle()

    batch = [_build_request_obj(m, p, id_present=False) for (m, p) in notifications]
    raw = json.dumps(batch).encode("utf-8")
    parsed = parse_message(raw)
    response = _run_dispatch(parsed, registry, lifecycle)

    assert response == [], f"expected [] for an all-notification batch; got {response!r}"


# --------------------------------------------------------------------- #
# Error-code priority order.
# --------------------------------------------------------------------- #


# WSP methods used as the registered set for the priority test. Each
# method is in WSP_METHODS, so ``invalid_params_bytes`` can drive its
# validator into a violation list. For
# ``method_not_found`` cases, the strategy below filters out these
# names so the registry lookup fails as expected.
_PRIORITY_REGISTERED_METHODS: tuple[str, ...] = (
    "environment/get",
    "environment/delete",
    "environment/create",
    "environment/execute",
)


@st.composite
def _parse_error_input(draw: st.DrawFn) -> tuple[str, bytes]:
    """Bytes that fail UTF-8 decoding or JSON parsing."""
    pool: list[bytes] = [
        b"\xff\xfe\xfd",  # invalid UTF-8 start byte
        b"\xc3\x28",  # invalid UTF-8 continuation byte
        b"\x80",  # stray UTF-8 continuation byte
        b"this is not json",
        b"{",
        b"[",
        b"{not json}",
        b"undefined",
        b'{"jsonrpc"',
    ]
    return ("parse_error", draw(st.sampled_from(pool)))


@st.composite
def _invalid_request_input(draw: st.DrawFn) -> tuple[str, bytes]:
    """Bytes that parse as JSON but are not valid JSON-RPC 2.0 requests."""
    pool: list[bytes] = [
        b"42",
        b'"hello"',
        b"true",
        b"false",
        b"null",
        b"[]",  # empty batch -> top-level INVALID_REQUEST per parser
        b"{}",
        b'{"method": "x", "id": 1}',  # missing jsonrpc
        b'{"jsonrpc": "1.0", "method": "x", "id": 1}',  # wrong version
        b'{"jsonrpc": "2.0", "id": 1}',  # missing method
        b'{"jsonrpc": "2.0", "method": 42, "id": 1}',  # non-string method
        b'{"jsonrpc": "2.0", "method": "x", "id": 1, "params": "no"}',
    ]
    return ("invalid_request", draw(st.sampled_from(pool)))


@st.composite
def _method_not_found_input(draw: st.DrawFn) -> tuple[str, bytes]:
    """Well-formed request whose method is absent from the registry.

    The method-name strategy excludes:

    * Members of :data:`_PRIORITY_REGISTERED_METHODS` (which would be
      found by registry lookup).
    * Lifecycle-reserved names (``"initialize"``, ``"exit"``) which the
      lifecycle gate intercepts before the registry lookup happens
      and so produce a different error code than the one
      method-not-found cases would expect.
    """
    method = draw(
        st.text(min_size=1, max_size=20).filter(
            lambda s: (
                s.strip() != ""
                and s not in _PRIORITY_REGISTERED_METHODS
                and s not in _LIFECYCLE_RESERVED
                and "\x00" not in s
            )
        )
    )
    request_id = draw(st.one_of(st.text(max_size=10), st.integers(min_value=0, max_value=1000)))
    obj = _build_request_obj(method, params=None, id_value=request_id, id_present=True)
    return ("method_not_found", json.dumps(obj).encode("utf-8"))


@st.composite
def _invalid_params_input(draw: st.DrawFn) -> tuple[str, bytes]:
    """Well-formed request with a registered WSP method whose validator fails.

    Each WSP method in :data:`_PRIORITY_REGISTERED_METHODS` rejects an
    empty params object with at least one violation (e.g.
    ``id-required`` for ``environment/get``), so the dispatcher's
    invalid-params branch is exercised reliably.
    """
    method = draw(st.sampled_from(_PRIORITY_REGISTERED_METHODS))
    bad_params = draw(
        st.sampled_from(
            [
                {},
                {"id": ""},
                {"name": ""},
                {"id": 123},  # wrong type for id
            ]
        )
    )
    request_id = draw(st.one_of(st.text(max_size=10), st.integers(min_value=0, max_value=1000)))
    obj = _build_request_obj(method, params=bad_params, id_value=request_id, id_present=True)
    # Defensive sanity check: the chosen method+params combination
    # must trigger the validator's failure path. If it doesn't (e.g.
    # an accidental future change to the validator), drop the example
    # rather than silently miscategorise the priority.
    method_def = WSP_METHODS[method]
    validated = method_def.validate_params(bad_params)
    assume(isinstance(validated, list) and validated)
    return ("invalid_params", json.dumps(obj).encode("utf-8"))


@given(
    case=st.one_of(
        _parse_error_input(),
        _invalid_request_input(),
        _method_not_found_input(),
        _invalid_params_input(),
    ),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_3_error_code_priority(case: tuple[str, bytes]) -> None:
    """Error-code priority order.

    Each generator above produces an input that satisfies exactly one
    of the four error conditions. The dispatcher must surface the
    error code corresponding to that condition, which under the
    priority order ``parse error > invalid request >
    method not found > invalid params`` is also the highest-priority
    satisfied condition for these single-condition inputs.
    """
    kind, raw = case

    expected_code = {
        "parse_error": _PARSE_ERROR,
        "invalid_request": _INVALID_REQUEST,
        "method_not_found": _METHOD_NOT_FOUND,
        "invalid_params": _INVALID_PARAMS,
    }[kind]

    registry = _make_registry(_PRIORITY_REGISTERED_METHODS)
    lifecycle = _initialized_lifecycle()

    parsed = parse_message(raw)
    response = _run_dispatch(parsed, registry, lifecycle)

    assert isinstance(response, JsonRpcResponse), (
        f"expected JsonRpcResponse for kind={kind!r}, raw={raw!r}; got {response!r}"
    )

    body = json.loads(serialize_response(response).decode("utf-8"))
    assert "error" in body, f"expected error response for kind={kind!r}, raw={raw!r}; got {body!r}"
    assert body["error"]["code"] == expected_code, (
        f"expected code {expected_code} for kind={kind!r}, "
        f"raw={raw!r}; got {body['error']['code']} "
        f"(message={body['error']['message']!r})"
    )


# --------------------------------------------------------------------- #
# Batch ordering and notification omission.
# --------------------------------------------------------------------- #


@st.composite
def _mixed_batch(
    draw: st.DrawFn,
) -> tuple[str, list[tuple[bool, Any]]]:
    """Build a batch of mixed id-bearing requests and notifications.

    Returns ``(method, entries)`` where ``method`` is the single
    identity-method used by every entry (so the dispatcher's reply-vs-
    omit decision depends purely on whether each entry has an ``id``
    field), and each ``entries`` element is ``(is_notification, id)``.
    The id field is drawn from the standard JSON-RPC id strategy when
    the entry is id-bearing and is unused otherwise.
    """
    method = draw(known_method_names)
    n = draw(st.integers(min_value=1, max_value=8))
    entries: list[tuple[bool, Any]] = []
    for _ in range(n):
        is_notification = draw(st.booleans())
        if is_notification:
            entries.append((True, None))
        else:
            entries.append((False, draw(json_rpc_ids)))
    return method, entries


@given(batch=_mixed_batch())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_4_batch_ordering_and_notification_omission(
    batch: tuple[str, list[tuple[bool, Any]]],
) -> None:
    """Batch ordering and notification omission.

    The response array length equals the number of non-notification
    entries in the batch, and the response at position ``k`` carries
    the id of the ``k``-th non-notification entry in input order.
    Notifications contribute nothing to the response array. An
    all-notifications batch produces an empty response list.
    """
    method, entries = batch

    registry = _make_registry(_NON_WSP_METHODS)
    lifecycle = _initialized_lifecycle()

    batch_objs: list[dict[str, Any]] = []
    expected_ids: list[Any] = []
    for is_notification, id_value in entries:
        # Use a tiny JSON object as params. Could be omitted just as
        # well; including it exercises the params-pass-through path.
        params: dict[str, Any] = {"k": "v"}
        obj = _build_request_obj(
            method,
            params,
            id_value=id_value,
            id_present=not is_notification,
        )
        batch_objs.append(obj)
        if not is_notification:
            expected_ids.append(id_value)

    raw = json.dumps(batch_objs).encode("utf-8")
    parsed = parse_message(raw)
    response = _run_dispatch(parsed, registry, lifecycle)

    # The dispatcher always returns a list for a parsed batch (possibly
    # empty when every entry was a notification).
    assert isinstance(response, list), f"expected list response for batch, got {response!r}"
    assert len(response) == len(expected_ids), (
        f"expected {len(expected_ids)} responses (one per non-notification entry); got {len(response)}: {response!r}"
    )

    for resp, expected_id in zip(response, expected_ids, strict=False):
        assert isinstance(resp, JsonRpcResponse)
        assert _ids_equal_with_type(resp.id, expected_id), (
            f"batch order or id preservation broken: expected id "
            f"{expected_id!r} (type {type(expected_id).__name__}), got "
            f"{resp.id!r} (type {type(resp.id).__name__})"
        )

"""Property test for handler dispatch round-trip.

For every method name ``m`` bound in a :class:`HandlerRegistry` and every
JSON-serializable value ``p``, dispatching a JSON-RPC request
``{method: m, params: p, id: i}`` MUST invoke the bound handler exactly
once with ``p`` as its argument and (when the handler returns a value
``v``) emit a :class:`JsonRpcResponse` whose ``id`` equals ``i`` and
whose ``result`` equals ``v``.

The test drives :func:`wispy.dispatcher.dispatch` against a
:class:`LifecycleManager` advanced past the ``-32002`` initialize gate
(via :meth:`LifecycleManager.on_initialize_success`) so that
non-``initialize`` requests are admitted. Method names are drawn from
the user-defined namespace -- explicitly excluded from
:data:`wispy.endpoints.WSP_METHODS` -- so the dispatcher's WSP
parameter validators do not fire. This isolates the property under
test (handler invocation and response shape) from the orthogonal
property of WSP-method param validation.

The :class:`JsonRpcRequest` dataclass is constructed directly rather
than via the JSON codec; the codec round-trip is exercised by the
notification property (and is irrelevant to handler-dispatch
correctness).
"""

from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from wispy.dispatcher import dispatch
from wispy.endpoints import WSP_METHODS, Capabilities
from wispy.lifecycle import LifecycleManager
from wispy.protocol import JsonRpcRequest, JsonRpcResponse
from wispy.registry import HandlerRegistry

# A canonical Capabilities object used to advance every fresh
# LifecycleManager into the INITIALIZED state. The actual capability
# values are immaterial to the property; the dispatcher only consults
# the manager's state.
_INITIAL_CAPS = Capabilities(methods=(), protocol_version="0.1.0")


# Hypothesis strategy producing JSON-serializable param values. Mirrors
# the JSON-RPC ``params`` value space (objects, arrays, strings,
# numbers, booleans, null) without ``NaN``/``Infinity`` (the codec
# rejects them and the dispatcher contract is over JSON-representable
# values).
_json_scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text()
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(
            st.text(min_size=1, max_size=8),
            children,
            max_size=4,
        )
    ),
    max_leaves=8,
)


# Method-name strategy: any non-empty string that is NOT a WSP-defined
# method, so the dispatcher skips param validation and runs our
# tracking handler directly.
_method_names = st.lists(
    st.text(min_size=1, max_size=16).filter(lambda s: s not in WSP_METHODS),
    unique=True,
    min_size=1,
    max_size=8,
)


# Request id strategy: JSON-RPC permits string, integer, or null ids
# for non-notification requests. The dispatcher echoes the id verbatim
# in the response, so the property holds for every well-typed id.
_request_ids = st.one_of(
    st.integers(),
    st.text(min_size=0, max_size=16),
    st.none(),
)


def _make_registry_and_calls(
    method_names: list[str],
) -> tuple[HandlerRegistry, dict[str, list[Any]]]:
    """Build a registry whose handlers record their invocations.

    Each handler is a fresh closure capturing a per-method call list;
    every invocation appends the received ``params`` value. The handler
    returns a deterministic dict that is a function of the method name
    and the params, so the test can predict the expected ``result`` and
    detect any handler mix-up (a wrong handler being dispatched would
    produce a result tagged with the wrong method name).
    """
    calls: dict[str, list[Any]] = {name: [] for name in method_names}
    registry = HandlerRegistry()

    def make_handler(name: str):
        def handler(params: Any, _name: str = name) -> dict[str, Any]:
            calls[_name].append(params)
            return {"echo": params, "method": _name}

        return handler

    for name in method_names:
        registry.register(name, make_handler(name))

    return registry, calls


@given(
    method_names=_method_names,
    target_index=st.integers(min_value=0),
    params=_json_values,
    request_id=_request_ids,
)
def test_handler_dispatch_round_trip(
    method_names: list[str],
    target_index: int,
    params: Any,
    request_id: int | str | None,
) -> None:
    """Handler dispatch round-trip.

    Build a registry from a generated method-name corpus where every
    name maps to a unique tracking handler. Pick one method ``m`` from
    the corpus, dispatch a single ``JsonRpcRequest`` for it with the
    generated params, and assert:

    * the handler bound to ``m`` was invoked exactly once,
    * its sole received argument is the original ``params`` value
      (identity is irrelevant; equality is what the contract specifies),
    * no other handler in the registry was invoked,
    * the dispatcher returned a :class:`JsonRpcResponse` whose ``id``
      equals the request id and whose ``result`` equals the handler's
      return value.
    """
    registry, calls = _make_registry_and_calls(method_names)
    target = method_names[target_index % len(method_names)]

    # Advance the lifecycle past the -32002 initialize gate so the
    # dispatcher reaches handler dispatch. The method under test is
    # never ``initialize``, so the on_initialize_success short-circuit
    # is the only way to legally admit it.
    manager = LifecycleManager()
    manager.on_initialize_success(_INITIAL_CAPS)

    request = JsonRpcRequest(
        method=target,
        params=params,
        id=request_id,
        is_notification=False,
    )

    result = asyncio.run(
        dispatch(
            request,
            registry=registry,
            lifecycle=manager,
            log=lambda _msg: None,
        )
    )

    # ---- Handler invocation ----------------------------------------- #
    # The chosen handler ran exactly once with the original params; no
    # other handler was disturbed.
    assert calls[target] == [params], f"handler for {target!r} expected to receive [{params!r}], got {calls[target]!r}"
    for name, recorded in calls.items():
        if name == target:
            continue
        assert recorded == [], f"unrelated handler for {name!r} was invoked: {recorded!r}"

    # ---- Response shape --------------------------------------------- #
    assert isinstance(result, JsonRpcResponse), (
        f"dispatch must return JsonRpcResponse for a non-notification request; got {type(result).__name__}"
    )
    assert result.id == request_id, f"response id {result.id!r} must echo request id {request_id!r}"
    expected_result = {"echo": params, "method": target}
    assert result.result == expected_result, (
        f"response result {result.result!r} must equal handler's return value {expected_result!r}"
    )

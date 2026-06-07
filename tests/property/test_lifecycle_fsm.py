"""Property test for the WSP server lifecycle FSM.

The :class:`~wispy.lifecycle.LifecycleManager` is a small finite-state
machine with one piece of cached side-state -- the
:class:`~wispy.endpoints.Capabilities` returned by the first successful
``initialize`` request. This module drives the manager with a Hypothesis
:class:`~hypothesis.stateful.RuleBasedStateMachine` that mirrors the
state diagram from ``design.md`` against a shadow model maintained by
the test, then checks per-step that:

* :meth:`~wispy.lifecycle.LifecycleManager.admit` returns the
  decision predicted by the shadow model for every
  ``(state, method, is_notification)`` triple,
* edge transitions
  (:meth:`~wispy.lifecycle.LifecycleManager.on_initialize_success`,
  :meth:`~wispy.lifecycle.LifecycleManager.on_shutdown_success`,
  :meth:`~wispy.lifecycle.LifecycleManager.on_exit`) move the manager
  in lockstep with the shadow state,
* the cached :class:`~wispy.endpoints.Capabilities` are preserved by
  identity across rejected re-``initialize`` requests, and
* every admit decision from the terminal ``EXITED`` state is the
  defensive ``-32600`` rejection.
"""

from __future__ import annotations

from typing import Any

from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

from wispy.endpoints import Capabilities
from wispy.errors import JsonRpcErrorCode
from wispy.lifecycle import (
    Allow,
    Exit,
    LifecycleManager,
    RejectWith,
    ServerState,
)

# Method-name strategy for the catch-all "any method other than the
# lifecycle-specific ones" rule. ``initialize``, ``shutdown``, and
# ``exit`` have dedicated rules above; everything else exercises the
# generic per-state branches in :meth:`LifecycleManager.admit`.
_RESERVED_METHODS = frozenset({"initialize", "shutdown", "exit"})
_other_method_names = st.text(min_size=1, max_size=32).filter(lambda s: s not in _RESERVED_METHODS)


# Compact aliases for the JSON-RPC error codes the FSM emits, so the
# test reads as close as possible to the design's transition table.
_INVALID_REQUEST = int(JsonRpcErrorCode.INVALID_REQUEST)
_SERVER_NOT_INITIALIZED = int(JsonRpcErrorCode.SERVER_NOT_INITIALIZED)


class LifecycleStateMachine(RuleBasedStateMachine):
    """Lifecycle FSM transitions.

    The machine runs a :class:`LifecycleManager` (the system under test)
    side-by-side with a shadow model: an explicit
    :class:`ServerState` plus a cached :class:`Capabilities` reference
    that records the value passed to the first
    :meth:`LifecycleManager.on_initialize_success`. Every rule:

    1. computes the expected admit decision from the shadow state,
    2. invokes :meth:`LifecycleManager.admit`,
    3. asserts the actual decision matches expectations
       (:class:`Allow` identity-check, :class:`RejectWith` error-code
       check, :class:`Exit` status check), and
    4. for the success-edge rules, advances both the manager and the
       shadow state in lockstep.

    Invariants run after every step and check the global properties
    that must hold regardless of which rule just ran.
    """

    # A single, stable :class:`Capabilities` object used for every
    # successful ``initialize`` so the identity-preservation invariant
    # has something concrete to compare against.
    _CAPS = Capabilities(methods=(), protocol_version="0.1.0")

    def __init__(self) -> None:
        super().__init__()
        self.manager = LifecycleManager()
        # Shadow model. The class invariant ``shadow_matches_manager``
        # ties this back to ``manager.state`` after every step.
        self.shadow_state = ServerState.UNINITIALIZED
        # Capabilities reference cached after the first successful
        # ``initialize``. Stays ``None`` while UNINITIALIZED.
        self.cached_caps: Capabilities | None = None

    # ------------------------------------------------------------------ #
    # Shadow-model helpers.
    # ------------------------------------------------------------------ #

    def _expected_decision(
        self,
        method: str,
        is_notification: bool,  # noqa: FBT001 - mirrors the FSM admit contract
    ) -> tuple[Any, ...]:
        """Return the expected admit decision shape for the shadow state.

        Encoded as a small tagged tuple to keep the comparison code in
        :meth:`_check_decision` straightforward:

        * ``("allow",)`` -- :class:`Allow` (singleton-style instance).
        * ``("reject", code)`` -- :class:`RejectWith` with that JSON-RPC
          error code.
        * ``("exit", status)`` -- :class:`Exit` with that process status.
        """
        # Defensive branch: any traffic post-EXITED is rejected with
        # -32600 regardless of method or notification flag.
        if self.shadow_state is ServerState.EXITED:
            return ("reject", _INVALID_REQUEST)

        # ``exit`` notifications are the only path to an :class:`Exit`
        # decision; status depends on whether ``shutdown`` ran first.
        if method == "exit" and is_notification:
            if self.shadow_state is ServerState.SHUTTING_DOWN:
                return ("exit", 0)
            return ("exit", 1)

        if self.shadow_state is ServerState.UNINITIALIZED:
            if method == "initialize":
                return ("allow",)
            # Anything else before initialize fails -32002.
            return ("reject", _SERVER_NOT_INITIALIZED)

        if self.shadow_state is ServerState.INITIALIZED:
            if method == "initialize":
                # Re-initialize is rejected with -32600
                # and capabilities are preserved (checked by the
                # invariant ``capabilities_preserved``).
                return ("reject", _INVALID_REQUEST)
            return ("allow",)

        # SHUTTING_DOWN: the ``exit``-notification branch was handled
        # above; everything else -- including a
        # non-notification ``exit`` request -- rejects -32600.
        return ("reject", _INVALID_REQUEST)

    def _check_decision(
        self,
        actual: Any,
        expected: tuple[Any, ...],
        method: str,
        is_notification: bool,  # noqa: FBT001 - mirrors the FSM admit contract
    ) -> None:
        """Assert that ``actual`` matches the shape of ``expected``."""
        ctx = f"method={method!r} is_notification={is_notification} shadow_state={self.shadow_state.name}"
        kind = expected[0]
        if kind == "allow":
            assert isinstance(actual, Allow), f"expected Allow ({ctx}); got {actual!r}"
        elif kind == "reject":
            assert isinstance(actual, RejectWith), f"expected RejectWith ({ctx}); got {actual!r}"
            assert actual.error.code == expected[1], (
                f"expected reject code {expected[1]} ({ctx}); "
                f"got {actual.error.code} (message={actual.error.message!r})"
            )
            # Rejection messages always carry a non-empty string.
            assert isinstance(actual.error.message, str)
            assert actual.error.message != ""
        else:  # "exit"
            assert isinstance(actual, Exit), f"expected Exit ({ctx}); got {actual!r}"
            assert actual.status == expected[1], f"expected exit status {expected[1]} ({ctx}); got {actual.status}"

    def _admit_and_check(self, method: str, is_notification: bool) -> None:  # noqa: FBT001 - mirrors the FSM admit contract
        """Drive one ``admit`` call and verify the resulting decision."""
        expected = self._expected_decision(method, is_notification)
        actual = self.manager.admit(method, is_notification)
        self._check_decision(actual, expected, method, is_notification)

    # ------------------------------------------------------------------ #
    # Admit-edge rules: every method/notification combination of interest.
    # ------------------------------------------------------------------ #

    @rule()
    def admit_initialize(self) -> None:
        """Lifecycle FSM transitions.

        ``initialize`` request from any state. Allowed only from
        UNINITIALIZED; rejected -32600 (re-initialize) from
        INITIALIZED; rejected -32600 (shutting down) from
        SHUTTING_DOWN; rejected -32600 (defensive) from EXITED.
        """
        self._admit_and_check("initialize", is_notification=False)

    @rule()
    def admit_initialize_notification(self) -> None:
        """Lifecycle FSM transitions.

        ``initialize`` notification: identical state-table behavior to
        the request form because the FSM does not special-case
        ``initialize`` on notification.
        """
        self._admit_and_check("initialize", is_notification=True)

    @rule()
    def admit_shutdown(self) -> None:
        """Lifecycle FSM transitions.

        ``shutdown`` request: rejected -32002 from UNINITIALIZED,
        allowed from INITIALIZED, rejected -32600 from SHUTTING_DOWN,
        rejected -32600 from EXITED.
        """
        self._admit_and_check("shutdown", is_notification=False)

    @rule()
    def admit_exit_notification(self) -> None:
        """Lifecycle FSM transitions.

        ``exit`` notification: drives the FSM toward EXITED with
        status 0 (post-shutdown) or status 1 (otherwise).
        """
        self._admit_and_check("exit", is_notification=True)

    @rule()
    def admit_exit_request(self) -> None:
        """Lifecycle FSM transitions.

        Non-notification ``exit`` is treated as any other method:
        rejected -32002 from UNINITIALIZED, allowed from INITIALIZED,
        rejected -32600 from SHUTTING_DOWN. It MUST NOT produce an
        ``Exit`` decision.
        """
        self._admit_and_check("exit", is_notification=False)

    @rule(method=_other_method_names, is_notification=st.booleans())
    def admit_other_method(self, method: str, is_notification: bool) -> None:  # noqa: FBT001 - mirrors the FSM admit contract
        """Lifecycle FSM transitions.

        Generic catch-all: any method name other than the lifecycle
        ones, in either notification or request form, exercises the
        per-state default branches of ``admit``.
        """
        self._admit_and_check(method, is_notification)

    # ------------------------------------------------------------------ #
    # Edge-completion rules: advance both manager and shadow state.
    # ------------------------------------------------------------------ #

    @precondition(lambda self: self.shadow_state is ServerState.UNINITIALIZED)
    @rule()
    def complete_initialize(self) -> None:
        """Lifecycle FSM transitions.

        Models the runtime calling ``on_initialize_success`` after a
        successful ``initialize`` handler. Caches the
        :class:`Capabilities` object by reference so the
        ``capabilities_preserved`` invariant can verify it survives a
        rejected re-``initialize`` later.
        """
        self.manager.on_initialize_success(self._CAPS)
        self.shadow_state = ServerState.INITIALIZED
        self.cached_caps = self._CAPS
        # Identity check: the manager must hold the very same object we
        # passed in, not a copy or a normalized variant.
        assert self.manager.capabilities is self._CAPS

    @precondition(lambda self: self.shadow_state is ServerState.INITIALIZED)
    @rule()
    def complete_shutdown(self) -> None:
        """Lifecycle FSM transitions.

        Models the runtime calling ``on_shutdown_success`` after a
        successful ``shutdown`` handler.
        """
        self.manager.on_shutdown_success()
        self.shadow_state = ServerState.SHUTTING_DOWN
        # Capabilities are not cleared by shutdown; preserve the cached
        # reference so the EXITED-path invariants stay coherent.

    @precondition(lambda self: self.shadow_state is not ServerState.EXITED)
    @rule()
    def advance_to_exited(self) -> None:
        """Lifecycle FSM transitions.

        Models the runtime acknowledging an ``exit`` notification by
        calling ``on_exit``. The returned status MUST equal what
        :meth:`admit` would have reported for the ``exit`` notification
        in this state: ``0`` from SHUTTING_DOWN, ``1`` otherwise.
        Crucially, this rule is what makes the EXITED-state defensive
        rejections reachable for the ``admit_*`` rules.
        """
        expected_status = 0 if self.shadow_state is ServerState.SHUTTING_DOWN else 1
        actual_status = self.manager.on_exit()
        assert actual_status == expected_status
        self.shadow_state = ServerState.EXITED

    # ------------------------------------------------------------------ #
    # Invariants: hold after every rule.
    # ------------------------------------------------------------------ #

    @invariant()
    def state_is_valid(self) -> None:
        """``manager.state`` MUST always be a :class:`ServerState` member."""
        assert isinstance(self.manager.state, ServerState)

    @invariant()
    def shadow_matches_manager(self) -> None:
        """The shadow state MUST track the manager's state exactly.

        Catches any drift between the test's model and the SUT: a rule
        that advances one without the other is a test-or-implementation
        bug.
        """
        assert self.manager.state is self.shadow_state, (
            f"shadow {self.shadow_state.name} drifted from manager {self.manager.state.name}"
        )

    @invariant()
    def capabilities_preserved(self) -> None:
        """Cached capabilities survive every state with identity intact.

        Two cases must hold simultaneously:

        * Until the first successful ``initialize``, no capabilities
          have been advertised and ``manager.capabilities`` MUST be
          ``None``. This includes the path UNINITIALIZED -> EXITED via
          a bare ``exit`` notification: we may reach
          EXITED without having ever cached a :class:`Capabilities`.
        * Once the test has cached a reference (after
          :meth:`complete_initialize`), the manager MUST hold the same
          :class:`Capabilities` object by identity, regardless of
          subsequent state transitions or rejected re-``initialize``
          attempts.
        """
        if self.cached_caps is None:
            assert self.manager.capabilities is None
        else:
            assert self.manager.capabilities is self.cached_caps


# pytest discovers the unittest.TestCase auto-generated by Hypothesis
# when it is bound to a module-level name beginning with ``Test``.
TestLifecycleFSM = LifecycleStateMachine.TestCase

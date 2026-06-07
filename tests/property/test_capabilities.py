"""Property tests for WSP capabilities advertisement.

The ``Capabilities``
object cached by the lifecycle manager after a successful ``initialize`` MUST
enumerate exactly the methods registered with the :class:`HandlerRegistry`
(each method name present once, none missing), and its ``protocol_version``
MUST match the semver regex
``^(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)$``.

Capabilities reflect the registry exactly.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from wispy.endpoints import PROTOCOL_VERSION, WSP_METHODS, Capabilities
from wispy.lifecycle import LifecycleManager, ServerState
from wispy.registry import HandlerRegistry

# Semver regex used to validate the advertised protocol version. The
# character class
# uses ``\d`` (any decimal digit per the Unicode default) per the design
# document. Compiled once at module scope so the property test does not
# pay re-compilation cost across Hypothesis examples.
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


# Pre-compute the corpus of WSP method names. ``WSP_METHODS`` is the
# single source of truth for which JSON-RPC methods are defined by WSP;
# Hypothesis draws random subsets from it to stand in for arbitrary
# Workflow_Tool registries.
_ALL_METHOD_NAMES = sorted(WSP_METHODS.keys())


def _noop_handler(_params: Any) -> None:
    """Identity-free placeholder handler used purely to satisfy registration.

    The capabilities property is concerned only with which method names
    are bound, never with what the handlers return, so a single shared
    no-op suffices for every binding.
    """
    return None


@given(
    method_names=st.lists(
        st.sampled_from(_ALL_METHOD_NAMES),
        unique=True,
        min_size=0,
        max_size=len(_ALL_METHOD_NAMES),
    ),
)
def test_capabilities_reflect_registry_exactly(
    method_names: list[str],
) -> None:
    """Capabilities reflect the registry exactly.

    Build a :class:`HandlerRegistry` containing a Hypothesis-drawn
    subset of WSP method names, construct the :class:`Capabilities`
    object the same way the dispatcher will, drive it through
    :meth:`LifecycleManager.on_initialize_success`, and assert:

    * the manager caches the exact ``Capabilities`` instance,
    * the multiset of advertised method names equals the registry's
      bound method set (each registered name appears exactly once,
      no name missing, no extra names introduced), and
    * the advertised ``protocol_version`` matches the semver regex.
    """
    # Build the registry with a no-op handler for each drawn method.
    registry = HandlerRegistry()
    for name in method_names:
        registry.register(name, _noop_handler)

    # Build the capabilities object directly from the registry. This
    # mirrors the design's "Capabilities reflect the registry exactly"
    # contract without depending on the dispatcher (which is not yet
    # wired into the lifecycle at this stage of the build plan).
    caps = Capabilities(
        methods=tuple(registry.methods()),
        protocol_version=PROTOCOL_VERSION,
    )

    # Drive the lifecycle FSM through a successful initialize.
    manager = LifecycleManager()
    assert manager.state is ServerState.UNINITIALIZED
    assert manager.capabilities is None

    manager.on_initialize_success(caps)

    # The manager must cache the very same Capabilities object handed
    # to it (identity, not just equality), and advance to INITIALIZED.
    assert manager.capabilities is caps
    assert manager.state is ServerState.INITIALIZED

    # Multiset equality: every registered method appears exactly once
    # in caps.methods, and caps.methods contains nothing else. Using a
    # Counter rather than ``set(...)`` would catch a duplicate-name bug
    # that ``set`` equality silently swallows.
    assert Counter(caps.methods) == Counter(method_names)

    # Requirement: protocol_version is semver-shaped.
    assert _SEMVER_RE.match(caps.protocol_version) is not None, (
        f"protocol_version {caps.protocol_version!r} does not match semver regex {_SEMVER_RE.pattern!r}"
    )

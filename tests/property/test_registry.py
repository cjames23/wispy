"""Property tests for the WSP handler registry.

Attempting to bind
a method name that is already registered MUST raise
:class:`wispy.errors.DuplicateRegistrationError` and leave the registry's
prior state unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from wispy.errors import DuplicateRegistrationError
from wispy.registry import HandlerRegistry

if TYPE_CHECKING:
    from collections.abc import Callable


def _make_handler(name: str) -> Callable[[Any], str]:
    """Return a distinct, identifiable handler for ``name``.

    Each handler is a fresh function object so identity comparisons can
    detect any silent overwrite by the registry. The returned value is a
    callable matching the :data:`wispy.registry.Handler` protocol.
    """

    def handler(_params: Any, _bound_name: str = name) -> str:
        return _bound_name

    handler.__name__ = f"handler_for_{name!r}"
    return handler


@given(
    method_names=st.lists(
        st.text(min_size=1, max_size=64),
        min_size=1,
        max_size=20,
        unique=True,
    ),
    duplicate_index=st.integers(min_value=0),
)
def test_duplicate_registration_leaves_registry_unchanged(
    method_names: list[str],
    duplicate_index: int,
) -> None:
    """Duplicate registration leaves registry unchanged.

    Build a registry from a Hypothesis-generated method-name corpus,
    snapshot internal state via ``methods()`` + ``lookup()`` for each
    name, attempt to register a fresh handler against an already-bound
    method, and assert that ``DuplicateRegistrationError`` is raised
    while the snapshot is unchanged.
    """
    registry = HandlerRegistry()
    original_handlers = {name: _make_handler(name) for name in method_names}

    for name in method_names:
        registry.register(name, original_handlers[name])

    # Snapshot internal state before the duplicate-registration attempt.
    methods_before = registry.methods()
    lookups_before = [(name, registry.lookup(name)) for name in methods_before]

    # Pick an already-registered method to attempt to re-bind.
    duplicate_name = method_names[duplicate_index % len(method_names)]
    intruder = _make_handler(f"intruder::{duplicate_name}")
    # The intruder is a different function object from whatever is
    # currently bound, so any silent overwrite would be detectable.
    assert intruder is not registry.lookup(duplicate_name)

    with pytest.raises(DuplicateRegistrationError):
        registry.register(duplicate_name, intruder)

    # State must be bitwise-equal to the pre-call snapshot: same method
    # set in the same order, and each name still maps to the exact
    # handler object originally bound (identity, not just equality).
    methods_after = registry.methods()
    lookups_after = [(name, registry.lookup(name)) for name in methods_after]

    assert methods_after == methods_before
    assert lookups_after == lookups_before
    assert registry.lookup(duplicate_name) is original_handlers[duplicate_name]

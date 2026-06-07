"""Property test for CLI param merge."""

from __future__ import annotations

import string
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from wispy.cli.main import merge_params

_keys = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8)
_values = st.one_of(
    st.text(min_size=0, max_size=16),
    st.integers(),
    st.booleans(),
    # ``None`` is excluded for flag values to match ``merge_params``
    # semantics (None means "flag not supplied"); the dedicated
    # ``test_none_flag_does_not_shadow_stdin`` test below adds it back
    # for the flag side.
)
_dicts = st.dictionaries(_keys, _values, max_size=8)


@given(stdin_params=_dicts, flag_params=_dicts)
def test_merge_keeps_stdin_overlays_flags(
    stdin_params: dict[str, Any],
    flag_params: dict[str, Any],
) -> None:
    """CLI param merge.

    For every pair of dicts ``(stdin_params, flag_params)``, the merged
    result:

    * Contains every stdin-only key with its stdin value.
    * Contains every flag key with its flag value (overriding any
      stdin entry of the same key).
    * Contains no other keys.
    """
    merged = merge_params(stdin_params, flag_params)
    expected_keys = set(stdin_params.keys()) | set(flag_params.keys())
    assert set(merged.keys()) == expected_keys
    for k, v in stdin_params.items():
        if k in flag_params:
            assert merged[k] == flag_params[k]
        else:
            assert merged[k] == v
    for k, v in flag_params.items():
        assert merged[k] == v


@given(
    stdin_params=_dicts,
    flag_params=st.dictionaries(_keys, st.none(), max_size=4),
)
def test_none_flag_does_not_shadow_stdin(
    stdin_params: dict[str, Any],
    flag_params: dict[str, Any],
) -> None:
    """CLI param merge (None flag preservation).

    A flag whose value is ``None`` means "flag not supplied" and MUST
    NOT shadow a same-named stdin entry. The merged dict's value for
    that key is the stdin value if present; otherwise, the key is
    absent entirely.
    """
    merged = merge_params(stdin_params, flag_params)
    for k, v in stdin_params.items():
        if k in flag_params:
            # flag_params has None for this key -> stdin value preserved.
            assert merged[k] == v
        else:
            assert merged[k] == v
    # Keys that are ONLY in flag_params (with None) are NOT in merged.
    flag_only = set(flag_params) - set(stdin_params)
    for k in flag_only:
        assert k not in merged

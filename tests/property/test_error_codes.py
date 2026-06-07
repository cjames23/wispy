"""Property tests for the WSP error code enum.

Every member of
:class:`wispy.errors.WspErrorCode` MUST have a numeric value inside the
WSP-reserved range ``[-31999, -31000]``.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from wispy.errors import WspErrorCode

# Inclusive bounds of the WSP-reserved JSON-RPC error code range.
WSP_CODE_MIN = -31999
WSP_CODE_MAX = -31000


@given(code=st.sampled_from(list(WspErrorCode)))
def test_wsp_error_codes_in_reserved_range(code: WspErrorCode) -> None:
    """WSP error codes are in the reserved range.

    For every member of ``WspErrorCode``, the integer value lies inside the
    closed interval ``[-31999, -31000]`` reserved for application-level
    WSP errors (and disjoint from the JSON-RPC reserved range).
    """
    assert WSP_CODE_MIN <= int(code) <= WSP_CODE_MAX, (
        f"WspErrorCode.{code.name} = {int(code)} is outside the reserved WSP range [{WSP_CODE_MIN}, {WSP_CODE_MAX}]"
    )

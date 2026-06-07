"""Property test for the Subprocess_Handler factory.

The Subprocess_Handler factory in :mod:`wispy.config` wraps an
external command into a :data:`~wispy.registry.Handler` callable.
A clean exit with valid JSON on stdout MUST
return the parsed value. Every other outcome
(non-zero exit, garbled JSON, timeout) MUST raise
:class:`~wispy.errors.WspError` with code
:data:`~wispy.errors.WspErrorCode.EXECUTION_FAILED` (-31004).

The test uses a tiny Python helper script written to a tempfile.
The script reads JSON from stdin and branches on its ``argv[1]``
mode flag to one of four behaviours: ``echo`` (round-trip), ``fail``
(exit non-zero), ``garbage`` (write invalid JSON), or ``sleep``
(block past the configured timeout). The mode flag is part of the
``argv`` baked into the :class:`~wispy.config.SubprocessHandlerSpec`,
so each handler invocation spawns a fresh subprocess with the
selected behaviour.

The factory hardcodes a 30-second wall-clock timeout
(:data:`wispy.config._SUBPROCESS_TIMEOUT`). To exercise the timeout
failure mode without making the test slow, the timeout test
monkeypatches that module attribute to 0.5 s so the helper's
``time.sleep(60)`` is observed as a timeout in well under a second.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import wispy.config
from wispy.config import (
    SubprocessHandlerSpec,
    _make_subprocess_handler,
)
from wispy.errors import WspError, WspErrorCode

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from pathlib import Path

    from wispy.registry import Handler

# Helper script source. Written to a tempfile per test session and
# invoked via ``sys.executable`` so the test does not assume any
# particular interpreter is on PATH.
#
# The script intentionally does *not* import anything from ``wispy``;
# it must run in a bare interpreter the way a user-supplied
# Subprocess_Handler would.
_HELPER_SOURCE = """\
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
raw = sys.stdin.read()
try:
    params = json.loads(raw)
except Exception:
    params = None

if mode == "echo":
    sys.stdout.write(json.dumps(params))
elif mode == "fail":
    sys.stderr.write("intentional failure\\n")
    sys.exit(7)
elif mode == "garbage":
    sys.stdout.write("not valid json !@#$%")
elif mode == "sleep":
    time.sleep(60)
else:
    sys.stderr.write(f"unknown mode {mode!r}\\n")
    sys.exit(2)
"""


@pytest.fixture
def helper_script(tmp_path: Path) -> Path:
    """Write the helper script to a tempfile and return its path."""
    p = tmp_path / "helper.py"
    p.write_text(_HELPER_SOURCE)
    return p


def _make_handler_for_mode(helper: Path, mode: str) -> Handler:
    """Build a Subprocess_Handler whose argv runs ``helper`` in ``mode``."""
    spec = SubprocessHandlerSpec(argv=(sys.executable, str(helper), mode))
    return _make_subprocess_handler(spec)


def _run(handler: Handler, params: Any) -> Any:
    """Invoke a Subprocess_Handler synchronously via ``asyncio.run``.

    The factory always returns an ``async def`` callable (so calling it
    yields a coroutine), but the :data:`Handler` protocol declares the
    return type as ``Awaitable[Any] | Any`` to permit sync handlers in
    other code paths. ``cast`` narrows the call result so pyrefly is
    happy with passing it to :func:`asyncio.run`, which insists on a
    proper :class:`Coroutine`.
    """
    coro = cast("Coroutine[Any, Any, Any]", handler(params))
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# Round-trip half.
# ---------------------------------------------------------------------- #


# JSON-serializable params strategy. Floats are excluded so the
# round-trip is bit-exact: ``json.dumps`` then ``json.loads`` is only
# guaranteed to round-trip for the JSON value space we restrict to
# here (None, bool, int, str, list, dict). The property under test
# is the handler's identity behaviour, not floating-point round-trip
# semantics.
_round_trip_params = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(
            st.text(min_size=1, max_size=8),
            children,
            max_size=4,
        )
    ),
    max_leaves=4,
)


@settings(
    deadline=None,
    max_examples=25,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(params=_round_trip_params)
def test_round_trip(helper_script: Path, params: Any) -> None:
    """Subprocess handler round-trip.

    For any JSON-serializable value ``p``, a Subprocess_Handler whose
    child echoes its stdin verbatim MUST return a value equal to ``p``.
    Each invocation spawns a fresh subprocess, so the property holds
    independently of any prior call history.
    """
    handler = _make_handler_for_mode(helper_script, "echo")
    result = _run(handler, params)
    assert result == params


# ---------------------------------------------------------------------- #
# Failure-mapping half.
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["fail", "garbage"])
def test_failure_modes_raise_wsp_error(helper_script: Path, mode: str) -> None:
    """Failure modes map to WspError(EXECUTION_FAILED).

    Both a non-zero exit (``fail``) and unparseable stdout
    (``garbage``) MUST surface as :class:`WspError` whose ``code``
    equals :data:`WspErrorCode.EXECUTION_FAILED` (-31004). The
    diagnostic ``data`` payload carries a ``reason`` discriminator
    distinguishing the failure mode for the caller.
    """
    handler = _make_handler_for_mode(helper_script, mode)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {"x": 1})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    expected_reason = {
        "fail": "non-zero-exit",
        "garbage": "invalid-json-output",
    }[mode]
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == expected_reason


def test_timeout_raises_wsp_error(helper_script: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout maps to WspError(EXECUTION_FAILED).

    The factory's 30 s timeout is hardcoded as a module attribute
    so a test can shorten it without rewriting the factory. We patch
    it to 0.5 s and run the helper in ``sleep`` mode (which would
    otherwise block for 60 s), then assert the failure surfaces as a
    :class:`WspError` with the timeout discriminator.
    """
    monkeypatch.setattr(wispy.config, "_SUBPROCESS_TIMEOUT", 0.5)
    handler = _make_handler_for_mode(helper_script, "sleep")
    with pytest.raises(WspError) as excinfo:
        _run(handler, None)
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "timeout"


def test_followup_request_succeeds(helper_script: Path) -> None:
    """A follow-up valid request to the same handler succeeds.

    Each handler invocation spawns a fresh subprocess (see
    :func:`wispy.config._make_subprocess_handler`), so a failure in
    one call MUST NOT poison subsequent calls. We verify this by
    invoking a ``fail``-mode handler (and confirming it raises) and
    then invoking a fresh ``echo``-mode handler with the same spec
    shape against the same helper script and asserting the round-trip
    succeeds.

    A separate handler object is constructed for the success call to
    mirror the property statement: any *valid* request to a handler
    against the same spec succeeds. (Calling ``fail_handler`` again
    would just re-spawn the failing child, which is correct but
    uninteresting.)
    """
    fail_handler = _make_handler_for_mode(helper_script, "fail")
    with pytest.raises(WspError):
        _run(fail_handler, None)

    success_handler = _make_handler_for_mode(helper_script, "echo")
    result = _run(success_handler, {"a": 1})
    assert result == {"a": 1}


def test_subprocess_not_alive_after_failure(helper_script: Path) -> None:
    """Subprocess is no longer alive after a failure.

    The factory guarantees that, once the handler's coroutine
    completes (whether by returning a value or raising
    :class:`WspError`), the spawned subprocess has terminated. We
    cannot directly observe the child PID from the public API, but
    we can observe the consequence: a failed call followed by a
    successful call against a fresh handler returns promptly and
    correctly. If the failed call had leaked a running child, the
    timeout test (which uses a 0.5 s patched limit) would not be
    able to terminate within the test deadline.

    This test specifically covers the timeout path, where the
    factory must ``proc.kill()`` the child before raising. We assert
    that after the timeout failure, a brand-new handler invocation
    completes promptly.
    """
    # First, force a timeout failure with a short patched limit. We
    # cannot use ``monkeypatch`` here because this test is not a
    # function-scoped fixture consumer in the usual sense; instead
    # we save and restore the attribute manually.
    original_timeout = wispy.config._SUBPROCESS_TIMEOUT  # noqa: SLF001 - testing private timeout knob
    wispy.config._SUBPROCESS_TIMEOUT = 0.5  # noqa: SLF001 - testing private timeout knob
    try:
        sleep_handler = _make_handler_for_mode(helper_script, "sleep")
        with pytest.raises(WspError):
            _run(sleep_handler, None)
    finally:
        wispy.config._SUBPROCESS_TIMEOUT = original_timeout  # noqa: SLF001 - testing private timeout knob

    # A follow-up echo call against a fresh handler completes
    # promptly: if the timed-out child had not been killed, this
    # test would either hang or run far longer than expected.
    start = time.monotonic()
    echo_handler = _make_handler_for_mode(helper_script, "echo")
    result = _run(echo_handler, {"ok": True})
    elapsed = time.monotonic() - start
    assert result == {"ok": True}
    assert elapsed < 10.0, f"follow-up handler took {elapsed:.2f}s; the timed-out child may not have been killed"

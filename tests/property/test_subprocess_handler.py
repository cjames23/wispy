"""Property and unit tests for the Subprocess_Handler factory.

The Subprocess_Handler factory in :mod:`wispy.config` wraps an
external command into a :data:`~wispy.registry.Handler` callable.
Each WSP method's request params are substituted into the command's
argv via ``{key}`` tokens, the process is spawned with stdin closed
immediately, and the result is constructed per the configured
:class:`~wispy.config.ResultMode`:

* ``"json"``     -- parse stdout as JSON; return the parsed value.
* ``"template"`` -- render a template table from the request params.
* ``"exec"``     -- return ``{exit_code, stdout, stderr}``.
* ``"none"``     -- return ``null``; non-zero exit is a failure.

Failures (spawn, timeout, non-zero exit for non-exec modes,
non-JSON stdout for ``"json"``, malformed template) all surface as
:class:`~wispy.errors.WspError` carrying
:data:`~wispy.errors.WspErrorCode.EXECUTION_FAILED` (-31004) with a
``data.reason`` discriminator.

Tests use a tiny Python helper script so they do not depend on any
particular external CLI being installed. The script reads its argv
to decide what to print and what exit code to use; this mirrors how
real CLIs (Hatch, pip, etc.) are driven by argv rather than stdin.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import wispy.config
from wispy.config import (
    ResultMode,
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
# The script intentionally does *not* import anything from ``wispy``.
# It accepts a mode in ``argv[1]`` and varies its output / exit
# behaviour accordingly. ``argv[2:]`` is the rest of the rendered
# argv -- the helper echoes those positions when asked, which lets
# the round-trip test verify substitution worked.
_HELPER_SOURCE = """\
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "echo-argv"
extra = sys.argv[2:]

if mode == "echo-argv":
    sys.stdout.write(json.dumps(extra))
elif mode == "echo-json":
    # Always print the same canonical JSON object on stdout. The
    # caller asserts on this exact value.
    sys.stdout.write(json.dumps({"ok": True, "argv": extra}))
elif mode == "fail":
    sys.stderr.write("intentional failure\\n")
    sys.exit(7)
elif mode == "garbage":
    sys.stdout.write("not valid json !@#$%")
elif mode == "silent":
    pass
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


def _spec(
    helper: Path,
    mode: str,
    *,
    template_extra: tuple[str, ...] = (),
    result_mode: ResultMode = ResultMode.JSON,
    result_template: Any = None,
) -> SubprocessHandlerSpec:
    """Build a :class:`SubprocessHandlerSpec` running ``helper`` in ``mode``."""
    argv_template = (sys.executable, str(helper), mode, *template_extra)
    return SubprocessHandlerSpec(
        argv_template=argv_template,
        result_mode=result_mode,
        result_template=result_template,
    )


def _run(handler: Handler, params: Any) -> Any:
    """Invoke a Subprocess_Handler synchronously via ``asyncio.run``.

    The factory always returns an ``async def`` callable (so calling
    it yields a coroutine), but the :data:`Handler` protocol declares
    the return type as ``Awaitable[Any] | Any`` to permit sync
    handlers in other code paths. ``cast`` narrows the call result so
    pyrefly is happy with passing it to :func:`asyncio.run`.
    """
    coro = cast("Coroutine[Any, Any, Any]", handler(params))
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# ResultMode.JSON.
# ---------------------------------------------------------------------- #


def test_json_mode_returns_parsed_stdout(helper_script: Path) -> None:
    """``result = "json"`` parses stdout as JSON and returns the value."""
    spec = _spec(helper_script, "echo-json", result_mode=ResultMode.JSON)
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {})
    assert isinstance(result, dict)
    assert result["ok"] is True


def test_json_mode_garbage_raises_invalid_json_output(helper_script: Path) -> None:
    """``result = "json"`` rejects non-JSON stdout."""
    spec = _spec(helper_script, "garbage", result_mode=ResultMode.JSON)
    handler = _make_subprocess_handler(spec)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "invalid-json-output"


# ---------------------------------------------------------------------- #
# ResultMode.NONE.
# ---------------------------------------------------------------------- #


def test_none_mode_returns_null(helper_script: Path) -> None:
    """``result = "none"`` returns None on exit 0 regardless of stdout."""
    spec = _spec(helper_script, "silent", result_mode=ResultMode.NONE)
    handler = _make_subprocess_handler(spec)
    assert _run(handler, {}) is None


def test_none_mode_nonzero_exit_raises(helper_script: Path) -> None:
    """``result = "none"`` still treats non-zero exit as a failure."""
    spec = _spec(helper_script, "fail", result_mode=ResultMode.NONE)
    handler = _make_subprocess_handler(spec)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "non-zero-exit"


# ---------------------------------------------------------------------- #
# ResultMode.EXEC.
# ---------------------------------------------------------------------- #


def test_exec_mode_returns_exit_code_stdout_stderr(helper_script: Path) -> None:
    """``result = "exec"`` returns the captured outcome verbatim."""
    spec = _spec(helper_script, "echo-argv", result_mode=ResultMode.EXEC)
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {})
    assert result["exit_code"] == 0
    assert result["stderr"] == ""
    # The helper printed an empty argv list because no extras were
    # passed. (Round-trip with substitution is exercised below.)
    assert result["stdout"] == "[]"


def test_exec_mode_nonzero_exit_is_not_a_failure(helper_script: Path) -> None:
    """``result = "exec"`` surfaces non-zero exits to the caller as a value.

    For ``environment/execute``, the exit code IS the result. The
    factory must not raise on non-zero exits in this mode.
    """
    spec = _spec(helper_script, "fail", result_mode=ResultMode.EXEC)
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {})
    assert result["exit_code"] == 7
    assert "intentional failure" in result["stderr"]


def test_exec_mode_splats_argv_token(helper_script: Path) -> None:
    """``"{argv}"`` in the template splats a list of strings into argv.

    This is the substitution mode used by ``environment/execute``:
    the WSP request carries an ``argv`` array of strings; the
    Config_File entry's ``command`` placeholder ``{argv}`` becomes
    those strings inserted at that argv position.
    """
    spec = SubprocessHandlerSpec(
        argv_template=(sys.executable, str(helper_script), "echo-argv", "{argv}"),
        result_mode=ResultMode.EXEC,
    )
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {"argv": ["one", "two", "three"]})
    # The helper echoes argv[2:] as JSON. The first arg is the mode
    # ("echo-argv"); the rest is the splatted argv.
    echoed = json.loads(result["stdout"])
    assert echoed == ["one", "two", "three"]


# ---------------------------------------------------------------------- #
# ResultMode.TEMPLATE.
# ---------------------------------------------------------------------- #


def test_template_mode_renders_result_from_params(helper_script: Path) -> None:
    """``result = "template"`` renders the configured table from params.

    Mirrors the canonical Hatch ``environment/create`` adapter: the
    CLI itself does not emit the created environment's metadata, so
    wispy synthesizes the WSP result from the request params plus
    static fields.
    """
    spec = _spec(
        helper_script,
        "silent",
        result_mode=ResultMode.TEMPLATE,
        result_template={
            "id": "{name}",
            "name": "{name}",
            "python_version": "{python_version}",
            "interpreter_path": "",
            "installed_packages": [],
            "extra": {},
        },
    )
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {"name": "scratch", "python_version": "3.12"})
    assert result == {
        "id": "scratch",
        "name": "scratch",
        "python_version": "3.12",
        "interpreter_path": "",
        "installed_packages": [],
        "extra": {},
    }


def test_template_mode_missing_param_raises(helper_script: Path) -> None:
    """A template that references a missing key raises EXECUTION_FAILED."""
    spec = _spec(
        helper_script,
        "silent",
        result_mode=ResultMode.TEMPLATE,
        result_template={"id": "{missing}"},
    )
    handler = _make_subprocess_handler(spec)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {"name": "scratch"})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "missing-template-key"


def test_template_mode_nested_substitution(helper_script: Path) -> None:
    """Templates substitute through nested dicts and lists."""
    spec = _spec(
        helper_script,
        "silent",
        result_mode=ResultMode.TEMPLATE,
        result_template={
            "outer": {"inner": "{name}"},
            "list": ["{name}", "literal", "{name}"],
        },
    )
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {"name": "x"})
    assert result == {
        "outer": {"inner": "x"},
        "list": ["x", "literal", "x"],
    }


# ---------------------------------------------------------------------- #
# Argv substitution property.
# ---------------------------------------------------------------------- #


def test_argv_substitution_rejects_nul_bytes(helper_script: Path) -> None:
    """NUL bytes in a substituted value are rejected (POSIX argv forbids NUL)."""
    spec = SubprocessHandlerSpec(
        argv_template=(sys.executable, str(helper_script), "echo-argv", "{value}"),
        result_mode=ResultMode.JSON,
    )
    handler = _make_subprocess_handler(spec)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {"value": "a\x00b"})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "unsupported-template-value"


@settings(
    deadline=None,
    max_examples=25,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(value=st.text(min_size=1, max_size=16).filter(lambda s: "\x00" not in s))
def test_argv_substitution_round_trip(helper_script: Path, value: str) -> None:
    """For any string value, ``{key}`` substitution renders it into argv.

    The helper echoes argv[2:] as JSON, so we can directly observe
    that the rendered value reached the child process unmodified.
    """
    spec = SubprocessHandlerSpec(
        argv_template=(sys.executable, str(helper_script), "echo-argv", "{value}"),
        result_mode=ResultMode.JSON,
    )
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {"value": value})
    assert result == [value]


def test_argv_substitution_coerces_integers(helper_script: Path) -> None:
    """Integer params are coerced to their decimal string form."""
    spec = SubprocessHandlerSpec(
        argv_template=(sys.executable, str(helper_script), "echo-argv", "{count}"),
        result_mode=ResultMode.JSON,
    )
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {"count": 42})
    assert result == ["42"]


def test_argv_substitution_rejects_booleans(helper_script: Path) -> None:
    """Boolean params are rejected: ``"True"`` is rarely what callers want."""
    spec = SubprocessHandlerSpec(
        argv_template=(sys.executable, str(helper_script), "echo-argv", "{flag}"),
        result_mode=ResultMode.JSON,
    )
    handler = _make_subprocess_handler(spec)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {"flag": True})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "unsupported-template-value"


def test_argv_substitution_missing_key_raises(helper_script: Path) -> None:
    """Referencing a missing key raises EXECUTION_FAILED."""
    spec = SubprocessHandlerSpec(
        argv_template=(sys.executable, str(helper_script), "echo-argv", "{missing}"),
        result_mode=ResultMode.JSON,
    )
    handler = _make_subprocess_handler(spec)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {"present": "x"})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "missing-template-key"


def test_argv_substitution_embedded(helper_script: Path) -> None:
    """``"prefix-{name}-suffix"`` substitutes inside a single argv element."""
    spec = SubprocessHandlerSpec(
        argv_template=(sys.executable, str(helper_script), "echo-argv", "prefix-{name}-suffix"),
        result_mode=ResultMode.JSON,
    )
    handler = _make_subprocess_handler(spec)
    result = _run(handler, {"name": "x"})
    assert result == ["prefix-x-suffix"]


# ---------------------------------------------------------------------- #
# Failure mapping.
# ---------------------------------------------------------------------- #


def test_timeout_raises_wsp_error(
    helper_script: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout maps to WspError(EXECUTION_FAILED, reason='timeout')."""
    monkeypatch.setattr(wispy.config, "_SUBPROCESS_TIMEOUT", 0.5)
    spec = _spec(helper_script, "sleep", result_mode=ResultMode.JSON)
    handler = _make_subprocess_handler(spec)
    with pytest.raises(WspError) as excinfo:
        _run(handler, {})
    assert excinfo.value.code == int(WspErrorCode.EXECUTION_FAILED)
    assert isinstance(excinfo.value.data, dict)
    assert excinfo.value.data.get("reason") == "timeout"


def test_followup_request_succeeds(helper_script: Path) -> None:
    """A failure does not poison subsequent requests against a new spec."""
    fail_spec = _spec(helper_script, "fail", result_mode=ResultMode.JSON)
    fail_handler = _make_subprocess_handler(fail_spec)
    with pytest.raises(WspError):
        _run(fail_handler, {})

    success_spec = _spec(helper_script, "echo-json", result_mode=ResultMode.JSON)
    success_handler = _make_subprocess_handler(success_spec)
    result = _run(success_handler, {})
    assert result["ok"] is True


def test_subprocess_not_alive_after_timeout(helper_script: Path) -> None:
    """After a timeout failure, a follow-up call completes promptly.

    The factory must ``proc.kill()`` the timed-out child before
    returning. We can't observe the PID directly, but a leaked child
    would either delay the follow-up call or starve the test runner.
    """
    original_timeout = wispy.config._SUBPROCESS_TIMEOUT  # noqa: SLF001 - testing private timeout knob
    wispy.config._SUBPROCESS_TIMEOUT = 0.5  # noqa: SLF001 - testing private timeout knob
    try:
        sleep_spec = _spec(helper_script, "sleep", result_mode=ResultMode.JSON)
        sleep_handler = _make_subprocess_handler(sleep_spec)
        with pytest.raises(WspError):
            _run(sleep_handler, {})
    finally:
        wispy.config._SUBPROCESS_TIMEOUT = original_timeout  # noqa: SLF001 - testing private timeout knob

    start = time.monotonic()
    success_spec = _spec(helper_script, "echo-json", result_mode=ResultMode.JSON)
    success_handler = _make_subprocess_handler(success_spec)
    result = _run(success_handler, {})
    elapsed = time.monotonic() - start
    assert result["ok"] is True
    assert elapsed < 10.0, f"follow-up handler took {elapsed:.2f}s; the timed-out child may have leaked"

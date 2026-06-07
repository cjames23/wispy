"""Integration tests for the stdio transport and EOF-drain semantics.

These tests exercise the full ``run_stdio`` pipeline -- transport, framing,
codec, dispatcher, and lifecycle FSM -- by spawning a real Python child
process configured with a tiny driver script that registers handlers and
then runs ``run_stdio(registry)``. Each test sends framed JSON-RPC
messages on the child's stdin and reads framed responses from the child's
stdout, then asserts the child's process exit status.

The two scenarios covered here exercise the EOF-drain semantics
plus the basic stdio happy path:

* :func:`test_initialize_then_eof` confirms that a single framed
  request -> framed response round-trip works and that closing stdin
  causes the process to exit with status 0 within the drain timeout.
* :func:`test_slow_handler_drains_before_exit` confirms that when stdin
  reaches EOF while a handler is still in flight, the server waits for
  the handler to complete (up to the 5 s drain budget), emits the
  response, and only then exits with status 0.

Every ``proc.wait`` is time-bounded so a stuck child fails the test
rather than hanging the suite.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from wispy.framing import DecodeError, FrameDecoder, encode_frame

# Repo root resolved from this file; the driver child needs ``src/`` on
# its PYTHONPATH so it can ``import wispy`` regardless of how it was
# launched (Hatch's editable install + an explicit PYTHONPATH belt for
# the suspenders).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"

# Empty file-descriptor lists for ``select.select``. Hoisted to module
# scope so they carry an explicit element type rather than the
# ambiguous ``list[Any]`` an inline ``[]`` would produce.
_NO_FDS: list[int] = []


# --------------------------------------------------------------------- #
# Driver scripts
# --------------------------------------------------------------------- #
#
# Each driver is a self-contained ``python -c`` snippet. The handler
# return values use ``Capabilities(...).to_jsonable()`` so the value is
# a plain ``dict`` (and therefore JSON-serializable through
# ``serialize_response``). The dispatcher will log a warning that
# ``initialize`` did not return a real ``Capabilities`` object; that
# warning is benign for these tests since we only assert on the wire
# response and the process exit status.


# Driver 1: just an ``initialize`` handler. Returns the Capabilities
# wire shape so the response can be serialized as JSON.
_DRIVER_INITIALIZE_ONLY = """
import asyncio
import sys

from wispy.endpoints import Capabilities
from wispy.registry import HandlerRegistry
from wispy.server import run_stdio

registry = HandlerRegistry()


def initialize(params):
    # Snapshot at request-time so the methods list reflects every
    # handler registered before run_stdio was called.
    return Capabilities(
        methods=tuple(registry.methods()),
        protocol_version="0.1.0",
    ).to_jsonable()


registry.register("initialize", initialize)
sys.exit(asyncio.run(run_stdio(registry)))
"""


# Driver 2: ``initialize`` plus a ``slow`` handler that sleeps for one
# second before returning. The ``slow`` method is not in WSP_METHODS, so
# the dispatcher skips param validation and the lifecycle FSM only gates
# on whether ``initialize`` has succeeded first (the test does send
# ``initialize`` first, so ``slow`` is admitted).
_DRIVER_WITH_SLOW = """
import asyncio
import sys

from wispy.endpoints import Capabilities
from wispy.registry import HandlerRegistry
from wispy.server import run_stdio

registry = HandlerRegistry()


def initialize(params):
    return Capabilities(
        methods=tuple(registry.methods()),
        protocol_version="0.1.0",
    ).to_jsonable()


async def slow(params):
    await asyncio.sleep(1.0)
    return {"slept": True}


registry.register("initialize", initialize)
registry.register("slow", slow)
sys.exit(asyncio.run(run_stdio(registry)))
"""


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _spawn_driver(driver_script: str) -> subprocess.Popen[bytes]:
    """Spawn the WSP driver child with ``PYTHONPATH=src/`` prepended.

    Setting ``PYTHONPATH`` explicitly keeps the test robust regardless
    of whether the package was installed (Hatch's hatch-test env does
    install in editable mode, but belt-and-suspenders is cheap).
    """
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_SRC_DIR) + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, "-c", driver_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _send(proc: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    """Frame ``message`` as JSON and write it to the child's stdin."""
    assert proc.stdin is not None
    payload = json.dumps(message).encode("utf-8")
    proc.stdin.write(encode_frame(payload))
    proc.stdin.flush()


def _read_one_frame(
    proc: subprocess.Popen[bytes],
    decoder: FrameDecoder,
    pending: list[bytes | DecodeError],
    timeout: float = 5.0,
) -> bytes:
    """Read exactly one decoded frame payload from the child's stdout.

    Returns the decoded payload bytes. Raises :class:`TimeoutError` if
    no complete frame becomes available within ``timeout`` seconds, or
    :class:`AssertionError` if the decoder yields a :class:`DecodeError`
    (these tests expect well-formed traffic from the server).
    """
    assert proc.stdout is not None
    if pending:
        item = pending.pop(0)
        if isinstance(item, DecodeError):
            msg = f"unexpected decode error: {item}"
            raise AssertionError(msg)
        return item

    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = f"timed out waiting for response after {timeout}s"
            raise TimeoutError(msg)
        rlist, _, _ = select.select([fd], _NO_FDS, _NO_FDS, remaining)
        if not rlist:
            msg = f"timed out waiting for response after {timeout}s"
            raise TimeoutError(msg)
        chunk = os.read(fd, 65536)
        if not chunk:
            msg = "child closed stdout before producing a frame"
            raise EOFError(msg)
        pending.extend(decoder.feed(chunk))
        if pending:
            item = pending.pop(0)
            if isinstance(item, DecodeError):
                msg = f"unexpected decode error: {item}"
                raise AssertionError(msg)
            return item


def _read_all_frames(
    proc: subprocess.Popen[bytes],
    decoder: FrameDecoder,
    pending: list[bytes | DecodeError],
    timeout: float = 5.0,
) -> list[bytes]:
    """Read every framed payload until the child closes stdout.

    Useful after stdin has been closed and the test wants to consume
    the trailing responses without knowing exactly how many to expect.
    Bounded by ``timeout`` so a stuck child fails the test.
    """
    assert proc.stdout is not None
    out: list[bytes] = []
    # First, drain anything already buffered in ``pending`` by previous
    # reads.
    while pending:
        item = pending.pop(0)
        if isinstance(item, DecodeError):
            msg = f"unexpected decode error: {item}"
            raise AssertionError(msg)  # noqa: TRY004 - AssertionError is the test failure conventional
        out.append(item)

    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = f"timed out reading frames after {timeout}s"
            raise TimeoutError(msg)
        rlist, _, _ = select.select([fd], _NO_FDS, _NO_FDS, remaining)
        if not rlist:
            msg = f"timed out reading frames after {timeout}s"
            raise TimeoutError(msg)
        chunk = os.read(fd, 65536)
        if not chunk:
            # EOF on stdout: child has closed its write end. Drain any
            # final items the decoder still has buffered, then return.
            for item in decoder.feed(b""):
                if isinstance(item, DecodeError):
                    msg = f"unexpected decode error: {item}"
                    raise AssertionError(msg)  # noqa: TRY004 - AssertionError is the test failure conventional
                out.append(item)
            return out
        for item in decoder.feed(chunk):
            if isinstance(item, DecodeError):
                msg = f"unexpected decode error: {item}"
                raise AssertionError(msg)  # noqa: TRY004 - AssertionError is the test failure conventional
            out.append(item)


def _close_stdin(proc: subprocess.Popen[bytes]) -> None:
    """Close the child's stdin to signal EOF."""
    assert proc.stdin is not None
    # Child may have already exited; that's fine.
    with contextlib.suppress(BrokenPipeError):
        proc.stdin.close()


def _wait_with_timeout(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> int:
    """Wait for ``proc`` to exit within ``timeout`` seconds, or fail."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait(timeout=2.0)
        msg = f"child did not exit within {timeout}s"
        raise AssertionError(msg) from exc


def _drain_stderr(proc: subprocess.Popen[bytes]) -> str:
    """Best-effort drain of the child's stderr for diagnostic context."""
    assert proc.stderr is not None
    try:
        return proc.stderr.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort drain may fail in many ways
        return ""


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #


def test_initialize_then_eof() -> None:
    """A single ``initialize`` then EOF round-trips and exits 0.

    Steps:

    1. Spawn the child with the initialize-only driver.
    2. Send a framed ``initialize`` request on stdin.
    3. Close stdin (signals EOF).
    4. Read one framed response from stdout and decode it.
    5. Assert the response is well-formed and the child exits 0
       within 5 seconds.
    """
    proc = _spawn_driver(_DRIVER_INITIALIZE_ONLY)
    try:
        decoder = FrameDecoder()
        pending: list[bytes | DecodeError] = []

        # Send a single initialize request.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "client_name": "stdio-test",
                    "client_protocol_version": "0.1.0",
                },
                "id": 1,
            },
        )

        # Close stdin to trigger the EOF drain. The runtime should
        # complete the in-flight initialize handler, emit the response,
        # and then exit with status 0.
        _close_stdin(proc)

        # Drain every frame until the child closes stdout. We expect
        # exactly one frame: the initialize response.
        frames = _read_all_frames(proc, decoder, pending, timeout=5.0)
        assert len(frames) == 1, (
            f"expected exactly one response frame, got {len(frames)}: "
            f"{[f.decode('utf-8', errors='replace') for f in frames]}"
        )

        resp = json.loads(frames[0])
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert "result" in resp, resp
        result = resp["result"]
        # The handler returned ``Capabilities(...).to_jsonable()``, so
        # the wire shape carries ``methods`` (a list of strings) and a
        # ``protocol_version`` semver string.
        assert isinstance(result, dict), result
        assert "initialize" in result["methods"]
        assert result["protocol_version"] == "0.1.0"

        status = _wait_with_timeout(proc, timeout=5.0)
        stderr = _drain_stderr(proc)
        assert status == 0, f"expected exit 0 after EOF drain, got {status}; stderr={stderr!r}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


def test_slow_handler_drains_before_exit() -> None:
    """A slow in-flight handler is awaited before EOF-driven exit.

    Steps:

    1. Spawn the child with a driver that registers ``initialize`` and
       a one-second async ``slow`` handler.
    2. Send ``initialize`` and read its response (so the lifecycle FSM
       is INITIALIZED and the next request is admitted).
    3. Send ``slow``. Do NOT wait for its response yet.
    4. Close stdin so the runtime sees EOF while ``slow`` is still
       running.
    5. Read every remaining frame from stdout. Expect both responses,
       in order: the initialize response was already read in step 2,
       so this final read is just the ``slow`` response.
    6. Assert the child exits 0 within 5 seconds (1 s sleep + drain
       headroom comfortably under the runtime's 5 s drain budget).
    """
    proc = _spawn_driver(_DRIVER_WITH_SLOW)
    try:
        decoder = FrameDecoder()
        pending: list[bytes | DecodeError] = []

        # Step 1: initialize and consume its response. We must do this
        # before sending ``slow`` because the lifecycle FSM rejects all
        # non-``initialize`` methods from the UNINITIALIZED state with
        # ``-32002``.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "client_name": "stdio-test",
                    "client_protocol_version": "0.1.0",
                },
                "id": 1,
            },
        )
        init_resp = json.loads(_read_one_frame(proc, decoder, pending))
        assert init_resp["id"] == 1
        assert "result" in init_resp, init_resp

        # Capture the timestamp before queueing the slow request so we
        # can confirm that the ``slow`` response was emitted only after
        # the handler's one-second sleep elapsed (rather than being
        # short-circuited).
        start = time.monotonic()

        # Step 2: send the slow request and immediately close stdin.
        # The runtime will see EOF while ``slow`` is still running and
        # must drain it before exiting with status 0.
        # Note: ``params`` is omitted entirely. JSON-RPC 2.0 (and our
        # codec) accepts an absent ``params`` field to mean "no
        # parameters"; an explicit ``"params": null`` is rejected as
        # invalid request because ``null`` is neither an object nor an
        # array.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "slow",
                "id": 2,
            },
        )
        _close_stdin(proc)

        # Step 3: read the remaining frames. We expect exactly one --
        # the ``slow`` response -- because the initialize response was
        # already consumed above.
        remaining = _read_all_frames(proc, decoder, pending, timeout=5.0)
        assert len(remaining) == 1, (
            f"expected exactly one trailing response frame, got "
            f"{len(remaining)}: "
            f"{[f.decode('utf-8', errors='replace') for f in remaining]}"
        )
        slow_resp = json.loads(remaining[0])
        assert slow_resp["jsonrpc"] == "2.0"
        assert slow_resp["id"] == 2
        assert "result" in slow_resp, slow_resp
        assert slow_resp["result"] == {"slept": True}

        elapsed = time.monotonic() - start
        # The handler sleeps one second; allow generous slack for slow
        # CI machines and small clock granularities. The point is that
        # the response was NOT emitted instantly (which would indicate
        # the handler was cancelled before completing).
        assert elapsed >= 0.9, (
            f"slow response emitted in {elapsed:.3f}s, which is too fast -- handler appears to have been cancelled"
        )

        status = _wait_with_timeout(proc, timeout=5.0)
        stderr = _drain_stderr(proc)
        assert status == 0, f"expected exit 0 after EOF drain, got {status}; stderr={stderr!r}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)

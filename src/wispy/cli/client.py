"""In-process JSON-RPC client over a child WSP_Server process.

The :class:`WspClient` is the bridge between the user-facing ``wsp``
command and the WSP_Server it targets. It owns the child subprocess,
performs the per-invocation JSON-RPC lifecycle (``initialize`` ->
requested method -> ``shutdown`` -> ``exit`` notification), and maps
the result onto a CLI-friendly exit code per the design's exit-code
table.

The class is intended to be used as an async context manager:

    async with WspClient(["mytool", "wsp-serve"]) as client:
        result = await client.call("environment/list", None)
        sys.exit(result.exit_code)

On context-manager exit, the client guarantees a best-effort
``shutdown`` + ``exit`` (if not already sent during ``call``), waits
up to 10 seconds for the child to terminate, and falls back to
``proc.kill()`` if the child does not exit on its own.

Timeouts:

* Launch:    30 seconds (``asyncio.create_subprocess_exec``)
* Per-call:  30 seconds per JSON-RPC response read

Exceeding either prints a human-readable error to stderr and surfaces
the generic-error exit code (1).

Exit-code mapping:

* Successful result                       -> ``SUCCESS`` (0)
* JSON-RPC error with ``code == -32601``  -> ``USAGE_OR_UNSUPPORTED`` (2)
* Any other JSON-RPC error                -> ``GENERIC_ERROR`` (1)

The wire protocol uses Content-Length framing (LSP-style) provided by
:mod:`wispy.framing`; JSON-RPC payloads are constructed manually here
because we only ever build a fixed handful of request shapes and that
is simpler than going through :mod:`wispy.protocol`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from wispy.cli._exit_codes import ExitCode
from wispy.errors import JsonRpcErrorCode
from wispy.framing import DecodeError, FrameDecoder, encode_frame

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType
    from typing import Self

__all__ = ["ClientResult", "WspClient", "wsp_client"]


# Time budgets for graceful termination and per-response reads. These
# are constants rather than constructor arguments because the design
# pins them.
_LAUNCH_TIMEOUT_S: float = 30.0
_RESPONSE_TIMEOUT_S: float = 30.0
_TERMINATE_TIMEOUT_S: float = 10.0
# Slack budget after ``proc.kill()`` to reap the child. Independent of
# the user-visible 10 s graceful-termination budget.
_KILL_REAP_TIMEOUT_S: float = 2.0
# Read chunk size for stdout. Large enough to swallow most responses
# in a single read; small enough to bound buffering on misbehaving
# servers.
_READ_CHUNK_SIZE: int = 65536


class ClientResult:
    """Outcome of a single WSP_CLI invocation.

    Attributes:
        exit_code: The CLI exit code corresponding to this outcome,
            chosen per the design's exit-code table.
        value: The handler's ``result`` value on success, otherwise
            ``None``.
        error: The JSON-RPC error object on failure, otherwise
            ``None``. Always ``None`` when ``exit_code`` is
            :data:`ExitCode.SUCCESS`.
    """

    __slots__ = ("error", "exit_code", "value")

    def __init__(
        self,
        exit_code: int,
        value: Any | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.value = value
        self.error = error


class WspClient:
    """Async context manager wrapping a launched WSP_Server child.

    Usage:

        async with WspClient(argv) as client:
            result = await client.call(method, params)

    The context manager owns the child process: ``__aenter__`` spawns
    it (with a 30 s launch timeout), and ``__aexit__`` shuts it down
    cleanly (best-effort ``shutdown`` + ``exit``, then a 10 s wait,
    then ``proc.kill()``).

    A single :meth:`call` performs the per-invocation lifecycle:
    ``initialize`` -> requested method. ``shutdown`` and ``exit`` are
    deferred to context-manager exit so that callers do not need to
    remember to send them manually.

    The client is single-use: callers should issue exactly one
    :meth:`call` per :class:`WspClient` instance, mirroring the
    "one ``WSP_Server`` child per CLI invocation" design.
    """

    __slots__ = (
        "_decoder",
        "_exit_sent",
        "_next_id",
        "_shutdown_sent",
        "argv",
        "proc",
    )

    def __init__(self, argv: list[str]) -> None:
        # Defensive copy so callers cannot mutate our argv after
        # construction.
        self.argv: list[str] = list(argv)
        self.proc: asyncio.subprocess.Process | None = None
        self._decoder: FrameDecoder = FrameDecoder()
        self._next_id: int = 1
        self._shutdown_sent: bool = False
        self._exit_sent: bool = False

    # ---------------------------------------------------------------- #
    # Context-manager protocol.
    # ---------------------------------------------------------------- #

    async def __aenter__(self) -> Self:
        try:
            self.proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self.argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=_LAUNCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            sys.stderr.write(f"wsp: failed to launch child process within {_LAUNCH_TIMEOUT_S:g}s: {self.argv!r}\n")
            raise
        except FileNotFoundError as exc:
            sys.stderr.write(f"wsp: failed to launch child process: {exc}\n")
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            # Best-effort graceful shutdown: send shutdown + exit only
            # if they have not already been sent. Both sends are
            # tolerant of broken pipes (the child may already be dead).
            if not self._shutdown_sent:
                # Includes BrokenPipeError, ConnectionResetError,
                # asyncio.TimeoutError, and EOFError. We are on
                # the teardown path; swallow to keep teardown
                # robust and continue to the kill fallback.
                with contextlib.suppress(Exception):
                    await self._send_request("shutdown", None)
            if not self._exit_sent:
                with contextlib.suppress(Exception):
                    await self._send_notification("exit")

            # Close stdin so the child notices EOF if it hasn't acted
            # on the exit notification yet.
            stdin = proc.stdin
            if stdin is not None:
                with contextlib.suppress(Exception):
                    if not stdin.is_closing():
                        stdin.close()

            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Fall back to SIGKILL if the child
                # does not terminate within the grace period.
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                # Reaping after SIGKILL really should not block.
                # If it does, leave the zombie to the OS.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=_KILL_REAP_TIMEOUT_S)
        finally:
            self.proc = None

    # ---------------------------------------------------------------- #
    # Public API.
    # ---------------------------------------------------------------- #

    async def call(self, method: str, params: Any) -> ClientResult:
        """Run the per-invocation lifecycle and return the result.

        Sends ``initialize`` first, then the requested ``method``.
        ``shutdown`` and ``exit`` are deferred to the context-manager
        exit.

        Returns a :class:`ClientResult` whose ``exit_code`` matches
        the design's exit-code table:

        * Success result                       -> 0
        * Error code ``-32601``                -> 2 ("method unsupported")
        * Any other JSON-RPC error             -> 1
        * Response timeout                     -> 1

        Stderr writes accompany every non-success outcome so the user
        sees a human-readable explanation.
        """
        # 1. initialize
        try:
            init_resp = await self._send_request(
                "initialize",
                {
                    "client_name": "wsp-cli",
                    "client_protocol_version": "0.1.0",
                },
            )
        except asyncio.TimeoutError:
            sys.stderr.write(f"wsp: response timeout after {_RESPONSE_TIMEOUT_S:g}s for initialize\n")
            return ClientResult(exit_code=ExitCode.GENERIC_ERROR)
        except EOFError as exc:
            sys.stderr.write(f"wsp: {exc}\n")
            return ClientResult(exit_code=ExitCode.GENERIC_ERROR)

        if "error" in init_resp:
            return _result_from_error_response(init_resp)

        # 2. requested method
        try:
            method_resp = await self._send_request(method, params)
        except asyncio.TimeoutError:
            sys.stderr.write(f"wsp: response timeout after {_RESPONSE_TIMEOUT_S:g}s for {method}\n")
            return ClientResult(exit_code=ExitCode.GENERIC_ERROR)
        except EOFError as exc:
            sys.stderr.write(f"wsp: {exc}\n")
            return ClientResult(exit_code=ExitCode.GENERIC_ERROR)

        if "error" in method_resp:
            return _result_from_error_response(method_resp)

        return ClientResult(
            exit_code=ExitCode.SUCCESS,
            value=method_resp.get("result"),
        )

    # ---------------------------------------------------------------- #
    # Internal helpers.
    # ---------------------------------------------------------------- #

    async def _send_request(self, method: str, params: Any) -> dict[str, Any]:
        """Send a JSON-RPC request and return the matching response.

        Raises :class:`asyncio.TimeoutError` if no response with the
        expected id arrives within :data:`_RESPONSE_TIMEOUT_S`, and
        :class:`EOFError` if the child closes stdout before
        responding.
        """
        rid = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": rid,
        }
        if params is not None:
            msg["params"] = params
        await self._write_frame(_encode_json(msg))
        if method == "shutdown":
            self._shutdown_sent = True
        return await self._read_response_for_id(rid)

    async def _send_notification(self, method: str) -> None:
        """Send a JSON-RPC notification (no id, no response)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        await self._write_frame(_encode_json(msg))
        if method == "exit":
            self._exit_sent = True

    async def _write_frame(self, payload: bytes) -> None:
        """Encode ``payload`` as a Content-Length frame and write it."""
        proc = self.proc
        if proc is None:
            msg = "WspClient not entered"
            raise RuntimeError(msg)
        stdin = proc.stdin
        if stdin is None:
            msg = "child stdin is not piped"
            raise RuntimeError(msg)
        stdin.write(encode_frame(payload))
        await stdin.drain()

    async def _read_response_for_id(self, expected_id: int) -> dict[str, Any]:
        """Read frames until we get a response with ``expected_id``.

        Server-initiated notifications (or responses to other ids) are
        silently skipped; :class:`DecodeError` records from the
        framing layer are skipped as well so a single corrupted frame
        does not derail the call.
        """
        proc = self.proc
        if proc is None:
            msg = "WspClient not entered"
            raise RuntimeError(msg)
        stdout = proc.stdout
        if stdout is None:
            msg = "child stdout is not piped"
            raise RuntimeError(msg)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RESPONSE_TIMEOUT_S

        while True:
            # First, drain anything the framing decoder already has
            # buffered. We must check this on every iteration because
            # a single read can yield multiple frames.
            for item in self._decoder.feed(b""):
                if isinstance(item, DecodeError):
                    sys.stderr.write(f"wsp: discarded {item.discarded} bytes from server stdout: {item.reason}\n")
                    continue
                resp = _parse_json_response(item)
                if resp.get("id") == expected_id:
                    return resp
                # Otherwise: response to another id or a server
                # notification; skip it.

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError

            chunk = await asyncio.wait_for(stdout.read(_READ_CHUNK_SIZE), timeout=remaining)
            if not chunk:
                msg = "server closed stdout before responding"
                raise EOFError(msg)
            for item in self._decoder.feed(chunk):
                if isinstance(item, DecodeError):
                    sys.stderr.write(f"wsp: discarded {item.discarded} bytes from server stdout: {item.reason}\n")
                    continue
                resp = _parse_json_response(item)
                if resp.get("id") == expected_id:
                    return resp


def _encode_json(msg: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message dict as compact UTF-8 bytes."""
    # ``separators`` produces compact output (no whitespace), matching
    # the wire form the server emits.
    return json.dumps(msg, separators=(",", ":")).encode("utf-8")


def _parse_json_response(payload: bytes) -> dict[str, Any]:
    """Parse a JSON-RPC response frame into a dict.

    Falls back to an empty dict on malformed input so the caller can
    skip the frame instead of crashing the CLI on a misbehaving
    server.
    """
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def _result_from_error_response(resp: dict[str, Any]) -> ClientResult:
    """Map a JSON-RPC error response to a :class:`ClientResult`.

    Mapping:

    * ``code == -32601``           -> ``ExitCode.USAGE_OR_UNSUPPORTED``
    * any other code                -> ``ExitCode.GENERIC_ERROR``

    Either way, a human-readable rendering of the error is written to
    stderr.
    """
    error = resp.get("error")
    if not isinstance(error, dict):
        # Defensive: if the server claimed it was an error but the
        # ``error`` member is not a dict, treat the whole response as
        # a generic error and surface what we have.
        sys.stderr.write(f"wsp: malformed error response from server: {resp!r}\n")
        return ClientResult(exit_code=ExitCode.GENERIC_ERROR)

    code = error.get("code")
    if code == int(JsonRpcErrorCode.METHOD_NOT_FOUND):
        message = error.get("message", "method not found")
        sys.stderr.write(f"wsp: method unsupported: {message}\n")
        return ClientResult(exit_code=ExitCode.USAGE_OR_UNSUPPORTED, error=error)

    sys.stderr.write(json.dumps(error, separators=(",", ":")) + "\n")
    return ClientResult(exit_code=ExitCode.GENERIC_ERROR, error=error)


@asynccontextmanager
async def wsp_client(argv: list[str]) -> AsyncIterator[WspClient]:
    """Convenience async context manager wrapping :class:`WspClient`.

    Equivalent to ``async with WspClient(argv) as client: yield client``,
    provided as a function-style alternative for callers that prefer
    that style.
    """
    client = WspClient(argv)
    async with client:
        yield client

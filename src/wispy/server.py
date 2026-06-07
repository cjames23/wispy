"""WSP server runtime: ``run_stdio`` and friends.

The runtime ties together the pure :mod:`wispy.protocol`,
:mod:`wispy.dispatcher`, :mod:`wispy.lifecycle`, and
:mod:`wispy.registry` modules with the asyncio
:class:`~wispy.transport.StdioTransport` to produce a working WSP
server. It is intentionally thin: every interesting decision lives in
one of the underlying modules. The runtime's responsibilities are:

* Construct a :class:`~wispy.lifecycle.LifecycleManager` for the run.
* Iterate the transport's framed messages, parsing each one and
  spawning a per-message asyncio task that calls
  :func:`~wispy.dispatcher.dispatch`.
* Surface :class:`~wispy.framing.DecodeError` records to stderr and
  continue; no JSON-RPC response is emitted because
  no id is recoverable from a malformed frame.
* Drain in-flight handler tasks on stdin EOF, cancelling any that take
  longer than ``drain_timeout``, and return exit
  status ``0``.
* Surface the :class:`~wispy.dispatcher.ExitDispatch` sentinel produced
  for an ``exit`` notification by flushing any attached responses,
  draining outstanding tasks, and returning the carried status.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from wispy.dispatcher import ExitDispatch, dispatch
from wispy.framing import DecodeError
from wispy.lifecycle import LifecycleManager
from wispy.protocol import (
    JsonRpcResponse,
    parse_message,
    serialize_response,
)
from wispy.transport import StdioTransport

if TYPE_CHECKING:
    from wispy.registry import HandlerRegistry

__all__ = ["run_stdio"]


async def run_stdio(
    registry: HandlerRegistry,
    *,
    transport: StdioTransport | None = None,
    drain_timeout: float = 5.0,
) -> int:
    """Run the WSP server over a stdio transport.

    The function returns when stdin reaches EOF (and any in-flight
    handler tasks have either completed or been cancelled after
    ``drain_timeout`` seconds), or when a dispatched ``exit``
    notification produces an :class:`ExitDispatch` sentinel and the
    in-flight tasks have drained.

    Args:
        registry: The :class:`HandlerRegistry` shared by every dispatched
            message. The runtime does not register any handlers itself;
            callers are responsible for binding ``initialize``,
            ``shutdown``, and any custom WSP methods before invoking
            ``run_stdio``.
        transport: An optional pre-built :class:`StdioTransport`. When
            ``None`` (the common case), a default transport bound to
            the real ``sys.stdin``/``sys.stdout``/``sys.stderr`` is
            constructed. Tests inject in-memory pipes here.
        drain_timeout: How long to wait, in seconds, for in-flight
            handler tasks to finish after stdin EOF (or after an
            ``exit`` notification) before cancelling them. Defaults to
            five seconds, which comfortably covers the typical
            sub-second handler while still bounding teardown.

    Returns:
        The desired process exit status: ``0`` for the EOF-drain path
        and for ``shutdown`` -> ``exit``, and
        whatever the lifecycle FSM chose otherwise (typically ``1``
        for ``exit`` without a preceding ``shutdown``.
    """
    if transport is None:
        transport = StdioTransport()
    lifecycle = LifecycleManager()
    tasks: set[asyncio.Task[None]] = set()
    # Set by ``handle_message`` once an ``exit`` notification has been
    # processed. The read loop polls it after each spawn so it can
    # stop reading promptly even if the client keeps stdin open.
    exit_status: int | None = None

    async def handle_message(raw: bytes) -> None:
        """Parse, dispatch, and emit responses for one framed message.

        ``handle_message`` is the per-message coroutine spawned as a
        task by the read loop. It encapsulates every step that depends
        on the parsed request -- parsing, dispatch, response
        serialisation, and exit-status capture -- so the read loop
        itself only has to worry about transport-level events.
        """
        nonlocal exit_status
        try:
            parsed = parse_message(raw)
            result = await dispatch(
                parsed,
                registry=registry,
                lifecycle=lifecycle,
                log=transport.log,
            )
        except Exception as exc:  # noqa: BLE001 - dispatcher MUST NOT raise; log and continue
            # The dispatcher contract guarantees no exceptions, but a
            # truly unexpected failure (e.g. a programming error in the
            # registry) should still be logged rather than propagated.
            transport.log(f"unexpected error in dispatch: {exc!r}")
            return

        if isinstance(result, ExitDispatch):
            # Flush any responses produced earlier in the same batch
            # before signalling exit. The transport's write lock keeps
            # this serialised with respect to other handler tasks
            # running in parallel.
            if result.response is not None:
                await _emit(result.response, transport)
            # Setting ``exit_status`` last is what the read loop polls
            # after each spawn; doing it after the write avoids a race
            # in which the read loop tears down before the exit
            # response is on the wire.
            exit_status = result.status
            return

        if result is None:
            # Notification (or batch consisting only of notifications):
            # nothing to emit.
            return

        await _emit(result, transport)

    try:
        async for item in transport.messages():
            if isinstance(item, DecodeError):
                # A malformed frame is reported on
                # stderr and the read loop continues. No JSON-RPC
                # response is produced because the request id, if any,
                # is unrecoverable from a malformed frame.
                transport.log(f"frame decode error: {item.reason} (discarded {item.discarded} bytes)")
                continue

            task = asyncio.create_task(handle_message(item))
            tasks.add(task)
            # Self-prune the set so completed tasks can be garbage
            # collected without us scanning the set on every iteration.
            task.add_done_callback(tasks.discard)

            if exit_status is not None:
                # An earlier ``exit`` task already set the status; stop
                # reading new messages and proceed to drain. The task
                # we just spawned is still in ``tasks`` and will be
                # awaited (or cancelled) by the drain below.
                break

    except Exception as exc:  # noqa: BLE001 - transport teardown must not propagate
        transport.log(f"unexpected error in transport: {exc!r}")

    # Drain in-flight handler tasks. ``asyncio.wait`` returns
    # ``(done, pending)`` after at most ``drain_timeout`` seconds; any
    # tasks still running are cancelled and gathered so their
    # cancellation propagates before the function returns.
    if tasks:
        # Snapshot the live tasks; ``tasks`` may be mutated by the
        # done-callback during the wait.
        in_flight = list(tasks)
        _, pending = await asyncio.wait(in_flight, timeout=drain_timeout)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # Flush any buffered stdout bytes one last time so the final
    # response is on the wire before we return.
    await transport.drain()

    return exit_status if exit_status is not None else 0


async def _emit(
    response: JsonRpcResponse | list[JsonRpcResponse],
    transport: StdioTransport,
) -> None:
    """Serialise ``response`` and write it through ``transport``.

    An empty batch response (``[]``) is silently dropped: per JSON-RPC
    2.0 a batch of only notifications produces no response object on
    the wire. Non-empty batches and single responses
    are framed and written via :meth:`StdioTransport.write`, which
    flushes stdout after each call so clients see responses
    immediately.
    """
    if isinstance(response, list) and not response:
        return
    payload = serialize_response(response)
    await transport.write(payload)

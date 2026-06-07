"""Asyncio stdio transport for the WSP server.

Implements :class:`StdioTransport`, the default transport that reads
Content-Length framed JSON-RPC messages from ``stdin`` and writes them to
``stdout``. All diagnostic logging is routed to ``stderr`` so the stdout
channel remains a pure framing stream.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, BinaryIO, TextIO

from wispy.framing import DecodeError, FrameDecoder, encode_frame

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["StdioTransport"]


class StdioTransport:
    """Async stdio transport for Content-Length framed messages.

    The transport owns a :class:`FrameDecoder` and surfaces decoded payloads
    (or :class:`DecodeError` records for malformed regions) via the
    :meth:`messages` async iterator. Outbound writes go through
    :meth:`write`, which frames the payload and flushes ``stdout`` after each
    response so the client sees responses immediately.

    Constructor arguments allow injection of arbitrary binary file-like
    objects in place of the real ``sys.stdin``/``sys.stdout``/``sys.stderr``
    streams. This is the seam that integration tests use to drive the
    transport over an in-memory pipe.
    """

    def __init__(
        self,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._stdin = stdin if stdin is not None else sys.stdin.buffer
        self._stdout = stdout if stdout is not None else sys.stdout.buffer
        self._stderr = stderr if stderr is not None else sys.stderr
        self._reader: asyncio.StreamReader | None = None
        self._reader_attached: bool = False
        self._read_chunk_size = 65536
        self._decoder = FrameDecoder()
        self._stdout_lock = asyncio.Lock()

    async def _ensure_reader(self) -> asyncio.StreamReader:
        if self._reader is not None:
            return self._reader
        loop = asyncio.get_running_loop()
        # 16 MiB internal buffer accommodates large workflow payloads while
        # still bounding memory if a peer floods the pipe.
        reader = asyncio.StreamReader(limit=2**24)
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, self._stdin)
        self._reader = reader
        self._reader_attached = True
        return reader

    async def messages(self) -> AsyncIterator[bytes | DecodeError]:
        """Yield decoded payloads (or :class:`DecodeError`) until stdin EOF.

        The iterator terminates when ``stdin`` reaches EOF, after surfacing
        any final items still buffered inside the decoder. The server uses
        this termination as its signal to begin draining in-flight handler
        tasks.
        """
        reader = await self._ensure_reader()
        while True:
            chunk = await reader.read(self._read_chunk_size)
            if not chunk:
                # EOF: surface any final items still in the decoder's
                # buffer (e.g. trailing garbage), then stop.
                for item in self._decoder.feed(b""):
                    yield item
                return
            for item in self._decoder.feed(chunk):
                yield item

    async def write(self, payload: bytes) -> None:
        """Frame ``payload`` and write it to stdout, flushing after each call.

        Concurrent dispatcher tasks may call :meth:`write` simultaneously, so
        the implementation serialises writes behind a lock to guarantee
        frame bytes are not interleaved mid-frame.
        """
        framed = encode_frame(payload)
        async with self._stdout_lock:
            self._stdout.write(framed)
            self._stdout.flush()

    async def drain(self) -> None:
        """Flush any buffered stdout bytes."""
        async with self._stdout_lock:
            self._stdout.flush()

    def log(self, msg: str) -> None:
        """Write a log line to stderr only.

        A trailing newline is appended if not already present so individual
        log lines stay separated when the host captures stderr line-by-line.
        """
        self._stderr.write(msg)
        if not msg.endswith("\n"):
            self._stderr.write("\n")
        self._stderr.flush()

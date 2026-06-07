# Transport & framing

`wispy` ships a single transport: stdio with LSP-style `Content-Length`
framing. The transport layer is small and decoupled from everything above
it, so the dispatcher and lifecycle FSM are reusable if a different
transport is added later.

## Frame format

Every JSON-RPC payload is preceded by a single header line and a blank
line:

```
Content-Length: 53\r\n
\r\n
{"jsonrpc":"2.0","method":"environment/list","id":1}
```

- The header value is the byte length of the payload.
- Only the `Content-Length` header is interpreted; other headers (e.g.
  `Content-Type`) are accepted and ignored.
- The payload must be valid UTF-8 JSON.

The wire format is intentionally identical to the LSP base protocol so that
existing JSON-RPC client libraries that already speak it (most editors,
language clients) work without modification.

## Streaming and chunking

`wispy.framing.FrameDecoder` is a streaming decoder. You can feed it any
chunk size — a byte at a time, a frame at a time, or a thousand frames at
once — and it will yield complete payloads as they become available. The
property test `test_framing.py` exercises arbitrary chunkings via Hypothesis
to confirm the decoder is invariant to how the input is split.

## Recovery

Two failure modes are recoverable:

- A malformed header block: the decoder yields a `DecodeError` describing
  the discarded byte count and reason, then advances past the header
  terminator (`\r\n\r\n`) to the next plausible frame boundary.
- A payload that isn't valid UTF-8: the decoder yields a `DecodeError` for
  the offending payload and continues with the next frame.

The runtime logs `DecodeError`s to stderr and continues. There is no JSON
response on the wire because the malformed bytes have no recoverable id.

## Stdin EOF

When stdin reaches EOF, the runtime waits up to five seconds for in-flight
handler tasks to complete, cancels anything still running, flushes stdout,
and exits with status `0`. This is the canonical way to end a session
without explicitly issuing `shutdown` and `exit`. The `wsp` CLI uses this
pattern when terminating servers it has spawned.

## Logging

All log and diagnostic output goes to stderr. The transport never writes to
stdout for any reason other than emitting a framed JSON-RPC response. This
makes it safe to capture stdout as a structured stream, pipe it through
other tools, or pipe it through a debugger that injects markers on stderr.

## Why not netstrings or `\n`-delimited?

Two reasons:

1. **Compatibility with LSP tooling.** Clients that speak LSP can be
   pointed at a `wispy` server with no transport changes.
2. **Robustness.** A single header tells the decoder exactly how many bytes
   to read, so corrupted payloads can't desynchronise the stream the way
   delimiter-based framings can.

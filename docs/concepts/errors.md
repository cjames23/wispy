# Error model

WSP uses two disjoint error code ranges:

- **JSON-RPC reserved (`-32768` to `-32000`)** for transport- and
  protocol-level failures emitted directly by the dispatcher.
- **WSP reserved (`-31999` to `-31000`)** for application-level failures
  raised by handlers.

The two ranges never overlap.

## JSON-RPC error codes

| Code      | Constant                  | When emitted                                                                 |
|----------:|---------------------------|------------------------------------------------------------------------------|
| `-32700`  | `PARSE_ERROR`             | Bytes are not valid UTF-8 JSON.                                              |
| `-32600`  | `INVALID_REQUEST`         | Bytes parse as JSON but aren't a valid JSON-RPC 2.0 request, an empty batch, a duplicate `initialize`, or any non-`exit` message after `shutdown`. |
| `-32601`  | `METHOD_NOT_FOUND`        | The method name has no registered handler.                                   |
| `-32602`  | `INVALID_PARAMS`          | The request `params` did not match the method's parameter schema.            |
| `-32603`  | `INTERNAL_ERROR`          | A handler raised an exception that wasn't a `ProtocolError`.                 |
| `-32002`  | `SERVER_NOT_INITIALIZED`  | A non-`initialize` method was called before `initialize` succeeded.          |

Per the JSON-RPC 2.0 spec, parse errors and invalid-request errors carry an
`id` of JSON `null`; the others echo the request id.

## WSP error codes

| Code      | Constant                       | Meaning                                                                |
|----------:|--------------------------------|------------------------------------------------------------------------|
| `-31001`  | `ENVIRONMENT_NOT_FOUND`        | The requested environment id is not managed by this server.            |
| `-31002`  | `ENVIRONMENT_NAME_CONFLICT`    | A new environment's name collides with an existing one.                |
| `-31003`  | `PYTHON_VERSION_UNAVAILABLE`   | The requested Python version cannot be provisioned on this host.       |
| `-31004`  | `EXECUTION_FAILED`             | A handler tried to do work and the work itself failed.                 |

Every WSP error response carries a non-empty human-readable `message` (no
longer than 500 characters; longer messages are truncated with an ellipsis)
and may include a structured `data` payload that describes the failure in
machine-readable form. The `data` field is omitted entirely when the
handler did not provide one.

## Priority order

A single inbound message can satisfy more than one of the dispatcher's
error conditions. The dispatcher applies a fixed priority order:

1. `-32700` Parse error
2. `-32600` Invalid request
3. `-32601` Method not found
4. `-32602` Invalid params

Only the highest-priority applicable code is emitted. Property tests under
`tests/property/test_dispatcher_protocol.py` exercise this priority across
the input space.

## Handler exception mapping

When a handler raises:

- `WspError(code, message, data)` → the response carries
  `(code, message, data)` exactly. `data` is omitted from the wire if the
  handler passed `None` (or omitted the argument).
- Any other `ProtocolError` subclass → the response code is remapped to
  `-31004` (`EXECUTION_FAILED`).
- Any other exception → the response code is `-32603` (`INTERNAL_ERROR`)
  and the full traceback is logged to stderr.

This makes the boundary between protocol-shaped failures (which clients
should recover from) and bugs (which clients shouldn't pretend to handle)
explicit and machine-readable.

## Notifications

Notifications never produce a response, even on error. A malformed
notification, a notification to an unknown method, or a notification whose
params are invalid is silently dropped.

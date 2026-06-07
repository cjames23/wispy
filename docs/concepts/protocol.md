# Protocol overview

The Workflow Server Protocol (WSP) is a JSON-RPC 2.0 protocol that runs over
stdio, framed with the LSP base protocol's `Content-Length` headers. It is
deliberately small: a lifecycle handshake, an extensible method registry, and
a structured error model.

## Message shape

Every WSP message is a single JSON-RPC 2.0 request, response, notification,
or batch. `wispy` accepts every shape the spec defines.

A request:

```json
{
  "jsonrpc": "2.0",
  "method": "environment/list",
  "id": 1
}
```

A successful response:

```json
{
  "jsonrpc": "2.0",
  "result": [
    {"id": "abc", "name": "scratch", "python_version": "3.12"}
  ],
  "id": 1
}
```

An error response:

```json
{
  "jsonrpc": "2.0",
  "error": {"code": -31001, "message": "environment 'missing' not found", "data": {"id": "missing"}},
  "id": 1
}
```

A notification (no `id`, no response is emitted):

```json
{"jsonrpc": "2.0", "method": "exit"}
```

`wispy` preserves the JSON type of `id` (string, integer, or null) verbatim
on the response. Floats, booleans, and other types are rejected as invalid
requests; the response carries a `null` id.

Batches are arrays of request objects. The response is an array containing
one entry per non-notification request, in the order the requests appeared.
An all-notifications batch produces no response on the wire.

## Framing

Inbound and outbound bytes are framed with a single header:

```
Content-Length: 53\r\n
\r\n
{"jsonrpc":"2.0","method":"environment/list","id":1}
```

The framing layer is robust to chunking — clients may write any portion of a
frame at a time. Malformed framing is reported on the server's stderr and the
offending bytes are skipped to the next plausible frame boundary.

## Endpoints

WSP currently defines the following methods:

| Method                  | Direction         | Description                                      |
|-------------------------|-------------------|--------------------------------------------------|
| `initialize`            | client → server   | Lifecycle handshake; returns `Capabilities`.     |
| `shutdown`              | client → server   | Asks the server to stop accepting new requests.  |
| `exit`                  | client → server (notification) | Terminates the server process.   |
| `environment/list`      | client → server   | Enumerate managed environments.                  |
| `environment/create`    | client → server   | Provision a new environment.                     |
| `environment/get`       | client → server   | Fetch full details for one environment.          |
| `environment/delete`    | client → server   | Remove an environment.                           |
| `environment/execute`   | client → server   | Run a command inside an environment.             |

See [WSP methods](../reference/methods.md) for the per-method parameter and
result schemas.

## Capabilities

A successful `initialize` response carries a `Capabilities` object that lists
every WSP method the server has bound to a handler:

```json
{
  "methods": ["environment/create", "environment/list", "initialize", "shutdown"],
  "protocol_version": "0.1.0"
}
```

Clients are expected to read `methods` and adapt to whatever the server
actually offers. A method that's defined by WSP but not bound on this
particular server surfaces as JSON-RPC error `-32601` (method not found) at
call time.

## Data model

The data shapes used by the `environment/*` methods are intentionally small.
Each is documented on the [WSP methods](../reference/methods.md) page; the
short version is:

- `Environment` — `id`, `name`, `python_version`, optionally
  `interpreter_path`, `installed_packages`, and `extra` for full details.
- `Package` — `name`, `version`.
- `ExecuteResult` — `exit_code`, `stdout`, `stderr`.
- `DeleteAck` — `id` of the deleted environment.

## What WSP is not

- **Not a scheduler.** WSP is a request/response protocol; long-running work
  is exposed through methods that return when the work is done.
- **Not a transport abstraction.** Today it speaks JSON-RPC 2.0 over stdio.
  The codec and the dispatcher are decoupled from the transport; future
  iterations may add additional transports without changing the wire shape.
- **Not opinionated about Python.** Although `wispy` is written in Python,
  WSP servers can be written in any language — the Config_File flow lets you
  bind methods to arbitrary subprocess commands.

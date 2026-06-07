# Public API

`wispy` re-exports everything you need for the programmatic flow from the
top-level package.

```python
from wispy import (
    # Lifecycle / capabilities
    Capabilities,
    PROTOCOL_VERSION,
    # Endpoint data models
    DeleteAck,
    Environment,
    ExecuteResult,
    Package,
    # Error model
    DuplicateRegistrationError,
    JsonRpcErrorCode,
    ProtocolError,
    WspError,
    WspErrorCode,
    # JSON-RPC types (advanced)
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    # Registry
    Handler,
    HandlerRegistry,
    # Server runtime
    run_stdio,
)
```

## Registry

`HandlerRegistry` is the source of truth mapping WSP method names to the
Python callables that service them. Both the programmatic flow and the
Config_File flow funnel through `HandlerRegistry.register`.

```python
class HandlerRegistry:
    def register(self, method: str, handler: Handler) -> None: ...
    def methods(self) -> list[str]: ...               # sorted
    def lookup(self, method: str) -> Handler | None: ...
```

`Handler` is `Callable[[Any], Awaitable[Any] | Any]`. Sync handlers run on
the default executor; async handlers are awaited directly.

`register` raises `DuplicateRegistrationError` if the method already has
a binding, leaving prior state unchanged.

## Server runtime

```python
async def run_stdio(
    registry: HandlerRegistry,
    *,
    transport: StdioTransport | None = None,
    drain_timeout: float = 5.0,
) -> int: ...
```

Returns the desired process exit status. Wrap in `asyncio.run` from a
script.

## Error types

```python
class WspError(ProtocolError):
    """Application-level error in the -31999..-31000 reserved range."""

    def __init__(
        self,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> None: ...
```

`code` must lie in the WSP-reserved range (`-31999` to `-31000`).
`message` must be non-empty; messages longer than 500 characters are
truncated with a trailing ellipsis. `data`, when provided, must be
JSON-serializable.

`ProtocolError` is the base class for any handler exception that should
map directly to a JSON-RPC error response. A `ProtocolError` that isn't
also a `WspError` (i.e. carries no WSP code) is remapped to `-31004`
`EXECUTION_FAILED` by the dispatcher.

```python
class JsonRpcErrorCode(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_NOT_INITIALIZED = -32002


class WspErrorCode(IntEnum):
    ENVIRONMENT_NOT_FOUND = -31001
    ENVIRONMENT_NAME_CONFLICT = -31002
    PYTHON_VERSION_UNAVAILABLE = -31003
    EXECUTION_FAILED = -31004
```

## Endpoint data models

```python
@dataclass(frozen=True)
class Capabilities:
    methods: tuple[str, ...]
    protocol_version: str = PROTOCOL_VERSION  # "0.1.0"

    def to_jsonable(self) -> dict[str, Any]: ...
    @classmethod
    def from_jsonable(cls, value: Any) -> Capabilities: ...


@dataclass(frozen=True)
class Environment:
    id: str
    name: str
    python_version: str
    interpreter_path: str | None = None
    installed_packages: tuple[Package, ...] | None = None
    extra: Mapping[str, Any] | None = None

    @property
    def is_details(self) -> bool: ...
    def to_jsonable(self) -> dict[str, Any]: ...
    @classmethod
    def from_jsonable(cls, value: Any) -> Environment: ...
```

`Environment` represents both the summary form (only `id`, `name`,
`python_version`) and the details form. `to_jsonable` omits the
detail-only fields when they're `None`; `from_jsonable` accepts either
shape.

```python
@dataclass(frozen=True)
class Package:
    name: str
    version: str


@dataclass(frozen=True)
class ExecuteResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DeleteAck:
    id: str
```

All four dataclasses expose `to_jsonable` and `from_jsonable` helpers.

## JSON-RPC types

```python
@dataclass(frozen=True)
class JsonRpcRequest:
    method: str
    params: Any | None
    id: str | int | None
    is_notification: bool


@dataclass(frozen=True)
class JsonRpcError:
    code: int
    message: str
    data: Any = _UNSET   # omitted on the wire when unset


@dataclass(frozen=True)
class JsonRpcResponse:
    id: str | int | None
    result: Any = _UNSET
    error: Any = _UNSET   # mutually exclusive with result
```

Most users won't need these directly; they're exposed for tools that want
to construct or inspect JSON-RPC messages without going through the
codec.

## Stability

The above surface is the supported public API for the `0.1.x` line.
Anything not listed here — including the dispatcher and lifecycle
internals — may change between patch releases.

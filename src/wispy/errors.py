"""WSP and JSON-RPC error model.

This module defines the error codes, exception hierarchy, and validation
invariants used by the WSP server runtime to translate handler failures
into JSON-RPC error responses.

The numeric ranges here are intentional:

* JSON-RPC 2.0 reserves -32768..-32000 for protocol-level errors.
* WSP reserves -31999..-31000 (entirely outside the JSON-RPC range)
  for application-level errors raised by handlers.

"""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any

__all__ = [
    "DuplicateRegistrationError",
    "JsonRpcErrorCode",
    "ProtocolError",
    "WspError",
    "WspErrorCode",
]


# Inclusive bounds of the WSP-reserved error code range.
_WSP_CODE_MIN = -31999
_WSP_CODE_MAX = -31000

# Max allowed length for a WSP error message on the wire.
_MAX_MESSAGE_LENGTH = 500

# Single-character ellipsis used to mark a truncated message. Using the
# typographic ellipsis keeps the truncated form exactly _MAX_MESSAGE_LENGTH
# characters long while still signalling truncation visually.
_ELLIPSIS = "\u2026"


class JsonRpcErrorCode(IntEnum):
    """Numeric error codes defined by the JSON-RPC 2.0 specification.

    These codes live in the JSON-RPC reserved range (-32768..-32000) and are
    emitted directly by the dispatcher for transport- and protocol-level
    failures rather than by individual WSP handlers.
    """

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_NOT_INITIALIZED = -32002


class WspErrorCode(IntEnum):
    """Numeric error codes defined by the Workflow Server Protocol.

    All codes in this enum lie within the WSP-reserved range
    -31999..-31000 and outside the JSON-RPC reserved
    range. Handlers raise :class:`WspError` carrying one of these codes
    to signal application-level failures.
    """

    ENVIRONMENT_NOT_FOUND = -31001
    ENVIRONMENT_NAME_CONFLICT = -31002
    PYTHON_VERSION_UNAVAILABLE = -31003
    EXECUTION_FAILED = -31004


class ProtocolError(Exception):
    """Base class for exceptions that map to a JSON-RPC error response.

    The dispatcher catches any subclass of :class:`ProtocolError` raised by
    a handler and translates the carried ``code``, ``message``, and
    ``data`` fields into a JSON-RPC error object. Subclasses (notably
    :class:`WspError`) may impose additional invariants on these fields.

    Subclasses that do not carry a WSP error code are mapped by the
    dispatcher to ``execution-failed`` (-31004) on the wire.
    """

    code: int
    message: str
    data: Any | None

    def __init__(
        self,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


class WspError(ProtocolError):
    """A handler-raisable error carrying a WSP-reserved error code.

    Construction enforces the invariants required for the error to be
    safely serialized into a JSON-RPC error response:

    * ``code`` MUST lie in the WSP-reserved range -31999..-31000
    * ``message`` MUST be a non-empty string. Messages longer than 500
      characters are truncated and marked with a trailing ellipsis so
      the resulting message is exactly 500 characters long
    * ``data``, when provided, MUST be JSON-serializable so the
      dispatcher can include it verbatim in the error response.
      A :class:`TypeError` is raised otherwise.
    """

    def __init__(
        self,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> None:
        if not isinstance(code, int) or isinstance(code, bool):
            msg = f"WspError code must be an int, got {type(code).__name__}"
            raise TypeError(msg)
        if not _WSP_CODE_MIN <= code <= _WSP_CODE_MAX:
            msg = f"WspError code {code} is outside the reserved WSP range {_WSP_CODE_MIN}..{_WSP_CODE_MAX}"
            raise ValueError(msg)

        if not isinstance(message, str):
            msg = f"WspError message must be a str, got {type(message).__name__}"
            raise TypeError(msg)
        if message == "":
            msg = "WspError message must be a non-empty string"
            raise ValueError(msg)

        if len(message) > _MAX_MESSAGE_LENGTH:
            # Reserve one character for the ellipsis so the final length is
            # exactly _MAX_MESSAGE_LENGTH.
            keep = _MAX_MESSAGE_LENGTH - len(_ELLIPSIS)
            message = message[:keep] + _ELLIPSIS

        if data is not None:
            try:
                json.dumps(data)
            except (TypeError, ValueError) as exc:
                msg = f"WspError data must be JSON-serializable: {exc}"
                raise TypeError(msg) from exc

        super().__init__(code, message, data)


class DuplicateRegistrationError(Exception):
    """Raised by :class:`HandlerRegistry` when a method is already bound.

    Attempting to register a handler against a method
    name that already has a binding leaves the registry unchanged and
    raises this exception.
    """

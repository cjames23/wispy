"""WSP method registry, parameter validators, and result schemas.

This module is the single source of truth for the names of WSP methods,
the per-field constraints on their parameters, and the wire shape of
their results. It is intentionally pure (no I/O, no global state aside
from the method dictionary) so that both the dispatcher and the CLI
argument parser can lean on it without coupling.

Validators follow a simple convention:

* On success they return a *normalized* params object (a dataclass
  defined in this module) or ``None`` for the params-less methods.
* On failure they return a ``list[Violation]`` -- a list of
  human-readable rule names. Returning a ``list`` is the failure
  signal; returning anything else is success. This shape makes
  it trivial for the dispatcher to surface every violated
  rule by passing the list straight through into the JSON-RPC
  error's ``data`` field.

The data model classes (:class:`Environment`, :class:`Package`,
:class:`ExecuteResult`, :class:`DeleteAck`, :class:`Capabilities`)
provide ``to_jsonable`` / ``from_jsonable`` helpers so callers can
move between Python objects and the JSON wire shape without pulling
in ``pydantic`` or any other third-party dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "PROTOCOL_VERSION",
    "WSP_METHODS",
    "Capabilities",
    "CreateEnvironmentParams",
    "DeleteAck",
    "DeleteEnvironmentParams",
    "Environment",
    "ExecuteParams",
    "ExecuteResult",
    "GetEnvironmentParams",
    "InitializeParams",
    "Package",
    "Violation",
    "WspMethod",
]


# --------------------------------------------------------------------- #
# Constants and type aliases.
# --------------------------------------------------------------------- #


PROTOCOL_VERSION: Final[str] = "0.1.0"

# A human-readable rule name describing one violated validation rule.
# Returned in lists so callers can surface every violation at once.
Violation = str

# Field length bounds, taken verbatim from the design's Data Models
# section. They are exposed at module scope so other modules (CLI,
# fallback Workflow_Tool) can reuse the exact same numbers.
ENVIRONMENT_ID_MAX_LEN: Final[int] = 128
ENVIRONMENT_NAME_MAX_LEN: Final[int] = 256
ENVIRONMENT_PYTHON_VERSION_MAX_LEN: Final[int] = 32
INITIALIZE_CLIENT_NAME_MAX_LEN: Final[int] = 255
INITIALIZE_CLIENT_PROTOCOL_VERSION_MAX_LEN: Final[int] = 64

# MAJOR.MINOR(.PATCH)? where each component is a non-negative integer
# with no leading zeros (except a single ``0``).
_PYTHON_VERSION_RE: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?$")


# Sentinel used to distinguish "key absent in params dict" from
# "key present with value ``None``", since both must be classified as
# required-field violations but only one of them implies a structural
# bug to be repaired by the handler.
class _Missing:
    __slots__: tuple[str, ...] = ()


_MISSING: Final = _Missing()


# --------------------------------------------------------------------- #
# Method-definition dataclass.
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class WspMethod:
    """A single WSP method definition.

    Attributes:
        name: Wire-form method name (e.g. ``"environment/list"``).
        validate_params: Callable that takes the raw ``params`` value
            from the request and returns either a normalized params
            object (any non-list value, including ``None``) or a
            ``list[Violation]`` describing every rule violated.
        validate_result: Callable that takes a handler-returned value
            and raises on shape violations. Used by the dispatcher to
            catch internal bugs in handlers; for params-less methods
            (e.g. ``shutdown``) this is a no-op.
    """

    name: str
    validate_params: Callable[[Any], Any | list[Violation]]
    validate_result: Callable[[Any], None]


# --------------------------------------------------------------------- #
# Wire data models.
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class Package:
    """One installed package, as reported in environment details."""

    name: str
    version: str

    def to_jsonable(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_jsonable(cls, value: Any) -> Package:
        if not isinstance(value, dict):
            msg = f"Package must be a JSON object, got {type(value).__name__}"
            raise TypeError(msg)
        try:
            name = value["name"]
            version = value["version"]
        except KeyError as exc:
            msg = f"Package is missing required field: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(name, str):
            msg = "Package.name must be a string"
            raise TypeError(msg)
        if not isinstance(version, str):
            msg = "Package.version must be a string"
            raise TypeError(msg)
        return cls(name=name, version=version)


@dataclass(frozen=True)
class Environment:
    """An Environment in either summary or details form.

    The summary form (returned by ``environment/list``) carries only
    ``id``, ``name``, and ``python_version``. The details form
    (returned by ``environment/get`` and ``environment/create``) adds
    ``interpreter_path``, ``installed_packages``, and ``extra``.

    The two forms are modeled as a single dataclass with the
    detail-only fields defaulting to ``None``: ``None`` distinguishes
    summary from details on serialization. ``extra`` is always present
    on the wire when the Environment is in details form, even when
    empty -- ``Environment(..., extra={})`` is still details.
    """

    id: str
    name: str
    python_version: str
    interpreter_path: str | None = None
    installed_packages: tuple[Package, ...] | None = None
    extra: Mapping[str, Any] | None = None

    @property
    def is_details(self) -> bool:
        """``True`` iff this Environment carries the detail-only fields."""
        return self.interpreter_path is not None and self.installed_packages is not None and self.extra is not None

    def to_jsonable(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "python_version": self.python_version,
        }
        if self.interpreter_path is not None:
            out["interpreter_path"] = self.interpreter_path
        if self.installed_packages is not None:
            out["installed_packages"] = [p.to_jsonable() for p in self.installed_packages]
        if self.extra is not None:
            # Copy so callers cannot mutate our internal mapping through
            # the returned dict.
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_jsonable(cls, value: Any) -> Environment:
        if not isinstance(value, dict):
            msg = f"Environment must be a JSON object, got {type(value).__name__}"
            raise TypeError(msg)
        try:
            id_ = value["id"]
            name = value["name"]
            python_version = value["python_version"]
        except KeyError as exc:
            msg = f"Environment is missing required field: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(id_, str):
            msg = "Environment.id must be a string"
            raise TypeError(msg)
        if not isinstance(name, str):
            msg = "Environment.name must be a string"
            raise TypeError(msg)
        if not isinstance(python_version, str):
            msg = "Environment.python_version must be a string"
            raise TypeError(msg)

        interpreter_path = value.get("interpreter_path")
        if interpreter_path is not None and not isinstance(interpreter_path, str):
            msg = "Environment.interpreter_path must be a string"
            raise TypeError(msg)

        raw_packages = value.get("installed_packages")
        installed_packages: tuple[Package, ...] | None
        if raw_packages is None:
            installed_packages = None
        else:
            if not isinstance(raw_packages, list):
                msg = "Environment.installed_packages must be a JSON array"
                raise TypeError(msg)
            installed_packages = tuple(Package.from_jsonable(p) for p in raw_packages)

        raw_extra = value.get("extra", _MISSING)
        extra: Mapping[str, Any] | None
        if raw_extra is _MISSING:
            extra = None
        else:
            if not isinstance(raw_extra, dict):
                msg = "Environment.extra must be a JSON object"
                raise TypeError(msg)
            extra = dict(raw_extra)

        return cls(
            id=id_,
            name=name,
            python_version=python_version,
            interpreter_path=interpreter_path,
            installed_packages=installed_packages,
            extra=extra,
        )


@dataclass(frozen=True)
class ExecuteResult:
    """The result returned by ``environment/execute``."""

    exit_code: int
    stdout: str
    stderr: str

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    @classmethod
    def from_jsonable(cls, value: Any) -> ExecuteResult:
        if not isinstance(value, dict):
            msg = f"ExecuteResult must be a JSON object, got {type(value).__name__}"
            raise TypeError(msg)
        try:
            exit_code = value["exit_code"]
            stdout = value["stdout"]
            stderr = value["stderr"]
        except KeyError as exc:
            msg = f"ExecuteResult is missing required field: {exc}"
            raise ValueError(msg) from exc
        # ``bool`` is a subclass of ``int``; reject it explicitly so that
        # serializing ``True`` as an exit_code is impossible.
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            msg = "ExecuteResult.exit_code must be an integer"
            raise TypeError(msg)
        if not isinstance(stdout, str):
            msg = "ExecuteResult.stdout must be a string"
            raise TypeError(msg)
        if not isinstance(stderr, str):
            msg = "ExecuteResult.stderr must be a string"
            raise TypeError(msg)
        return cls(exit_code=exit_code, stdout=stdout, stderr=stderr)


@dataclass(frozen=True)
class DeleteAck:
    """Acknowledgement returned by ``environment/delete``."""

    id: str

    def to_jsonable(self) -> dict[str, Any]:
        return {"id": self.id}

    @classmethod
    def from_jsonable(cls, value: Any) -> DeleteAck:
        if not isinstance(value, dict):
            msg = f"DeleteAck must be a JSON object, got {type(value).__name__}"
            raise TypeError(msg)
        try:
            id_ = value["id"]
        except KeyError as exc:
            msg = f"DeleteAck is missing required field: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(id_, str):
            msg = "DeleteAck.id must be a string"
            raise TypeError(msg)
        return cls(id=id_)


@dataclass(frozen=True)
class Capabilities:
    """The result of a successful ``initialize`` call."""

    methods: tuple[str, ...] = ()
    protocol_version: str = PROTOCOL_VERSION

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "methods": list(self.methods),
            "protocol_version": self.protocol_version,
        }

    @classmethod
    def from_jsonable(cls, value: Any) -> Capabilities:
        if not isinstance(value, dict):
            msg = f"Capabilities must be a JSON object, got {type(value).__name__}"
            raise TypeError(msg)
        try:
            methods = value["methods"]
            protocol_version = value["protocol_version"]
        except KeyError as exc:
            msg = f"Capabilities is missing required field: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(methods, list) or not all(isinstance(m, str) for m in methods):
            msg = "Capabilities.methods must be a list of strings"
            raise TypeError(msg)
        if not isinstance(protocol_version, str):
            msg = "Capabilities.protocol_version must be a string"
            raise TypeError(msg)
        return cls(methods=tuple(methods), protocol_version=protocol_version)


# --------------------------------------------------------------------- #
# Normalized params dataclasses.
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class InitializeParams:
    """Normalized parameters for ``initialize``."""

    client_name: str
    client_protocol_version: str


@dataclass(frozen=True)
class CreateEnvironmentParams:
    """Normalized parameters for ``environment/create``.

    Note: only the syntactic shape is normalized here. Conflict and
    version-availability checks are performed by the handler, not the
    validator.
    """

    name: str
    python_version: str


@dataclass(frozen=True)
class GetEnvironmentParams:
    """Normalized parameters for ``environment/get``."""

    id: str


@dataclass(frozen=True)
class DeleteEnvironmentParams:
    """Normalized parameters for ``environment/delete``."""

    id: str


@dataclass(frozen=True)
class ExecuteParams:
    """Normalized parameters for ``environment/execute``."""

    id: str
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] | None = None


# --------------------------------------------------------------------- #
# Validators.
# --------------------------------------------------------------------- #


def _as_params_object(params: Any) -> dict[str, Any] | list[Violation]:
    """Coerce a raw ``params`` value to a dict or report a violation.

    Several WSP endpoints accept by-name params; their validators all
    need the same "params is a JSON object or report a single
    violation" preamble. JSON arrays (positional params) are rejected
    because no WSP method defines positional parameters.
    """
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    return ["params-not-object"]


def _take(params: dict[str, Any], key: str) -> Any:
    """Return ``params[key]`` or :data:`_MISSING` when the key is absent."""
    return params.get(key, _MISSING)


def _validate_no_params(params: Any) -> None | list[Violation]:
    """Validator for methods that accept no parameters.

    Per JSON-RPC 2.0, ``params`` may be omitted entirely (``None`` here),
    or supplied as an empty array or empty object. Anything else is a
    violation since the method has no parameters to interpret.
    """
    if params is None:
        return None
    if isinstance(params, list) and not params:
        return None
    if isinstance(params, dict) and not params:
        return None
    return ["params-not-empty"]


def _validate_initialize_params(
    params: Any,
) -> InitializeParams | list[Violation]:
    """Validator for ``initialize``.

    Collects every applicable violation; does not short-circuit. Rule
    names mirror the field they apply to plus a suffix describing the
    failure mode so callers can render targeted messages.
    """
    coerced = _as_params_object(params)
    if isinstance(coerced, list):
        return coerced

    violations: list[Violation] = []

    client_name = _take(coerced, "client_name")
    if client_name is _MISSING:
        violations.append("client-name-required")
    elif not isinstance(client_name, str):
        violations.append("client-name-type")
    elif not (1 <= len(client_name) <= INITIALIZE_CLIENT_NAME_MAX_LEN):
        violations.append("client-name-length")

    client_protocol_version = _take(coerced, "client_protocol_version")
    if client_protocol_version is _MISSING:
        violations.append("client-protocol-version-required")
    elif not isinstance(client_protocol_version, str):
        violations.append("client-protocol-version-type")
    elif not (1 <= len(client_protocol_version) <= INITIALIZE_CLIENT_PROTOCOL_VERSION_MAX_LEN):
        violations.append("client-protocol-version-length")

    if violations:
        return violations
    # If we reach here, both fields are non-empty strings.
    return InitializeParams(
        client_name=cast("str", client_name),
        client_protocol_version=cast("str", client_protocol_version),
    )


def _validate_create_environment_params(
    params: Any,
) -> CreateEnvironmentParams | list[Violation]:
    """Validator for ``environment/create``.

    Per the design's task spec, the only rules evaluated here are the
    *syntactic* ones, in the order ``name-required``, ``name-length``,
    ``python-version-required``, ``python-version-format``. Conflict
    and version-availability are handler-level concerns.

    Violations are collected and returned together so every
    violated rule is reported at once -- the
    handler simply passes the list through.
    """
    coerced = _as_params_object(params)
    if isinstance(coerced, list):
        return coerced

    violations: list[Violation] = []

    # ---- name ---------------------------------------------------------
    name = _take(coerced, "name")
    name_str: str | None = None
    if name is _MISSING or name is None:
        violations.append("name-required")
    elif not isinstance(name, str):
        # A non-string ``name`` still counts as "name-required" for our
        # surface area: the rule is "the request must carry a usable
        # ``name`` string." Adding a separate "name-type" rule would
        # leak validator implementation detail into the wire vocabulary
        # the handler reuses.
        violations.append("name-required")
    elif name == "":
        violations.append("name-required")
    else:
        name_str = name
        if len(name) > ENVIRONMENT_NAME_MAX_LEN:
            violations.append("name-length")

    # ---- python_version ----------------------------------------------
    python_version = _take(coerced, "python_version")
    py_str: str | None = None
    if python_version is _MISSING or python_version is None or not isinstance(python_version, str) or python_version == "":
        violations.append("python-version-required")
    else:
        py_str = python_version
        if len(python_version) > ENVIRONMENT_PYTHON_VERSION_MAX_LEN or _PYTHON_VERSION_RE.match(python_version) is None:
            violations.append("python-version-format")

    if violations:
        return violations

    return CreateEnvironmentParams(name=cast("str", name_str), python_version=cast("str", py_str))


def _validate_id_params(
    params: Any,
    cls: type,
) -> Any:
    """Shared validator for ``environment/get`` and ``environment/delete``."""
    coerced = _as_params_object(params)
    if isinstance(coerced, list):
        return coerced

    violations: list[Violation] = []
    id_ = _take(coerced, "id")
    if id_ is _MISSING or id_ is None or not isinstance(id_, str) or id_ == "":
        violations.append("id-required")
    elif len(id_) > ENVIRONMENT_ID_MAX_LEN:
        violations.append("id-length")

    if violations:
        return violations
    return cls(id=id_)


def _validate_get_environment_params(
    params: Any,
) -> GetEnvironmentParams | list[Violation]:
    return _validate_id_params(params, GetEnvironmentParams)


def _validate_delete_environment_params(
    params: Any,
) -> DeleteEnvironmentParams | list[Violation]:
    return _validate_id_params(params, DeleteEnvironmentParams)


def _validate_execute_params(params: Any) -> ExecuteParams | list[Violation]:
    """Validator for ``environment/execute``.

    Rejects empty ``argv`` and any ``argv`` element that is not a
    string; both conditions surface to the dispatcher as ``-32602``.
    The remaining checks (id presence, optional cwd / env shape) are
    likewise enforced here so the handler can assume normalized
    params.
    """
    coerced = _as_params_object(params)
    if isinstance(coerced, list):
        return coerced

    violations: list[Violation] = []

    id_ = _take(coerced, "id")
    if id_ is _MISSING or id_ is None or not isinstance(id_, str) or id_ == "":
        violations.append("id-required")
    elif len(id_) > ENVIRONMENT_ID_MAX_LEN:
        violations.append("id-length")

    argv_raw = _take(coerced, "argv")
    argv_tuple: tuple[str, ...] = ()
    if argv_raw is _MISSING or argv_raw is None:
        violations.append("argv-required")
    elif not isinstance(argv_raw, list):
        violations.append("argv-type")
    elif len(argv_raw) == 0:
        violations.append("argv-empty")
    elif not all(isinstance(a, str) for a in argv_raw):
        violations.append("argv-element-type")
    else:
        argv_tuple = tuple(argv_raw)

    cwd_raw = coerced.get("cwd", None)
    if cwd_raw is not None and not isinstance(cwd_raw, str):
        violations.append("cwd-type")

    env_raw = coerced.get("env", None)
    env_dict: Mapping[str, str] | None = None
    if env_raw is not None:
        if not isinstance(env_raw, dict):
            violations.append("env-type")
        elif not all(isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()):
            violations.append("env-entry-type")
        else:
            env_dict = dict(env_raw)

    if violations:
        return violations

    return ExecuteParams(
        id=cast("str", id_),
        argv=argv_tuple,
        cwd=cwd_raw,
        env=env_dict,
    )


# --------------------------------------------------------------------- #
# Result validators.
# --------------------------------------------------------------------- #


def _validate_initialize_result(result: Any) -> None:
    if not isinstance(result, Capabilities):
        msg = f"initialize result must be Capabilities, got {type(result).__name__}"
        raise TypeError(msg)
    if not isinstance(result.protocol_version, str) or not result.protocol_version:
        msg = "initialize result protocol_version must be a non-empty string"
        raise ValueError(msg)
    if not isinstance(result.methods, tuple) or not all(isinstance(m, str) for m in result.methods):
        msg = "initialize result methods must be a tuple of strings"
        raise TypeError(msg)


def _validate_shutdown_result(result: Any) -> None:
    # ``shutdown`` returns null on the wire; the dispatcher accepts
    # ``None`` from the handler and serializes it accordingly.
    if result is not None:
        msg = f"shutdown result must be None, got {type(result).__name__}"
        raise TypeError(msg)


def _validate_environment_list_result(result: Any) -> None:
    if not isinstance(result, list):
        msg = f"environment/list result must be a list, got {type(result).__name__}"
        raise TypeError(msg)
    for env in result:
        if not isinstance(env, Environment):
            msg = f"environment/list entries must be Environment, got {type(env).__name__}"
            raise TypeError(msg)


def _validate_environment_details_result(result: Any) -> None:
    if not isinstance(result, Environment):
        msg = f"environment details result must be Environment, got {type(result).__name__}"
        raise TypeError(msg)
    if not result.is_details:
        msg = "environment details result must include interpreter_path, installed_packages, and extra"
        raise ValueError(msg)


def _validate_delete_result(result: Any) -> None:
    if not isinstance(result, DeleteAck):
        msg = f"environment/delete result must be DeleteAck, got {type(result).__name__}"
        raise TypeError(msg)


def _validate_execute_result(result: Any) -> None:
    if not isinstance(result, ExecuteResult):
        msg = f"environment/execute result must be ExecuteResult, got {type(result).__name__}"
        raise TypeError(msg)


# --------------------------------------------------------------------- #
# Method registry.
# --------------------------------------------------------------------- #


WSP_METHODS: Final[dict[str, WspMethod]] = {
    method.name: method
    for method in (
        WspMethod(
            name="initialize",
            validate_params=_validate_initialize_params,
            validate_result=_validate_initialize_result,
        ),
        WspMethod(
            name="shutdown",
            validate_params=_validate_no_params,
            validate_result=_validate_shutdown_result,
        ),
        WspMethod(
            name="environment/list",
            validate_params=_validate_no_params,
            validate_result=_validate_environment_list_result,
        ),
        WspMethod(
            name="environment/create",
            validate_params=_validate_create_environment_params,
            validate_result=_validate_environment_details_result,
        ),
        WspMethod(
            name="environment/get",
            validate_params=_validate_get_environment_params,
            validate_result=_validate_environment_details_result,
        ),
        WspMethod(
            name="environment/delete",
            validate_params=_validate_delete_environment_params,
            validate_result=_validate_delete_result,
        ),
        WspMethod(
            name="environment/execute",
            validate_params=_validate_execute_params,
            validate_result=_validate_execute_result,
        ),
    )
}

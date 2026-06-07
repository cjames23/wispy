"""Config_File loading and handler resolution (TOML/JSON).

This module reads a WSP server Config_File and translates it into a
list of :class:`ConfigEntry` records that the ``__main__`` bootstrap
later turns into :data:`~wispy.registry.Handler` callables via
:func:`make_handler`.

The schema is intentionally narrow:

* The top-level document is a table/object. Unknown top-level keys are
  ignored so that future server-wide settings can be added without
  breaking older configs.
* The ``handlers`` key is a table whose keys are WSP method names
  (validated against :data:`~wispy.endpoints.WSP_METHODS`) and whose
  values are *handler entries*.
* A handler entry must specify exactly one of ``import`` (a dotted
  Python attribute path) or ``command`` (a non-empty array of strings
  for a Subprocess_Handler). Any other key inside a handler entry is
  rejected.

All rejection paths raise :class:`ConfigError` with a message that
matches the design's "Config_File startup errors" table; the caller in
``__main__`` translates these to ``wispy: config error: <path>:
<reason>`` on stderr and exits 1.

The Subprocess_Handler factory (task 13.1) wraps the configured
``argv`` in an async coroutine that spawns the process per request,
pipes JSON params on stdin, and parses JSON from stdout. Failures map
to :class:`~wispy.errors.WspError` with ``EXECUTION_FAILED``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import shutil
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from wispy.registry import Handler

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

from wispy.endpoints import WSP_METHODS
from wispy.errors import WspError, WspErrorCode

# Subprocess_Handler invocations have a 30-second
# wall-clock timeout that is independent of the dispatcher's per-request
# timeout. Exceeding it is mapped to ``EXECUTION_FAILED`` with diagnostic
# context in ``data``.
_SUBPROCESS_TIMEOUT = 30.0

# A handler entry that names both ``import`` and ``command`` has both
# keys set; this constant names the count for PLR2004 compliance.
_BOTH_KINDS_COUNT = 2

__all__ = [
    "ConfigEntry",
    "ConfigError",
    "PythonHandlerSpec",
    "SubprocessHandlerSpec",
    "load_config",
    "make_handler",
]


@dataclass(frozen=True)
class PythonHandlerSpec:
    """A handler implemented as an in-process Python callable.

    ``import_path`` is a dotted path of the form ``pkg.module.attr``;
    the trailing component is resolved via ``getattr`` after importing
    the leading components as a module.
    """

    import_path: str


@dataclass(frozen=True)
class SubprocessHandlerSpec:
    """A handler implemented as an external command.

    ``argv`` is a non-empty tuple. ``argv[0]`` must resolve on ``PATH``
    via :func:`shutil.which`; subsequent elements are passed verbatim
    when the Subprocess_Handler is invoked.
    """

    argv: tuple[str, ...]


@dataclass(frozen=True)
class ConfigEntry:
    """One Config_File handler binding."""

    method: str
    spec: PythonHandlerSpec | SubprocessHandlerSpec


class ConfigError(Exception):
    """Raised by :func:`load_config` and :func:`make_handler` on rejection.

    The message text is the human-readable reason that the
    ``__main__`` bootstrap concatenates into ``wispy: config error:
    <path>: <reason>`` before exiting with status 1.
    """


def load_config(path: Path) -> list[ConfigEntry]:
    """Read and validate a TOML or JSON Config_File.

    The extension match is case-insensitive (``.toml`` / ``.json``);
    any other extension is rejected. Unknown top-level keys are
    accepted, but unknown keys *inside* a handler entry are rejected.
    Each method name is validated against
    :data:`~wispy.endpoints.WSP_METHODS` so a typo surfaces here
    rather than at first request.
    """
    suffix = path.suffix.lower()
    if suffix == ".toml":
        try:
            with open(path, "rb") as f:
                doc = tomllib.load(f)
        except OSError as exc:
            msg = f"failed to read config file: {exc}"
            raise ConfigError(msg) from exc
        except tomllib.TOMLDecodeError as exc:
            msg = f"failed to parse TOML: {exc}"
            raise ConfigError(msg) from exc
    elif suffix == ".json":
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except OSError as exc:
            msg = f"failed to read config file: {exc}"
            raise ConfigError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"failed to parse JSON: {exc}"
            raise ConfigError(msg) from exc
    else:
        msg = f"unsupported config extension {suffix!r}; expected .toml or .json"
        raise ConfigError(msg)

    if not isinstance(doc, dict):
        msg = "top-level config must be a table/object"
        raise ConfigError(msg)

    handlers = doc.get("handlers", {})
    if not isinstance(handlers, dict):
        msg = "'handlers' must be a table/object"
        raise ConfigError(msg)

    entries: list[ConfigEntry] = []
    for method, spec_obj in handlers.items():
        if method not in WSP_METHODS:
            msg = f"unknown WSP method {method!r}"
            raise ConfigError(msg)
        if not isinstance(spec_obj, dict):
            msg = f"handler entry for {method!r} must be a table/object"
            raise ConfigError(msg)

        keys = set(spec_obj.keys())
        # Exactly one of {import, command}.
        kinds = keys & {"import", "command"}
        if len(kinds) == 0:
            msg = f"handler entry for {method!r} must specify either 'import' or 'command'"
            raise ConfigError(msg)
        if len(kinds) == _BOTH_KINDS_COUNT:
            msg = f"handler entry for {method!r} must not specify both 'import' and 'command'"
            raise ConfigError(msg)
        unknown = keys - {"import", "command"}
        if unknown:
            msg = f"handler entry for {method!r} has unknown keys: {sorted(unknown)!r}"
            raise ConfigError(msg)

        spec: PythonHandlerSpec | SubprocessHandlerSpec
        if "import" in spec_obj:
            import_path = spec_obj["import"]
            if not isinstance(import_path, str) or not import_path:
                msg = f"handler entry for {method!r}: 'import' must be a non-empty string"
                raise ConfigError(msg)
            # Validate the dotted path resolves to a callable now;
            # make_handler re-resolves later, but
            # surfacing the error at startup is the whole point.
            try:
                _resolve_import(import_path)
            except ConfigError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                msg = f"handler entry for {method!r}: failed to resolve {import_path!r}: {exc}"
                raise ConfigError(msg) from exc
            spec = PythonHandlerSpec(import_path=import_path)
        else:
            command = spec_obj["command"]
            if not isinstance(command, list) or not command:
                msg = f"handler entry for {method!r}: 'command' must be a non-empty array"
                raise ConfigError(msg)
            for i, arg in enumerate(command):
                if not isinstance(arg, str):
                    msg = f"handler entry for {method!r}: 'command' element {i} is not a string"
                    raise ConfigError(msg)
            if shutil.which(command[0]) is None:
                msg = f"handler entry for {method!r}: command[0] {command[0]!r} not on PATH"
                raise ConfigError(msg)
            spec = SubprocessHandlerSpec(argv=tuple(command))

        entries.append(ConfigEntry(method=method, spec=spec))

    return entries


def _resolve_import(import_path: str) -> Any:
    """Import the dotted path and return the resolved attribute.

    Raises :class:`ConfigError` if the module cannot be imported, the
    attribute is missing, or the resolved object is not callable.
    """
    if "." not in import_path:
        msg = f"import path {import_path!r} must be a dotted path like 'pkg.module.attr'"
        raise ConfigError(msg)
    module_name, _, attr = import_path.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        msg = f"cannot import module {module_name!r}: {exc}"
        raise ConfigError(msg) from exc
    if not hasattr(module, attr):
        msg = f"module {module_name!r} has no attribute {attr!r}"
        raise ConfigError(msg)
    obj = getattr(module, attr)
    if not callable(obj):
        msg = f"{import_path!r} resolved but is not callable: {type(obj).__name__}"
        raise ConfigError(msg)
    return obj


def _make_subprocess_handler(spec: SubprocessHandlerSpec) -> Handler:
    """Build a :data:`~wispy.registry.Handler` for a Subprocess_Handler.

    The returned coroutine spawns ``argv`` via
    :func:`asyncio.create_subprocess_exec`, pipes ``json.dumps(params)``
    on stdin, and waits up to 30 seconds for the process to terminate.
    On clean exit (returncode 0) with valid JSON on stdout, the parsed
    value is returned. Any other outcome raises
    :class:`~wispy.errors.WspError` carrying ``EXECUTION_FAILED``
    (-31004) with a ``reason`` discriminator plus
    ``returncode`` and captured ``stderr`` in ``data``.

    ``argv`` is captured from the spec at factory time. ``shutil.which``
    has already been checked at config load, so we do
    not re-validate here, but we still wrap the spawn itself in a
    try/except in case the executable disappears from PATH between load
    and call.
    """
    argv = list(spec.argv)

    async def handler(params: Any) -> Any:
        try:
            payload = json.dumps(params).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WspError(
                int(WspErrorCode.EXECUTION_FAILED),
                f"failed to serialize params for subprocess handler: {exc}",
                data={"reason": "params-not-json-serializable"},
            ) from exc

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise WspError(
                int(WspErrorCode.EXECUTION_FAILED),
                f"failed to spawn subprocess handler: {exc}",
                data={
                    "reason": "spawn-failed",
                    "argv": argv,
                },
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=payload),
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except Exception:  # noqa: BLE001 - teardown after timeout must be robust
                stderr = b""
            msg = f"subprocess handler timed out after {_SUBPROCESS_TIMEOUT}s"
            raise WspError(
                int(WspErrorCode.EXECUTION_FAILED),
                msg,
                data={
                    "reason": "timeout",
                    "argv": argv,
                    "stderr": stderr.decode("utf-8", errors="replace"),
                },
            ) from exc

        returncode = proc.returncode
        if returncode != 0:
            msg = f"subprocess handler exited with non-zero status {returncode}"
            raise WspError(
                int(WspErrorCode.EXECUTION_FAILED),
                msg,
                data={
                    "reason": "non-zero-exit",
                    "returncode": returncode,
                    "argv": argv,
                    "stderr": stderr.decode("utf-8", errors="replace"),
                },
            )

        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WspError(
                int(WspErrorCode.EXECUTION_FAILED),
                f"subprocess handler did not produce valid JSON: {exc}",
                data={
                    "reason": "invalid-json-output",
                    "argv": argv,
                    "stderr": stderr.decode("utf-8", errors="replace"),
                },
            ) from exc

    return handler


def make_handler(entry: ConfigEntry) -> Handler:
    """Build a :data:`~wispy.registry.Handler` callable for ``entry``.

    For :class:`PythonHandlerSpec`, re-resolve the dotted path and
    return the callable directly. :func:`load_config` has already
    validated the path, so this should not fail in practice.

    For :class:`SubprocessHandlerSpec`, return an async wrapper that
    spawns the configured ``argv`` per request, pipes JSON params on
    stdin, and parses JSON from stdout. Failures are translated to
    :class:`~wispy.errors.WspError` carrying ``EXECUTION_FAILED``.
    """
    if isinstance(entry.spec, PythonHandlerSpec):
        return _resolve_import(entry.spec.import_path)
    if isinstance(entry.spec, SubprocessHandlerSpec):
        return _make_subprocess_handler(entry.spec)
    msg = f"unknown handler spec: {entry.spec!r}"
    raise TypeError(msg)

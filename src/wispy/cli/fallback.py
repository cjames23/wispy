"""Built-in fallback Workflow_Tool backed by ``venv``.

This module wires the WSP method surface (``initialize``, ``shutdown``,
``environment/list``, ``environment/create``, ``environment/get``,
``environment/delete``, ``environment/execute``) to a tiny on-disk
Workflow_Tool whose implementation lives entirely in the Python
standard library: it persists Environments via :class:`FallbackState`
and creates them via :class:`venv.EnvBuilder`. The fallback exists so
the WSP_CLI is useful out of the box -- a user with no Workflow_Tool
selected still has working ``environment/*`` calls.

Design highlights:

* The dispatcher runs WSP method validators *before* reaching a
  handler, so each handler receives a normalized params dataclass
  (e.g. :class:`CreateEnvironmentParams`) and only has to enforce the
  handler-level rules: ``name-conflict`` and
  ``python-version-available`` for ``environment/create``,
  unknown-id for ``environment/get`` / ``environment/delete`` /
  ``environment/execute``.
* All index reads and writes are wrapped in :meth:`FallbackState.lock`
  so concurrent WSP_CLI invocations against the same state directory
  cannot corrupt the index (the lock primitive is
  implemented in :mod:`wispy.cli.state`).
* ``subprocess`` and ``venv`` are imported at module scope so tests
  can monkey-patch :data:`wispy.cli.fallback.subprocess.run` and
  :meth:`venv.EnvBuilder.create` to drive the rule-based property
  tests without ever spawning a real interpreter.
* Handlers return JSON-serializable dicts (via ``Environment``,
  ``DeleteAck``, ``ExecuteResult``, and ``Capabilities``'
  ``to_jsonable`` helpers) -- the dispatcher feeds those values
  straight into :func:`json.dumps`.

See ``design.md`` -> ``cli.fallback``.
"""

from __future__ import annotations

import json as _json
import os
import re
import shutil
import subprocess
import uuid
import venv
from typing import TYPE_CHECKING, Any

from wispy.cli.state import FallbackState
from wispy.endpoints import (
    PROTOCOL_VERSION,
    Capabilities,
    DeleteAck,
    Environment,
    ExecuteResult,
    Package,
)
from wispy.errors import WspError, WspErrorCode
from wispy.registry import HandlerRegistry

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["make_fallback_registry"]


# Name of the venv subdirectory that contains the interpreter. POSIX
# venvs use ``bin``; Windows venvs use ``Scripts``.
_VENV_BIN_DIR = "Scripts" if os.name == "nt" else "bin"

# Name of the interpreter executable inside ``_VENV_BIN_DIR``.
_VENV_PY_NAME = "python.exe" if os.name == "nt" else "python"

# The validators in :mod:`wispy.endpoints` already enforce the format
# ``MAJOR.MINOR(.PATCH)?``, so this regex only has to extract the
# leading two components for ``shutil.which`` lookup.
_PY_MAJOR_MINOR_RE = re.compile(r"^(\d+)\.(\d+)")


def _resolve_python(python_version: str) -> str | None:
    """Return the path to a ``pythonX.Y`` interpreter on PATH, if any.

    Used to implement the ``python-version-available`` handler-level
    rule. Returns ``None`` when no compatible
    interpreter is on PATH; the caller turns that into a
    ``PYTHON_VERSION_UNAVAILABLE`` :class:`WspError`.
    """
    match = _PY_MAJOR_MINOR_RE.match(python_version)
    if match is None:
        # Defensive: the validator should have caught a malformed
        # version string before we got here, but we refuse to crash if
        # it did not.
        return None
    major, minor = match.group(1), match.group(2)
    return shutil.which(f"python{major}.{minor}")


def _venv_interpreter_path(envs_dir: Path, env_id: str) -> Path:
    """Return the absolute path to the interpreter inside an env."""
    return envs_dir / env_id / _VENV_BIN_DIR / _VENV_PY_NAME


def _list_installed_packages(interpreter: Path) -> list[Package]:
    """Best-effort enumeration of packages installed in the venv.

    Calls ``<interpreter> -m pip list --format=json`` with a tight
    timeout. Any failure (pip absent, non-zero exit, malformed output,
    timeout) yields ``[]`` rather than raising; ``environment/get``
    must succeed for envs without pip.
    """
    try:
        proc = subprocess.run(
            [str(interpreter), "-m", "pip", "list", "--format=json"],
            check=False, capture_output=True,
            timeout=20,
        )
    except (
        FileNotFoundError,
        PermissionError,
        OSError,
        subprocess.TimeoutExpired,
    ):
        return []
    if proc.returncode != 0:
        return []
    try:
        raw = proc.stdout.decode("utf-8", errors="replace")
        data = _json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[Package] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        version = entry.get("version")
        if isinstance(name, str) and isinstance(version, str):
            out.append(Package(name=name, version=version))
    return out


def make_fallback_registry(
    state_root: Path | None = None,
) -> HandlerRegistry:
    """Build a :class:`HandlerRegistry` wired to the fallback handlers.

    Args:
        state_root: Optional override for the on-disk state directory.
            When ``None``, :class:`FallbackState` resolves the
            location from ``$WISPY_STATE_DIR`` / ``$XDG_STATE_HOME`` /
            ``%LOCALAPPDATA%`` per its documented precedence. Tests
            pass an explicit path so each invocation gets a fresh
            tmpdir.

    Returns:
        A :class:`HandlerRegistry` with bindings for ``initialize``,
        ``shutdown``, and every ``environment/*`` method.
    """
    state = FallbackState(state_root)
    state.ensure_layout()
    registry = HandlerRegistry()

    # ------------------------------------------------------------- #
    # Lifecycle handlers.
    # ------------------------------------------------------------- #

    def _initialize(_params: Any) -> Any:
        # Mirrors the ``__main__`` module's lifecycle handler shape:
        # return the JSON-serializable dict so the dispatcher can hand
        # it straight to ``json.dumps``. The lifecycle FSM accepts the
        # value opaquely.
        return Capabilities(
            methods=tuple(registry.methods()),
            protocol_version=PROTOCOL_VERSION,
        ).to_jsonable()

    def _shutdown(_params: Any) -> None:
        return None

    # ------------------------------------------------------------- #
    # environment/list.
    # ------------------------------------------------------------- #

    def _environment_list(_params: Any) -> Any:
        with state.lock():
            envs = state.read_index()
        # Deterministic ordering by ascending id.
        envs_sorted = sorted(envs, key=lambda e: e.id)
        return [e.to_jsonable() for e in envs_sorted]

    # ------------------------------------------------------------- #
    # environment/create.
    # ------------------------------------------------------------- #

    def _environment_create(params: Any) -> Any:
        # ``params`` is a CreateEnvironmentParams dataclass thanks to
        # the dispatcher's WSP_METHODS validation; the only checks
        # left are the handler-level ones.
        with state.lock():
            envs = state.read_index()

            # Handler-level violations. Order matters for the
            # error-code priority:
            # ``name-conflict`` > ``python-version-available``.
            violations: list[str] = []
            if any(e.name == params.name for e in envs):
                violations.append("name-conflict")
            interp = _resolve_python(params.python_version)
            if interp is None:
                violations.append("python-version-available")

            if violations:
                if "name-conflict" in violations:
                    raise WspError(
                        int(WspErrorCode.ENVIRONMENT_NAME_CONFLICT),
                        f"environment name {params.name!r} already exists",
                        data={
                            "violations": violations,
                            "name": params.name,
                        },
                    )
                # Only python-version-available remains.
                raise WspError(
                    int(WspErrorCode.PYTHON_VERSION_UNAVAILABLE),
                    f"python version {params.python_version!r} is not available on PATH",
                    data={"violations": violations},
                )

            new_id = f"env-{uuid.uuid4().hex[:12]}"
            target = state.envs_dir / new_id

            # The on-disk venv is created via ``venv.EnvBuilder``.
            # ``with_pip=True`` makes the env
            # immediately useful (``pip install`` works without
            # ensurepip gymnastics); the property tests stub this call
            # so the cost only matters in real CLI invocations.
            try:
                builder = venv.EnvBuilder(with_pip=True)
                builder.create(str(target))
            except Exception as exc:
                # Clean up the partial directory and
                # leave the index untouched so a retry behaves like a
                # fresh request.
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                raise WspError(
                    int(WspErrorCode.EXECUTION_FAILED),
                    f"failed to create venv: {exc}",
                    data={"reason": "venv-create-failed"},
                ) from exc

            interpreter_path = _venv_interpreter_path(state.envs_dir, new_id)

            # Persist the summary form. The detail-only fields are
            # reconstructed on demand by ``environment/get`` so the
            # index stays compact and the truth lives on disk.
            summary = Environment(
                id=new_id,
                name=params.name,
                python_version=params.python_version,
            )
            envs.append(summary)
            state.atomic_write_index(envs)

        # Return the details form. ``installed_packages`` and
        # ``extra`` are present on the
        # wire even when empty; the to_jsonable serializer preserves
        # that.
        details = Environment(
            id=new_id,
            name=params.name,
            python_version=params.python_version,
            interpreter_path=str(interpreter_path),
            installed_packages=(),
            extra={},
        )
        return details.to_jsonable()

    # ------------------------------------------------------------- #
    # environment/get.
    # ------------------------------------------------------------- #

    def _environment_get(params: Any) -> Any:
        with state.lock():
            envs = state.read_index()

        match = next((e for e in envs if e.id == params.id), None)
        if match is None:
            raise WspError(
                int(WspErrorCode.ENVIRONMENT_NOT_FOUND),
                f"environment {params.id!r} not found",
                data={"id": params.id},
            )

        # Reconstruct the details form. ``interpreter_path`` is taken
        # from the on-disk layout (the index never persists it);
        # ``installed_packages`` is queried lazily; ``extra`` is the
        # always-present empty placeholder reserved for tool-specific
        # fields.
        interpreter = _venv_interpreter_path(state.envs_dir, match.id)
        packages = _list_installed_packages(interpreter)
        details = Environment(
            id=match.id,
            name=match.name,
            python_version=match.python_version,
            interpreter_path=str(interpreter),
            installed_packages=tuple(packages),
            extra={},
        )
        return details.to_jsonable()

    # ------------------------------------------------------------- #
    # environment/delete.
    # ------------------------------------------------------------- #

    def _environment_delete(params: Any) -> Any:
        with state.lock():
            envs = state.read_index()
            for index, env in enumerate(envs):
                if env.id != params.id:
                    continue
                target = state.envs_dir / env.id
                if target.exists():
                    try:
                        shutil.rmtree(target)
                    except OSError as exc:
                        # A known-id deletion that
                        # fails surfaces as ``execution-failed`` with
                        # the index left intact, so the env is still
                        # listable / gettable afterwards.
                        raise WspError(
                            int(WspErrorCode.EXECUTION_FAILED),
                            f"failed to remove environment directory: {exc}",
                            data={
                                "reason": "rmtree-failed",
                                "id": params.id,
                            },
                        ) from exc
                del envs[index]
                state.atomic_write_index(envs)
                return DeleteAck(id=params.id).to_jsonable()

        # No matching id -> environment-not-found, index unchanged.
        raise WspError(
            int(WspErrorCode.ENVIRONMENT_NOT_FOUND),
            f"environment {params.id!r} not found",
            data={"id": params.id},
        )

    # ------------------------------------------------------------- #
    # environment/execute.
    # ------------------------------------------------------------- #

    def _environment_execute(params: Any) -> Any:
        with state.lock():
            envs = state.read_index()

        match = next((e for e in envs if e.id == params.id), None)
        if match is None:
            raise WspError(
                int(WspErrorCode.ENVIRONMENT_NOT_FOUND),
                f"environment {params.id!r} not found",
                data={"id": params.id},
            )

        bin_dir = state.envs_dir / match.id / _VENV_BIN_DIR

        # Prepend the env's bin/Scripts directory to
        # PATH so executables installed inside the venv (the
        # interpreter, console-scripts) are picked up by the child.
        # The user's ``env`` overlay (when supplied) sits between the
        # parent's environment and the PATH adjustment, matching the
        # principle of least surprise: ``PATH`` from ``env`` is also
        # respected as the base, but the venv directory still wins.
        child_env: dict[str, str] = dict(os.environ)
        if params.env:
            child_env.update(params.env)
        existing_path = child_env.get("PATH", "")
        child_env["PATH"] = f"{bin_dir}{os.pathsep}{existing_path}" if existing_path else str(bin_dir)

        try:
            proc = subprocess.run(
                list(params.argv),
                check=False, cwd=params.cwd,
                env=child_env,
                capture_output=True,
                timeout=300,
            )
        except (
            FileNotFoundError,
            PermissionError,
            NotADirectoryError,
            OSError,
            subprocess.TimeoutExpired,
        ) as exc:
            # Command launch failure -> WSP error
            # ``execution-failed`` with a human-readable message in
            # ``data`` so clients can surface the cause.
            raise WspError(
                int(WspErrorCode.EXECUTION_FAILED),
                f"failed to launch command: {exc}",
                data={"reason": "spawn-failed"},
            ) from exc

        # Decode bytes with ``errors="replace"`` so
        # arbitrary child output never crashes the response path.
        stdout_bytes = proc.stdout if proc.stdout is not None else b""
        stderr_bytes = proc.stderr if proc.stderr is not None else b""
        return ExecuteResult(
            exit_code=proc.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        ).to_jsonable()

    # ------------------------------------------------------------- #
    # Registration.
    # ------------------------------------------------------------- #

    registry.register("initialize", _initialize)
    registry.register("shutdown", _shutdown)
    registry.register("environment/list", _environment_list)
    registry.register("environment/create", _environment_create)
    registry.register("environment/get", _environment_get)
    registry.register("environment/delete", _environment_delete)
    registry.register("environment/execute", _environment_execute)
    return registry

"""On-disk persistence for the WSP_CLI fallback Workflow_Tool.

The fallback Workflow_Tool stores its registry of environments in
``index.json`` under a per-host state directory, with one venv per
environment under ``envs/<id>/``. The design (see ``design.md`` ->
``cli.fallback``) calls for two guarantees:

* **Atomic writes.** A torn write must never leave ``index.json``
  half-rewritten. We write to ``index.json.tmp`` and ``os.replace``
  it, which is atomic on the same filesystem on both POSIX and
  Windows.
* **Mutual exclusion across CLI invocations.** Each WSP_CLI
  invocation is a separate process, so multiple concurrent
  invocations could otherwise race on the read-modify-write
  sequence. We acquire an exclusive advisory lock on a sentinel
  file (``flock`` on POSIX, ``msvcrt.locking`` on Windows) for the
  duration of each critical section.

The index stores the *summary* form of :class:`Environment` -- the
detail-only fields (``interpreter_path``, ``installed_packages``,
``extra``) are reconstructed lazily by the ``environment/get``
handler from the on-disk venv. ``Environment.to_jsonable`` already
omits ``None``-valued detail fields, and
``Environment.from_jsonable`` accepts entries where those fields
are absent, so the round-trip is automatic regardless of which
form callers pass in.

This module implements the persistence primitives only; the
actual ``environment/*`` handlers live in
:mod:`wispy.cli.fallback` (added in a later task).
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if os.name == "nt":  # pragma: no cover - platform-specific
    import msvcrt
else:
    import fcntl

from wispy.endpoints import Environment

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["FallbackState"]


def _default_state_dir() -> Path:
    """Resolve the on-disk fallback state directory.

    Resolution order:

    1. ``$WISPY_STATE_DIR`` if set (test override and explicit user
       opt-in).
    2. On POSIX, ``$XDG_STATE_HOME/wispy/fallback`` if set, else
       ``~/.local/state/wispy/fallback``.
    3. On Windows, ``%LOCALAPPDATA%\\wispy\\fallback``; if
       ``LOCALAPPDATA`` is somehow unset (very unusual outside of
       stripped-down CI environments), fall back to
       ``~/AppData/Local/wispy/fallback`` so we still produce a
       usable path rather than crashing.
    """
    override = os.environ.get("WISPY_STATE_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "wispy" / "fallback"
        return Path.home() / "AppData" / "Local" / "wispy" / "fallback"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else (Path.home() / ".local" / "state")
    return base / "wispy" / "fallback"


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive, blocking advisory lock on ``path``.

    POSIX uses ``fcntl.flock`` with ``LOCK_EX``; Windows uses
    ``msvcrt.locking`` with ``LK_LOCK`` over a single byte at offset
    0. The sentinel file is created if missing. Any unlock or close
    error propagates after the inner block has run, but unlocking
    happens in a ``finally`` so the lock is always released even
    when the block raises.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        with open(path, "a+b") as f:
            f.seek(0)
            # LK_LOCK is the blocking variant: it waits until the
            # byte range is acquired rather than returning EAGAIN.
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        with open(path, "a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class FallbackState:
    """On-disk persistence for the WSP_CLI fallback Workflow_Tool.

    The class is a thin facade around three paths -- ``index.json``,
    ``envs/``, and a sentinel ``.lock`` file -- plus the read /
    atomic-write / lock primitives that the fallback handlers
    compose on top.

    Instances are cheap; the state directory and the ``envs/``
    subdirectory are created lazily by :meth:`ensure_layout`,
    :meth:`atomic_write_index`, and :meth:`lock`, so constructing a
    ``FallbackState`` does not touch the filesystem.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else _default_state_dir()
        self.envs_dir = self.root / "envs"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".lock"

    def ensure_layout(self) -> None:
        """Create the state directory tree if it does not exist yet."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.envs_dir.mkdir(parents=True, exist_ok=True)

    def read_index(self) -> list[Environment]:
        """Return the environments recorded in ``index.json``.

        Returns ``[]`` when the file is absent (a fresh install or a
        Workflow_Tool that has never created an env). A present
        ``index.json`` whose top level is not a JSON list is treated
        as corruption and surfaces a ``TypeError``; callers are
        expected to translate that to a ``WspError`` at the handler
        layer.
        """
        try:
            text = self.index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            msg = f"index.json must contain a JSON list at top level; got {type(data).__name__}"
            raise TypeError(msg)
        return [Environment.from_jsonable(entry) for entry in data]

    def atomic_write_index(self, envs: list[Environment]) -> None:
        """Atomically rewrite ``index.json`` with ``envs``.

        We serialize through ``Environment.to_jsonable`` so callers
        may pass either summary- or details-form ``Environment``
        instances. The design treats the summary form as canonical
        for the index and reconstructs detail fields lazily, but the
        helpers round-trip both shapes, so this method does not need
        to police that choice.
        """
        self.ensure_layout()
        tmp = self.index_path.with_suffix(".json.tmp")
        payload = json.dumps(
            [env.to_jsonable() for env in envs],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.index_path)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Acquire the per-host file lock around an index R-M-W."""
        self.ensure_layout()
        with _file_lock(self.lock_path):
            yield

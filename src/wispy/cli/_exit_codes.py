"""Canonical exit-code constants for the WSP_CLI.

This module exists so that :mod:`wispy.cli.client` and other CLI
internals can reference :class:`ExitCode` without having to import
:mod:`wispy.cli.main`. That avoids a circular import that would
otherwise force the dependents of :class:`ExitCode` to be loaded
lazily inside functions.
"""

from __future__ import annotations

__all__ = ["ExitCode"]


class ExitCode:
    """Canonical exit codes for the ``wsp`` CLI.

    Values match the design's exit-code table:

    * ``SUCCESS`` (0): the WSP call returned a successful result.
    * ``GENERIC_ERROR`` (1): any non-usage runtime failure (timeouts,
      JSON-RPC errors other than ``-32601``, child process crashes).
    * ``USAGE_OR_UNSUPPORTED`` (2): argparse usage errors or a target
      WSP server that responded with ``-32601`` (method not found).
    """

    SUCCESS = 0
    GENERIC_ERROR = 1
    USAGE_OR_UNSUPPORTED = 2

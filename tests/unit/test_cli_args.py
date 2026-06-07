"""Unit tests for the WSP_CLI argument parser.

Covers behaviours of :mod:`wispy.cli.main` that are easier to nail
down with concrete examples than with property tests:

* ``--tool`` and ``--config`` cannot be combined; argparse exits with
  status 2 and writes a usage error to stderr.
* The parser exposes one subcommand per user-facing WSP method
  (lifecycle methods ``initialize``/``shutdown`` are intentionally
  excluded -- the CLI wraps those automatically).
* ``--params-json -`` reads from stdin, enforces a 1 MiB cap, and
  rejects non-object payloads.
"""

from __future__ import annotations

import pytest

from wispy.cli.main import (
    _read_stdin_json,
    build_parser,
    main,
)


class _FakeBuffer:
    """Minimal ``sys.stdin.buffer`` replacement returning canned bytes."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out, self._data = self._data, b""
            return out
        out = self._data[:n]
        self._data = self._data[n:]
        return out


class _FakeStdin:
    """Minimal ``sys.stdin`` replacement exposing a ``buffer`` attribute."""

    def __init__(self, data: bytes) -> None:
        self.buffer = _FakeBuffer(data)


# ---------------------------------------------------------------------------
# --tool / --config mutual exclusion.
# ---------------------------------------------------------------------------


def test_tool_and_config_together_exits_2_and_writes_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--tool`` and ``--config`` together is a usage error.

    ``--tool`` is declared with ``argparse.REMAINDER``, so any flags
    placed after it are swallowed into the tool argv. Putting
    ``--config`` *before* ``--tool`` lets argparse parse both top-level
    flags before REMAINDER kicks in.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["--config", "y.toml", "--tool", "echo", "x"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    # ``parser.error`` prefixes the message with the program's usage
    # line; the explanatory text we control mentions both flag names.
    assert "--tool" in err
    assert "--config" in err


# ---------------------------------------------------------------------------
# One subcommand per user-facing WSP method.
# ---------------------------------------------------------------------------


def test_subcommand_per_user_facing_wsp_method() -> None:
    """The parser exposes a subparser for every user-facing method.

    Lifecycle methods (``initialize``/``shutdown``) are intentionally
    excluded -- the CLI wraps those automatically around every call.
    """
    parser = build_parser()

    # Argparse stores subparser choices on the ``_SubParsersAction``
    # registered with the parser. This is implementation-detail
    # introspection but it is the standard way to enumerate the
    # subcommands argparse knows about.
    sub_action = next(
        a
        for a in parser._subparsers._group_actions  # type: ignore[union-attr]  # noqa: SLF001 - testing argparse internals
        if a.choices
    )
    expected = {
        "environment/list",
        "environment/create",
        "environment/get",
        "environment/delete",
        "environment/execute",
    }
    assert expected.issubset(set(sub_action.choices.keys()))
    # No lifecycle methods leak into the CLI surface.
    assert "initialize" not in sub_action.choices
    assert "shutdown" not in sub_action.choices


# ---------------------------------------------------------------------------
# --params-json - stdin handling.
# ---------------------------------------------------------------------------


def test_params_json_rejects_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stdin payload larger than 1 MiB is a usage error."""
    parser = build_parser()
    big = b"x" * (1024 * 1024 + 1)
    monkeypatch.setattr("sys.stdin", _FakeStdin(big))
    with pytest.raises(SystemExit) as excinfo:
        _read_stdin_json(parser)
    assert excinfo.value.code == 2


def test_params_json_reads_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small JSON object on stdin is parsed and returned as a dict."""
    parser = build_parser()
    monkeypatch.setattr("sys.stdin", _FakeStdin(b'{"foo": "bar"}'))
    result = _read_stdin_json(parser)
    assert result == {"foo": "bar"}


def test_params_json_rejects_non_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-object JSON value (e.g. a string) is a usage error."""
    parser = build_parser()
    monkeypatch.setattr("sys.stdin", _FakeStdin(b'"hello"'))
    with pytest.raises(SystemExit) as excinfo:
        _read_stdin_json(parser)
    assert excinfo.value.code == 2


def test_params_json_empty_stdin_returns_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty stdin is treated as "no params" and returns ``{}``.

    Documents the contract used by callers that always opt into
    ``--params-json -`` regardless of whether the user piped anything.
    """
    parser = build_parser()
    monkeypatch.setattr("sys.stdin", _FakeStdin(b""))
    assert _read_stdin_json(parser) == {}

"""Unit tests for :func:`wispy.config.load_config` rejection paths.

Each rejection reason from the Config_File startup-error
table gets one focused test that writes a temporary file with the
minimum content needed to exercise the failure path, then asserts that
:func:`load_config` raises :class:`~wispy.config.ConfigError` with a
message that names the offending detail (file extension, method name,
``PATH``, etc.).

The final test pins down the equivalence between the TOML and JSON
front ends: the same logical schema written in both formats MUST
produce equal ``list[ConfigEntry]`` results, so callers can pick the
format their toolchain prefers without any semantic drift.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from wispy.config import (
    ConfigEntry,
    ConfigError,
    PythonHandlerSpec,
    load_config,
)

if TYPE_CHECKING:
    from pathlib import Path

# A dotted path that is guaranteed to be importable and callable in any
# CPython runtime, used as the fixture for tests that need a *valid*
# Python handler spec while exercising an unrelated rejection path or
# the TOML/JSON equivalence check.
_VALID_IMPORT = "json.dumps"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Extension dispatch
# ---------------------------------------------------------------------------


def test_unsupported_extension_rejected(tmp_path: Path) -> None:
    """``.yaml`` is not a supported Config_File extension."""
    p = _write(
        tmp_path / "cfg.yaml",
        json.dumps({"handlers": {"initialize": {"import": _VALID_IMPORT}}}),
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert "unsupported" in msg or ".yaml" in msg


# ---------------------------------------------------------------------------
# Parse failures
# ---------------------------------------------------------------------------


def test_toml_parse_failure_rejected(tmp_path: Path) -> None:
    """A malformed ``.toml`` file surfaces as ConfigError."""
    p = _write(tmp_path / "bad.toml", "not = =\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert "TOML" in msg or "parse" in msg


def test_json_parse_failure_rejected(tmp_path: Path) -> None:
    """A malformed ``.json`` file surfaces as ConfigError."""
    p = _write(tmp_path / "bad.json", "{not json}")
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert "JSON" in msg or "parse" in msg


# ---------------------------------------------------------------------------
# Unknown WSP method
# ---------------------------------------------------------------------------


def test_unknown_wsp_method_rejected(tmp_path: Path) -> None:
    """An unrecognized method name is named in the rejection message."""
    p = _write(
        tmp_path / "cfg.toml",
        '[handlers]\n"fake/method" = { import = "os.path.join" }\n',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    assert "fake/method" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Handler resolution
# ---------------------------------------------------------------------------


def test_unresolvable_import_path_rejected(tmp_path: Path) -> None:
    """A dotted path whose module does not exist is rejected."""
    p = _write(
        tmp_path / "cfg.toml",
        '[handlers]\ninitialize = { import = "wispy.nonexistent.module.thing" }\n',
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_subprocess_argv0_not_on_path_rejected(tmp_path: Path) -> None:
    """``command[0]`` not resolvable on PATH is named in the message."""
    p = _write(
        tmp_path / "cfg.toml",
        '[handlers]\ninitialize = { command = ["definitely-not-on-path-asdfqwer"] }\n',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    assert "PATH" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Handler entry shape
# ---------------------------------------------------------------------------


def test_handler_with_both_import_and_command_rejected(
    tmp_path: Path,
) -> None:
    """An entry that specifies both ``import`` and ``command`` is rejected."""
    p = _write(
        tmp_path / "cfg.toml",
        '[handlers]\ninitialize = { import = "json.dumps", command = ["echo"] }\n',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert "import" in msg
    assert "command" in msg


def test_handler_with_neither_import_nor_command_rejected(
    tmp_path: Path,
) -> None:
    """An entry that specifies neither ``import`` nor ``command`` is rejected."""
    p = _write(
        tmp_path / "cfg.toml",
        "[handlers]\ninitialize = { }\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    msg = str(excinfo.value)
    assert "import" in msg
    assert "command" in msg


def test_handler_entry_with_unknown_key_rejected(tmp_path: Path) -> None:
    """An entry with a key outside ``{import, command}`` is rejected."""
    p = _write(
        tmp_path / "cfg.toml",
        '[handlers]\ninitialize = { import = "json.dumps", extra = "boom" }\n',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(p)
    assert "extra" in str(excinfo.value)


# ---------------------------------------------------------------------------
# TOML / JSON equivalence
# ---------------------------------------------------------------------------


def test_toml_and_json_identical_schemas_produce_equal_entries(
    tmp_path: Path,
) -> None:
    """The same logical config in TOML and JSON yields equal entry lists."""
    toml_path = _write(
        tmp_path / "cfg.toml",
        f'[handlers]\ninitialize = {{ import = "{_VALID_IMPORT}" }}\n',
    )
    json_path = _write(
        tmp_path / "cfg.json",
        json.dumps({"handlers": {"initialize": {"import": _VALID_IMPORT}}}),
    )

    toml_entries = load_config(toml_path)
    json_entries = load_config(json_path)

    expected = [
        ConfigEntry(
            method="initialize",
            spec=PythonHandlerSpec(import_path=_VALID_IMPORT),
        )
    ]
    assert toml_entries == expected
    assert json_entries == expected
    assert toml_entries == json_entries

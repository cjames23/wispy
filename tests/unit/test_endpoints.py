"""Unit tests for the WSP endpoint parameter validators.

Covers ``WSP_METHODS[name].validate_params`` for every method registered
in :mod:`wispy.endpoints`. For each method this module asserts:

* a passing example, where the validator returns the expected
  normalized params dataclass with the expected field values;
* one failing example per documented rule, where the validator
  returns a ``list[Violation]`` containing that rule name; and
* (for ``environment/create``) that multiple violations from a single
  call are accumulated into one returned list.
"""

from __future__ import annotations

from typing import Any

import pytest

from wispy.endpoints import (
    ENVIRONMENT_ID_MAX_LEN,
    ENVIRONMENT_NAME_MAX_LEN,
    ENVIRONMENT_PYTHON_VERSION_MAX_LEN,
    INITIALIZE_CLIENT_NAME_MAX_LEN,
    INITIALIZE_CLIENT_PROTOCOL_VERSION_MAX_LEN,
    WSP_METHODS,
    CreateEnvironmentParams,
    DeleteEnvironmentParams,
    ExecuteParams,
    GetEnvironmentParams,
    InitializeParams,
)

# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _validate(method: str, params: Any) -> Any:
    """Run the registered validator for ``method`` on ``params``."""
    return WSP_METHODS[method].validate_params(params)


def _assert_violation(result: Any, rule: str) -> None:
    """Assert ``result`` is a list[Violation] containing ``rule``."""
    assert isinstance(result, list), f"expected list[Violation] failure, got success value: {result!r}"
    assert rule in result, f"expected rule {rule!r} in violation list, got {result!r}"


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitializeValidator:
    """Initialize validator tests."""

    def test_passing_example_returns_normalized_params(self) -> None:
        result = _validate(
            "initialize",
            {"client_name": "wsp-cli", "client_protocol_version": "0.1.0"},
        )
        assert result == InitializeParams(client_name="wsp-cli", client_protocol_version="0.1.0")

    def test_params_not_object_when_params_is_array(self) -> None:
        # JSON-RPC positional params are not supported; a JSON array
        # should yield a single ``params-not-object`` violation.
        result = _validate("initialize", ["wsp-cli", "0.1.0"])
        _assert_violation(result, "params-not-object")

    def test_client_name_required_when_missing(self) -> None:
        result = _validate("initialize", {"client_protocol_version": "0.1.0"})
        _assert_violation(result, "client-name-required")

    def test_client_name_type_when_not_a_string(self) -> None:
        result = _validate(
            "initialize",
            {"client_name": 42, "client_protocol_version": "0.1.0"},
        )
        _assert_violation(result, "client-name-type")

    def test_client_name_length_when_empty(self) -> None:
        # Length 0 is below the inclusive lower bound of 1.
        result = _validate(
            "initialize",
            {"client_name": "", "client_protocol_version": "0.1.0"},
        )
        _assert_violation(result, "client-name-length")

    def test_client_name_length_when_above_max(self) -> None:
        # Length above the inclusive upper bound of 255 is rejected.
        too_long = "x" * (INITIALIZE_CLIENT_NAME_MAX_LEN + 1)
        result = _validate(
            "initialize",
            {
                "client_name": too_long,
                "client_protocol_version": "0.1.0",
            },
        )
        _assert_violation(result, "client-name-length")

    def test_client_name_length_at_upper_bound_is_accepted(self) -> None:
        # The bound itself is inclusive: 255 chars is still valid.
        ok = "x" * INITIALIZE_CLIENT_NAME_MAX_LEN
        result = _validate(
            "initialize",
            {"client_name": ok, "client_protocol_version": "0.1.0"},
        )
        assert isinstance(result, InitializeParams)
        assert result.client_name == ok

    def test_client_protocol_version_required_when_missing(self) -> None:
        result = _validate("initialize", {"client_name": "wsp-cli"})
        _assert_violation(result, "client-protocol-version-required")

    def test_client_protocol_version_type_when_not_a_string(self) -> None:
        result = _validate(
            "initialize",
            {"client_name": "wsp-cli", "client_protocol_version": 1},
        )
        _assert_violation(result, "client-protocol-version-type")

    def test_client_protocol_version_length_when_empty(self) -> None:
        result = _validate(
            "initialize",
            {"client_name": "wsp-cli", "client_protocol_version": ""},
        )
        _assert_violation(result, "client-protocol-version-length")

    def test_client_protocol_version_length_when_above_max(self) -> None:
        too_long = "v" * (INITIALIZE_CLIENT_PROTOCOL_VERSION_MAX_LEN + 1)
        result = _validate(
            "initialize",
            {
                "client_name": "wsp-cli",
                "client_protocol_version": too_long,
            },
        )
        _assert_violation(result, "client-protocol-version-length")

    def test_client_protocol_version_length_at_upper_bound_is_accepted(
        self,
    ) -> None:
        ok = "v" * INITIALIZE_CLIENT_PROTOCOL_VERSION_MAX_LEN
        result = _validate(
            "initialize",
            {"client_name": "wsp-cli", "client_protocol_version": ok},
        )
        assert isinstance(result, InitializeParams)
        assert result.client_protocol_version == ok


# ---------------------------------------------------------------------------
# shutdown and environment/list (no-params validators)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["shutdown", "environment/list"])
class TestNoParamsValidators:
    """Validators for methods that accept no parameters."""

    def test_passing_example_with_omitted_params(self, method: str) -> None:
        # ``params`` may be omitted entirely (parsed as ``None``).
        assert _validate(method, None) is None

    def test_passing_example_with_empty_object(self, method: str) -> None:
        assert _validate(method, {}) is None

    def test_passing_example_with_empty_array(self, method: str) -> None:
        assert _validate(method, []) is None

    def test_params_not_empty_with_non_empty_object(self, method: str) -> None:
        result = _validate(method, {"unexpected": True})
        _assert_violation(result, "params-not-empty")

    def test_params_not_empty_with_non_empty_array(self, method: str) -> None:
        result = _validate(method, [1])
        _assert_violation(result, "params-not-empty")

    def test_params_not_empty_with_scalar(self, method: str) -> None:
        result = _validate(method, "nope")
        _assert_violation(result, "params-not-empty")


# ---------------------------------------------------------------------------
# environment/create
# ---------------------------------------------------------------------------


class TestCreateEnvironmentValidator:
    """environment/create validator tests."""

    def test_passing_example_returns_normalized_params(self) -> None:
        result = _validate(
            "environment/create",
            {"name": "scratch", "python_version": "3.12"},
        )
        assert result == CreateEnvironmentParams(name="scratch", python_version="3.12")

    def test_passing_example_with_three_part_version(self) -> None:
        result = _validate(
            "environment/create",
            {"name": "scratch", "python_version": "3.12.4"},
        )
        assert isinstance(result, CreateEnvironmentParams)
        assert result.python_version == "3.12.4"

    def test_params_not_object_when_params_is_array(self) -> None:
        result = _validate("environment/create", ["scratch", "3.12"])
        _assert_violation(result, "params-not-object")

    def test_name_required_when_missing(self) -> None:
        result = _validate("environment/create", {"python_version": "3.12"})
        _assert_violation(result, "name-required")

    def test_name_required_when_empty_string(self) -> None:
        result = _validate(
            "environment/create",
            {"name": "", "python_version": "3.12"},
        )
        _assert_violation(result, "name-required")

    def test_name_required_when_not_a_string(self) -> None:
        # Non-string ``name`` collapses into ``name-required`` per the
        # validator's documented surface area.
        result = _validate(
            "environment/create",
            {"name": 17, "python_version": "3.12"},
        )
        _assert_violation(result, "name-required")

    def test_name_length_when_above_max(self) -> None:
        too_long = "n" * (ENVIRONMENT_NAME_MAX_LEN + 1)
        result = _validate(
            "environment/create",
            {"name": too_long, "python_version": "3.12"},
        )
        _assert_violation(result, "name-length")

    def test_python_version_required_when_missing(self) -> None:
        result = _validate("environment/create", {"name": "scratch"})
        _assert_violation(result, "python-version-required")

    def test_python_version_required_when_empty_string(self) -> None:
        result = _validate(
            "environment/create",
            {"name": "scratch", "python_version": ""},
        )
        _assert_violation(result, "python-version-required")

    def test_python_version_required_when_not_a_string(self) -> None:
        result = _validate(
            "environment/create",
            {"name": "scratch", "python_version": 312},
        )
        _assert_violation(result, "python-version-required")

    @pytest.mark.parametrize(
        "bad_version",
        [
            "3",  # missing .MINOR
            "3.x",  # non-integer minor
            "3.12.4-rc1",  # extra suffix
            "03.12",  # leading zero on major
            "3.012",  # leading zero on minor
            "v3.12",  # spurious prefix
        ],
    )
    def test_python_version_format_when_malformed(self, bad_version: str) -> None:
        result = _validate(
            "environment/create",
            {"name": "scratch", "python_version": bad_version},
        )
        _assert_violation(result, "python-version-format")

    def test_python_version_format_when_above_length_cap(self) -> None:
        # A syntactically well-formed version that exceeds the length
        # cap still trips ``python-version-format``.
        long_version = "1." + "1" * ENVIRONMENT_PYTHON_VERSION_MAX_LEN
        assert len(long_version) > ENVIRONMENT_PYTHON_VERSION_MAX_LEN
        result = _validate(
            "environment/create",
            {"name": "scratch", "python_version": long_version},
        )
        _assert_violation(result, "python-version-format")

    def test_violations_accumulate_in_a_single_list(self) -> None:
        # Missing both required fields -> both rule names returned in
        # the same call. No short-circuiting.
        result = _validate("environment/create", {})
        assert isinstance(result, list)
        assert "name-required" in result
        assert "python-version-required" in result

    def test_name_length_and_python_version_format_accumulate(
        self,
    ) -> None:
        # An over-long name AND a malformed python_version together:
        # the validator should report both.
        result = _validate(
            "environment/create",
            {
                "name": "n" * (ENVIRONMENT_NAME_MAX_LEN + 1),
                "python_version": "not-a-version",
            },
        )
        assert isinstance(result, list)
        assert "name-length" in result
        assert "python-version-format" in result


# ---------------------------------------------------------------------------
# environment/get and environment/delete (id-only validators)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "params_cls"),
    [
        ("environment/get", GetEnvironmentParams),
        ("environment/delete", DeleteEnvironmentParams),
    ],
)
class TestIdOnlyValidators:
    """Validators for endpoints that take only an environment ``id``."""

    def test_passing_example_returns_normalized_params(self, method: str, params_cls: type) -> None:
        result = _validate(method, {"id": "env-1"})
        assert result == params_cls(id="env-1")

    def test_params_not_object_when_params_is_array(self, method: str, params_cls: type) -> None:  # noqa: ARG002 - parametrize name required
        result = _validate(method, ["env-1"])
        _assert_violation(result, "params-not-object")

    def test_id_required_when_missing(self, method: str, params_cls: type) -> None:  # noqa: ARG002 - parametrize name required
        result = _validate(method, {})
        _assert_violation(result, "id-required")

    def test_id_required_when_empty_string(self, method: str, params_cls: type) -> None:  # noqa: ARG002 - parametrize name required
        result = _validate(method, {"id": ""})
        _assert_violation(result, "id-required")

    def test_id_required_when_not_a_string(self, method: str, params_cls: type) -> None:  # noqa: ARG002 - parametrize name required
        result = _validate(method, {"id": 7})
        _assert_violation(result, "id-required")

    def test_id_length_when_above_max(self, method: str, params_cls: type) -> None:  # noqa: ARG002 - parametrize name required
        too_long = "i" * (ENVIRONMENT_ID_MAX_LEN + 1)
        result = _validate(method, {"id": too_long})
        _assert_violation(result, "id-length")

    def test_id_length_at_upper_bound_is_accepted(self, method: str, params_cls: type) -> None:
        ok = "i" * ENVIRONMENT_ID_MAX_LEN
        result = _validate(method, {"id": ok})
        assert result == params_cls(id=ok)


# ---------------------------------------------------------------------------
# environment/execute
# ---------------------------------------------------------------------------


class TestExecuteValidator:
    """environment/execute validator tests."""

    def test_passing_example_minimal_params(self) -> None:
        result = _validate(
            "environment/execute",
            {"id": "env-1", "argv": ["python", "-V"]},
        )
        assert result == ExecuteParams(
            id="env-1",
            argv=("python", "-V"),
            cwd=None,
            env=None,
        )

    def test_passing_example_with_cwd_and_env(self) -> None:
        result = _validate(
            "environment/execute",
            {
                "id": "env-1",
                "argv": ["python", "-V"],
                "cwd": "/tmp",
                "env": {"FOO": "bar"},
            },
        )
        assert isinstance(result, ExecuteParams)
        assert result.id == "env-1"
        assert result.argv == ("python", "-V")
        assert result.cwd == "/tmp"
        assert result.env == {"FOO": "bar"}

    def test_params_not_object_when_params_is_array(self) -> None:
        result = _validate("environment/execute", ["env-1", ["python", "-V"]])
        _assert_violation(result, "params-not-object")

    # ---- id rules ----------------------------------------------------

    def test_id_required_when_missing(self) -> None:
        result = _validate("environment/execute", {"argv": ["python"]})
        _assert_violation(result, "id-required")

    def test_id_required_when_empty(self) -> None:
        result = _validate("environment/execute", {"id": "", "argv": ["python"]})
        _assert_violation(result, "id-required")

    def test_id_length_when_above_max(self) -> None:
        too_long = "i" * (ENVIRONMENT_ID_MAX_LEN + 1)
        result = _validate(
            "environment/execute",
            {"id": too_long, "argv": ["python"]},
        )
        _assert_violation(result, "id-length")

    # ---- argv rules --------------------------------------------------

    def test_argv_required_when_missing(self) -> None:
        result = _validate("environment/execute", {"id": "env-1"})
        _assert_violation(result, "argv-required")

    def test_argv_type_when_not_a_list(self) -> None:
        result = _validate(
            "environment/execute",
            {"id": "env-1", "argv": "python -V"},
        )
        _assert_violation(result, "argv-type")

    def test_argv_empty(self) -> None:
        # Empty argv is rejected as Invalid params.
        result = _validate("environment/execute", {"id": "env-1", "argv": []})
        _assert_violation(result, "argv-empty")

    def test_argv_element_type_when_non_string_element(self) -> None:
        # Any non-string element is rejected.
        result = _validate(
            "environment/execute",
            {"id": "env-1", "argv": ["python", 7]},
        )
        _assert_violation(result, "argv-element-type")

    # ---- cwd / env rules --------------------------------------------

    def test_cwd_type_when_not_a_string(self) -> None:
        result = _validate(
            "environment/execute",
            {"id": "env-1", "argv": ["python"], "cwd": 7},
        )
        _assert_violation(result, "cwd-type")

    def test_env_type_when_not_a_mapping(self) -> None:
        result = _validate(
            "environment/execute",
            {
                "id": "env-1",
                "argv": ["python"],
                "env": ["FOO=bar"],
            },
        )
        _assert_violation(result, "env-type")

    def test_env_entry_type_when_value_is_not_a_string(self) -> None:
        result = _validate(
            "environment/execute",
            {
                "id": "env-1",
                "argv": ["python"],
                "env": {"FOO": 7},
            },
        )
        _assert_violation(result, "env-entry-type")

# wispy

> The whitespace between your workflow tool and everything that talks to it.

`wispy` is a Python library and command-line client for the **Workflow Server
Protocol (WSP)** — a small JSON-RPC 2.0 protocol that lets workflow tools
(environment managers, task runners, test runners, and so on) expose their
capabilities to consuming tools through a uniform interface, in the same spirit
as the Language Server Protocol does for editors.

If you build a workflow tool, `wispy` lets you ship a WSP server with one
import. If you want to talk to one, the bundled `wsp` CLI gives you a working
client out of the box, plus a built-in fallback workflow tool backed by
`venv` so the CLI is useful before any tool is installed.

- [Documentation](https://cjames23.github.io/wispy/)
- [Source](https://github.com/cjames23/wispy/)
- [Issues](https://github.com/cjames23/wispy/issues)

## Highlights

- **Standard JSON-RPC 2.0** over LSP-style `Content-Length` framing on stdio —
  any JSON-RPC client library can talk to a wispy server.
- **Two integration paths.** Take a runtime dependency and register Python
  callables, or stay decoupled with a TOML/JSON config file that points at
  imports or external commands.
- **Batteries-included CLI.** `wsp` performs the per-call lifecycle
  (`initialize` → method → `shutdown` → `exit`) and translates JSON-RPC errors
  into well-defined exit codes.
- **Working fallback workflow tool.** With no target configured, `wsp` manages
  on-disk virtual environments through `environment/list`, `create`, `get`,
  `delete`, and `execute`, persisted across invocations.
- **Verified by property-based tests.** The dispatcher, framing, lifecycle FSM,
  registry, and error model are exercised under `hypothesis`, with integration
  tests covering the full transport on top.

## Install

```sh
pip install wispy
```

`wispy` requires Python 3.10 or newer and has no runtime dependencies on
3.11+ (3.10 pulls in `tomli` for TOML parsing).

## Quickstart

### Use the `wsp` CLI against the built-in fallback

With nothing else configured, the CLI runs the in-process fallback workflow
tool, which uses `venv` to manage environments under
`$WISPY_STATE_DIR` (defaults to `$XDG_STATE_HOME/wispy/fallback/` on POSIX,
`%LOCALAPPDATA%\wispy\fallback\` on Windows).

```sh
# Create an environment.
wsp environment/create --name scratch --python-version 3.12

# List environments.
wsp environment/list

# Run a command inside one.
echo '{"id":"<env-id>","argv":["python","-V"]}' | wsp environment/execute --params-json -

# Delete it.
wsp environment/delete --id <env-id>
```

### Embed `wispy` in a workflow tool

```python
import asyncio

from wispy import Capabilities, HandlerRegistry, run_stdio


def initialize(_params):
    return Capabilities(
        methods=tuple(registry.methods()),
        protocol_version="0.1.0",
    ).to_jsonable()


def list_envs(_params):
    return [
        {"id": "default", "name": "default", "python_version": "3.12"},
    ]


registry = HandlerRegistry()
registry.register("initialize", initialize)
registry.register("shutdown", lambda _params: None)
registry.register("environment/list", list_envs)

raise SystemExit(asyncio.run(run_stdio(registry)))
```

### Or describe a server with a Config_File

If you don't want a runtime dependency on `wispy`, ship a TOML file that
maps WSP methods to your CLI's subcommands. wispy renders the request
params into argv with `{name}`-style substitution and constructs the WSP
result per a per-method `result` mode.

```toml
# my-tool.toml
[handlers."environment/list"]
command = ["my-tool", "env", "show", "--json"]
result = "json"

[handlers."environment/create"]
command = ["my-tool", "env", "create", "{name}", "-p", "{python_version}"]
result = "template"
template = { id = "{name}", name = "{name}", python_version = "{python_version}", interpreter_path = "", installed_packages = [], extra = {} }

[handlers."environment/delete"]
command = ["my-tool", "env", "remove", "{id}"]
result = "template"
template = { id = "{id}" }

[handlers."environment/execute"]
command = ["my-tool", "run", "-e", "{id}", "--", "{argv}"]
result = "exec"
```

Then any client can launch it with:

```sh
python -m wispy --config my-tool.toml
```

`wsp` knows how to drive that too:

```sh
wsp --config my-tool.toml environment/list
```

See [Ship a Config_File](docs/guides/config-file.md) for the full schema
and [Adapt an existing CLI](docs/guides/adapt-cli.md) for a worked
example walking through how Hatch could expose its `env` subcommands as
a WSP server with no Python glue.

## CLI exit codes

| Status | Meaning                                                                  |
|-------:|--------------------------------------------------------------------------|
| `0`    | The call succeeded; the JSON result was written to stdout.               |
| `1`    | The server returned a JSON-RPC error (other than method-not-found), or the call timed out. The error object is on stderr. |
| `2`    | Usage error, or the server reported `-32601` (method unsupported).       |

## Documentation

The full documentation lives in [`docs/`](./docs) and is built with
[Zensical](https://zensical.org/). To preview it locally:

```sh
hatch run docs:serve
```

To build a static site:

```sh
hatch run docs:build
```

The published site is at <https://cjames23.github.io/wispy/>.

## Development

```sh
# Run the test matrix in isolated environments.
hatch test

# Lint and type-check.
hatch check code
hatch check types

# Build the documentation locally.
hatch run docs:serve
```

The test suite uses property-based testing extensively (see
[`tests/property/`](./tests/property/)) and is structured so that pure logic —
the JSON-RPC codec, the lifecycle FSM, the dispatcher, the registry, the error
model — is verified against universal properties before any I/O is wired in.

## License

`wispy` is released under the [MIT License](./LICENSE).

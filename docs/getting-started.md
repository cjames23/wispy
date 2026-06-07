# Getting started

## Install

`wispy` is published on PyPI and supports Python 3.10 and newer.

```sh
pip install wispy
```

That installs both the `wispy` Python library and the `wsp` command-line
client.

If you're working from a checkout, an editable install works the same way:

```sh
pip install -e .
```

## Try the CLI

With nothing else configured, `wsp` runs a built-in workflow tool that uses
Python's `venv` module to manage real on-disk environments.

```sh
# Confirm the install.
wsp --help

# Create a virtual environment named "scratch" using the host's Python 3.12.
wsp environment/create --name scratch --python-version 3.12

# List what's there.
wsp environment/list

# Pull the full details of one.
wsp environment/get --id <env-id>

# Run a command inside it. Use --params-json - to pass an argv array.
echo '{"id": "<env-id>", "argv": ["python", "-V"]}' \
  | wsp environment/execute --params-json -

# Tear it down.
wsp environment/delete --id <env-id>
```

The CLI prints the JSON result of each call to stdout on success, the
JSON-RPC error object to stderr on failure, and chooses an exit code based on
the response — see [Exit codes](reference/exit-codes.md).

State for the fallback tool lives under `${WISPY_STATE_DIR}` if you set it,
otherwise under the standard XDG / `LOCALAPPDATA` location. Pointing
`WISPY_STATE_DIR` at a temp directory is the recommended way to isolate
experiments.

## Drive a tool you wrote

Two paths, depending on whether you want a runtime dependency on `wispy`:

- **Programmatic:** import `wispy`, build a `HandlerRegistry`, register your
  Python callables, and call `run_stdio`. This is the right choice when your
  tool is already in Python and a small dependency is acceptable. Continue
  with [Embed wispy in a workflow tool](guides/embed-library.md).
- **Config_File:** ship a TOML or JSON file that maps WSP method names to
  Python dotted paths or external commands; consumers run
  `python -m wispy --config <path>`. This is the right choice when you want
  zero runtime coupling to `wispy`, or when your handlers live in a different
  language. Continue with [Ship a Config_File](guides/config-file.md).

## Next steps

- Read the [Protocol overview](concepts/protocol.md) to understand the
  request/response shapes and the data model.
- Read the [Lifecycle](concepts/lifecycle.md) page to understand how the
  `initialize` / `shutdown` / `exit` handshake gates everything else.
- Skim [WSP methods](reference/methods.md) for the per-method parameter and
  result schemas.

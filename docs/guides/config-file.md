# Ship a Config_File

If you don't want to depend on `wispy` at runtime — for example because your
tool is implemented in another language, or because you'd rather not pin a
Python library version — you can describe a WSP server with a TOML or JSON
config file and let `wispy` itself launch it.

## Schema

The schema is the same in both formats. The top-level document is an
object; the `handlers` key maps WSP method names to *handler entries*; each
handler entry has exactly one of `import` or `command`.

### TOML

```toml
# my-tool.toml
[handlers]
"environment/list"   = { import = "my_tool.handlers:list_envs" }
"environment/create" = { command = ["my-tool-helper", "create"] }
"environment/get"    = { command = ["my-tool-helper", "get"] }
```

### JSON

```json
{
  "handlers": {
    "environment/list":   {"import": "my_tool.handlers.list_envs"},
    "environment/create": {"command": ["my-tool-helper", "create"]},
    "environment/get":    {"command": ["my-tool-helper", "get"]}
  }
}
```

The extension is matched case-insensitively (`.toml` or `.json`); any other
extension is rejected at startup.

## Handler kinds

### Python imports

```toml
"environment/list" = { import = "my_tool.handlers.list_envs" }
```

The dotted path is resolved with `importlib.import_module` plus
`getattr`. The trailing component must point at a callable. The handler
receives the request `params` as a single argument and returns a
JSON-serializable value, exactly the same contract as the programmatic
flow.

The path is resolved at config-load time. If the import fails or the
resolved object isn't callable, `wispy` exits with status `1` and writes
`wispy: config error: <path>: <reason>` to stderr — *before* the transport
is constructed, so the client never sees a partially-initialized server.

### Subprocess commands

```toml
"environment/create" = { command = ["my-tool-helper", "create"] }
```

`command` is a non-empty array of strings. Per request, `wispy`:

1. Spawns the command with `asyncio.create_subprocess_exec` and pipes
   captured stdin, stdout, and stderr.
2. Writes `json.dumps(params).encode("utf-8")` on the subprocess's stdin
   and closes the pipe.
3. Waits up to **30 seconds** for the subprocess to exit.
4. On clean exit (return code 0) with valid JSON on stdout, returns the
   parsed value as the JSON-RPC result.
5. On any failure (non-zero exit, invalid JSON, timeout), kills the
   subprocess if necessary and raises `WspError(EXECUTION_FAILED)` with
   diagnostic context — captured stderr, return code, and a `reason`
   discriminator — in the error's `data` field.

The first element of `command` (`argv[0]`) is resolved on `PATH` at
startup with `shutil.which`. If it's not found, the server exits with the
same `wispy: config error:` line as for unresolvable imports.

## Validation

At startup, `wispy` validates:

- The file extension and parse.
- That the document is an object and `handlers` is an object.
- That every method name is a defined WSP method.
- That every handler entry has exactly one of `import` and `command`,
  and no unknown keys.
- That every `import` resolves to a callable.
- That every `command[0]` resolves on `PATH`.

Any failure is reported on stderr and the process exits with status `1`
without serving any requests. See
[Error model](../concepts/errors.md) for what happens once requests are
flowing.

## Launching

Once you have a config file, any WSP client can launch your tool with:

```sh
python -m wispy --config my-tool.toml
```

Or via the bundled CLI:

```sh
wsp --config my-tool.toml environment/list
wsp --config my-tool.toml environment/create --name scratch --python-version 3.12
```

## What's not in the config

Two things WSP defines but the Config_File doesn't carry:

- **`initialize` and `shutdown`.** `wispy` registers built-in handlers for
  these automatically. You don't write them, and the Config_File rejects
  attempts to override them.
- **The `exit` notification.** It's a transport-level concern, not a
  handler.

## Method not configured

If a client requests a defined WSP method that your Config_File doesn't
bind, the server returns JSON-RPC error `-32601` (method not found). The
`Capabilities` object returned from `initialize` only advertises the
methods you explicitly bound, so well-behaved clients will skip those
calls in the first place.

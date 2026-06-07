# Exit codes

The `wsp` CLI uses three exit codes.

| Status | Constant                  | Meaning                                                                                                          |
|-------:|---------------------------|------------------------------------------------------------------------------------------------------------------|
| `0`    | `ExitCode.SUCCESS`        | The WSP call returned a successful result. The JSON `result` was written to stdout.                              |
| `1`    | `ExitCode.GENERIC_ERROR`  | Any non-usage runtime failure: a JSON-RPC error other than `-32601`, a launch or per-call timeout, or a crash. The JSON-RPC error object is on stderr. |
| `2`    | `ExitCode.USAGE_OR_UNSUPPORTED` | Argparse usage error, or the server reported `-32601` (method not found / not supported). A short explanation is on stderr. |

## Why `-32601` is `2`, not `1`

`-32601` is special. Every other JSON-RPC error indicates that the
request was understood and the server tried to satisfy it but failed —
the appropriate response is to surface the error and let the caller
recover (status `1`).

`-32601` instead means *the server doesn't implement this method*. From
the user's perspective that's structurally similar to "you ran the wrong
subcommand" — a configuration mistake rather than a runtime failure —
and it deserves an exit code that scripts can branch on without sniffing
the JSON.

## Programmatic access

```python
from wispy.cli.main import ExitCode

ExitCode.SUCCESS                # 0
ExitCode.GENERIC_ERROR          # 1
ExitCode.USAGE_OR_UNSUPPORTED   # 2
```

The same constants are also available from `wispy.cli._exit_codes` if
you want to import them without pulling in the rest of the CLI module.

# wispy

`wispy` is a Python library and command-line client for the **Workflow Server
Protocol (WSP)** — a small JSON-RPC 2.0 protocol that lets workflow tools
(environment managers, task runners, test runners) expose their capabilities to
consuming tools through a uniform interface.

WSP is to workflow tools roughly what the Language Server Protocol is to
editors: a stable, machine-readable surface that decouples the tool from the
client.

## Why use wispy

- **Two integration paths.** Take a runtime dependency on the library and
  register Python callables, or stay completely decoupled by writing a TOML or
  JSON config file that points at imports or external commands.
- **A complete CLI client.** The bundled `wsp` command performs the
  per-invocation lifecycle (`initialize` → method → `shutdown` → `exit`),
  translates JSON-RPC error responses into well-defined exit codes, and
  prints results as JSON.
- **A useful out-of-the-box experience.** When no target is configured, `wsp`
  serves a built-in workflow tool backed by Python's standard `venv` module
  with on-disk persistence, so you get working `environment/*` calls before
  installing anything else.
- **Conformance you can rely on.** The protocol implementation is verified
  against property-based tests covering the JSON-RPC codec, the
  Content-Length framing, the lifecycle finite-state machine, and the error
  model. See [Testing](development/testing.md) for the property catalogue.

## Where to start

- New to WSP? Begin with [Getting started](getting-started.md), then read the
  [Protocol overview](concepts/protocol.md) for the data model.
- Already have a workflow tool? Jump to
  [Embed wispy in a workflow tool](guides/embed-library.md) to wire it up in
  a few lines, or to [Ship a Config_File](guides/config-file.md) if you'd
  prefer no runtime dependency.
- Have an existing CLI you'd like to expose over WSP? Read
  [Adapt an existing CLI](guides/adapt-cli.md) for a worked example.
- Just want to run commands? Skip to [Use the wsp CLI](guides/cli.md).

## Project status

`wispy` is a young library. The first iteration ships:

- The `WSP_Server` runtime over stdio with `Content-Length` framing.
- Programmatic and Config_File handler registration.
- The lifecycle methods (`initialize`, `shutdown`, `exit`) and the
  `environment/*` family (`list`, `create`, `get`, `delete`, `execute`).
- The `wsp` CLI client and the bundled fallback workflow tool.

Future iterations will add Python interpreter installation endpoints, test
endpoints, and task endpoints. See the [WSP method reference](reference/methods.md)
for the current surface.

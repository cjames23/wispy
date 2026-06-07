# Testing

`wispy` is built around the idea that the protocol implementation should
be verified against universal properties before any I/O is wired in. The
test suite is organised in three tiers, each with a different scope and
guarantee.

## Layout

```
tests/
├── unit/           # Fast, deterministic, single-module tests.
├── property/       # Hypothesis-driven property tests.
└── integration/    # End-to-end tests that spawn real subprocesses.
```

Run any tier on its own:

```sh
hatch test --python 3.12 tests/unit/
hatch test --python 3.12 tests/property/
hatch test --python 3.12 tests/integration/
```

Or all of them together:

```sh
hatch test --python 3.12
```

## Properties under test

The property tests are the canonical specification of how the protocol
is supposed to behave. Each one corresponds to a universal claim about
the implementation:

- **JSON-RPC response well-formedness and id preservation.** Every
  response carries `jsonrpc == "2.0"`, exactly one of `result` /
  `error`, and an `id` whose JSON type and value match the request.
- **Notifications produce no response.** A request without an `id`
  field never causes the dispatcher to emit any output, even on error.
- **Error-code priority.** Inputs that satisfy more than one of
  {parse error, invalid request, method not found, invalid params}
  produce only the highest-priority code.
- **Batch ordering and notification omission.** The response array's
  length equals the number of non-notification entries, and the order
  matches the input.
- **Frame round-trip and recovery.** The framing decoder is invariant
  to chunking and recovers from arbitrary garbage prefixes.
- **Handler dispatch round-trip.** A handler bound to a method is
  invoked exactly once with the request params, and its return value
  becomes the response `result`.
- **Duplicate registration leaves the registry unchanged.** Attempting
  to register a method twice raises and the registry's state is
  bitwise-equal to its pre-call snapshot.
- **Handler exception mapping.** `WspError` round-trips, other
  `ProtocolError`s remap to `-31004`, anything else becomes `-32603`.
- **Subprocess handler round-trip and failure mapping.** Round-trip
  works for any JSON-serializable value; non-zero exit, garbled stdout,
  and timeout all surface as `WspError(EXECUTION_FAILED)`.
- **Lifecycle FSM transitions.** Driven by a `RuleBasedStateMachine`
  that mirrors the design's state diagram against a shadow model.
- **Capabilities reflect the registry exactly.** The `Capabilities`
  cached after `initialize` enumerates every registered method, and
  `protocol_version` is semver-shaped.
- **Environment endpoints behave as a model.** A
  `RuleBasedStateMachine` over `create` / `get` / `delete` / `list` /
  `execute`, with an in-memory dictionary as the oracle.
- **WSP error codes are in the reserved range.** Every member of
  `WspErrorCode` lies in `[-31999, -31000]`.
- **CLI param merge.** Stdin-supplied JSON and explicit flags merge
  the way the spec describes; flags with `None` values don't shadow.
- **CLI exit code mapping.** Successful results map to `0`, `-32601`
  to `2`, anything else to `1`.
- **Fallback Workflow_Tool persistence.** Each rule rebuilds the
  registry from scratch (simulating a fresh CLI invocation), and the
  on-disk `index.json` is the only thing that carries state forward.

## Integration tests

The integration tests spawn real Python subprocesses and exercise the
full pipeline:

- `test_stdio_transport.py` — frame round-trip and EOF drain.
- `test_lifecycle_runtime.py` — `initialize` → `shutdown` → `exit`
  status codes, and pre-`initialize` rejections.
- `test_cli_fallback_e2e.py` — full CRUD round-trip through `wsp`
  against the in-process fallback (creates a real venv on disk,
  optional and skipped when `with_pip=True` venv creation isn't
  supported on the host).
- `test_cli_modes_e2e.py` — `--config` and `--tool` modes.

## Running a single property test

Property tests are conventional pytest functions; you can run one in
isolation:

```sh
hatch test --python 3.12 tests/property/test_lifecycle_fsm.py::TestLifecycleFSM
```

To increase the example budget for a deeper run, set the Hypothesis
profile via env var:

```sh
hatch test --python 3.12 -- --hypothesis-profile=ci tests/property/
```

## Why so much property-based testing?

The pure parts of the codebase — the JSON-RPC codec, the framing
decoder, the dispatcher, the registry, the lifecycle FSM, the error
model, the environment endpoint validators — have universal claims that
*example-based* tests are bad at expressing. Hypothesis generates
adversarial inputs across the same input space and shrinks failures
into minimal counterexamples, which is exactly the right tool for
verifying a protocol implementation.

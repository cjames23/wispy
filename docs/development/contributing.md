# Contributing

`wispy` is an open-source Python project. Contributions are welcome — bug
reports, documentation fixes, new features, and performance improvements
are all in scope.

## Development setup

The project uses [Hatch](https://hatch.pypa.io/) for environment management.
Install Hatch (e.g. `pipx install hatch`), clone the repo, and you're
ready:

```sh
git clone https://github.com/cjames23/wispy.git
cd wispy
```

Hatch creates and caches isolated environments on demand. There is no
top-level `pip install` step.

## Test, lint, type-check

```sh
# Run the full test suite on every supported Python (3.10–3.14).
hatch test

# Or just on one version.
hatch test --python 3.12

# Lint.
hatch check code

# Type-check.
hatch check types
```

The test environment is intentionally separate from the project's runtime
dependencies. `pytest`, `hypothesis`, and friends live only in
`[tool.hatch.envs.hatch-test]`, so they cannot leak into a downstream
consumer that installs `wispy`.

## Documentation

```sh
# Preview the docs on http://localhost:8000.
hatch run docs:serve

# Build the static site into ./site.
hatch run docs:build
```

The site is built with [Zensical](https://zensical.org/) — see
[`zensical.toml`](https://github.com/cjames23/wispy/blob/main/zensical.toml)
for the configuration and [`docs/`](https://github.com/cjames23/wispy/tree/main/docs)
for the source.

## Code style

- Absolute imports only. Anything inside `wispy.*` imports its
  dependencies via `from wispy.X import Y`, never `from .X import Y`.
- The project uses Hatch's default ruff rule set (no project-level
  overrides). When you add new code, run `hatch check code` to make
  sure it passes.
- Type-checked with `pyrefly` in strict mode. Run `hatch check types`
  before sending a change.
- The codebase is `from __future__ import annotations` everywhere, so
  PEP 604 union syntax (`X | Y`) is fine on every supported Python.

## Pull request flow

1. Fork the repo and create a topic branch.
2. Make your change. If it's user-visible, update the docs under
   `docs/`. If it changes the public API, update
   [`docs/reference/api.md`](../reference/api.md).
3. Run `hatch test`, `hatch check code`, and `hatch check types`.
4. Commit with a focused message; small, reviewable PRs land faster.
5. Open the PR. CI runs the same three checks across the supported
   Python versions.

## Reporting issues

File issues at <https://github.com/cjames23/wispy/issues>. Useful
details:

- The Python version and operating system.
- The exact command you ran (`wsp ...`) or a minimal reproduction
  script.
- The full output, including stderr.
- For protocol-level reports, the framed bytes you sent (you can capture
  them by piping through `tee` and a small inspector).

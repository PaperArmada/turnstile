# Contributing to Turnstile

Thanks for your interest in contributing. Turnstile is pre-1.0: the core
engine is stable, the multi-actor layer is still evolving. [STABILITY.md](STABILITY.md)
names exactly which surface is which. Changes to the experimental surface are
easy to land; changes to the stable surface need an issue and discussion first,
since existing process definitions and state files depend on it.

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/PaperArmada/turnstile.git
cd turnstile
uv sync --all-packages
```

The workspace has three packages:

```
packages/
  turnstile-core/     # Engine: models, loader, validator, persistence, engine
  turnstile-mcp/      # MCP server wrapping the core
  turnstile-cli/      # CLI
```

Most changes land in `turnstile-core`. The MCP server and CLI are thin
wrappers over it, and all tests currently live in `turnstile-core/tests/`.

## Running tests

```bash
uv run pytest packages/ -v
```

This is what CI runs. Every behavior change needs a test. Tests must be
deterministic and self-contained (the suite uses `tmp_path` fixtures rather
than touching the repo's own `.processes/` or `.process-state/`).

## Validating process definitions

If you change a process definition or the loader:

```bash
uv run --package turnstile-cli turnstile validate .processes/<name>.yaml
uv run --package turnstile-cli turnstile dry-run .processes/<name>.yaml
```

`validate` checks structure; `dry-run` walks the state machine without
creating an instance.

## Code style

No formatter or linter is enforced. Match the style of the surrounding code:
Pydantic v2 models, type hints on public functions, docstrings that state
behavior rather than restate names. Keep comments for constraints the code
cannot express.

## Pull requests

- Branch from `main`, open the PR against `main`.
- CI (the test suite) must pass.
- Keep PRs focused: one change per PR. Reference the issue if one exists.
- For anything beyond a small fix, open an issue first so the approach can
  be discussed before you invest in an implementation.

## Contributing process definitions

Process definitions are the main way to contribute without touching engine
code:

- `.processes/` is the curated starter pack that ships with Turnstile. It is
  deliberately small; additions need to be broadly useful and are held to the
  design principles in [`docs/principles/`](docs/principles/).
- `examples/` holds definitions that showcase specific engine features.
  New examples are welcome.

Propose a definition with the "Process definition" issue template before
opening a PR, and include `turnstile validate` output.

## AI agent contributors

This project is built with and for AI agents. If you are an agent (or driving
one), [AGENTS.md](AGENTS.md) has the project-specific instructions, and the
repo dogfoods its own process enforcement: run `turnstile list` to see the
processes this project uses on itself.

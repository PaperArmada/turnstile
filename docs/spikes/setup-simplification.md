# Setup Simplification Spike

## Question
How can we simplify the consumer setup path?

## Findings

### Current setup (6 steps, fragile)
1. Clone turnstile repo somewhere
2. `cd` to consumer project
3. `TURNSTILE_PROJECT_DIR=$(pwd) uv --directory /path/to/turnstile run --package turnstile-cli turnstile init`
4. `TURNSTILE_PROJECT_DIR=$(pwd) uv --directory /path/to/turnstile run --package turnstile-cli turnstile enforce monitor`
5. Add `.mcp.json` and `.claude/settings.json` to `.gitignore`
6. Restart Claude Code

Pain points: requires local clone, env var dance, machine-specific paths everywhere.

### New setup with uvx (2 steps)
1. `uvx --python 3.12 --from "turnstile-cli @ git+https://github.com/PaperArmada/turnstile#subdirectory=packages/turnstile-cli" turnstile init`
2. Restart Claude Code

Why it works: `uvx` runs packages directly from git without a local clone. Unlike `uv --directory`, it does NOT change CWD, so `os.getcwd()` returns the consumer's project directory. No `TURNSTILE_PROJECT_DIR` needed.

### Verified
- `uvx --from git+...#subdirectory=packages/turnstile-cli` resolves dependencies correctly
- `uvx --from git+...#subdirectory=packages/turnstile-mcp` runs the MCP server
- CWD is preserved (consumer's processes are visible)
- Packages are cached by uvx, auto-updated on next invocation

### Design: two modes

**Standard mode** (`turnstile init`): For consumers using turnstile as a tool.
- `.mcp.json` uses `uvx` to run the MCP server from git
- Guard hook uses `uvx` to run the CLI from git
- No local clone required
- Updates happen when uvx refreshes its cache

**Dev mode** (`turnstile init --dev`): For developing turnstile itself.
- `.mcp.json` uses `uv --directory` with `TURNSTILE_PROJECT_DIR` wrapper
- Guard hook uses `uv --directory`
- Requires local clone
- Changes are live (no cache)

### Implementation plan

1. Refactor `turnstile init` to generate uvx-based configs by default
2. Add `--dev` flag that generates the current `uv --directory` + env var configs
3. Update `generate_hook_config` in guard.py to support uvx mode
4. `turnstile init` should also set up enforcement (combine steps 3+4 from old flow)
5. Update README with the simplified setup instructions
6. Bootstrap command: a one-liner users can copy-paste

### Bootstrap one-liner
```bash
uvx --python 3.12 --from "turnstile-cli @ git+https://github.com/PaperArmada/turnstile#subdirectory=packages/turnstile-cli" turnstile init
```

Could be shortened with a shell alias or a URL shortener, but the raw command is copy-pasteable.

# Fresh Install

> Every artifact, example, and default must work for someone installing the software for the first time on a machine you've never seen.

**Design as if the next user has zero context about your development environment.**

## Rules

1. **No hardcoded paths.** Absolute paths to specific machines, home directories, or clone locations must never appear in committed code, configuration templates, or documentation examples. Use discovery (env vars, relative paths, package resolution) or clearly marked placeholders.

2. **Configuration must be portable.** If a config file works on your machine but breaks on someone else's, it's a bug. Machine-specific values belong in gitignored files, environment variables, or generated output, never in tracked sources.

3. **Examples must be copy-pasteable.** Documentation examples should work after a single substitution (e.g., replacing `/path/to/X`), not require the reader to understand your project layout or conventions.

4. **Defaults must be self-contained.** The software should do something useful with zero configuration beyond what's generated during installation. Every required input that can be discovered at runtime should be.

5. **Test the setup path, not just the happy path.** If the first-run experience is broken, nothing else matters. The distance from `git clone` to working software is the most important metric.

## Anti-patterns

- Committing `.mcp.json` with absolute paths to your clone directory
- Documentation that says "run this command" where the command only works from a specific working directory
- Auto-generated config files that embed the generating machine's filesystem layout
- Defaults that assume a specific OS, shell, or directory structure

## Application to Turnstile

Turnstile is consumed by other projects via an MCP server. The consumer needs to point at the turnstile installation, but the consumer's own processes, state, and validations must resolve relative to the consumer's project root, not the turnstile source tree. The boundary between "where turnstile lives" (package resolution) and "where turnstile operates" (project root) must be clean and never conflated.

# Fail Open

> When enforcement infrastructure encounters an error, it must allow the action to proceed rather than blocking it.

**The cost of a false block (agent stuck, developer waiting) is always higher than the cost of a missed check.**

## Rules

1. **Errors allow, never block.** If the guard can't read the registry, find the state directory, parse JSON, or complete any check, the action proceeds. A broken guardrail that blocks work is worse than no guardrail at all.

2. **Degrade gracefully across layers.** Each enforcement layer (entry guard, state machine gates, CI checks) should function independently. A failure in one layer must not cascade to block the others.

3. **Log failures visibly.** Failing open silently hides problems. When enforcement degrades, surface that it degraded so the issue can be fixed. The agent or user should know the guard was skipped, even if the action was allowed.

4. **Default to off, not on.** New enforcement features start disabled. The user opts in after verifying the system works. An enforcement mode that ships enabled and then breaks on first use destroys trust permanently.

## Anti-patterns

- Catching an exception in the guard and exiting with a non-zero code (blocks the action on error)
- Requiring the state directory to exist before the first process is started
- Making the MCP server crash if registry.yaml is missing or malformed
- Validation gates that fail because a command isn't installed on the system

## Application to Turnstile

The guard wraps its entire logic in a try/except that swallows all errors and exits cleanly. Monitor mode exists as a stepping stone so users can verify enforcement works before enabling hard blocks. The registry defaults to `enforcement: off`. Validation gates with `severity: info` always allow the transition regardless of outcome.

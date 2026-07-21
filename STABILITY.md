# Stability and versioning

Turnstile is pre-1.0. This document names which parts of the surface area we treat as stable, which we expect to evolve, and what to expect from version bumps.

## Versioning intent

We follow [semantic versioning](https://semver.org/), with the standard pre-1.0 caveat: minor version bumps in the `0.x` line may include breaking changes when they're necessary to land the Layer 2 work, and we'll call them out in release notes.

The path to 1.0 is the moment the surface listed under [Stable](#stable) is frozen and any change to it requires a deprecation path.

## Stable

These will not change in backward-incompatible ways without a deprecation notice in a previous release.

**Engine core**
- Transition semantics for `initial`, `normal`, and `terminal` states
- Validation gate execution order (on_exit → transition → on_enter)
- Override / skip / abandon / undo semantics
- Parameter substitution (`${var_name}`) in commands and `parameter_map` values

**Process definition schema (for stable state types)**
- Top-level: `name`, `description`, `version`, `metadata`, `parameters`, `states`
- State fields: `id`, `type`, `description`, `transitions`, `on_enter`, `on_exit`, `permissions`
- Gates: `command`, `expect`, `message`, `severity`, `timeout`, `evidence`, `max_age`, `any`/`all` composites
- Expect expressions: `empty`, `not_empty`, `equals`, `not_equals`, `contains`, `starts_with`, `ends_with`, `matches`, `greater_than`, `less_than`, `exit_code`

**MCP tools**
- `process_list`, `process_start`, `process_status`, `process_transition`, `process_skip`, `process_abandon`, `process_undo`, `process_history`, `process_validate_definition`, `process_graph`, `process_dry_run`, `process_diff`, `process_check_completed`

**CLI**
- `validate`, `list`, `status`, `graph`, `dry-run`, `schema`, `check-completed`, `hooks`, `init`, `enforce`

**Persistence layout**
- `.process-state/{active,completed,abandoned}/<instance_id>.json`
- `.process-state/log.txt` append-only event log
- Instance JSON shape: `process_name`, `current_state`, `history`, `parameters`, `overrides`, `started_at`, `updated_at`

**Registry**
- `extends`, `local`, `settings` (state_dir, require_override_reason, log_retention_days, enforcement, notifications)
- Notification templates use `${name}` shell syntax (expanded from environment
  variables). The earlier `{name}` brace form is no longer substituted; a
  template still using it logs a warning and emits the literal text.

## Experimental

These ship and work, but may evolve before 1.0. Breaking changes are possible without a deprecation cycle, though we'll keep them rare and announce them in release notes.

**Layer 2 state types**
- `wait` states and the `signal` mechanism (`process_signal`, `SignalSpec`, `required_fields`)
- `dispatch` states and async child instances (`parameter_map`, `immediate`, `assign_to`)
- `subprocess` states (synchronous parent-suspend pattern)

**Multi-actor primitives**
- `role` on states and in history entries
- `agent_context` (`guidance`, `reference_files`, `tools`, `skill_directives`)
- Session tracking on history entries (`session_id`)
- `process_handoff` semantics
- `required_metadata` declarations on states

**Process inheritance**
- `extends` directive in process overrides
- `add_states`, `patch_states`, `parameters.append` operations under `overrides`

**Other**
- `process_migrate` and definition-hash-based migration prompts
- `process_analytics` and the analytics output shape
- `process_reload_definitions` MCP tool
- `check-completed --parent` filter

## Out of scope for stability guarantees

These are not promised to be stable in any form:

- Internal Python APIs (anything not exposed through the MCP server or CLI)
- Log formats in `.process-state/log.txt`
- The wire format of transition metadata (we may add fields)
- Schema for the Mermaid graph output

## Reporting compatibility breaks

If you adopt Turnstile and find that a release broke something we've called stable, file an issue with the version you were on, the version that broke, and a minimal reproducer. Stability is a promise; we'd rather hear about it than have you discover it silently.

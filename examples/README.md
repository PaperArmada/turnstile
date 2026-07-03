# Example process definitions

Process definitions that demonstrate specific engine features but are not part
of the starter pack in [`.processes/`](../.processes/). Use them as reading
material or as starting points for your own definitions.

| File | Demonstrates |
|------|--------------|
| [`quick-fix.yaml`](quick-fix.yaml) | The smallest useful process: one working state, two terminal states, per-state edit permissions. A good first read. |
| [`heist.yaml`](heist.yaml) | Branching transitions, multiple terminal states, composite (`any`/`all`) validation gates, parameter defaults. A toy domain, deliberately: none of the gates depend on your project. |
| [`retrospective.yaml`](retrospective.yaml) | A governance loop: reviewing a process definition against the history of its completed instances, with a stability guard parameter. |
| [`project-main.yaml`](project-main.yaml) | A persistent coordinator built on the experimental multi-actor layer: `dispatch` states that spawn child processes, a `wait` state gating process creation on owner approval, and three roles (coordinator, owner, developer). Depends on `feature-development`, `bug-fix`, `spike`, `create-process`, and `retrospective` being registered. See [STABILITY.md](../STABILITY.md) for the status of these primitives. |

## Using an example

Copy the file into your project's `.processes/` directory, register it, and
validate:

```bash
cp examples/heist.yaml .processes/
# add its name under `local:` in .processes/registry.yaml
turnstile validate .processes/heist.yaml
```

`turnstile dry-run .processes/heist.yaml` walks the state machine without
creating an instance, which is the quickest way to understand a definition.

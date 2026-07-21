#!/usr/bin/env python3
"""Fresh-machine flip-readiness check.

Exercises the first-hour experience end to end in a throwaway project:

  1. `turnstile init` installs the curated starter pack.
  2. `turnstile list` shows all 11 starters, including security-review.
  3. A validation gate blocks a transition while its condition is unmet,
     then allows it once the condition is satisfied.
  4. The instance runs to a terminal state.

Every step prints what it is doing and asserts the outcome; the script exits
non-zero on the first failure. Run from the repo root:

    uv run --package turnstile-cli python scripts/verify_flip_ready.py

Note: this drives the engine directly and installs via `--dev` (local source).
The fully faithful launch recording — a clean container installing via uvx from
the pushed v0.1.0 tag with no cache — additionally requires the tag to be
reachable on GitHub, and is the one gate step that cannot run pre-push.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from turnstile_core.engine import Engine

REPO_ROOT = Path(__file__).resolve().parent.parent

GATED_PROCESS = """\
name: gated-demo
version: "1.0.0"
description: "Minimal process with a gate that blocks until a marker exists"
parameters:
  - name: task_name
    required: true
states:
  - id: start
    type: initial
    transitions: [ready]
  - id: ready
    description: "Entry gate requires ready.txt to be non-empty"
    transitions: [done]
    on_enter:
      validate:
        - command: "cat ready.txt"
          expect: not_empty
          message: "ready.txt must exist and be non-empty before entering 'ready'"
          severity: error
  - id: done
    type: terminal
"""


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m✗ {msg}\033[0m")
    sys.exit(1)


def step(n: int, title: str) -> None:
    print(f"\n\033[1m[{n}] {title}\033[0m")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="turnstile-flipcheck-"))
    print(f"Fresh project: {workdir}")

    step(1, "turnstile init installs the starter pack")
    r = subprocess.run(
        [
            "uv", "run", "--package", "turnstile-cli", "turnstile",
            "--project", str(workdir),
            "init", "--dev", "--turnstile-dir", str(REPO_ROOT),
            "--enforce", "monitor",
        ],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:
        fail(f"init failed: {r.stderr or r.stdout}")
    installed = sorted(
        p.stem for p in (workdir / ".processes").glob("*.yaml")
        if p.name != "registry.yaml"
    )
    if len(installed) != 11:
        fail(f"expected 11 starters, got {len(installed)}: {installed}")
    ok(f"installed 11 starters: {', '.join(installed)}")

    step(2, "turnstile list shows the pack (incl. security-review)")
    r = subprocess.run(
        [
            "uv", "run", "--package", "turnstile-cli", "turnstile",
            "--project", str(workdir), "list",
        ],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if "security-review" not in r.stdout:
        fail(f"security-review missing from list output:\n{r.stdout}")
    ok("list shows security-review")

    # Register a gated demo process in the fresh project.
    (workdir / ".processes" / "gated-demo.yaml").write_text(GATED_PROCESS)
    registry_path = workdir / ".processes" / "registry.yaml"
    registry = yaml.safe_load(registry_path.read_text())
    registry.setdefault("local", []).append("gated-demo")
    registry_path.write_text(yaml.dump(registry, sort_keys=False))
    engine = Engine(workdir)
    loop = asyncio.new_event_loop()

    step(3, "A failing gate BLOCKS the transition")
    inst = engine.start("gated-demo", {"task_name": "flip-check"})
    iid = inst["instance_id"]
    ok(f"started gated-demo @ {inst['current_state']} (instance {iid})")
    res = loop.run_until_complete(engine.transition(iid, "ready"))
    if res.success:
        fail("gate did NOT block: transition to 'ready' succeeded with no ready.txt")
    if engine._store.load(iid).current_state != "start":
        fail("blocked transition still mutated the persisted state")
    ok(f"blocked (success={res.success}, still @ start): {res.message}")

    step(4, "Satisfy the gate; the SAME transition now passes")
    (workdir / "ready.txt").write_text("ready\n")
    res = loop.run_until_complete(engine.transition(iid, "ready"))
    if not res.success:
        fail(f"gate still blocking after ready.txt created: {res.message}")
    ok(f"passed → {res.new_state}")

    step(5, "Run to terminal")
    res = loop.run_until_complete(engine.transition(iid, "done"))
    if not res.success or res.new_state != "done":
        fail(f"could not reach terminal: {res.message}")
    ok("reached 'done'")

    print("\n\033[1;32mFLIP-READY CHECK PASSED\033[0m")
    print("  init → list → gate blocks → gate passes → terminal, all verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

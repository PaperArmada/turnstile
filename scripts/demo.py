"""60-second narrated demo of the Turnstile engine.

Drives peer-review through its happy + rejection paths so the value
(legal transitions, role handoffs, signal-only wait advancement) is
visible to a viewer. Designed to be recorded with asciinema:

    asciinema rec -c 'uv run python scripts/demo.py' demo.cast

Runs against a temporary project root, no setup required.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

from turnstile_core.engine import Engine
from turnstile_core.errors import TransitionError


REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESS_YAML = REPO_ROOT / ".processes" / "peer-review.yaml"

STEP_PAUSE = 1.2   # seconds between narrated steps
INPUT_PAUSE = 0.6  # seconds after showing a call before showing the response


def banner(text: str) -> None:
    print()
    print(f"━━━ {text} " + "━" * max(0, 60 - len(text)))
    print()
    time.sleep(STEP_PAUSE)


def call(label: str, snippet: str) -> None:
    print(f"  {label}")
    print(f"  $ {snippet}")
    time.sleep(INPUT_PAUSE)


def result(state: str, role: str, message: str = "") -> None:
    role_str = role if role else "(none)"
    print(f"    → state: {state}    role: {role_str}")
    if message:
        print(f"      {message}")
    time.sleep(STEP_PAUSE)


def reject(message: str) -> None:
    print(f"    ✗ rejected: {message}")
    time.sleep(STEP_PAUSE)


def main() -> int:
    if not PROCESS_YAML.exists():
        print(f"missing process definition: {PROCESS_YAML}", file=sys.stderr)
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="turnstile-demo-"))
    try:
        (workdir / ".processes").mkdir()
        shutil.copy(PROCESS_YAML, workdir / ".processes" / "peer-review.yaml")
        engine = Engine(workdir)
        loop = asyncio.new_event_loop()

        print()
        print("Turnstile: 60-second demo")
        print("Process: peer-review (submit → review → revise → accept)")
        time.sleep(STEP_PAUSE)

        banner("1. Agent starts a peer-review instance")
        call("agent:", 'process_start("peer-review", {artifact: "API design doc"})')
        r = engine.start("peer-review", {"artifact": "API design doc"})
        iid = r["instance_id"]
        result(r["current_state"], "", f'instance: {iid}')

        banner("2. Transition to 'prepare' (role: submitter)")
        call("agent:", f'process_transition("{iid}", "prepare")')
        tr = loop.run_until_complete(engine.transition(iid, "prepare"))
        result(tr.new_state, tr.role, "agent_context provisioned for submitter")

        banner("3. Submit for review (wait state, role: reviewer)")
        call("agent:", f'process_transition("{iid}", "awaiting_review")')
        tr = loop.run_until_complete(engine.transition(iid, "awaiting_review"))
        result(tr.new_state, tr.role, "⏸ waiting for signal: review_decision")

        banner("4. Try to skip ahead; engine rejects")
        call("agent:", f'process_transition("{iid}", "accepted")')
        try:
            loop.run_until_complete(engine.transition(iid, "accepted"))
            print("    (unexpected: no rejection)")
        except TransitionError as err:
            reject(str(err))

        banner("5. Reviewer signals 'changes_requested' (role hands back)")
        call("reviewer:", f'process_signal("{iid}", "review_decision", '
             '{decision: "changes_requested", feedback: "tighten paragraph 2"})')
        sr = engine.receive_signal(
            iid,
            "review_decision",
            {"decision": "changes_requested", "feedback": "tighten paragraph 2"},
            target_state="revise",
        )
        result(sr["new_state"], sr["role"], "role handoff: reviewer → submitter")

        banner("6. Resubmit and accept (terminal)")
        call("submitter:", f'process_transition("{iid}", "awaiting_review")')
        tr = loop.run_until_complete(engine.transition(iid, "awaiting_review"))
        result(tr.new_state, tr.role)

        call("reviewer:", f'process_signal("{iid}", "review_decision", '
             '{decision: "accepted", feedback: "approved"})')
        sr = engine.receive_signal(
            iid,
            "review_decision",
            {"decision": "accepted", "feedback": "approved"},
            target_state="accepted",
        )
        result(sr["new_state"], sr["role"], "✓ terminal: accepted")

        banner("Done")
        print("  6 transitions · 3 role handoffs · 1 illegal transition rejected")
        print()
        loop.close()
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

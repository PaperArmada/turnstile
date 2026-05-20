"""60-second narrated demo of the Turnstile engine.

Drives peer-review through its happy + rejection paths so the value
(legal transitions, role handoffs, signal-only wait advancement) is
visible to a viewer. Designed to be recorded with asciinema:

    asciinema rec --cols 100 --rows 28 --idle-time-limit 1 -q \
        -c 'uv run --package turnstile-core python scripts/demo.py' \
        docs/assets/demo.cast

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

STEP_PAUSE = 0.55
INPUT_PAUSE = 0.22
TYPE_SPEED = 0.013  # seconds per character

# ANSI escapes
R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
RED = "\033[91m"
GRN = "\033[92m"
YLW = "\033[93m"
CYN = "\033[96m"
GRY = "\033[90m"


def write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def type_out(s: str, speed: float = TYPE_SPEED) -> None:
    for ch in s:
        write(ch)
        time.sleep(speed)


def step(num: int, title: str) -> None:
    write(f"\n  {B}{CYN}▸{R} {B}{num}.{R}  {B}{title}{R}\n\n")
    time.sleep(STEP_PAUSE)


def call(label: str, snippet: str) -> None:
    write(f"    {GRN}{label}{R} {D}→{R} ")
    type_out(snippet)
    write("\n")
    time.sleep(INPUT_PAUSE)


def result(state: str, role: str, note: str = "") -> None:
    role_part = f"{YLW}{role}{R}" if role else f"{D}(none){R}"
    write(f"      {D}state:{R} {GRN}{state}{R}    {D}role:{R} {role_part}\n")
    if note:
        write(f"      {D}{note}{R}\n")
    time.sleep(STEP_PAUSE)


def reject(message: str) -> None:
    import textwrap
    # Visible prefix on line 1 is "      ✗ rejected: " (18 chars). Wrap
    # content to 80 chars; on screen, line 1 = 18 + 80 = 98 cols, subsequent
    # lines = 18 + 80 = 98 cols. All fit inside a 100-col terminal.
    wrapped = textwrap.wrap(
        message, width=80, break_long_words=False, break_on_hyphens=False
    )
    first, rest = wrapped[0], wrapped[1:]
    write(f"      {RED}{B}✗ rejected{R}{RED}:{R} {first}\n")
    for line in rest:
        write(f"                  {line}\n")  # 18-space indent to align under body
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

        write(f"\n  {B}Turnstile{R}  {D}process engine for AI agents{R}\n")
        write(f"  {GRY}peer-review: submit → review → revise → accept{R}\n")
        time.sleep(STEP_PAUSE * 1.5)

        step(1, "Agent starts a peer-review instance")
        call("agent", 'process_start("peer-review", {artifact: "API design doc"})')
        r = engine.start("peer-review", {"artifact": "API design doc"})
        iid = r["instance_id"]
        result(r["current_state"], "", f"instance: {iid}")

        step(2, "Transition to 'prepare' (role: submitter)")
        call("agent", f'process_transition("{iid}", "prepare")')
        tr = loop.run_until_complete(engine.transition(iid, "prepare"))
        result(tr.new_state, tr.role, "agent_context provisioned for submitter")

        step(3, "Submit for review (wait state, role flips to reviewer)")
        call("agent", f'process_transition("{iid}", "awaiting_review")')
        tr = loop.run_until_complete(engine.transition(iid, "awaiting_review"))
        result(tr.new_state, tr.role, "waiting for signal: review_decision")

        step(4, "Try to skip ahead; engine rejects")
        call("agent", f'process_transition("{iid}", "accepted")')
        try:
            loop.run_until_complete(engine.transition(iid, "accepted"))
            write("      (unexpected: no rejection)\n")
        except TransitionError as err:
            reject(str(err))

        step(5, "Reviewer signals changes_requested (role hands back)")
        call("reviewer", f'process_signal("{iid}", "review_decision", '
             '{decision: "changes_requested"})')
        sr = engine.receive_signal(
            iid,
            "review_decision",
            {"decision": "changes_requested", "feedback": "tighten paragraph 2"},
            target_state="revise",
        )
        result(sr["new_state"], sr["role"], "role handoff: reviewer → submitter")

        step(6, "Resubmit and accept (terminal)")
        call("submitter", f'process_transition("{iid}", "awaiting_review")')
        tr = loop.run_until_complete(engine.transition(iid, "awaiting_review"))
        result(tr.new_state, tr.role)

        call("reviewer", f'process_signal("{iid}", "review_decision", '
             '{decision: "accepted"})')
        sr = engine.receive_signal(
            iid,
            "review_decision",
            {"decision": "accepted", "feedback": "approved"},
            target_state="accepted",
        )
        result(sr["new_state"], sr["role"], f"{GRN}{B}✓{R} {GRN}terminal: accepted{R}")

        write(f"\n  {D}6 transitions · 3 role handoffs · 1 illegal transition rejected{R}\n\n")
        time.sleep(STEP_PAUSE * 1.5)
        loop.close()
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

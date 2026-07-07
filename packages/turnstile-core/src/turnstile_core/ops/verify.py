"""EXPERIMENTAL — acceptance-time conformance verification.

Proof of concept for the mechanisms in docs/guarantee.md. This module
answers one question at the acceptance boundary: *did this work follow
the declared workflow?* It is designed to run where the agent has no
reach (CI on a protected branch), and to trust nothing the agent could
have written:

- **Gate re-execution** (PROVEN): re-runs declared gates against the
  current worktree. Recorded results are ignored; reality is
  recomputed. A forged ledger cannot survive this.
- **Hash-chained trail + external anchor** (ATTESTED): the instance
  history is hash-chained; the chain head is compared against an
  anchor written outside the agent's environment while the work
  happened. Rewriting history breaks the match.
- **Artifact binding** (PROVEN): history entries carry git SHAs; the
  commit range under review must be reachable from the final certified
  SHA. The trail certifies *this diff*, not just that a process ran.
- **Authenticated signals** (HUMAN): signals on human-gated wait
  states must carry a signature made with a key the agent does not
  hold. A self-approved wait state fails verification.

PoC caveats, stated plainly: anchoring here is a file path the caller
promises lives outside the agent's write domain, and signal
authentication is an HMAC with an operator-held key. In production
these become a remote append-only anchor and an identity provider
(e.g., a GitHub review approval). The *mechanisms* — recomputation,
chain comparison, reachability, signature checking — are what this
module proves out, and they are unchanged by that substitution.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from turnstile_core.definition.loader import discover_definitions_full
from turnstile_core.instance import ProcessInstance, StateStore
from turnstile_core.instance.model import HistoryEntry
from turnstile_core.runtime.gates import has_blocking_failures, run_validations

CheckStatus = Literal["PROVEN", "ATTESTED", "HUMAN", "INFO", "FAIL"]


# ---------------------------------------------------------------------------
# Acceptance policy
# ---------------------------------------------------------------------------


class ReverifyGate(BaseModel):
    """A gate to re-execute at verification time."""

    state: str
    hook: str = "on_exit"  # "on_exit" or "on_enter"


class AcceptancePolicy(BaseModel):
    """What a process instance must demonstrate to be accepted."""

    process: str
    # States that must appear in the walk, in this order.
    required_states: list[str] = Field(default_factory=list)
    # The instance must have reached a terminal state.
    require_completed: bool = True
    # Whether skips/overrides are tolerated at all.
    allow_skips: bool = False
    # Gates to re-execute against the current worktree.
    reverify: list[ReverifyGate] = Field(default_factory=list)
    # Wait states whose signals must be operator-signed.
    human_signal_states: list[str] = Field(default_factory=list)
    # Path to the operator HMAC key (must live outside the agent's
    # write domain; see module docstring).
    signal_key_file: str | None = None
    # Path to the anchor file (must live outside the agent's write
    # domain). If set, chain-vs-anchor mismatch is a failure.
    anchor_file: str | None = None
    # Require the commit range under review to be certified.
    enforce_binding: bool = False


class VerifyCheck(BaseModel):
    status: CheckStatus
    name: str
    detail: str = ""


class VerifyReport(BaseModel):
    instance_id: str
    process_name: str
    passed: bool
    checks: list[VerifyCheck] = Field(default_factory=list)

    def add(self, status: CheckStatus, name: str, detail: str = "") -> None:
        self.checks.append(VerifyCheck(status=status, name=name, detail=detail))
        if status == "FAIL":
            self.passed = False

    def render(self) -> str:
        lines = [
            f"turnstile verify — {self.process_name} "
            f"[{self.instance_id}]: {'PASS' if self.passed else 'FAIL'}"
        ]
        for c in self.checks:
            lines.append(f"  {c.status:<8} {c.name}" + (f" — {c.detail}" if c.detail else ""))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hash chain + anchoring
# ---------------------------------------------------------------------------


def _canonical(entry: HistoryEntry) -> bytes:
    return json.dumps(
        entry.model_dump(by_alias=True), sort_keys=True, separators=(",", ":")
    ).encode()


def compute_chain_head(instance: ProcessInstance) -> str:
    """Hash-chain the instance history and return the head digest.

    Each entry's hash folds in the previous one, so editing, inserting,
    or deleting any historical entry changes the head.
    """
    head = hashlib.sha256(
        f"turnstile:{instance.process_name}:{instance.instance_id}".encode()
    ).hexdigest()
    for entry in instance.history:
        head = hashlib.sha256((head.encode() + _canonical(entry))).hexdigest()
    return head


def write_anchor(anchor_path: Path, instance: ProcessInstance) -> str:
    """Record the current chain head in the anchor file.

    In production this is a remote append-only log written under a
    credential the agent does not hold; the PoC uses a file the caller
    keeps outside the agent's write domain. Call it on every
    transition so the anchor is contemporaneous.
    """
    head = compute_chain_head(instance)
    anchors: dict[str, Any] = {}
    if anchor_path.exists():
        anchors = json.loads(anchor_path.read_text())
    anchors[instance.instance_id] = {
        "head": head,
        "entries": len(instance.history),
    }
    anchor_path.write_text(json.dumps(anchors, indent=2))
    return head


# ---------------------------------------------------------------------------
# Signal signatures
# ---------------------------------------------------------------------------


def sign_signal(
    key: bytes, instance_id: str, signal_name: str, data: dict[str, Any]
) -> str:
    """Produce the operator signature for a signal payload."""
    payload = {k: v for k, v in data.items() if k != "signature"}
    message = (
        f"{instance_id}|{signal_name}|"
        + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    ).encode()
    return hmac_mod.new(key, message, hashlib.sha256).hexdigest()


def signal_signature_valid(
    key: bytes, instance_id: str, signal_name: str, data: dict[str, Any]
) -> bool:
    provided = data.get("signature")
    if not isinstance(provided, str):
        return False
    expected = sign_signal(key, instance_id, signal_name, data)
    return hmac_mod.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Git helpers (artifact binding)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def certified_sha(instance: ProcessInstance) -> str | None:
    """The last git SHA recorded in the instance history, if any."""
    for entry in reversed(instance.history):
        sha = entry.metadata.get("git_sha")
        if isinstance(sha, str) and sha:
            return sha
    return None


def range_is_certified(repo: Path, commit_range: str, certified: str) -> tuple[bool, str]:
    """Check that every commit in the range is reachable from the
    certified SHA — i.e., the instance's final checkpoint covers the
    work under review."""
    try:
        range_shas = _git(repo, "rev-list", commit_range).splitlines()
        reachable = set(_git(repo, "rev-list", certified).splitlines())
    except subprocess.CalledProcessError as e:
        return False, f"git error: {e.stderr.strip() or e}"
    uncovered = [s for s in range_shas if s not in reachable]
    if uncovered:
        return False, (
            f"{len(uncovered)} commit(s) not covered by certified "
            f"snapshot {certified[:12]}: "
            + ", ".join(s[:12] for s in uncovered[:5])
        )
    return True, f"{len(range_shas)} commit(s) covered by {certified[:12]}"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _visited_sequence(instance: ProcessInstance) -> list[str]:
    seq: list[str] = []
    for h in instance.history:
        if not seq:
            seq.append(h.from_state)
        seq.append(h.to_state)
    return seq


async def verify_instance(
    project_root: Path,
    instance_id: str,
    policy: AcceptancePolicy,
    commit_range: str | None = None,
    state_dir: str = ".process-state",
) -> VerifyReport:
    """Verify one instance against an acceptance policy.

    Returns a report whose ``passed`` flag is the acceptance decision.
    Everything marked PROVEN is recomputed here and does not rely on
    the stored ledger; ATTESTED items rely on the anchored chain.
    """
    store = StateStore(project_root / state_dir)
    instance = store.load_any(instance_id)

    report = VerifyReport(
        instance_id=instance.instance_id,
        process_name=instance.process_name,
        passed=True,
    )

    # --- identity ---------------------------------------------------------
    if instance.process_name != policy.process:
        report.add(
            "FAIL", "process",
            f"instance is '{instance.process_name}', policy expects "
            f"'{policy.process}'",
        )
        return report

    # --- trail integrity --------------------------------------------------
    head = compute_chain_head(instance)
    if policy.anchor_file:
        anchor_path = Path(policy.anchor_file)
        anchored = None
        if anchor_path.exists():
            anchored = json.loads(anchor_path.read_text()).get(instance.instance_id)
        if anchored is None:
            report.add("FAIL", "trail integrity", "no anchor recorded for this instance")
        elif anchored["head"] != head or anchored["entries"] != len(instance.history):
            report.add(
                "FAIL", "trail integrity",
                "chain head does not match anchor — history was modified "
                "after anchoring",
            )
        else:
            report.add(
                "ATTESTED", "trail integrity",
                f"{len(instance.history)} entries, chain intact, anchor matches",
            )
    else:
        report.add(
            "INFO", "trail integrity",
            f"chain head {head[:12]} (unanchored — informational only)",
        )

    # --- completion and state walk -----------------------------------------
    if policy.require_completed and instance.status != "completed":
        report.add("FAIL", "completion", f"instance status is '{instance.status}'")

    visited = _visited_sequence(instance)
    cursor = 0
    missing_or_disordered = []
    for required in policy.required_states:
        try:
            cursor = visited.index(required, cursor)
        except ValueError:
            missing_or_disordered.append(required)
    if missing_or_disordered:
        report.add(
            "FAIL", "state walk",
            f"required states missing or out of order: "
            f"{', '.join(missing_or_disordered)} (walk: {' -> '.join(visited)})",
        )
    elif policy.required_states:
        report.add(
            "ATTESTED", "state walk",
            " -> ".join(policy.required_states),
        )

    # --- exceptions ---------------------------------------------------------
    if instance.overrides and not policy.allow_skips:
        details = "; ".join(
            f"{o.from_state} -> {o.to_state} ({o.reason})" for o in instance.overrides
        )
        report.add("FAIL", "exceptions", f"unauthorized skip(s): {details}")
    else:
        report.add(
            "PROVEN", "exceptions",
            f"{len(instance.overrides)} skip(s), policy allows: {policy.allow_skips}",
        )

    # --- human signals -------------------------------------------------------
    if policy.human_signal_states:
        key: bytes | None = None
        if policy.signal_key_file and Path(policy.signal_key_file).exists():
            key = Path(policy.signal_key_file).read_bytes()
        for entry in instance.history:
            if not entry.triggered_by.startswith("signal: "):
                continue
            if entry.from_state not in policy.human_signal_states:
                continue
            signal_name = entry.triggered_by.removeprefix("signal: ")
            data = entry.metadata.get("signal_data", {})
            if key is None:
                report.add(
                    "FAIL", f"human signal @ {entry.from_state}",
                    "no operator key available to verify signature",
                )
            elif signal_signature_valid(key, instance.instance_id, signal_name, data):
                signer = data.get("approved_by", "operator")
                report.add(
                    "HUMAN", f"human signal @ {entry.from_state}",
                    f"'{signal_name}' signature valid — {signer}",
                )
            else:
                report.add(
                    "FAIL", f"human signal @ {entry.from_state}",
                    f"'{signal_name}' carries no valid operator signature "
                    f"(self-approval?)",
                )

    # --- gate re-execution ----------------------------------------------------
    if policy.reverify:
        discovered = discover_definitions_full(project_root)
        disc = discovered.get(policy.process)
        if disc is None:
            report.add("FAIL", "gate re-execution", f"definition '{policy.process}' not found")
        else:
            defn = disc.definition
            gate_params = {**instance.parameters, "instance_id": instance.instance_id}
            for rv in policy.reverify:
                state = defn.get_state(rv.state)
                hooks = getattr(state, rv.hook, None) if state else None
                rules = hooks.validations if hooks else []
                if not rules:
                    report.add(
                        "FAIL", f"gate re-execution @ {rv.state}.{rv.hook}",
                        "no such gates in definition",
                    )
                    continue
                results = await run_validations(rules, gate_params, project_root)
                if has_blocking_failures(results):
                    failed = [r for r in results if not r.passed]
                    report.add(
                        "FAIL", f"gate re-execution @ {rv.state}.{rv.hook}",
                        f"{len(failed)} gate(s) do not hold against the "
                        f"current worktree: "
                        + "; ".join(f"{r.message or r.command}" for r in failed[:3]),
                    )
                else:
                    report.add(
                        "PROVEN", f"gate re-execution @ {rv.state}.{rv.hook}",
                        f"{len(results)} gate(s) re-executed and passing now",
                    )

    # --- artifact binding -------------------------------------------------------
    if policy.enforce_binding:
        cert = certified_sha(instance)
        if commit_range is None:
            report.add("FAIL", "artifact binding", "no commit range provided")
        elif cert is None:
            report.add(
                "FAIL", "artifact binding",
                "no git_sha checkpoints recorded in the instance history",
            )
        else:
            ok, detail = range_is_certified(project_root, commit_range, cert)
            report.add("PROVEN" if ok else "FAIL", "artifact binding", detail)

    return report

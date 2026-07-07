"""EXPERIMENTAL: conformance report rendering.

Turns a verification result plus the instance trail into a Markdown
document — the artifact that puts the proof where decisions happen:
PR comments, CI summaries, compliance archives. The report never adds
claims beyond what verification established; it makes those claims
legible.
"""

from __future__ import annotations

from typing import Any

from turnstile_core.instance.model import ProcessInstance
from turnstile_core.ops.verify import VerifyReport

_STATUS_ICON = {
    "PROVEN": "🟢",
    "ATTESTED": "🔵",
    "HUMAN": "👤",
    "INFO": "⚪",
    "FAIL": "🔴",
}

_LEGEND = (
    "**PROVEN** — recomputed during verification; does not trust the "
    "stored ledger. **ATTESTED** — hash-chained trail matches its "
    "external anchor. **HUMAN** — authenticated approval. "
    "**INFO** — reported, not load-bearing. See docs/guarantee.md."
)


def _short(sha: str, n: int = 10) -> str:
    return sha[:n] if sha else ""


def _short_time(iso: str) -> str:
    # 2026-07-07T18:22:33.123456+00:00 -> 2026-07-07 18:22 UTC
    if "T" not in iso:
        return iso
    date, _, rest = iso.partition("T")
    return f"{date} {rest[:5]} UTC"


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(
    instance: ProcessInstance,
    report: VerifyReport,
    commit_range: str | None = None,
) -> str:
    """Render a self-contained Markdown conformance report."""
    verdict = "✅ **PASS** — workflow followed" if report.passed else \
              "❌ **FAIL** — workflow NOT verified"
    lines: list[str] = []
    lines.append(
        f"## Turnstile conformance report — `{instance.process_name}` "
        f"({instance.instance_id})"
    )
    lines.append("")
    lines.append(verdict)
    lines.append("")
    meta = [
        f"process version `{instance.process_version or '?'}`",
        f"started {_short_time(instance.started_at)}",
        f"finished {_short_time(instance.updated_at)}",
        f"status `{instance.status}`",
    ]
    if commit_range:
        meta.append(f"commit range `{commit_range}`")
    lines.append("_" + " · ".join(meta) + "_")
    lines.append("")

    # --- Verification checks ------------------------------------------------
    lines.append("### Verification")
    lines.append("")
    lines.append("| | Status | Check | Detail |")
    lines.append("|---|---|---|---|")
    for c in report.checks:
        icon = _STATUS_ICON.get(c.status, "")
        lines.append(
            f"| {icon} | {c.status} | {_md_escape(c.name)} "
            f"| {_md_escape(c.detail)} |"
        )
    lines.append("")

    # --- Timeline -------------------------------------------------------------
    lines.append("### Timeline")
    lines.append("")
    lines.append("| # | Transition | When | Trigger | Gates | Commit |")
    lines.append("|---|---|---|---|---|---|")
    for i, h in enumerate(instance.history, 1):
        passed = sum(1 for v in h.validations if v.get("passed"))
        failed = len(h.validations) - passed
        gates = "—"
        if h.validations:
            gates = f"{passed} passed" + (f", {failed} failed" if failed else "")
        trigger = h.triggered_by or "transition"
        sha = _short(str(h.metadata.get("git_sha", "")))
        lines.append(
            f"| {i} | `{h.from_state}` → `{h.to_state}` "
            f"| {_short_time(h.at)} | {_md_escape(trigger)} "
            f"| {gates} | {f'`{sha}`' if sha else '—'} |"
        )
    lines.append("")

    # --- Exceptions -------------------------------------------------------------
    if instance.overrides:
        lines.append("### Exceptions (skips / overrides)")
        lines.append("")
        lines.append("| From | To | When | Reason |")
        lines.append("|---|---|---|---|")
        for o in instance.overrides:
            lines.append(
                f"| `{o.from_state}` | `{o.to_state}` | {_short_time(o.at)} "
                f"| {_md_escape(o.reason)} |"
            )
        lines.append("")

    # --- Approvals -------------------------------------------------------------
    approvals = []
    for h in instance.history:
        if h.triggered_by.startswith("signal: "):
            data = h.metadata.get("signal_data", {})
            approvals.append({
                "signal": h.triggered_by.removeprefix("signal: "),
                "state": h.from_state,
                "by": data.get("approved_by", "(unattributed)"),
                "signed": "signature" in data,
                "at": h.at,
            })
    if approvals:
        lines.append("### Approvals")
        lines.append("")
        lines.append("| Signal | At state | By | Signed | When |")
        lines.append("|---|---|---|---|---|")
        for a in approvals:
            signed = "🔏 yes" if a["signed"] else "⚠️ no"
            lines.append(
                f"| `{a['signal']}` | `{a['state']}` | {_md_escape(a['by'])} "
                f"| {signed} | {_short_time(a['at'])} |"
            )
        lines.append("")

    # --- Walk diagram --------------------------------------------------------
    walk: list[str] = []
    for h in instance.history:
        if not walk:
            walk.append(h.from_state)
        walk.append(h.to_state)
    if walk:
        lines.append("### Walk")
        lines.append("")
        lines.append("```mermaid")
        lines.append("stateDiagram-v2")
        lines.append(f"    [*] --> {walk[0]}")
        for a, b in zip(walk, walk[1:]):
            lines.append(f"    {a} --> {b}")
        if instance.status == "completed":
            lines.append(f"    {walk[-1]} --> [*]")
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append(f"<sub>{_LEGEND}</sub>")
    lines.append("")
    return "\n".join(lines)

"""Tests for the Markdown conformance report renderer."""

from turnstile_core.instance.model import ProcessInstance
from turnstile_core.ops.report import render_markdown
from turnstile_core.ops.verify import VerifyReport


def make_instance(**overrides) -> ProcessInstance:
    data = {
        "instance_id": "abc123",
        "process_name": "guarded-release",
        "process_version": "1.0.0",
        "current_state": "ship",
        "status": "completed",
        "started_at": "2026-07-07T10:00:00+00:00",
        "updated_at": "2026-07-07T10:45:00+00:00",
        "history": [
            {"from": "start", "to": "build", "at": "2026-07-07T10:01:00+00:00",
             "metadata": {"git_sha": "deadbeefcafe1234"}},
            {"from": "build", "to": "approval", "at": "2026-07-07T10:20:00+00:00",
             "validations": [{"passed": True}, {"passed": True}]},
            {"from": "approval", "to": "ship", "at": "2026-07-07T10:44:00+00:00",
             "triggered_by": "signal: release_approval",
             "metadata": {"signal_data": {
                 "approved": True, "approved_by": "matt",
                 "signature": "abc",
             }}},
        ],
    }
    data.update(overrides)
    return ProcessInstance(**data)


def make_report(passed=True) -> VerifyReport:
    r = VerifyReport(
        instance_id="abc123", process_name="guarded-release", passed=True
    )
    r.add("ATTESTED", "trail integrity", "3 entries, chain intact")
    r.add("PROVEN", "gate re-execution @ build.on_exit", "2 gate(s) passing")
    r.add("HUMAN", "human signal @ approval", "signature valid — matt")
    if not passed:
        r.add("FAIL", "artifact binding", "1 commit(s) not covered")
    return r


class TestRenderMarkdown:
    def test_pass_report_structure(self):
        md = render_markdown(make_instance(), make_report(), "main..HEAD")
        assert "✅ **PASS**" in md
        assert "### Verification" in md
        assert "### Timeline" in md
        assert "### Walk" in md
        assert "stateDiagram-v2" in md
        assert "`start` → `build`" in md
        assert "`deadbeefca`" in md          # short sha in timeline
        assert "commit range `main..HEAD`" in md
        assert "PROVEN" in md and "ATTESTED" in md and "HUMAN" in md

    def test_fail_report_marked(self):
        md = render_markdown(make_instance(), make_report(passed=False))
        assert "❌ **FAIL**" in md
        assert "🔴 | FAIL | artifact binding" in md

    def test_approvals_section(self):
        md = render_markdown(make_instance(), make_report())
        assert "### Approvals" in md
        assert "| `release_approval` | `approval` | matt | 🔏 yes |" in md

    def test_unsigned_approval_flagged(self):
        inst = make_instance()
        del inst.history[-1].metadata["signal_data"]["signature"]
        md = render_markdown(inst, make_report())
        assert "⚠️ no" in md

    def test_exceptions_section_only_when_present(self):
        md = render_markdown(make_instance(), make_report())
        assert "### Exceptions" not in md

        inst = make_instance(overrides=[{
            "from_state": "build", "to_state": "ship",
            "at": "2026-07-07T10:30:00+00:00", "reason": "hotfix",
        }])
        md = render_markdown(inst, make_report())
        assert "### Exceptions" in md
        assert "hotfix" in md

    def test_gate_counts_in_timeline(self):
        md = render_markdown(make_instance(), make_report())
        assert "2 passed" in md

    def test_walk_diagram_shows_completion(self):
        md = render_markdown(make_instance(), make_report())
        assert "ship --> [*]" in md

    def test_pipes_escaped(self):
        report = VerifyReport(
            instance_id="abc123", process_name="guarded-release", passed=True
        )
        report.add("INFO", "check | with pipe", "detail | with pipe")
        md = render_markdown(make_instance(), report)
        assert "check \\| with pipe" in md

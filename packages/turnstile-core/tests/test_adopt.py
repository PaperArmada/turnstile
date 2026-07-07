"""Tests for turnstile_core.ops.adopt — CLAUDE.md migration."""

import yaml

from turnstile_core.definition.loader import load_definition
from turnstile_core.definition.model import ProcessDefinition
from turnstile_core.ops.adopt import (
    analyze,
    build_definition,
    build_policy,
    extract_candidates,
    find_source,
    render_adoption_report,
)

SAMPLE = """\
# My Project

A web app for tracking things.

## Development

Always run the test suite before committing:

```bash
npm test
npm run lint
```

Build with `npm run build` after any dependency change.

## Rules

- Never push directly to main.
- Never force push.
- Do not deploy on Fridays.
- Always write descriptive commit messages.
- Before opening a PR, run `npx tsc --noEmit` to typecheck.
- After merging, delete your feature branch.
- Use camelCase for variable names.

## Setup

```bash
npm install
cp .env.example .env
```
"""


class TestExtraction:
    def test_commands_from_fences(self):
        commands = [c for c in extract_candidates(SAMPLE) if c.kind == "command"]
        texts = [c.text for c in commands]
        assert "npm test" in texts
        assert "npm run lint" in texts
        assert "npm install" in texts

    def test_inline_commands_in_rules(self):
        commands = [c for c in extract_candidates(SAMPLE) if c.kind == "command"]
        assert any(c.text == "npx tsc --noEmit" for c in commands)

    def test_rules_with_line_numbers_and_sections(self):
        rules = [c for c in extract_candidates(SAMPLE) if c.kind == "rule"]
        push_rule = next(c for c in rules if "push directly" in c.text)
        assert push_rule.section == "Rules"
        assert push_rule.line > 0

    def test_prose_without_imperatives_ignored(self):
        rules = [c for c in extract_candidates(SAMPLE) if c.kind == "rule"]
        assert not any("web app for tracking" in c.text for c in rules)


class TestAnalysis:
    def test_gate_commands_identified(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        gate_cmds = [g["command"] for g in adoption.gates]
        assert "npm test" in gate_cmds
        assert "npm run lint" in gate_cmds
        assert "npx tsc --noEmit" in gate_cmds
        # Setup commands are not gates
        assert "npm install" not in gate_cmds

    def test_prohibitions_become_deny_globs(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        assert "git push*" in adoption.deny_commands
        assert "git push --force*" in adoption.deny_commands
        assert "*deploy*" in adoption.deny_commands

    def test_deny_sources_reference_lines(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        assert any("push directly" in c.text for c in adoption.deny_sources)

    def test_sequence_rules_need_judgment(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        assert any(
            "delete your feature branch" in c.text
            for c in adoption.needs_judgment
        )

    def test_style_rules_stay_in_markdown(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        assert any(
            "descriptive commit messages" in c.text
            for c in adoption.stays_in_markdown
        )
        # Lines with no imperative marker at all (e.g. the camelCase
        # convention) are never captured — they stay in markdown by
        # omission rather than classification.
        all_captured = (
            adoption.deny_sources + adoption.needs_judgment
            + adoption.stays_in_markdown
        )
        assert not any("camelCase" in c.text for c in all_captured)

    def test_gate_dedup(self):
        text = "```bash\npytest\npytest\n```\n"
        adoption = analyze(text, "CLAUDE.md")
        assert len(adoption.gates) == 1


class TestDrafting:
    def test_definition_is_valid(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        data = build_definition("my-workflow", adoption)
        defn = ProcessDefinition(**data)
        assert defn.name == "my-workflow"
        assert defn.initial_state().id == "start"

    def test_gates_on_verify_exit(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        data = build_definition("my-workflow", adoption)
        verify = next(s for s in data["states"] if s["id"] == "verify")
        gate_cmds = [v["command"] for v in verify["on_exit"]["validate"]]
        assert "npm test" in gate_cmds
        assert all(
            v["expect"] == "exit_code(0)"
            for v in verify["on_exit"]["validate"]
        )

    def test_denies_on_working_states(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        data = build_definition("my-workflow", adoption)
        for state_id in ("understand", "implement", "verify"):
            state = next(s for s in data["states"] if s["id"] == state_id)
            assert "git push*" in state["permissions"]["deny_commands"]

    def test_bare_skeleton_when_nothing_found(self):
        adoption = analyze("# Empty\n\nJust a readme.\n", "CLAUDE.md")
        data = build_definition("bare", adoption)
        defn = ProcessDefinition(**data)
        verify = defn.get_state("verify")
        assert verify.on_exit is None
        assert defn.get_state("implement").permissions.deny_commands == []

    def test_policy_matches(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        policy = build_policy("my-workflow", adoption)
        assert policy["process"] == "my-workflow"
        assert policy["reverify"] == [{"state": "verify", "hook": "on_exit"}]
        assert policy["enforce_binding"] is True

    def test_policy_without_gates_has_no_reverify(self):
        adoption = analyze("# Empty\n", "CLAUDE.md")
        policy = build_policy("bare", adoption)
        assert "reverify" not in policy


class TestReport:
    def test_report_sections(self):
        adoption = analyze(SAMPLE, "CLAUDE.md")
        report = render_adoption_report(
            adoption, "my-workflow",
            ".processes/my-workflow.yaml",
            ".processes/policies/my-workflow.yaml",
        )
        assert "### Adopted as gates" in report
        assert "### Adopted as command restrictions" in report
        assert "### Needs your judgment" in report
        assert "### Stays in CLAUDE.md" in report
        assert "### Next steps" in report
        assert "CLAUDE.md:" in report  # line references

    def test_empty_source_reports_bare_skeleton(self):
        adoption = analyze("# Empty\n", "CLAUDE.md")
        report = render_adoption_report(adoption, "bare", "a", "b")
        assert "No mechanizable content" in report


class TestEndToEnd:
    def test_written_draft_loads_and_discovers(self, tmp_path):
        """The written draft must survive the real loader and coexist
        with discovery (policies dir is ignored by discovery)."""
        (tmp_path / "CLAUDE.md").write_text(SAMPLE)
        proc_dir = tmp_path / ".processes"
        proc_dir.mkdir()
        (proc_dir / "policies").mkdir()

        adoption = analyze(SAMPLE, "CLAUDE.md")
        defn_data = build_definition("my-workflow", adoption)
        (proc_dir / "my-workflow.yaml").write_text(
            yaml.dump(defn_data, sort_keys=False)
        )
        (proc_dir / "policies" / "my-workflow.yaml").write_text(
            yaml.dump(build_policy("my-workflow", adoption), sort_keys=False)
        )

        defn = load_definition(proc_dir / "my-workflow.yaml")
        assert defn.name == "my-workflow"

        from turnstile_core.definition.loader import discover_definitions_full
        discovered = discover_definitions_full(tmp_path)
        assert "my-workflow" in discovered
        assert len(discovered) == 1

    def test_find_source_preference(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# agents")
        assert find_source(tmp_path).name == "AGENTS.md"
        (tmp_path / "CLAUDE.md").write_text("# claude")
        assert find_source(tmp_path).name == "CLAUDE.md"
        assert find_source(tmp_path / "nowhere" if False else tmp_path) is not None

    def test_no_source_returns_none(self, tmp_path):
        assert find_source(tmp_path) is None


class TestDenyGlobMapping:
    def test_force_push_alone_stays_narrow(self):
        from turnstile_core.ops.adopt import _deny_globs_for
        globs = _deny_globs_for("Never force push.")
        assert "git push --force*" in globs
        assert "git push*" not in globs

    def test_plain_and_force_push_both_blocked(self):
        from turnstile_core.ops.adopt import _deny_globs_for
        globs = _deny_globs_for(
            "Never push directly to main. Never force push."
        )
        assert "git push*" in globs
        assert "git push --force*" in globs

    def test_unmappable_prohibition_empty(self):
        from turnstile_core.ops.adopt import _deny_globs_for
        assert _deny_globs_for("Never use global variables.") == []

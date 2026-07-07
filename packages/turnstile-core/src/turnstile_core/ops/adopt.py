"""EXPERIMENTAL: adopt an existing CLAUDE.md / rules file into a process.

Every CLAUDE.md in the wild is a process definition wishing it were
executable: "always run the tests before committing", "never push to
main", a block of build commands. This module turns that begging
markdown into the beginnings of an enforceable workflow.

The split of labor mirrors the engine's own philosophy:

- **Deterministic extraction (this module):** find the commands, the
  imperative rules, and the prohibitions; classify what can be
  mechanized (gates, deny_commands) and what cannot; draft a *valid*
  process definition and acceptance policy.
- **Judgment (the human or their agent):** review the draft, resolve
  the items the adoption report marks as needing judgment, and decide
  how strict the policy should be.

The adoption report is honest by construction: every extracted item is
listed with its source line and where it went — adopted as a gate,
adopted as a command restriction, needs judgment, or stays in markdown
(behavioral guidance is CLAUDE.md's proper job; see the structural vs.
behavioral distinction in docs/vision.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from turnstile_core.definition.model import ProcessDefinition

# Files we know how to find, in preference order.
KNOWN_SOURCES = [
    "CLAUDE.md",
    "AGENTS.md",
    ".cursorrules",
    ".cursor/rules",
    "GEMINI.md",
    ".github/copilot-instructions.md",
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
_FENCE_RE = re.compile(r"^(```+|~~~+)\s*([\w+-]*)")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)")
_RULE_HINT_RE = re.compile(
    r"\b(always|never|must|do not|don't|before|after|ensure|require[sd]?"
    r"|verify|make sure|first|prior to)\b",
    re.I,
)
_PROHIBITION_RE = re.compile(r"\b(never|do not|don't|must not|no direct)\b", re.I)
_SEQUENCE_RE = re.compile(r"\b(before|after|first|then|prior to|once)\b", re.I)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

_CODE_LANGS = {"", "bash", "sh", "shell", "console", "zsh", "text"}

# Tools whose presence at the start of an inline code span marks it as
# a command rather than an identifier.
_COMMAND_TOOLS = {
    "git", "uv", "uvx", "npm", "pnpm", "yarn", "npx", "make", "pytest",
    "cargo", "go", "python", "python3", "pip", "poetry", "ruff", "mypy",
    "eslint", "tsc", "docker", "gradle", "mvn", "rspec", "bundle",
    "rake", "just", "tox", "deno", "bun",
}

# Substrings that mark a command as verification-shaped (gate-worthy).
_GATE_MARKERS = [
    "pytest", " test", "test ", "tests/", "jest", "vitest", "rspec",
    "ruff", "eslint", "flake8", "pylint", "clippy", "golangci",
    "mypy", "tsc", "pyright", "typecheck", "lint",
    "fmt --check", "format --check", "prettier --check",
]

# Prohibition keyword -> command glob(s). Order matters: more specific
# phrases first so "force push" wins over "push".
_DENY_MAP: list[tuple[str, list[str]]] = [
    ("force push", ["git push --force*", "git push -f*"]),
    ("force-push", ["git push --force*", "git push -f*"]),
    ("push", ["git push*"]),
    ("deploy", ["*deploy*"]),
    ("publish", ["*publish*"]),
    ("release", ["gh release*"]),
    ("merge", ["gh pr merge*", "git merge*"]),
    ("tag", ["git tag *"]),
    ("rebase", ["git rebase*"]),
]


@dataclass
class Candidate:
    """A piece of workflow-shaped content found in the source file."""

    kind: str  # "command" | "rule"
    text: str
    line: int
    section: str


@dataclass
class Adoption:
    """The result of analyzing a source file."""

    source: str
    gates: list[dict[str, str]] = field(default_factory=list)
    deny_commands: list[str] = field(default_factory=list)
    deny_sources: list[Candidate] = field(default_factory=list)
    needs_judgment: list[Candidate] = field(default_factory=list)
    stays_in_markdown: list[Candidate] = field(default_factory=list)
    other_commands: list[Candidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def find_source(project_root: Path) -> Path | None:
    """Locate the most likely rules file in a project."""
    for name in KNOWN_SOURCES:
        path = project_root / name
        if path.is_file():
            return path
    return None


def extract_candidates(text: str) -> list[Candidate]:
    """Pull commands and imperative rules out of markdown, with line
    numbers and section context. Purely lexical; no judgment."""
    candidates: list[Candidate] = []
    section = ""
    in_fence = False
    fence_lang = ""

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()

        fence = _FENCE_RE.match(line.strip())
        if fence:
            if in_fence:
                in_fence = False
            else:
                in_fence = True
                fence_lang = fence.group(2).lower()
            continue

        if in_fence:
            if fence_lang not in _CODE_LANGS:
                continue
            cmd = line.strip()
            if not cmd or cmd.startswith("#"):
                continue
            cmd = cmd.removeprefix("$ ").strip()
            candidates.append(
                Candidate(kind="command", text=cmd, line=lineno, section=section)
            )
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(2).strip()
            continue

        bullet = _BULLET_RE.match(line)
        sentence = bullet.group(1).strip() if bullet else line.strip()
        if sentence and _RULE_HINT_RE.search(sentence):
            candidates.append(
                Candidate(kind="rule", text=sentence, line=lineno, section=section)
            )
            # Inline commands inside rules are command candidates too
            for span in _INLINE_CODE_RE.findall(sentence):
                first_word = span.strip().split(" ")[0]
                if first_word in _COMMAND_TOOLS:
                    candidates.append(
                        Candidate(
                            kind="command", text=span.strip(),
                            line=lineno, section=section,
                        )
                    )

    return candidates


def _is_gate_command(command: str) -> bool:
    lowered = command.lower()
    return any(marker in lowered for marker in _GATE_MARKERS)


def _deny_globs_for(rule_text: str) -> list[str]:
    """Map a prohibition sentence to command globs.

    Keywords are consumed on match (more specific phrases first), so
    "never force push" yields only the force globs, while "never push
    directly to main; never force push" yields both the force globs
    and the plain "git push*" — the leftover "push" is a distinct
    prohibition, not part of the already-matched phrase.
    """
    working = rule_text.lower()
    globs: list[str] = []
    for keyword, patterns in _DENY_MAP:
        if keyword in working:
            working = working.replace(keyword, " ")
            for p in patterns:
                if p not in globs:
                    globs.append(p)
    return globs


def analyze(text: str, source_name: str) -> Adoption:
    """Classify extracted candidates into what can be mechanized."""
    candidates = extract_candidates(text)
    adoption = Adoption(source=source_name)

    seen_gate_commands: set[str] = set()
    for c in candidates:
        if c.kind == "command":
            normalized = " ".join(c.text.split())
            if _is_gate_command(normalized):
                if normalized not in seen_gate_commands:
                    seen_gate_commands.add(normalized)
                    adoption.gates.append({
                        "command": normalized,
                        "message": (
                            f"Adopted from {source_name}:{c.line}"
                            + (f" ({c.section})" if c.section else "")
                        ),
                    })
            else:
                adoption.other_commands.append(c)
            continue

        # Rules
        if _PROHIBITION_RE.search(c.text):
            globs = _deny_globs_for(c.text)
            if globs:
                for g in globs:
                    if g not in adoption.deny_commands:
                        adoption.deny_commands.append(g)
                adoption.deny_sources.append(c)
            else:
                adoption.needs_judgment.append(c)
        elif _SEQUENCE_RE.search(c.text):
            adoption.needs_judgment.append(c)
        else:
            adoption.stays_in_markdown.append(c)

    # Keep drafts reviewable: cap gates, surface the overflow
    if len(adoption.gates) > 6:
        overflow = adoption.gates[6:]
        adoption.gates = adoption.gates[:6]
        for g in overflow:
            adoption.needs_judgment.append(Candidate(
                kind="command", text=g["command"], line=0,
                section="(gate overflow — add by hand if wanted)",
            ))

    return adoption


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def build_definition(name: str, adoption: Adoption) -> dict[str, Any]:
    """Draft a process definition dict from an adoption analysis.

    The skeleton is understand -> implement -> verify -> done, with
    extracted gates on verify's exit and prohibitions as
    deny_commands on every working state. Guaranteed to validate as a
    ProcessDefinition.
    """
    denies = adoption.deny_commands

    def perms(edit: bool) -> dict[str, Any]:
        p: dict[str, Any] = {}
        if not edit:
            p["edit"] = False
        if denies:
            p["deny_commands"] = list(denies)
        return p

    states: list[dict[str, Any]] = [
        {"id": "start", "type": "initial", "transitions": ["understand"]},
    ]

    understand: dict[str, Any] = {
        "id": "understand",
        "description": "Survey the work before changing anything",
        "transitions": ["implement", "abandoned"],
    }
    if perms(False):
        understand["permissions"] = perms(False)
    states.append(understand)

    implement: dict[str, Any] = {
        "id": "implement",
        "description": "Do the work",
        "transitions": ["verify", "understand", "abandoned"],
    }
    if perms(True):
        implement["permissions"] = perms(True)
    states.append(implement)

    verify: dict[str, Any] = {
        "id": "verify",
        "description": (
            f"Prove the work holds — gates adopted from {adoption.source}"
        ),
        "transitions": ["done", "implement"],
    }
    if perms(False):
        verify["permissions"] = perms(False)
    if adoption.gates:
        verify["on_exit"] = {
            "validate": [
                {
                    "command": g["command"],
                    "expect": "exit_code(0)",
                    "message": g["message"],
                }
                for g in adoption.gates
            ]
        }
    states.append(verify)

    states.append({"id": "done", "type": "terminal"})
    states.append({
        "id": "abandoned",
        "type": "terminal",
        "description": "Work abandoned (not worth pursuing, or superseded)",
    })

    data: dict[str, Any] = {
        "name": name,
        "description": (
            f"Adopted from {adoption.source} by 'turnstile adopt' — "
            f"review before enforcing"
        ),
        "version": "0.1.0",
        "metadata": {
            "author": "turnstile adopt",
            "tags": ["adopted"],
        },
        "parameters": [
            {
                "name": "task_name",
                "description": "Short name for the piece of work",
                "required": True,
            },
        ],
        "states": states,
    }

    # Fail here, not at the user's first `turnstile list`.
    ProcessDefinition(**data)
    return data


def build_policy(name: str, adoption: Adoption) -> dict[str, Any]:
    """Draft the matching acceptance policy."""
    policy: dict[str, Any] = {
        "process": name,
        "required_states": ["understand", "implement", "verify", "done"],
        "require_completed": True,
        "allow_skips": False,
        "enforce_binding": True,
    }
    if adoption.gates:
        policy["reverify"] = [{"state": "verify", "hook": "on_exit"}]
    return policy


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_adoption_report(
    adoption: Adoption,
    name: str,
    definition_path: str,
    policy_path: str,
) -> str:
    """The honest ledger of what went where."""
    src = adoption.source
    lines = [f"## Adoption report — {src} → `{name}`", ""]

    lines.append(f"Draft definition: `{definition_path}`")
    lines.append(f"Draft policy: `{policy_path}`")
    lines.append("")

    if adoption.gates:
        lines.append(f"### Adopted as gates ({len(adoption.gates)})")
        lines.append("")
        lines.append("Run on leaving `verify`, expecting exit code 0. "
                     "Re-executed at acceptance by the policy.")
        lines.append("")
        for g in adoption.gates:
            lines.append(f"- `{g['command']}` — {g['message']}")
        lines.append("")

    if adoption.deny_commands:
        lines.append("### Adopted as command restrictions")
        lines.append("")
        lines.append(
            "Blocked in every working state ("
            + ", ".join(f"`{g}`" for g in adoption.deny_commands)
            + "), from:"
        )
        lines.append("")
        for c in adoption.deny_sources:
            lines.append(f"- {src}:{c.line} — \"{c.text}\"")
        lines.append("")

    if adoption.needs_judgment:
        lines.append(f"### Needs your judgment ({len(adoption.needs_judgment)})")
        lines.append("")
        lines.append(
            "These are workflow-shaped but could not be mechanized "
            "automatically — sequencing rules become state ordering; "
            "conditional prohibitions become gates or extra states:"
        )
        lines.append("")
        for c in adoption.needs_judgment:
            loc = f"{src}:{c.line}" if c.line else c.section
            lines.append(f"- {loc} — \"{c.text}\"")
        lines.append("")

    if adoption.stays_in_markdown:
        lines.append(
            f"### Stays in {src} ({len(adoption.stays_in_markdown)})"
        )
        lines.append("")
        lines.append(
            "Behavioral guidance — style, naming, judgment. Turnstile "
            "enforces structure, not judgment (docs/vision.md); these "
            "belong exactly where they are:"
        )
        lines.append("")
        for c in adoption.stays_in_markdown[:10]:
            lines.append(f"- {src}:{c.line} — \"{c.text}\"")
        if len(adoption.stays_in_markdown) > 10:
            lines.append(
                f"- … and {len(adoption.stays_in_markdown) - 10} more"
            )
        lines.append("")

    if not adoption.gates and not adoption.deny_commands:
        lines.append(
            "> No mechanizable content found — the draft is a bare "
            "skeleton. Add gates by hand (docs/reference.md)."
        )
        lines.append("")

    lines.append("### Next steps")
    lines.append("")
    lines.append(f"1. Review the draft: `turnstile validate {definition_path}`"
                 f" and `turnstile dry-run --file {definition_path}`")
    lines.append("2. Resolve the judgment items above (edit the draft).")
    lines.append(f"3. Try it: `process_start(\"{name}\", "
                 f"{{\"task_name\": \"...\"}})` on your next task.")
    lines.append("4. When it has earned trust, wire the policy into CI "
                 "(docs/verification.md) and make the check required.")
    lines.append("")
    return "\n".join(lines)

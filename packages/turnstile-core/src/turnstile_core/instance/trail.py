"""Trail integrity: hash-chained history and external anchoring.

The chain makes an instance's history tamper-evident; the anchor makes
tampering *detectable by someone other than the writer* by depositing
the chain head outside the agent's write domain while the work
happens. See docs/guarantee.md — the anchor location must not be
writable by the agent, or it proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from turnstile_core.instance.model import HistoryEntry, ProcessInstance

logger = logging.getLogger(__name__)


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
    """Record the current chain head in a file-based anchor.

    The file must live outside the agent's write domain (an operator
    directory, a mounted read-only-to-the-agent volume). For remote
    anchoring, use Anchor with a command instead.
    """
    head = compute_chain_head(instance)
    anchors: dict[str, Any] = {}
    if anchor_path.exists():
        anchors = json.loads(anchor_path.read_text())
    anchors[instance.instance_id] = {
        "head": head,
        "entries": len(instance.history),
    }
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    anchor_path.write_text(json.dumps(anchors, indent=2))
    return head


class Anchor:
    """Deposits chain heads at every persistence event.

    Two backends, usable together:

    - ``file``: append/update a JSON anchor file (path should be
      outside the agent's write domain).
    - ``command``: run a shell command with ``{instance_id}``,
      ``{head}``, and ``{entries}`` substituted — e.g. a curl to an
      append-only endpoint, or a git-notes push under a separate
      credential.

    Anchoring is fail-open (a broken anchor must not brick the
    engine) but never silent: failures are logged, and verification
    will fail later anyway when the anchor is missing.
    """

    def __init__(
        self,
        file: str | Path | None = None,
        command: str | None = None,
    ) -> None:
        self.file = Path(file).expanduser() if file else None
        self.command = command

    def record(self, instance: ProcessInstance) -> None:
        head = compute_chain_head(instance)
        if self.file is not None:
            try:
                write_anchor(self.file, instance)
            except Exception:
                logger.warning(
                    "Anchor file write failed for %s (continuing)",
                    instance.instance_id, exc_info=True,
                )
        if self.command:
            cmd = (
                self.command
                .replace("{instance_id}", instance.instance_id)
                .replace("{head}", head)
                .replace("{entries}", str(len(instance.history)))
            )
            try:
                subprocess.run(
                    cmd, shell=True, capture_output=True, timeout=10, check=True
                )
            except Exception:
                logger.warning(
                    "Anchor command failed for %s (continuing): %s",
                    instance.instance_id, cmd, exc_info=True,
                )

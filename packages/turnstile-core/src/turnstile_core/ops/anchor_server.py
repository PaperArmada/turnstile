"""EXPERIMENTAL: reference remote anchor server.

A minimal append-only anchor store (stdlib only) demonstrating the
production shape of contemporaneous anchoring from docs/guarantee.md.
Run it anywhere the agent has no credentials — another machine, an
internal service, a locked-down container:

    python -m turnstile_core.ops.anchor_server --port 8123 --state /var/lib/turnstile

Point the engine at it in .processes/registry.yaml:

    settings:
      verification:
        anchor_command: >-
          curl -fsS -X POST http://anchor.internal:8123/anchors
          -H 'Content-Type: application/json'
          -d '{"instance_id":"{instance_id}","head":"{head}","entries":{entries}}'

And fetch the anchor map in CI before verifying:

    curl -fsS http://anchor.internal:8123/anchors -o /tmp/anchors.json
    turnstile verify --policy policy.yaml   # policy.anchor_file: /tmp/anchors.json

Append-only semantics — the property that makes anchors meaningful:

- A new instance may be anchored freely.
- An existing anchor may only be *extended*: a POST with more entries
  than recorded supersedes (the chain grew).
- A POST with fewer entries, or the same count and a different head,
  is a rewrite attempt and is rejected with 409. History can grow;
  it cannot be edited.

Every accepted and rejected request is appended to ``anchors.log``
for after-the-fact forensics.
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class AnchorStore:
    """Append-only anchor state with a JSON snapshot and an audit log."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = state_dir / "anchors.json"
        self.log_path = state_dir / "anchors.log"
        self._lock = threading.Lock()
        self._anchors: dict[str, dict] = {}
        if self.snapshot_path.exists():
            self._anchors = json.loads(self.snapshot_path.read_text())

    def record(self, instance_id: str, head: str, entries: int) -> tuple[bool, str]:
        """Apply append-only rules. Returns (accepted, reason)."""
        with self._lock:
            existing = self._anchors.get(instance_id)
            if existing is not None:
                if entries < existing["entries"]:
                    self._log("REJECT", instance_id, head, entries,
                              "fewer entries than recorded (truncation)")
                    return False, "rewrite rejected: fewer entries than recorded"
                if entries == existing["entries"] and head != existing["head"]:
                    self._log("REJECT", instance_id, head, entries,
                              "same entry count, different head (edit)")
                    return False, "rewrite rejected: head mismatch at same entry count"
                if entries == existing["entries"]:
                    return True, "idempotent"
            self._anchors[instance_id] = {"head": head, "entries": entries}
            self.snapshot_path.write_text(json.dumps(self._anchors, indent=2))
            self._log("ACCEPT", instance_id, head, entries, "")
            return True, "anchored"

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._anchors)

    def _log(self, verdict: str, instance_id: str, head: str,
             entries: int, note: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with open(self.log_path, "a") as f:
            f.write(f"[{ts}] {verdict} {instance_id} entries={entries} "
                    f"head={head}{' — ' + note if note else ''}\n")


def make_handler(store: AnchorStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet by default
            pass

        def _respond(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/anchors":
                self._respond(200, store.snapshot())
                return
            if self.path.startswith("/anchors/"):
                iid = self.path.removeprefix("/anchors/").strip("/")
                entry = store.snapshot().get(iid)
                if entry is None:
                    self._respond(404, {"error": f"no anchor for '{iid}'"})
                else:
                    self._respond(200, {iid: entry})
                return
            self._respond(404, {"error": "unknown path"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/anchors":
                self._respond(404, {"error": "unknown path"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length))
                instance_id = str(data["instance_id"])
                head = str(data["head"])
                entries = int(data["entries"])
            except Exception as e:
                self._respond(400, {"error": f"bad request: {e}"})
                return
            accepted, reason = store.record(instance_id, head, entries)
            self._respond(200 if accepted else 409,
                          {"accepted": accepted, "reason": reason})

    return Handler


def serve(state_dir: Path, host: str = "127.0.0.1", port: int = 8123) -> ThreadingHTTPServer:
    """Build (but do not start) the server; caller runs serve_forever()."""
    store = AnchorStore(state_dir)
    return ThreadingHTTPServer((host, port), make_handler(store))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--state", default="./anchor-state",
                        help="Directory for anchors.json and anchors.log")
    args = parser.parse_args()
    server = serve(Path(args.state), args.host, args.port)
    print(f"turnstile anchor server on {args.host}:{args.port} "
          f"(state: {args.state})")
    server.serve_forever()


if __name__ == "__main__":
    main()

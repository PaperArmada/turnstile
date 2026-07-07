"""Tests for the reference anchor server: append-only semantics and
the full remote-anchoring loop (engine -> server -> CI fetch -> verify)."""

import json
import threading
import urllib.request

import pytest

from turnstile_core.ops.anchor_server import AnchorStore, serve


class TestAppendOnlyStore:
    def test_new_anchor_accepted(self, tmp_path):
        store = AnchorStore(tmp_path)
        accepted, _ = store.record("abc", "head1", 1)
        assert accepted

    def test_chain_growth_accepted(self, tmp_path):
        store = AnchorStore(tmp_path)
        store.record("abc", "head1", 1)
        accepted, _ = store.record("abc", "head2", 2)
        assert accepted
        assert store.snapshot()["abc"] == {"head": "head2", "entries": 2}

    def test_truncation_rejected(self, tmp_path):
        store = AnchorStore(tmp_path)
        store.record("abc", "head3", 3)
        accepted, reason = store.record("abc", "forged", 2)
        assert not accepted
        assert "fewer entries" in reason
        assert store.snapshot()["abc"]["head"] == "head3"

    def test_edit_at_same_count_rejected(self, tmp_path):
        store = AnchorStore(tmp_path)
        store.record("abc", "head3", 3)
        accepted, reason = store.record("abc", "forged", 3)
        assert not accepted
        assert "head mismatch" in reason

    def test_idempotent_repost_accepted(self, tmp_path):
        store = AnchorStore(tmp_path)
        store.record("abc", "head3", 3)
        accepted, reason = store.record("abc", "head3", 3)
        assert accepted
        assert reason == "idempotent"

    def test_rejections_logged(self, tmp_path):
        store = AnchorStore(tmp_path)
        store.record("abc", "head3", 3)
        store.record("abc", "forged", 2)
        log = (tmp_path / "anchors.log").read_text()
        assert "ACCEPT abc" in log
        assert "REJECT abc" in log

    def test_survives_restart(self, tmp_path):
        AnchorStore(tmp_path).record("abc", "head1", 1)
        reloaded = AnchorStore(tmp_path)
        assert reloaded.snapshot()["abc"]["entries"] == 1


@pytest.fixture
def server(tmp_path):
    httpd = serve(tmp_path / "state", host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()


def post(base, payload):
    req = urllib.request.Request(
        f"{base}/anchors",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestHttpApi:
    def test_post_and_get(self, server):
        code, body = post(server, {"instance_id": "abc", "head": "h1", "entries": 1})
        assert code == 200 and body["accepted"]

        with urllib.request.urlopen(f"{server}/anchors") as resp:
            snapshot = json.loads(resp.read())
        assert snapshot["abc"] == {"head": "h1", "entries": 1}

        with urllib.request.urlopen(f"{server}/anchors/abc") as resp:
            single = json.loads(resp.read())
        assert single["abc"]["head"] == "h1"

    def test_rewrite_gets_409(self, server):
        post(server, {"instance_id": "abc", "head": "h3", "entries": 3})
        code, body = post(server, {"instance_id": "abc", "head": "forged", "entries": 3})
        assert code == 409
        assert not body["accepted"]

    def test_bad_request(self, server):
        code, _ = post(server, {"instance_id": "abc"})
        assert code == 400

    def test_engine_anchor_command_end_to_end(self, server, tmp_path):
        """The full remote loop: engine anchors via anchor_command,
        CI fetches the snapshot, verify compares against it."""
        import asyncio
        import subprocess
        import yaml

        from turnstile_core.ops.verify import (
            AcceptancePolicy,
            verify_instance,
        )
        from turnstile_core.runtime.engine import Engine

        root = tmp_path / "project"
        (root / ".processes").mkdir(parents=True)
        (root / ".processes" / "simple.yaml").write_text(yaml.dump({
            "name": "simple",
            "states": [
                {"id": "start", "type": "initial", "transitions": ["done"]},
                {"id": "done", "type": "terminal"},
            ],
        }))
        (root / ".processes" / "registry.yaml").write_text(yaml.dump({
            "version": "1.0",
            "settings": {"verification": {
                "record_git_sha": False,
                "anchor_command": (
                    "curl -fsS -X POST " + server + "/anchors "
                    "-H 'Content-Type: application/json' "
                    '-d \'{"instance_id":"{instance_id}",'
                    '"head":"{head}","entries":{entries}}\''
                ),
            }},
        }))

        engine = Engine(root)
        iid = engine.start("simple")["instance_id"]
        asyncio.run(engine.transition(iid, "done"))

        # "CI" fetches the anchor snapshot from the remote
        fetched = tmp_path / "fetched-anchors.json"
        with urllib.request.urlopen(f"{server}/anchors") as resp:
            fetched.write_bytes(resp.read())

        policy = AcceptancePolicy(
            process="simple",
            required_states=["done"],
            anchor_file=str(fetched),
        )
        report = asyncio.run(verify_instance(root, iid, policy))
        assert report.passed, report.render()
        assert any(
            c.status == "ATTESTED" and c.name == "trail integrity"
            for c in report.checks
        )

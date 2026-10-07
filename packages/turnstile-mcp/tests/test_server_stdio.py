"""Wire-level smoke test for the MCP server over stdio.

Spawns ``python -m turnstile_mcp.server`` as a real subprocess and speaks
JSON-RPC to it. A plain import test would not catch a server that imports
but fails to serve, and this is the only test that exercises the ``mcp``
SDK at all: the repo's ``uv.lock`` is untracked, so CI resolves the SDK
fresh, and an upstream break (such as the 2.0 removal of
``mcp.server.fastmcp``, GH #42) surfaces here rather than in every
user's fresh install.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

EXPECTED_TOOLS = {
    "process_list", "process_reload_definitions", "process_info",
    "process_start", "process_status", "process_transition", "process_skip",
    "process_signal", "process_abandon", "process_undo", "process_handoff",
    "process_history", "process_validate_definition", "process_graph",
    "process_dry_run", "process_diff", "process_migrate", "process_gc",
    "process_analytics", "process_check_completed",
}

MINIMAL_PROCESS = """\
name: smoke
description: "stdio smoke"
version: "1.0.0"
states:
  - id: start
    type: initial
    transitions: [done]
  - id: done
    type: terminal
"""

REGISTRY = 'version: "1.0"\nenforcement:\n  mode: "off"\nlocal:\n- smoke\n'

INITIALIZE = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "smoke", "version": "0"},
}


def _rpc(id_: int, method: str, params: dict | None = None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _run_session(project: Path, requests: list[dict]) -> dict[int, dict]:
    """Drive the server request by request; return responses keyed by id.

    Each request is written and its response awaited before the next one is
    sent, and stdin is closed only at the end. Writing everything and closing
    stdin up front races the server's EOF shutdown against the later calls.
    The first request must be ``initialize``; the ``initialized``
    notification is sent once its response has arrived, per the spec.
    """
    assert requests[0]["method"] == "initialize"
    # Pin the project explicitly: the server honours TURNSTILE_PROJECT_DIR
    # over cwd, and a dev shell may have it exported.
    env = {**os.environ, "TURNSTILE_PROJECT_DIR": str(project)}
    stderr_path = project / "server.stderr"
    responses: dict[int, dict] = {}
    with stderr_path.open("w") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-m", "turnstile_mcp.server"],
            cwd=project,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,  # a file, so the server can never block on it
            text=True,
        )
        assert proc.stdin and proc.stdout
        watchdog = threading.Timer(60, proc.kill)
        watchdog.start()
        try:
            for req in requests:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        proc.wait(timeout=10)
                        pytest.fail(
                            f"server closed stdout before answering "
                            f"id={req['id']}; stderr:\n{stderr_path.read_text()}"
                        )
                    msg = json.loads(line)
                    if msg.get("id") != req["id"]:
                        continue
                    if "error" in msg:
                        pytest.fail(
                            f"JSON-RPC error for {req['method']}: "
                            f"{msg['error']}; stderr:\n{stderr_path.read_text()}"
                        )
                    responses[req["id"]] = msg
                    break
                if req["method"] == "initialize":
                    proc.stdin.write(json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    ) + "\n")
                    proc.stdin.flush()
            proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "server did not exit after stdin EOF; stderr:\n"
                    f"{stderr_path.read_text()}"
                )
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
    return responses


def _text(call_result: dict) -> str:
    return next(
        c["text"] for c in call_result["content"] if c.get("type") == "text"
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proc_dir = tmp_path / ".processes"
    proc_dir.mkdir()
    (proc_dir / "smoke.yaml").write_text(MINIMAL_PROCESS)
    (proc_dir / "registry.yaml").write_text(REGISTRY)
    return tmp_path


def test_initialize_lists_tools_and_serves_a_call(project: Path) -> None:
    responses = _run_session(project, [
        _rpc(1, "initialize", INITIALIZE),
        _rpc(2, "tools/list", {}),
        _rpc(3, "tools/call", {"name": "process_list", "arguments": {}}),
    ])

    init = responses[1]["result"]
    assert init["serverInfo"]["name"] == "turnstile"
    # mcp 2.x reports an empty version unless the server declares one.
    assert init["serverInfo"]["version"] == (
        importlib.metadata.version("turnstile-mcp")
    )

    tools = {t["name"]: t for t in responses[2]["result"]["tools"]}
    assert set(tools) == EXPECTED_TOOLS
    # The error-surfacing wrapper must not hide the handler signatures
    # from the SDK's schema generation.
    assert tools["process_info"]["inputSchema"]["required"] == ["name"]
    assert set(tools["process_transition"]["inputSchema"]["required"]) == {
        "instance_id", "target_state",
    }

    call = responses[3]["result"]
    assert not call.get("isError"), call
    listed = json.loads(_text(call))
    assert [p["name"] for p in listed["processes"]] == ["smoke"]


def test_engine_errors_reach_the_client_with_their_message(project: Path) -> None:
    """mcp 2.x strips the text of any exception that is not a ToolError.

    Engine errors are the server's guidance to the agent, so the server
    re-raises them as ToolError. Pin that the reason survives the wire.
    """
    responses = _run_session(project, [
        _rpc(1, "initialize", INITIALIZE),
        _rpc(2, "tools/call", {
            "name": "process_info", "arguments": {"name": "nope"},
        }),
        _rpc(3, "tools/call", {
            "name": "process_transition",
            "arguments": {"instance_id": "zzz", "target_state": "done"},
        }),
    ])

    info = responses[2]["result"]
    assert info.get("isError") is True, info
    assert "nope" in _text(info), _text(info)

    transition = responses[3]["result"]
    assert transition.get("isError") is True, transition
    assert "zzz" in _text(transition), _text(transition)

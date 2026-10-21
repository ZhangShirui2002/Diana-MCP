import json
import threading
import time

import pytest

from diana_mcp.bridge import diana_bridge
from diana_mcp.bridge_client import BridgeEndpoint, BridgeError, request


@pytest.fixture
def server():
    srv = diana_bridge.BridgeServer({}, port=0, token="secret")
    yield srv
    srv.close()


def test_execute_returns_final_expression_and_stdout(server):
    result = server.execute("x = 2\nprint('hi')\nx * 21")
    assert result["ok"]
    assert result["stdout"] == "hi\n"
    assert result["value"] == "42"


def test_bridge_itself_blocks_solver_aliases_and_restores_the_diana_function():
    original = lambda name: "ran " + name
    srv = diana_bridge.BridgeServer({"runSolver": original}, port=0, token="secret")
    try:
        result = srv.execute("f = runSolver\nf('analysis')")
        assert not result["ok"] and "reviewed MCP solve workflow" in result["error"]
        assert srv.namespace["runSolver"] is original
    finally:
        srv.close()


def test_state_persists_between_calls(server):
    server.execute("counter = 1")
    assert server.execute("counter += 1\ncounter")["value"] == "2"


def test_error_keeps_traceback_and_partial_stdout(server):
    result = server.execute("print('before')\n1 / 0")
    assert not result["ok"]
    assert "ZeroDivisionError" in result["error"]
    assert result["stdout"] == "before\n"


def test_system_exit_does_not_escape(server):
    result = server.execute("raise SystemExit(3)")
    assert not result["ok"]
    assert "SystemExit" in result["error"]


def test_long_output_is_truncated(server):
    result = server.execute("print('x' * 300000)")
    assert result["ok"]
    assert "[truncated" in result["stdout"]
    assert len(result["stdout"]) < 210000


def test_dispatch_rejects_wrong_token(server):
    response = server.dispatch(json.dumps({"token": "nope", "op": "exec", "code": "1"}).encode())
    assert response == {"id": None, "ok": False, "error": "invalid token"}


def test_dispatch_drops_expired_requests_without_running_them(server):
    raw = json.dumps({"token": "secret", "op": "exec", "code": "ran = True", "deadline": time.time() - 1})
    response = server.dispatch(raw.encode())
    assert response["expired"] and not response["ok"]
    assert "ran" not in server.namespace


def test_socket_round_trip_and_stop(server):
    def pump():
        while not server.closed:
            server.poll()
            time.sleep(0.01)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    endpoint = BridgeEndpoint(server.host, server.port, "secret")

    assert request(endpoint, "ping", timeout=5)["info"]["bridge_version"] == diana_bridge.BRIDGE_VERSION
    assert request(endpoint, "exec", timeout=5, code="6 * 7")["value"] == "42"
    with pytest.raises(BridgeError, match="invalid token"):
        request(BridgeEndpoint(server.host, server.port, "wrong"), "ping", timeout=5)

    assert request(endpoint, "stop", timeout=5)["ok"]
    thread.join(timeout=5)
    assert server.closed

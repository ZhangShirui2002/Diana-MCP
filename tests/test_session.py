import json
import subprocess
import sys

import pytest

from diana_mcp.bridge_client import BridgeError
from diana_mcp.session import SessionManager, pid_alive


def test_pid_alive():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert not pid_alive(process.pid)
    import os

    assert pid_alive(os.getpid())


def test_discover_attach_execute_detach(standalone_bridge):
    session_dir, process, data = standalone_bridge
    manager = SessionManager([session_dir])

    sessions = manager.discover()
    assert [s.pid for s in sessions] == [data["pid"]]
    assert sessions[0].responsive and not sessions[0].launched_here

    session = manager.attach()
    assert session.pid == data["pid"]
    assert manager.execute("1 + 1", timeout=10)["value"] == "2"

    manager.detach()
    assert process.wait(timeout=10) == 0
    assert manager.discover() == []


def test_stale_session_file_is_removed(tmp_path):
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    stale = tmp_path / f"{finished.pid}.json"
    stale.write_text(json.dumps({"pid": finished.pid, "host": "127.0.0.1", "port": 1, "token": "x"}))

    assert SessionManager([tmp_path]).discover() == []
    assert not stale.exists()


def test_require_without_session_asks_for_diana_start(tmp_path):
    with pytest.raises(BridgeError, match="diana_start"):
        SessionManager([tmp_path]).require()


def test_kill_tree_never_shuts_down_the_whole_wine_prefix(monkeypatch):
    """Cancelling one job must not touch the user's DianaIE: killing a host pid kills that process only.

    An earlier version ran `wineserver -k` here, which killed every DIANA process sharing the prefix.
    """
    import signal as signal_module

    from diana_mcp import config, session as session_module

    monkeypatch.setattr(config, "IS_WINDOWS", False)
    calls, killed = [], []
    monkeypatch.setattr(session_module.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(session_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    session_module.kill_tree(4321)

    assert killed == [(4321, getattr(signal_module, "SIGKILL", signal_module.SIGTERM))]
    assert not any("wineserver" in " ".join(call) for call in calls)

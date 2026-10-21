"""The bridge runs inside DianaIE's embedded Python 3.7; exercise it with DIANA's bundled interpreter."""
import json
import sys
import time
from pathlib import Path

import pytest

from diana_mcp import changes as ch
from diana_mcp import config
from diana_mcp.bridge_client import BridgeEndpoint, request

from conftest import start_standalone_bridge


def _diana_python():
    """DIANA's bundled interpreter, or None. Only used directly on Windows: off Windows it is a .exe that needs
    Wine and Windows spellings for every path in these tests, which is exercised on the Windows side instead."""
    if not config.IS_WINDOWS:
        return None
    try:
        installs = config.find_installs()
    except FileNotFoundError:
        return None
    return next((str(i.python) for i in installs if i.python.is_file()), None)


PYTHONS = [pytest.param(sys.executable, id="server-python"),
           pytest.param(_diana_python(), id="diana-python-3.7",
                        marks=pytest.mark.skipif(_diana_python() is None, reason="no DIANA Python"))]


@pytest.mark.parametrize("python", PYTHONS)
def test_journal_tags_user_and_bridge_commands(tmp_path, python):
    """Lines appearing between requests are the user's; lines written while serving a request are the AI's."""
    log = tmp_path / "IEhistory_260919_120000.abc.running.py"
    log.write_text("# header\n", encoding="utf-8")
    process, data = start_standalone_bridge(tmp_path / "state" / "sessions", python=python,
                                            env={"DIANA_MCP_TEST_LOG": str(log)})
    try:
        assert data["log_file"] == str(log) and data["journal"].endswith("IEhistory_260919_120000.jsonl")
        with open(log, "a", encoding="utf-8") as f:
            f.write('createSheet( "a", [ [ 0, 0 ], [ 1, 0 ], [ 1, 1 ] ] )\n')
        time.sleep(0.3)
        endpoint = BridgeEndpoint(data["host"], data["port"], data["token"])
        code = f"with open({str(log)!r}, 'a', encoding='utf-8') as f:\n    f.write('generateMesh( [  ] )\\n')"
        assert request(endpoint, "exec", timeout=10, code=code)["ok"]
        time.sleep(0.3)
        entries, _ = ch.read_log(log)
        ch.annotate(entries, ch.read_journal(Path(data["journal"]))[1])
        assert [(e.name, e.source) for e in entries] == [("createSheet", "user"), ("generateMesh", "ai")]
        assert json.loads((tmp_path / "state" / "log_dirs.json").read_text(encoding="utf-8")) == [str(tmp_path)]
    finally:
        process.kill()
        process.wait()


@pytest.mark.skipif(_diana_python() is None, reason="no DIANA installation with a bundled Python")
def test_bridge_protocol_under_diana_python(tmp_path):
    process, data = start_standalone_bridge(tmp_path / "sessions", python=_diana_python())
    try:
        endpoint = BridgeEndpoint(data["host"], data["port"], data["token"])
        assert request(endpoint, "exec", timeout=10, code="import sys\nsys.version_info[:2]")["value"] == "(3, 7)"
        failure = request(endpoint, "exec", timeout=10, code="undefined_name")
        assert not failure["ok"] and "NameError" in failure["error"]
        assert request(endpoint, "stop", timeout=10)["ok"]
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()

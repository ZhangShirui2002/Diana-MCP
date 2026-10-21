import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from diana_mcp import config


@pytest.fixture(autouse=True)
def no_real_embedding_model(monkeypatch, tmp_path):
    """Tests never load the embedding model configured on this machine, nor write into <project>/models."""
    monkeypatch.setenv("DIANA_MCP_VECTOR_MODEL", "none")
    monkeypatch.setenv("DIANA_MCP_MODELS_DIR", str(tmp_path / "models"))


def start_standalone_bridge(session_dir: Path, python: str = sys.executable,
                            env: dict | None = None) -> tuple[subprocess.Popen, dict]:
    """Run the bridge without DianaIE (plain polling loop) and wait for its session file."""
    import os

    session_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([python, str(config.BRIDGE_SCRIPT), "--standalone", str(session_dir)],
                               env=dict(os.environ, **(env or {})))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        files = list(session_dir.glob("*.json"))
        if files:
            return process, json.loads(files[0].read_text(encoding="utf-8"))
        if process.poll() is not None:
            raise RuntimeError(f"standalone bridge exited with {process.returncode}")
        time.sleep(0.1)
    process.kill()
    raise TimeoutError("standalone bridge did not write a session file")


@pytest.fixture
def standalone_bridge(tmp_path):
    process, data = start_standalone_bridge(tmp_path / "sessions")
    yield tmp_path / "sessions", process, data
    if process.poll() is None:
        process.kill()
        process.wait()

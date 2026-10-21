import threading
import time

import pytest

from diana_mcp import changes as ch
from diana_mcp.session import SessionManager
from diana_mcp.tracking import Tracker, format_report

from test_changes import write_log


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    """A fake ~/.DianaIE with one running session, no bridge and no running DianaIE."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ch, "log_dirs", lambda: [logs])
    monkeypatch.setattr(ch, "logs_of_running_dianaie", lambda files=None: {})
    log = write_log(logs / "IEhistory_260919_120000.abc.running.py",
                    'createSheet( "a", [ [ 0, 0 ], [ 1, 0 ], [ 1, 1 ] ] )', 'setViewPoint( "TOP" )')
    return Tracker(SessionManager([tmp_path / "sessions"])), log


def append(log, *lines):
    with open(log, "a", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))


def test_changes_lists_new_operations_once(isolated_history):
    tracker, log = isolated_history
    first = tracker.changes()
    assert first["since"] == "本次 DianaIE 会话开始以来"
    assert [op["what"] for op in first["operations"]] == ["新建面 “a”（3 个顶点）"]
    assert any("没有连接 bridge" in n for n in first["notes"])
    assert first["dianaie"]["tracking"] == "history only"
    assert "model_changes" not in first

    append(log, 'subtract( "a", [ "b" ], False, True )', "undo( 1 )", "fitAll(  )")
    second = tracker.changes()
    assert second["since"] == "上次查看之后"
    ops = second["operations"]
    assert [op["what"] for op in ops] == ["布尔减：从 “a” 中减去 “b”", "撤销 1 步"]
    assert ops[0]["undone"] and ops[1]["reverted"] == ["布尔减：从 “a” 中减去 “b”"]
    assert tracker.changes()["operations"] == []


def test_peek_does_not_advance(isolated_history):
    tracker, log = isolated_history
    tracker.changes()  # first look
    append(log, 'generateMesh( [  ] )')
    assert len(tracker.changes(advance=False)["operations"]) == 1
    assert len(tracker.changes()["operations"]) == 1
    assert tracker.changes()["operations"] == []


def test_wait_for_changes_returns_after_the_user_pauses(isolated_history):
    tracker, log = isolated_history
    tracker.changes()  # set the cursor
    timer = threading.Timer(0.3, append, (log, 'generateMesh( [  ] )'))
    timer.start()
    started = time.monotonic()
    assert tracker.wait_for_changes(timeout=10, quiet=0.4, poll=0.05)
    assert time.monotonic() - started >= 0.6
    timer.join()


def test_wait_ignores_views_and_queries(isolated_history):
    tracker, log = isolated_history
    tracker.changes()
    append(log, 'setViewPoint( "TOP" )', "projectName(  )")
    assert not tracker.wait_for_changes(timeout=1.0, quiet=0.2, poll=0.05)


def test_format_report():
    report = {
        "operations_total": 2,
        "operations": [
            {"what": "新建面 “a”", "undone": True},
            {"what": "撤销 1 步", "reverted": ["新建面 “a”"]},
        ],
        "model_changes": [{"text": "材料 “C30”：YOUNG 3e+10 → 3.2e+10", "by": "user"},
                          {"text": "新增分析 “L”", "by": "ai"}],
        "notes": ["x"],
    }
    text = format_report(report)
    assert "你的操作（2 步）：" in text
    assert "新建面 “a”（已撤销）" in text
    assert "撤销 1 步 → 撤销/重做了：新建面 “a”" in text
    assert "材料 “C30”：YOUNG 3e+10 → 3.2e+10\n" in text + "\n"
    assert "新增分析 “L”（AI）" in text
    assert text.endswith("注：x")

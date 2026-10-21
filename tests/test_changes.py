import json
from datetime import datetime
from pathlib import Path

import pytest

from diana_mcp import changes as ch
from diana_mcp.bridge import diana_bridge

HEADER = (
    "##############################################################################\n"
    "# DianaIE 10.5 update 2021-05-01 07:33:44 rev 948\n"
    "##############################################################################\n"
)


def write_log(path: Path, *lines: str) -> Path:
    path.write_text(HEADER + "".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_parse_call_reads_literal_arguments():
    name, args, kwargs = ch.parse_call('setParameter( "MATERIAL", "混凝土", "LINEAR/ELASTI/YOUNG", 3e+10 )')
    assert name == "setParameter" and args == ["MATERIAL", "混凝土", "LINEAR/ELASTI/YOUNG", 3e10] and kwargs == {}
    name, args, _ = ch.parse_call('selectResult( { "component": "TDtY", "result": "Displacements" } )')
    assert args == [{"component": "TDtY", "result": "Displacements"}]
    assert ch.parse_call("x = 1") is None and ch.parse_call("not python (") is None


def test_read_log_offsets_multiline_calls_and_partial_lines(tmp_path):
    log = tmp_path / "IEhistory_260919_162933.abc.running.py"
    log.write_bytes(
        HEADER.encode()
        + b'createSheet( "a", [ [ 0, 0 ],\n  [ 1, 0 ], [ 1, 1 ] ] )\n'
        + b"saveProject(  )\n"
        + b"generateMesh( [ "  # DianaIE is still writing this line
    )
    entries, next_offset = ch.read_log(log)
    assert [e.name for e in entries] == ["createSheet", "saveProject"]
    assert entries[0].args[1] == [[0, 0], [1, 0], [1, 1]]
    assert entries[0].offset == len(HEADER.encode())
    assert next_offset == log.stat().st_size - len(b"generateMesh( [ ")
    more, _ = ch.read_log(log, next_offset)
    assert more == []


@pytest.mark.parametrize("name, expected", [
    ("setParameter", "model"), ("createSheet", "model"), ("generateMesh", "model"), ("attach", "model"),
    ("setAnalysisCommandDetail", "analysis"), ("addAnalysis", "analysis"), ("setActiveInPhase", "analysis"),
    ("undo", "undo"), ("saveProject", "project"), ("importModel", "project"), ("runSolver", "run"),
    ("setViewPoint", "view"), ("fitAll", "view"), ("showAll", "view"), ("setShapeColor", "view"),
    ("setViewSettingValue", "view"), ("selectResult", "results"), ("gotoFrame", "results"),
    ("projectName", "query"), ("elementClassType", "query"), ("addSectionParameter", "report"),
])
def test_categories(name, expected):
    assert ch.category(name) == expected


def test_descriptions_are_readable():
    assert ch.describe("setParameter", ["MATERIAL", "C30", "LINEAR/ELASTI/YOUNG", 3.2e10]) == \
        "材料 “C30”：LINEAR/ELASTI/YOUNG = 3.2e+10（弹性模量）"
    assert ch.describe("createSheet", ["Sheet 8", [[0, 0], [1, 0], [1, 1]]]) == "新建面 “Sheet 8”（3 个顶点）"
    assert ch.describe("subtract", ["Sheet 8", ["Sheet 1"], False, True]) == "布尔减：从 “Sheet 8” 中减去 “Sheet 1”"
    assert ch.describe("attach", ["GEOMETRYLOAD", "P", "beam", [[2, 0.4]]]) == "荷载 “P” 挂到 “beam” 的 1 个点"
    assert ch.describe("setUnit", ["LENGTH", "MM"]) == "单位：长度 改为 MM"
    assert ch.describe("unknownCommand", [1, "x"]) == "unknownCommand(1, x)"


def entries_of(*calls, batches=None, counts=None):
    entries = []
    for i, text in enumerate(calls):
        entry = ch.Entry(i * 10, i * 10 + 9, text)
        entry.name, entry.args, entry.kwargs = ch.parse_call(text)
        entry.category = ch.category(entry.name)
        entry.description = ch.describe(entry.name, entry.args, text)
        if batches:
            entry.batch = batches[i]
            entry.undo = counts[batches[i]]
        entries.append(entry)
    return entries


def test_undo_without_journal_reverts_the_last_modifying_call():
    entries = entries_of(
        'createSheet( "a", [ [ 0, 0 ], [ 1, 0 ], [ 1, 1 ] ] )',
        'subtract( "a", [ "b" ], False, True )',
        'setViewPoint( "TOP" )',
        "undo( 1 )",
        'undo( 1 )',
        "redo( 1 )",
    )
    ch.resolve_undo(entries)
    assert entries[3].reverted == ["布尔减：从 “a” 中减去 “b”"]
    assert entries[4].reverted == ["新建面 “a”（3 个顶点）"]
    assert entries[5].reverted == ["新建面 “a”（3 个顶点）"]
    assert not entries[0].undone and entries[1].undone and not entries[2].undone


def test_undo_with_journal_reverts_a_whole_gui_action():
    # One GUI dialog wrote three calls in one tick (one undo step), then the user pressed Ctrl+Z.
    entries = entries_of(
        'addSet( "GEOMETRYLOADSET", "Dead" )',
        'createPointLoad( "P", "Dead" )',
        'setParameter( "GEOMETRYLOAD", "P", "FORCE/VALUE", -5 )',
        "undo( 1 )",
        batches=[0, 0, 0, 1], counts={0: 5, 1: 4},
    )
    ch.resolve_undo(entries, starts=[(0, 4)])  # the bridge started with 4 undo steps
    assert all(e.undone for e in entries[:3])
    assert len(entries[3].reverted) == 1 and "新建点荷载 “P”" in entries[3].reverted[0]


def test_counts_without_a_start_fall_back_to_one_step_per_call():
    entries = entries_of(
        'addSet( "GEOMETRYLOADSET", "Dead" )',
        'createPointLoad( "P", "Dead" )',
        "undo( 1 )",
        batches=[0, 0, 1], counts={0: 5, 1: 4},
    )
    ch.resolve_undo(entries)
    assert [e.undone for e in entries[:2]] == [False, True]


def test_undo_before_tracking_is_reported():
    entries = entries_of("undo( 1 )")
    ch.resolve_undo(entries)
    assert entries[0].reverted == ["（更早的操作，记录开始之前）"]


def test_open_project_clears_the_undo_history():
    entries = entries_of('createSheet( "a", [ [ 0, 0 ], [ 1, 0 ], [ 1, 1 ] ] )', 'openProject( "x.dpf" )', "undo( 1 )")
    ch.resolve_undo(entries)
    assert not entries[0].undone
    assert entries[2].reverted == ["（更早的操作，记录开始之前）"]


def test_history_files_and_keys(tmp_path):
    write_log(tmp_path / "IEhistory_260918_203920.XqpEZb.running.py")
    write_log(tmp_path / "IEhistory_260919_171938.py")
    (tmp_path / "unrelated.py").write_text("x", encoding="utf-8")
    files = ch.history_files([tmp_path])
    assert [f.key for f in files] == ["IEhistory_260918_203920", "IEhistory_260919_171938"]
    assert files[0].running and not files[1].running
    assert files[1].started == datetime(2026, 9, 19, 17, 19, 38)
    assert ch.find_history("IEhistory_260919_171938", [tmp_path]).path.name == "IEhistory_260919_171938.py"
    assert ch.log_key("IEhistory_260918_203920.XqpEZb.running.py") == diana_bridge.log_key(
        "C:/x/IEhistory_260918_203920.XqpEZb.running.py") == "IEhistory_260918_203920"


def test_journal_annotates_sources_and_counts(tmp_path):
    log = write_log(tmp_path / "IEhistory_260919_120000.abc.running.py",
                    'setParameter( "MATERIAL", "C", "LINEAR/ELASTI/YOUNG", 1.0 )', "projectName(  )")
    entries, end = ch.read_log(log)
    journal = tmp_path / "j.jsonl"
    first, second = entries
    journal.write_text("\n".join(json.dumps(r) for r in [
        {"event": "start", "offset": first.offset, "t": 1},
        {"start": first.offset, "end": first.end, "src": "user", "t": 2.5, "undo": 3, "redo": 0},
        {"start": second.offset, "end": end, "src": "ai", "t": 3.5, "undo": 3, "redo": 0},
        "not json",
    ]), encoding="utf-8")
    events, records = ch.read_journal(journal)
    assert ch.tracking_started(events) == first.offset
    ch.annotate(entries, records)
    assert [(e.source, e.undo, e.batch) for e in entries] == [("user", 3, 0), ("ai", 3, 1)]


def test_bridge_journal_tags_user_and_ai_parts(tmp_path):
    log = write_log(tmp_path / "IEhistory_260919_120000.abc.running.py")
    counts = iter([(0, 0), (1, 0), (2, 0)])
    journal = diana_bridge.Journal(str(log), str(tmp_path / "journals" / "j.jsonl"), counts=lambda: next(counts))
    with open(log, "a", encoding="utf-8") as f:
        f.write('createSheet( "a", [ [ 0, 0 ], [ 1, 0 ], [ 1, 1 ] ] )\n')
    assert journal.flush("user") > 0
    with open(log, "a", encoding="utf-8") as f:
        f.write('generateMesh( [  ] )\nsetPara')  # an unfinished line stays for the next flush
    journal.flush("ai")
    assert journal.flush("ai") == 0
    events, records = ch.read_journal(tmp_path / "journals" / "j.jsonl")
    assert [(r.source, r.undo) for r in records] == [("user", 1), ("ai", 2)]
    entries, _ = ch.read_log(log)
    ch.annotate(entries, records)
    assert [(e.name, e.source) for e in entries] == [("createSheet", "user"), ("generateMesh", "ai")]


def test_bridge_parses_window_titles():
    assert diana_bridge.parse_title("Diana Interactive Environment \u2014 ss_beam.dpf*") == ("ss_beam", True)
    assert diana_bridge.parse_title("Diana Interactive Environment \u2014 ss_beam.dpf") == ("ss_beam", False)
    assert diana_bridge.parse_title("Diana Interactive Environment") == ("", False)
    assert diana_bridge.parse_title("Notepad") == (None, None)


def test_cursor_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path))
    assert ch.load_cursor("IEhistory_1") is None
    ch.save_cursor("IEhistory_1", 1234)
    assert ch.load_cursor("IEhistory_1") == 1234

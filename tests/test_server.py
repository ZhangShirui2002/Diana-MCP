import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from diana_mcp import config, server
from diana_mcp.api_reference import CACHE_FORMAT, ApiReferenceStore
from diana_mcp.session import SessionManager

from test_api_reference import FUNCTIONS

DATA = __import__("pathlib").Path(__file__).parent / "data"


def call(tool, **arguments):
    return asyncio.run(server.mcp.call_tool(tool, arguments))


def text(result):
    return result.content[0].text


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No live sessions, a cached API reference, and a fake DIANA 10.5 install."""
    api = tmp_path / "api"
    api.mkdir()
    (api / "diana-10.5.json").write_text(
        json.dumps({"format": CACHE_FORMAT, "diana_version": "Diana 10.5", "functions": FUNCTIONS}))
    monkeypatch.setattr(server, "sessions", SessionManager([tmp_path / "sessions"]))
    monkeypatch.setattr(server, "api_store", ApiReferenceStore(api))
    monkeypatch.setattr(config, "default_install", lambda: config.DianaInstall(tmp_path, "10.5"))
    return tmp_path


def test_tools_are_registered():
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert names == {
        "diana_status", "diana_start", "diana_execute", "diana_screenshot",
        "diana_model_summary", "diana_api_search", "diana_api_doc", "diana_stop",
        "diana_solve", "diana_run_solver", "diana_job_status", "diana_job_cancel",
        "diana_load_results", "diana_plot_result", "diana_read_out", "diana_read_tb",
        "diana_manual_search", "diana_manual_read", "diana_keywords", "diana_parameters",
        "diana_analysis_commands", "diana_command_log", "diana_results_table",
        "diana_changes", "diana_model_snapshot", "diana_model_diff", "diana_autostart",
        "diana_knowledge_search", "diana_knowledge_feedback", "diana_glossary",
        "diana_experience_add", "diana_experience_update", "diana_experience_list", "diana_experience_mine_logs",
        "diana_experience_verify", "diana_experience_review", "diana_experience_export", "diana_guide",
        "diana_review_prepare", "diana_review_status", "diana_parameter_study_prepare",
    }


def test_instructions_fit_what_clients_keep():
    assert len(server.INSTRUCTIONS) <= 2048  # Claude Code keeps only the first 2048 characters
    for essential in ("diana_guide", "setParameter", "pause()", "diana_changes", "diana_knowledge_search",
                      "verified only after the user"):
        assert essential in server.INSTRUCTIONS


def test_guide_has_every_part():
    full = text(call("diana_guide"))
    for heading in ("Workflow:", "DIANA 10.5 scripting facts", "Following the user's own work", "Knowledge base",
                    "Rules:"):
        assert heading in full
    assert "{python}" not in full and "-m diana_mcp watch --follow" in full
    part = text(call("diana_guide", topic="scripting"))
    assert "setParameter" in part and "Workflow:" not in part


@pytest.fixture
def knowledge_dir(isolated, monkeypatch):
    from diana_mcp.experience import ExperienceStore

    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "t")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "t@e")
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(isolated / "state"))
    monkeypatch.setattr(server, "experiences", ExperienceStore(isolated / "knowledge"))
    return isolated / "knowledge"


def test_experience_tools_draft_search_and_approve(knowledge_dir):
    added = json.loads(text(call("diana_experience_add", title="Too small step in AUTOMA 时减小 MINSIZ",
                                  type="convergence", symptom="ABORT /DIANA/NL/IS41/0001",
                                  fix="检查模型后减小 MINSIZ", triggers=["IS41/0001"])))
    assert added["created"] and added["status"] == "未验证"
    again = json.loads(text(call("diana_experience_add", title="另一个标题", type="convergence", symptom="x",
                                  triggers=["IS41/0001"])))
    assert not again["created"] and again["similar"][0]["id"] == added["id"]

    found = json.loads(text(call("diana_knowledge_search", query="作业中止 IS41/0001")))
    assert found["results"][0]["ref"] == added["id"] and found["results"][0]["status"] == "未验证"
    assert json.loads(text(call("diana_knowledge_feedback", search_id=found["search_id"], helpful=[added["id"]])))["recorded"]

    with pytest.raises(ToolError, match="approval"):
        call("diana_experience_update", id=added["id"], status="verified")
    approved = json.loads(text(call("diana_experience_update", id=added["id"], status="verified",
                                    approval="批准", add_evidence=["2026-10-01 作业 x1"])))
    assert approved["status"] == "已验证" and len(approved["git"]) == 2

    listing = json.loads(text(call("diana_experience_list")))
    assert listing["counts"]["已验证"] == 1 and listing["matches"] == 1
    full = json.loads(text(call("diana_experience_list", id=added["id"])))
    assert full["approval"]["note"] == "批准" and full["evidence"] == ["2026-10-01 作业 x1"]
    assert json.loads(text(call("diana_experience_list", status="draft")))["matches"] == 0
    with pytest.raises(ToolError, match="no experience entry"):
        call("diana_experience_update", id="nope", tags=["x"])


def test_glossary_needs_the_translation_file(isolated):
    with pytest.raises(ToolError, match="glossary unavailable"):
        call("diana_glossary", term="受压断裂能")


@pytest.fixture
def fake_history(isolated, monkeypatch):
    """A fake ~/.DianaIE and state folder, no running DianaIE."""
    from diana_mcp import changes as ch

    logs = isolated / "logs"
    logs.mkdir()
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(isolated / "state"))
    monkeypatch.setattr(ch, "log_dirs", lambda: [logs])
    monkeypatch.setattr(ch, "logs_of_running_dianaie", lambda files=None: {})
    monkeypatch.setattr(server, "tracker", server.Tracker(server.sessions, server.SnapshotStore(isolated / "snaps")))
    (logs / "IEhistory_260918_090000.py").write_text(
        'openProject( "F:/old/frame.dpf" )\nsetViewPoint( "TOP" )\n', encoding="utf-8")
    log = logs / "IEhistory_260919_120000.abc.running.py"
    log.write_text('newProject( "F:/p/beam", 10, {} )\ncreateSheet( "beam", [ [ 0, 0 ], [ 4, 0 ], [ 4, 1 ] ] )\n'
                   'fitAll(  )\nundo( 1 )\n', encoding="utf-8")
    return log


def test_command_log_describes_history_without_a_bridge(fake_history):
    result = json.loads(text(call("diana_command_log")))
    assert result["tracking"].startswith("history only")
    whats = [e["what"] for e in result["entries"]]
    assert whats == ["新建项目 F:/p/beam", "新建面 “beam”（3 个顶点）", "fitAll()", "撤销 1 步"]
    assert [e["category"] for e in result["entries"]] == ["project", "model", "view", "undo"]
    assert result["entries"][1]["undone"] and result["entries"][3]["reverted"] == ["新建面 “beam”（3 个顶点）"]
    edits = json.loads(text(call("diana_command_log", edits_only=True, find="beam")))
    assert [e["call"][:11] for e in edits["entries"]] == ["newProject(", "createSheet"]


def test_command_log_lists_and_reads_earlier_sessions(fake_history):
    listed = json.loads(text(call("diana_command_log", list_sessions=True)))["sessions"]
    assert [s["key"] for s in listed] == ["IEhistory_260919_120000", "IEhistory_260918_090000"]
    assert listed[0]["running"] and listed[1]["projects"] == ["frame"]
    old = json.loads(text(call("diana_command_log", session="IEhistory_260918_090000")))
    assert [e["what"] for e in old["entries"]][0] == "打开项目 F:/old/frame.dpf"


def test_changes_without_a_bridge_lists_operations_only(fake_history):
    first = json.loads(text(call("diana_changes")))
    assert [op["what"] for op in first["operations"]][-1] == "撤销 1 步"
    assert any("没有连接 bridge" in n for n in first["notes"])
    with open(fake_history, "a", encoding="utf-8") as f:
        f.write('setUnit( "LENGTH", "MM" )\n')
    second = json.loads(text(call("diana_changes")))
    assert [op["what"] for op in second["operations"]] == ["单位：长度 改为 MM"]


def test_autostart_tool_uses_the_user_base(isolated, monkeypatch):
    for dll in ("python37.dll",):
        (isolated / "bin").mkdir(exist_ok=True)
        (isolated / "bin" / dll).write_bytes(b"")
    monkeypatch.setenv("PYTHONUSERBASE", str(isolated / "userbase"))
    assert json.loads(text(call("diana_autostart")))["enabled"] is False
    assert json.loads(text(call("diana_autostart", action="enable")))["enabled"] is True
    assert (isolated / "userbase" / "Python37" / "site-packages" / "diana_mcp_autostart.pth").is_file()
    assert json.loads(text(call("diana_autostart", action="disable")))["enabled"] is False


def test_read_tb_index_and_rows():
    path = str(DATA / "linsta_beam_small.tb")
    index = json.loads(text(call("diana_read_tb", path=path)))
    assert index["analysis_types"] == ["LINSTA"]
    assert [r["result"] for r in index["results"]] == ["DISPLA TOTAL TRANSL", "FORCE REACTI TRANSL", "STRESS TOTAL CAUCHY"]
    assert index["results"][0]["load_cases"] == "1"

    rows = json.loads(text(call("diana_read_tb", path=path, result="displa", node_ids=[6])))
    assert rows["rows_total"] == 1 and rows["rows"][0]["DtY"] == -4.321e-4


def test_read_out_summary():
    summary = json.loads(text(call("diana_read_out", path=str(DATA / "linsta_beam.out"), tail_lines=2)))
    assert summary["completed"] and summary["elapsed_s"] == 10.0
    assert summary["tail"][-1] == " Elapsed time: 10 seconds"


def test_status_without_sessions(isolated):
    status = json.loads(text(call("diana_status")))
    assert status["sessions"] == []
    assert status["api_reference_cached_versions"] == ["10.5"]
    assert "runpy.run_path" in status["manual_attach"]


def test_api_search_and_doc_use_the_cached_reference(isolated):
    assert "generateMesh( shapes = QStringList() )" in text(call("diana_api_search", query="mesh"))
    assert text(call("diana_api_doc", name="createsheet")).startswith("createSheet (function)")


def test_unknown_command_suggests_close_names(isolated):
    with pytest.raises(ToolError, match="Did you mean: createSheet"):
        call("diana_api_doc", name="createSheets")


def test_execute_without_session_asks_for_diana_start(isolated):
    with pytest.raises(ToolError, match="diana_start"):
        call("diana_execute", code="1")


def test_execute_cannot_bypass_the_solver_gate(isolated):
    for code in ("runSolver('analysis')", "diana.runStabor('analysis')", "f = runSolver\nf('analysis')",
                 "globals()['runSolver']('analysis')", "runPipe('analysis')"):
        with pytest.raises(ToolError, match="禁止.*直接提交计算"):
            call("diana_execute", code=code)


def test_file_solver_requires_a_trusted_baseline_or_campaign(isolated, tmp_path):
    dat, dcf = tmp_path / "m.dat", tmp_path / "a.dcf"
    dat.write_text("'END'\n", encoding="utf-8")
    dcf.write_text("*LINSTA\n*END\n", encoding="utf-8")
    with pytest.raises(ToolError, match="baseline_id.*campaign_id|基准模型"):
        call("diana_run_solver", dat_path=str(dat), dcf_path=str(dcf))


def test_solve_without_authorization_routes_to_gui_review(monkeypatch):
    shown = []
    monkeypatch.setattr(server, "diana_review_prepare", lambda analysis: shown.append(analysis) or {
        "review_required": True, "analysis": analysis
    })
    result = server.diana_solve("Linear static")
    assert result == {"review_required": True, "analysis": "Linear static"}
    assert shown == ["Linear static"]


def test_execute_against_standalone_bridge(standalone_bridge, monkeypatch):
    session_dir, _, _ = standalone_bridge
    monkeypatch.setattr(server, "sessions", SessionManager([session_dir]))

    result = json.loads(text(call("diana_execute", code="print('hello')\n2 ** 10")))
    assert result["stdout"] == "hello\n"
    assert result["value"] == "1024"

    with pytest.raises(ToolError, match="ZeroDivisionError"):
        call("diana_execute", code="print('partial')\n1 / 0")


def test_handbook_tips_come_with_the_first_result_only(knowledge_dir, monkeypatch):
    server.experiences.add(title="attach snaps to existing vertices", kind="api-usage", symptom="attach",
                           entry_id="attach-snaps")
    server.experiences.approve("attach-snaps", "批准")
    monkeypatch.setattr(server, "_tips_shown", False)
    first = json.loads(text(call("diana_status")))
    assert first["handbook_tips"]["verified_lessons"] == ["attach snaps to existing vertices [attach-snaps]"]
    assert "handbook_tips" not in json.loads(text(call("diana_status")))
    assert "[attach-snaps]" in text(call("diana_guide", topic="knowledge"))


def test_experience_list_is_compact_unless_details_are_asked(knowledge_dir):
    server.experiences.add(title="Too small step in AUTOMA", kind="convergence", symptom="x", triggers=["IS41/0001"],
                           entry_id="small-step")
    listed = json.loads(text(call("diana_experience_list")))["entries"][0]
    assert set(listed) == {"id", "title", "status", "type"}
    detailed = json.loads(text(call("diana_experience_list", details=True)))["entries"][0]
    assert detailed["triggers"] == ["IS41/0001"] and detailed["source"]


def test_results_table_rounds_and_limits_the_inline_rows(isolated, monkeypatch):
    table = [["Node", "TDtY"]] + [[i, -1.23456789012345e-4 * i] for i in range(1, 51)]
    monkeypatch.setattr(server.sessions, "run_checked", lambda code, timeout=0: json.dumps(table) + "\n")
    result = json.loads(text(call("diana_results_table", analysis="a", result="Displacements")))
    assert result["rows_total"] == 50 and len(result["rows"]) == 30
    assert result["rows"][0] == [1, -0.000123457]
    full = json.loads(text(call("diana_results_table", analysis="a", result="Displacements", digits=0, max_rows=2)))
    assert full["rows"] == [[1, -1.23456789012345e-4], [2, -1.23456789012345e-4 * 2]]


def test_model_summary_of_a_big_model_is_condensed(knowledge_dir, monkeypatch):
    summary = {"shapes": [f"s{i}" for i in range(100)], "materials": ["C30"], "units": {"LENGTH": "M"},
               "validation_issues": [f'Shape "s{i}" has no material' for i in range(40)] + ["Mesh is empty"]}
    monkeypatch.setattr(server.sessions, "model_summary", lambda: summary)
    result = json.loads(text(call("diana_model_summary")))
    assert len(result["shapes"]) == 30 and result["lists_cut"]["total"] == {"shapes": 100}
    assert result["materials"] == ["C30"]
    assert result["validation_issues"] == ['Shape "s0" has no material（同类共 40 条）', "Mesh is empty"]
    assert result["validation_issues_total"] == 41


def test_changes_list_the_latest_operations_and_the_total(fake_history):
    result = json.loads(text(call("diana_changes", max_operations=1)))
    assert len(result["operations"]) == 1 and result["operations_total"] > 1
    assert result["operations"][0]["what"] == "撤销 1 步"


def test_profiles_split_the_tools_without_gaps(monkeypatch):
    """A split deployment (knowledge on the user's machine, compute where DIANA runs) must cover every tool
    exactly once, and each half's instructions must survive the client's 2048-character cut."""
    import importlib

    from diana_mcp import server as module

    full = {t.name for t in module.mcp._tool_manager.list_tools()}
    halves = {}
    try:
        for profile in ("compute", "knowledge"):
            monkeypatch.setenv("DIANA_MCP_PROFILE", profile)
            reloaded = importlib.reload(module)
            halves[profile] = {t.name for t in reloaded.mcp._tool_manager.list_tools()}
            assert len(reloaded.INSTRUCTIONS) <= 2048
        assert halves["compute"] | halves["knowledge"] == full
        assert not halves["compute"] & halves["knowledge"]
        assert halves["knowledge"] == module.KNOWLEDGE_TOOLS
    finally:
        monkeypatch.delenv("DIANA_MCP_PROFILE", raising=False)
        importlib.reload(module)

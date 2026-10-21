"""End-to-end run against the real DianaIE. Opt in with DIANA_MCP_LIVE_TESTS=1 (needs a DIANA license)."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest

import subprocess

from diana_mcp import autostart, config, server
from diana_mcp.api_reference import ApiReferenceStore
from diana_mcp.bridge.diana_review import apply_decision
from diana_mcp.jobs import JobManager
from diana_mcp.review_gate import GuardStore
from diana_mcp.session import SessionManager, kill_tree, pid_alive
from diana_mcp.snapshots import SnapshotStore, snapshot_saved_project
from diana_mcp.tracking import Tracker

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("DIANA_MCP_LIVE_TESTS") != "1", reason="set DIANA_MCP_LIVE_TESTS=1"),
]


EXAMPLE = Path(__file__).parents[1] / "examples" / "simply_supported_beam" / "build_model.py"


def call(tool, **arguments):
    return asyncio.run(server.mcp.call_tool(tool, arguments))


def payload(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server, "sessions", SessionManager([tmp_path / "state" / "sessions"]))
    monkeypatch.setattr(server, "api_store", ApiReferenceStore(tmp_path / "state" / "api"))
    monkeypatch.setattr(server, "tracker", Tracker(server.sessions, SnapshotStore(tmp_path / "state" / "snapshots")))
    gate = GuardStore(tmp_path / "state" / "review-gate")
    monkeypatch.setattr(server, "guard", gate)
    monkeypatch.setattr(server, "jobs", JobManager(tmp_path / "state" / "jobs", guard=gate))
    return tmp_path


def test_live_beam_example_solves_in_background_and_matches_theory(isolated_state):
    tmp_path = isolated_state
    started = payload(call("diana_start", working_dir=str(tmp_path / "work")))
    pid = started["pid"]
    try:
        script = EXAMPLE.read_text(encoding="utf-8").replace(
            'PROJECT_DIR = r"F:/Diana-MCP/examples/simply_supported_beam"', f"PROJECT_DIR = {tmp_path.as_posix()!r}"
        )
        call("diana_execute", code=script, timeout_seconds=300)

        prepared = payload(call("diana_solve", analysis="Linear static"))
        assert prepared["review_required"]
        record_path = server.guard.record_path("reviews", prepared["id"])
        apply_decision(record_path, server.guard.root / "audit.jsonl", "approve_once", "真机测试已核对")
        job = payload(call("diana_solve", analysis="Linear static", review_id=prepared["id"]))
        deadline = time.monotonic() + 300
        while job["state"] == "running" and time.monotonic() < deadline:
            time.sleep(1)
            job = payload(call("diana_job_status", job_id=job["id"]))
        assert job["state"] == "finished", job
        assert job["progress"]["completed"] and ".ff" not in job["files"]

        tb = str(Path(job["directory"]) / "ss_beam.tb")
        (row,) = payload(call("diana_read_tb", path=tb, result="DISPLA", node_ids=[6]))["rows"]
        theory = 10000 * 4.0**3 / (48 * 3e10 * 0.2 * 0.4**3 / 12) + 10000 * 4.0 / (4 * 5 / 6 * 1.25e10 * 0.08)
        assert abs(-row["DtY"] - theory) / theory < 0.02

        reactions = payload(call("diana_read_tb", path=tb, result="REACTI"))["rows"]
        assert abs(sum(r["FBY"] for r in reactions) - 10000) < 1e-6

        loaded = payload(call("diana_load_results", job_id=job["id"]))
        assert any(r["result"] == "Displacements" for r in loaded["results"])
        plot = call("diana_plot_result", analysis="Linear static", result="Displacements", component="DtY", viewpoint="top")
        assert plot.content[1].type == "image"

        assert payload(call("diana_stop", save=True))["diana_closed"]
    finally:
        if pid_alive(pid):
            kill_tree(pid)


# Simulates the user: DIANA commands fired by Win32 timers on DianaIE's GUI thread, outside any bridge request, so the
# journal sees them exactly like GUI actions.
USER_EDITS = r"""
import ctypes, ctypes.wintypes as wt
_sim_type = ctypes.WINFUNCTYPE(None, wt.HWND, wt.UINT, ctypes.c_size_t, wt.DWORD)
_user32 = ctypes.windll.user32
_user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, _sim_type]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
_sim_actions = [
    lambda: setParameter("MATERIAL", "C30", "LINEAR/ELASTI/YOUNG", 3.2e10),
    lambda: createSheet("tmp", [[1, 1], [2, 1], [2, 2], [1, 2]]),
    lambda: undo(1),
]
def _sim_fire(hwnd, msg, event_id, when):
    _user32.KillTimer(None, event_id)
    _sim_actions.pop(0)()
_sim_callback = _sim_type(_sim_fire)
for _i in range(len(_sim_actions)):
    _user32.SetTimer(None, 0, 400 * (_i + 1), _sim_callback)
"""


def test_live_change_tracking(isolated_state, monkeypatch):
    tmp_path = isolated_state
    monkeypatch.setattr(server, "snapshots", SnapshotStore(tmp_path / "state" / "snapshots"))
    monkeypatch.setattr(server, "tracker", Tracker(server.sessions, server.snapshots))
    pid = payload(call("diana_start", working_dir=str(tmp_path / "work")))["pid"]
    try:
        call("diana_execute", code=(
            f"newProject({(tmp_path / 'track').as_posix()!r}, 10)\n"
            "setModelAnalysisAspects(['STRUCT'])\nsetModelDimension('2D')\n"
            "createSheet('beam', [[0, 0], [0, 0.4], [4, 0.4], [4, 0]])\n"
            "addMaterial('C30', 'CONCR', 'LEI', [])\n"
            "setParameter('MATERIAL', 'C30', 'LINEAR/ELASTI/YOUNG', 3e10)\n"
            "assignMaterial('C30', 'SHAPE', ['beam'])\n"
        ))
        first = payload(call("diana_changes"))
        assert first["dianaie"]["tracking"] == "journal"
        assert first["operations"] == [] and first["ai_operations_hidden"] >= 5
        assert any("首次记录" in note for note in first["notes"])

        call("diana_execute", code=USER_EDITS)
        seen = payload(call("diana_changes", wait_seconds=60, quiet_seconds=1.5))
        ops = seen["operations"]
        assert [op["source"] for op in ops] == ["user"] * 3
        assert ops[0]["what"] == "材料 “C30”：LINEAR/ELASTI/YOUNG = 3.2e+10（弹性模量）"
        assert ops[1]["what"] == "新建面 “tmp”（4 个顶点）" and ops[1]["undone"]
        assert ops[2]["reverted"] == ["新建面 “tmp”（4 个顶点）"]
        (change,) = seen["model_changes"]
        assert (change["item"], change["old"], change["new"], change["by"]) == ("C30", 3e10, 3.2e10, "user")
        assert seen["baseline"]["from_earlier_dianaie_session"] is False

        call("diana_execute", code="setParameter('MATERIAL', 'C30', 'LINEAR/ELASTI/YOUNG', 3e10)")
        mine = payload(call("diana_changes"))
        assert mine["operations"] == [] and mine["ai_operations_hidden"] == 1
        assert [(c["new"], c["by"]) for c in mine["model_changes"]] == [(3e10, "ai")]

        log = payload(call("diana_command_log", source="user"))
        assert [e["call"].split("(")[0] for e in log["entries"]] == ["setParameter", "createSheet", "undo"]

        payload(call("diana_model_snapshot", label="checkpoint"))
        call("diana_execute", code="addMaterial('C40', 'CONCR', 'LEI', [])")
        compared = payload(call("diana_model_diff", before="checkpoint"))
        assert [c["text"] for c in compared["changes"]] == ["新增材料 “C40”：混凝土和砌体 / 线弹性各向同性"]

        assert payload(call("diana_stop", save=False))["diana_closed"]
    finally:
        if pid_alive(pid):
            kill_tree(pid)


def wait_for_job(job_id, seconds=300):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = payload(call("diana_job_status", job_id=job_id))
        if status["state"] != "running":
            return status
        time.sleep(1)
    raise TimeoutError(job_id)


def test_live_learning_from_a_diverged_then_successful_run(tmp_path, monkeypatch):
    """The real solver: the nonlinear beam diverges at step 9 with 12 x 0.25; with 5 steps it converges."""
    import shutil

    from diana_mcp.experience import ExperienceStore

    source = next((EXAMPLE.parent / "ss_beam_nl_jobs").glob("Nonlinear_*"))
    run = tmp_path / "run"
    run.mkdir()
    for name in ("ss_beam_nl.dat", "ss_beam_nl.dcf"):
        shutil.copy(source / name, run / name)
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path / "state"))
    gate = GuardStore(tmp_path / "state" / "review-gate")
    manager = JobManager(tmp_path / "state" / "jobs", guard=gate)
    monkeypatch.setattr(server, "guard", gate)
    monkeypatch.setattr(server, "jobs", manager)
    monkeypatch.setattr(server, "experiences", ExperienceStore(tmp_path / "knowledge", use_git=False))
    server.experiences.add(title="发散时减小荷载步或改用弧长法", kind="convergence", symptom="LB40/0053 发散",
                           triggers=["/DIANA/AP/LB40/0053"], entry_id="smaller-steps-on-divergence")
    dat, dcf = str(run / "ss_beam_nl.dat"), str(run / "ss_beam_nl.dcf")

    # First certify the convergent five-step file as a baseline through a real validation solve.
    original = Path(dcf).read_bytes()
    Path(dcf).write_bytes(original.replace(b"0.25(12)", b"0.25(5)"))
    reviewed = gate.create_review(
        project=str(run / "ss_beam_nl.dpf"), analysis="Nonlinear", snapshot={"analyses": {"Nonlinear": {}}},
        dat=Path(dat), dcf=Path(dcf), diana_version="10.5", units={}, validation_issues=[], changes=[],
    )
    gate.gui_decide(reviewed["id"], "approve_baseline", "真机测试批准基准验证")
    candidate = gate.finalize_baseline(reviewed["id"])
    permit = gate.permit_review(
        reviewed["id"], reviewed["state_fingerprint"], reviewed["dat_sha256"], {"passed": True}
    )
    validation = manager.submit(Path(reviewed["dat"]), Path(reviewed["dcf"]), solver=None, permit_id=permit["id"])
    validation = wait_for_job(validation.id)
    assert validation["state"] == "finished"
    assert gate.get_baseline(candidate["id"])["status"] == "approved"

    campaign = gate.create_campaign(
        candidate["id"], name="荷载步参数测试",
        parameters=[{"name": "steps", "path": "ANALYSIS/Nonlinear", "values": [5, 12]}],
        combination="one-at-a-time", self_checks=["输入完整"],
        quality_checks=["求解正常结束", "全部增量步收敛"],
    )
    gate.gui_decide(campaign["id"], "approve_campaign", "批准测试 5/12 步")

    Path(dcf).write_bytes(original)
    failed = wait_for_job(payload(call("diana_run_solver", dat_path=dat, dcf_path=dcf, overwrite=True,
                                       campaign_id=campaign["id"], case_parameters={"steps": 12},
                                       change_manifest=["ANALYSIS/Nonlinear"]))["id"])
    assert failed["outcome"] == "problem" and failed["problem"]["errors"][0]["code"] == "/DIANA/AP/LB40/0053"
    assert failed["related_experience"][0]["id"] == "smaller-steps-on-divergence"

    (run / "ss_beam_nl.dcf").write_bytes((run / "ss_beam_nl.dcf").read_bytes().replace(b"0.25(12)", b"0.25(5)"))
    solved = wait_for_job(payload(call("diana_run_solver", dat_path=dat, dcf_path=dcf, overwrite=True,
                                       campaign_id=campaign["id"], case_parameters={"steps": 5},
                                       change_manifest=[]))["id"])
    assert solved["outcome"] == "success"
    learned = solved["learned"]
    assert learned["after_problem_job"] == failed["id"]
    assert learned["approval_candidates"] == [{"id": "smaller-steps-on-divergence", "title": "发散时减小荷载步或改用弧长法"}]
    draft = server.experiences.get(learned["draft"])
    assert draft.status == "draft" and draft.triggers[0] == "/DIANA/AP/LB40/0053"
    assert "-      STEPS EXPLIC SIZES 0.25(12)" in draft.sections["解决"]
    assert "+      STEPS EXPLIC SIZES 0.25(5)" in draft.sections["解决"]


def test_live_verification_scripts_run_in_a_batch_dianaie(tmp_path, monkeypatch):
    from diana_mcp.experience import ExperienceStore
    from diana_mcp.verify import verify

    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path / "state"))
    store = ExperienceStore(tmp_path / "knowledge", use_git=False)
    store.add(title="setParameter 只接受模型目录之下的路径", kind="api-usage", symptom="x", entry_id="settable",
              notes="")
    script = ('newProject(WORKDIR + "/p", 10)\n'
              'addMaterial("M", "CONCR", "LEI", [])\n'
              'assert setParameter("MATERIAL", "M", "LINEAR/ELASTI/YOUNG", 3e10) is True\n'
              'assert setParameter("MATERIAL", "M", "YOUNG", 3e10) is False\n')
    store.update("settable", sections={**store.get("settable").sections, "验证脚本": f"```python\n{script}```"})
    store.add(title="一条会失败的检查", kind="workflow", symptom="x", entry_id="failing")
    store.update("failing", sections={**store.get("failing").sections, "验证脚本": "```python\nassert shapes() == ['x']\n```"})
    report = verify(store, config.default_install(), timeout=600)
    assert report["passed"] == ["settable"] and report["failed"][0]["id"] == "failing"
    assert store.get("settable").checks["result"] == "pass" and store.get("failing").checks["result"] == "fail"


def test_live_autostart_starts_the_bridge_in_a_plain_dianaie(tmp_path):
    install = config.default_install()
    base, sessions_dir = tmp_path / "userbase", tmp_path / "sessions"
    autostart.enable(install, base, session_dir=sessions_dir)
    env = dict(os.environ, PYTHONUSERBASE=str(base))
    env.pop("DIANA_MCP_SESSION_DIR", None)
    process = subprocess.Popen([str(install.dianaie), "--noSplash"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        manager = SessionManager([sessions_dir])
        deadline = time.monotonic() + 180
        found = []
        while not found and time.monotonic() < deadline and process.poll() is None:
            found = [s for s in manager.discover() if s.pid == process.pid]
            time.sleep(1)
        assert found, "autostart did not start a bridge"
        assert found[0].info["log_file"] and found[0].info["journal"]
        manager.current = found[0]
        manager.close_diana(save=False)
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:
            kill_tree(process.pid)


def test_live_snapshot_of_a_saved_project():
    project = Path(__file__).parents[1] / "examples" / "simply_supported_beam" / "ss_beam.dpf"
    snap = snapshot_saved_project(config.default_install(), project)
    assert snap["project"]["path"] == project.resolve().as_posix()
    assert list(snap["materials"]) == ["Concrete"] and list(snap["analyses"]) == ["Linear static"]
    assert "vertex (2, 0.4)" in snap["loads"]["P"]["parameters"]["FORCE/TARGET"]
    assert snap["mesh"] == {"nodes": 2097, "elements": 640}


def test_live_session_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server, "sessions", SessionManager([tmp_path / "state" / "sessions"]))
    monkeypatch.setattr(server, "api_store", ApiReferenceStore(tmp_path / "state" / "api"))
    project = (tmp_path / "live_demo").as_posix()

    started = payload(call("diana_start", working_dir=str(tmp_path / "work"), attach_only=False))
    assert started["action"] == "launched" and started["launched_by_mcp"]
    pid = started["pid"]
    try:
        code = (
            f"newProject({project!r}, 10)\n"
            "setModelAnalysisAspects(['STRUCT'])\n"
            "setModelDimension('2D')\n"
            "createSheet('plate', [[0, 0, 0], [0, 0.5, 0], [2, 0.5, 0], [2, 0, 0]])\n"
            "shapes()"
        )
        assert payload(call("diana_execute", code=code))["value"] == "['plate']"

        # DIANA's own select() must survive: the bridge runs in its own module namespace.
        assert payload(call("diana_execute", code="type(select).__name__"))["value"] == "'function'"

        assert "createSheet(" in call("diana_api_search", query="create sheet").content[0].text
        assert server.api_store.cached_versions() == ["10.5"]

        summary = payload(call("diana_model_summary"))
        assert summary["shapes"] == ["plate"]
        assert summary["projectName"] == "live_demo"

        shot = call("diana_screenshot", viewpoint="top", width=800, height=400)
        assert shot.content[1].type == "image" and len(shot.content[1].data) > 1000

        stopped = payload(call("diana_stop", save=True))
        assert stopped["diana_closed"]
        assert not pid_alive(pid)
        assert (tmp_path / "live_demo.dpf").is_file()
    finally:
        if pid_alive(pid):
            kill_tree(pid)

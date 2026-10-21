import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from diana_mcp.jobs import JobError, JobManager
from diana_mcp.review_gate import GateError, GuardStore, state_fingerprint
from diana_mcp.bridge.diana_review import apply_decision, render_record


FAKE = [sys.executable, str(Path(__file__).parent / "fake_diana.py")]


def inputs(folder: Path) -> tuple[Path, Path]:
    dat = folder / "model.dat"
    dcf = folder / "analysis.dcf"
    dat.write_text("'COORDINATES'\n'END'\n", encoding="utf-8")
    dcf.write_text("*LINSTA\n  SOLVE PARDIS\n*END\n", encoding="utf-8")
    return dat, dcf


def review(store: GuardStore, dat: Path, dcf: Path) -> dict:
    return store.create_review(
        project="F:/cases/model.dpf",
        analysis="linear",
        snapshot={"project": {"name": "model"}, "mesh": {"nodes": 2, "elements": 1}},
        dat=dat,
        dcf=dcf,
        diana_version="10.5",
        units={"length": "m", "force": "N"},
        validation_issues=[],
        changes=[],
    )


def validated_baseline(store: GuardStore, dat: Path, dcf: Path) -> dict:
    item = review(store, dat, dcf)
    store.gui_decide(item["id"], "approve_baseline", "批准当前模型进入验证求解")
    baseline = store.finalize_baseline(item["id"])
    assert baseline["status"] == "pending_validation"
    permit = store.permit_review(item["id"], item["state_fingerprint"], item["dat_sha256"], {"passed": True})
    store.consume_permit(permit["id"], Path(item["dat"]), Path(item["dcf"]))
    store.record_quality("validation-job", True, ["正常结束", "全部收敛"], permit["id"])
    return store.get_baseline(baseline["id"])


def test_unreviewed_submission_is_blocked_before_process_start(tmp_path):
    store = GuardStore(tmp_path / "guard")
    manager = JobManager(tmp_path / "jobs", guard=store)
    dat, dcf = inputs(tmp_path)

    with pytest.raises(JobError, match="人工审阅|许可"):
        manager.submit(dat, dcf, solver=FAKE)
    assert not (tmp_path / "model.solver.log").exists()


def test_gui_approval_issues_one_use_permit_and_audit(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    item = review(store, dat, dcf)
    with pytest.raises(GateError, match="尚未"):
        store.permit_review(item["id"], item["state_fingerprint"], item["dat_sha256"],
                            self_check={"passed": True})

    store.gui_decide(item["id"], "approve_once", "人工确认模型及求解设置正确")
    permit = store.permit_review(item["id"], item["state_fingerprint"], item["dat_sha256"],
                                 self_check={"passed": True})
    accepted = store.consume_permit(permit["id"], dat, dcf)
    assert accepted["source_type"] == "review"
    with pytest.raises(GateError, match="已使用"):
        store.consume_permit(permit["id"], dat, dcf)

    events = [json.loads(line) for line in (store.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {event["event"] for event in events} >= {"review.created", "review.decided", "permit.issued", "permit.consumed"}
    assert next(e for e in events if e["event"] == "review.decided")["source"] == "diana_gui"


def test_one_use_permit_cannot_be_consumed_by_two_mcp_clients(tmp_path):
    first = GuardStore(tmp_path / "guard")
    second = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    item = review(first, dat, dcf)
    first.gui_decide(item["id"], "approve_once", "批准")
    permit = first.permit_review(item["id"], item["state_fingerprint"], item["dat_sha256"], {"passed": True})

    def consume(store):
        try:
            store.consume_permit(permit["id"], dat, dcf)
            return "accepted"
        except GateError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (first, second)))
    assert sorted(results) == ["accepted", "rejected"]


def test_model_change_invalidates_debug_authorization(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    item = review(store, dat, dcf)
    store.gui_decide(item["id"], "approve_once", "同意本次调试计算")
    changed = state_fingerprint({"project": {"name": "model"}, "mesh": {"nodes": 3}}, dcf.read_bytes())
    with pytest.raises(GateError, match="已变化|失效"):
        store.permit_review(item["id"], changed, item["dat_sha256"], self_check={"passed": True})
    assert store.get_review(item["id"])["status"] == "stale"


def test_dat_change_hidden_from_the_snapshot_still_invalidates_review(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    item = review(store, dat, dcf)
    store.gui_decide(item["id"], "approve_once", "批准")
    with pytest.raises(GateError, match="模型数据.*变化"):
        store.permit_review(item["id"], item["state_fingerprint"], "different-hash", {"passed": True})
    assert store.get_review(item["id"])["status"] == "stale"


def test_snapshot_timestamp_does_not_invalidate_an_unchanged_review(tmp_path):
    _, dcf = inputs(tmp_path)
    before = {"taken": 1.0, "project": {"name": "model", "stored_path": "old"}, "mesh": {"elements": 1}}
    after = {"taken": 99.0, "project": {"name": "model", "stored_path": "new"}, "mesh": {"elements": 1}}
    assert state_fingerprint(before, dcf.read_bytes()) == state_fingerprint(after, dcf.read_bytes())


def test_baseline_is_immutable_and_exact_export_can_run_in_batch(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    baseline = validated_baseline(store, dat, dcf)
    assert baseline["status"] == "approved"
    baseline_dat = Path(baseline["dat"])
    baseline_dcf = Path(baseline["dcf"])
    assert baseline_dat.read_bytes() == dat.read_bytes()
    assert baseline_dcf.read_bytes() == dcf.read_bytes()

    dat.write_text("changed", encoding="utf-8")
    assert baseline_dat.read_text(encoding="utf-8") != "changed"
    permit = store.permit_baseline(baseline["id"])
    assert store.consume_permit(permit["id"], baseline_dat, baseline_dcf)["source_type"] == "baseline"


@pytest.mark.parametrize("dcf_text, expected", [
    ("*LINSTA\n*END\n", "approved"),
    ("*NONLIN\n  DIVERGE\n*END\n", "validation_failed"),
])
def test_baseline_trust_is_decided_by_the_validation_job(tmp_path, dcf_text, expected):
    store = GuardStore(tmp_path / "guard")
    manager = JobManager(tmp_path / "jobs", guard=store)
    dat, dcf = inputs(tmp_path)
    dcf.write_text(dcf_text, encoding="utf-8")
    item = review(store, dat, dcf)
    store.gui_decide(item["id"], "approve_baseline", "同意验证求解")
    baseline = store.finalize_baseline(item["id"])
    assert baseline["status"] == "pending_validation"
    permit = store.permit_review(item["id"], item["state_fingerprint"], item["dat_sha256"], {"passed": True})
    job = manager.submit(Path(item["dat"]), Path(item["dcf"]), solver=FAKE, permit_id=permit["id"])
    deadline = time.monotonic() + 15
    while job.state == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        job = manager.get(job.id)
    assert job.state != "running"
    assert store.get_baseline(baseline["id"])["status"] == expected
    events = {json.loads(line)["event"] for line in (store.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()}
    assert {"baseline.created", "permit.consumed", "submission.started", "quality.checked"} <= events
    if expected != "approved":
        with pytest.raises(GateError, match="尚未通过"):
            store.permit_baseline(baseline["id"])


def test_campaign_variant_range_and_self_check(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    baseline = validated_baseline(store, dat, dcf)
    campaign = store.create_campaign(
        baseline["id"],
        name="弹性模量与网格参数分析",
        parameters=[
            {"name": "E", "path": "MATERIAL/concrete/LINEAR/ELASTI/YOUNG", "min": 2.0e10, "max": 4.0e10},
            {"name": "mesh", "path": "MESH/default_size", "values": [0.05, 0.1, 0.2]},
        ],
        combination="full-factorial",
        self_checks=["validateModel('ALL') 为空", "参数在批准范围内"],
        quality_checks=["求解正常结束", "所有增量步收敛"],
    )
    with pytest.raises(GateError, match="尚未"):
        store.permit_variant(campaign["id"], dat, dcf, {"E": 3.0e10, "mesh": 0.1},
                             changes=["MATERIAL/concrete/LINEAR/ELASTI/YOUNG", "MESH/default_size"],
                             self_check={"passed": True, "checks": []})
    store.gui_decide(campaign["id"], "approve_campaign", "批准本轮参数分析方案")

    with pytest.raises(GateError, match="范围"):
        store.permit_variant(campaign["id"], dat, dcf, {"E": 9.0e10, "mesh": 0.1},
                             changes=["MATERIAL/concrete/LINEAR/ELASTI/YOUNG", "MESH/default_size"],
                             self_check={"passed": True, "checks": []})
    with pytest.raises(GateError, match="自检"):
        store.permit_variant(campaign["id"], dat, dcf, {"E": 3.0e10, "mesh": 0.1},
                             changes=["MATERIAL/concrete/LINEAR/ELASTI/YOUNG", "MESH/default_size"],
                             self_check={"passed": False, "checks": ["validateModel failed"]})
    with pytest.raises(GateError, match="未获批准"):
        store.permit_variant(campaign["id"], dat, dcf, {"E": 3.0e10, "mesh": 0.1},
                             changes=["SHAPES/new-part"], self_check={"passed": True, "checks": []})

    permit = store.permit_variant(
        campaign["id"], dat, dcf, {"E": 3.0e10, "mesh": 0.1},
        changes=["MATERIAL/concrete/LINEAR/ELASTI/YOUNG", "MESH/default_size"],
        self_check={"passed": True, "checks": ["validateModel ok"]},
    )
    assert store.consume_permit(permit["id"], dat, dcf)["source_type"] == "variant"


def test_permit_rejects_changed_files(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    item = review(store, dat, dcf)
    store.gui_decide(item["id"], "approve_once", "批准")
    permit = store.permit_review(item["id"], item["state_fingerprint"], item["dat_sha256"],
                                 self_check={"passed": True})
    dcf.write_text("*NONLIN\n*END\n", encoding="utf-8")
    with pytest.raises(GateError, match="内容不一致"):
        store.consume_permit(permit["id"], dat, dcf)


def test_review_panel_payload_contains_full_model_checks_changes_and_solver_commands(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    dcf.write_text("*NONLIN\nBEGIN EXECUT\n  SOLVE PARDIS MAXTHR 4\nEND EXECUT\n*END\n", encoding="utf-8")
    item = store.create_review(
        project="F:/cases/model.dpf", analysis="nonlinear",
        snapshot={"materials": {"concrete": {"YOUNG": 3.0e10}}, "mesh": {"nodes": 20, "elements": 10}},
        dat=dat, dcf=dcf, diana_version="10.5", units={"length": "m"},
        validation_issues=["support missing"], changes=[{"text": "网格尺寸 0.2 -> 0.1"}],
    )
    text = render_record(Path(store._path("reviews", item["id"])))
    assert "完整模型状态" in text
    assert '"YOUNG": 30000000000.0' in text
    assert "support missing" in text
    assert "网格尺寸 0.2 -> 0.1" in text
    assert "SOLVE PARDIS MAXTHR 4" in text


def test_only_panel_callback_writes_diana_gui_decision(tmp_path):
    store = GuardStore(tmp_path / "guard")
    dat, dcf = inputs(tmp_path)
    item = review(store, dat, dcf)
    record_path = store._path("reviews", item["id"])
    result = apply_decision(record_path, store.root / "audit.jsonl", "approve_once", "已核对")
    assert result["decision_source"] == "diana_gui"
    assert store.get_review(item["id"])["status"] == "approved"
    events = [json.loads(line) for line in (store.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "review.decided" and events[-1]["source"] == "diana_gui"

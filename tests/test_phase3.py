"""Phase 3 of the knowledge base: usage statistics, resident tips, review, verification, handbook, evaluation."""
import json
import time

import pytest

from diana_mcp import evaluation, handbook, review, tips, usage, verify
from diana_mcp.experience import ExperienceStore
from diana_mcp.session import settable_path


@pytest.fixture
def store(tmp_path):
    store = ExperienceStore(tmp_path / "knowledge", use_git=False)
    store.add(title="attach 只吸附到已有顶点", kind="api-usage", symptom="荷载挂错位置", cause="吸附规则",
              fix="把点写进轮廓", triggers=["attach"], entry_id="attach-snaps")
    store.add(title="attach 只会吸附到已有的顶点上", kind="api-usage", symptom="同上", entry_id="attach-snaps-copy")
    store.add(title="发散时减小荷载步", kind="convergence", symptom="LB40/0053", triggers=["/DIANA/AP/LB40/0053"],
              entry_id="smaller-steps")
    store.add(title="历史日志里的 IS41/0001", kind="convergence", symptom="Too small step",
              triggers=["/DIANA/NL/IS41/0001"], source="history-log", entry_id="msg-is41")
    return store


def test_usage_counts_searches_recommendations_and_feedback(tmp_path):
    logs = tmp_path / "logs"
    usage.append("search_log.jsonl", {"id": "s1", "t": 10.0, "query": "q",
                                      "results": [{"key": "experience:a"}, {"key": "manual:Theory/x.html"}]}, logs)
    usage.log_recommendation("job:1", ["a", "b"], logs)
    usage.append("feedback.jsonl", {"t": 20.0, "search_id": "s1", "helpful": ["a", "Theory/x.html"],
                                    "unhelpful": ["b"]}, logs)
    stats = usage.load(logs)
    assert (stats["experience:a"].shown, stats["experience:a"].recommended, stats["experience:a"].helpful) == (1, 1, 1)
    assert stats["manual:Theory/x.html"].helpful == 1 and stats["experience:b"].unhelpful == 1
    assert stats["experience:a"].factor == pytest.approx(1.1) and stats["experience:b"].factor == pytest.approx(0.85)
    assert set(usage.experience_usage(logs)) == {"a", "b"}
    assert usage.key_of("api:createSheet") == "api:createSheet" and usage.key_of("x-y") == "experience:x-y"


def test_resident_tips_rank_verified_entries_by_usage(store):
    store.approve("smaller-steps", "同意")
    store.approve("attach-snaps", "同意")
    stats = {"smaller-steps": usage.Usage(helpful=3), "attach-snaps": usage.Usage(helpful=1)}
    section = tips.resident_section(store, stats, "10.5")
    lines = [l for l in section.splitlines() if l.startswith("- ")]
    assert lines == ["- 发散时减小荷载步 [smaller-steps]", "- attach 只吸附到已有顶点 [attach-snaps]"]
    assert tips.resident_tips(store, stats, "10.5")["verified_lessons"] == [
        "发散时减小荷载步 [smaller-steps]", "attach 只吸附到已有顶点 [attach-snaps]"]
    store.record_check("attach-snaps", {"date": "2026-09-22", "diana_version": "10.4", "result": "pass"})
    assert "re-run diana_experience_verify" in tips.resident_section(store, stats, "10.5")
    assert "re-run diana_experience_verify" in tips.resident_tips(store, stats, "10.5")["note"]
    empty = ExperienceStore(store.root.parent / "empty", use_git=False)
    assert tips.resident_section(empty, {}, "10.5") == "" and tips.resident_tips(empty, {}, "10.5") == {}


def test_review_lists_what_to_bring_to_the_user(store):
    store.record_check("smaller-steps", {"date": "2026-09-22", "diana_version": "10.5", "result": "fail",
                                         "detail": "AssertionError"})
    stats = {"attach-snaps": usage.Usage(helpful=2), "msg-is41": usage.Usage(recommended=3),
             "attach-snaps-copy": usage.Usage(unhelpful=2)}
    report = review.review(store, stats, solved={"smaller-steps": ["job-1"]}, installed_version="10.5")
    suggestions = {s["id"]: s["why"] for s in report["approval_suggestions"]}
    assert "2 次被标为有用" in suggestions["attach-snaps"]
    assert "作业 job-1" in suggestions["smaller-steps"] and "先补全" in suggestions["smaller-steps"]
    # AI drafts without a cause, and a history-log draft that keeps being recommended
    assert sorted(e["id"] for e in report["needs_explanation"]) == ["attach-snaps-copy", "msg-is41", "smaller-steps"]
    assert [sorted(e["id"] for e in g) for g in report["similar_groups"]] == [["attach-snaps", "attach-snaps-copy"]]
    assert report["failed_checks"][0]["id"] == "smaller-steps"
    assert report["reported_unhelpful"][0]["id"] == "attach-snaps-copy"
    assert report["counts"]["status"] == {"未验证": 4}


SCRIPT_BODY = "## 验证脚本\n\n说明\n\n```python\nassert 1 + 1 == 2\n```\n"


def test_verify_records_checks_and_disputes_failed_verified_entries(store, monkeypatch):
    for entry_id in ("attach-snaps", "smaller-steps"):
        entry = store.get(entry_id)
        store.update(entry_id, sections={**entry.sections, "验证脚本": SCRIPT_BODY.split("\n\n", 1)[1]})
    store.approve("smaller-steps", "同意")
    assert store.get("attach-snaps").script == "assert 1 + 1 == 2"
    calls = []

    def fake_run(install, specs, timeout):
        calls.append([s["id"] for s in specs])
        return {"attach-snaps": {"ok": True}, "smaller-steps": {"ok": False, "error": "Traceback\nAssertionError: x"}}

    monkeypatch.setattr(verify, "run_scripts", fake_run)
    install = type("Install", (), {"version": "10.5"})()
    report = verify.verify(store, install)
    assert sorted(calls[0]) == ["attach-snaps", "smaller-steps"]
    assert report["passed"] == ["attach-snaps"] and report["failed"][0]["error"] == "AssertionError: x"
    assert store.get("attach-snaps").checks["result"] == "pass"
    disputed = store.get("smaller-steps")
    assert disputed.status == "disputed" and "自动验证失败" in disputed.dispute["note"]
    assert verify.verify(store, install, ids=["msg-is41"])["without_script"] == ["msg-is41"]


def test_handbook_markdown_and_html(store, tmp_path):
    store.approve("attach-snaps", "同意")
    store.update("smaller-steps", sections={"解决": "```diff\n-  STEPS 0.25(12)\n+  STEPS 0.25(5)\n```"})
    markdown = handbook.build_markdown(store.entries(), {"attach-snaps": usage.Usage(helpful=2)})
    assert "## 目录" in markdown and "## API 用法" in markdown and "## 收敛策略" in markdown
    assert "标为有用 2 次" in markdown and "`/DIANA/AP/LB40/0053`" in markdown
    html = handbook.to_html(markdown)
    assert '<span class="del">-  STEPS 0.25(12)</span>' in html and '<a id="e-attach-snaps"></a>' in html
    assert "<table>" in html and '<a href="#e-smaller-steps">' in html and "<script" not in html
    result = handbook.export(store, {}, tmp_path / "out", statuses=("verified",))
    assert result["entries"] == 1 and (tmp_path / "out" / "DIANA 经验手册.html").is_file()


def test_evaluation_scores_ranks_and_reads_feedback(tmp_path):
    class FakeBase:
        def search(self, query, limit):
            refs = {"a": [("经验", "x"), ("手册", "Theory/p.html")], "b": [("API", "createSheet")]}[query]
            return {"results": [{"source": s, "ref": r} for s, r in refs]}

    items = [{"query": "a", "expect": ["manual:Theory/p.html"]}, {"query": "b", "expect": ["experience:zzz"]}]
    report = evaluation.evaluate(FakeBase(), items)
    assert report["recall@1"] == 0.0 and report["recall@5"] == 0.5 and report["mrr@10"] == 0.25
    assert report["misses"][0]["query"] == "b"
    logs = tmp_path / "logs"
    usage.append("search_log.jsonl", {"id": "s9", "query": "发散了"}, logs)
    usage.append("feedback.jsonl", {"search_id": "s9", "helpful": ["smaller-steps"]}, logs)
    extra = tmp_path / "q.jsonl"
    extra.write_text(json.dumps({"query": "a", "expect": ["manual:Theory/p.html"]}, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    items = evaluation.load_items([extra], logs)
    assert [i["origin"] for i in items] == ["q.jsonl", "feedback"]
    assert items[1] == {"query": "发散了", "expect": ["experience:smaller-steps"], "origin": "feedback"}


@pytest.mark.parametrize("path, expected", [
    ("/MATERI(1)/MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG", "LINEAR/ELASTI/YOUNG"),
    ("/MATERI(1)/MATCLS/CONCR/TSCR/TENSIL/TENSTR", "TENSIL/TENSTR"),
    ("/GEOMET(1)/GEOCLS/SHEET/MEMBRA/THICK", "THICK"),
    ("/POINT(1)/FORCE/VALUE", "FORCE/VALUE"),
    ("/MATERI(1)/MATCLS/MCNAME", None),
    ("/MATERI(1)/ASGNCL/ELEMEN", None),
    ("not a path", None),
])
def test_settable_path(path, expected):
    assert settable_path(path) == expected

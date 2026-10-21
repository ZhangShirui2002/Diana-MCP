import shutil
import subprocess

import pytest

from diana_mcp.experience import Entry, ExperienceError, ExperienceStore, render_body

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git_log(root):
    return subprocess.run(["git", "-C", str(root), "log", "--format=%s%n%b"], capture_output=True, text=True,
                          encoding="utf-8").stdout


@pytest.fixture
def store(tmp_path, monkeypatch):
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@e"), ("GIT_COMMITTER_NAME", "t"),
                       ("GIT_COMMITTER_EMAIL", "t@e")):
        monkeypatch.setenv(key, value)
    return ExperienceStore(tmp_path / "knowledge")


def add(store, **extra):
    fields = dict(title="素混凝土梁荷载控制过峰后发散", kind="收敛策略", symptom="第 9 步发散，LB40/0053",
                  cause="软化段", fix="改用弧长法", triggers=["LB40/0053"], tags=["非线性"])
    fields.update(extra)
    return store.add(**fields)


def test_entry_text_roundtrip_and_aliases(tmp_path):
    entry = Entry(id="attach-snaps", title="t", type="api-usage", status="draft", triggers=["attach"],
                  created="2026-09-22", updated="2026-09-22", body=render_body({"现象": "a", "解决": "b"}))
    text = entry.to_text().replace("status: draft", "status: 已验证").replace("type: api-usage", "type: API 用法")
    path = tmp_path / "attach-snaps.md"
    parsed = Entry.from_text(text, path)
    assert parsed.status == "verified" and parsed.type == "api-usage"
    assert parsed.sections["现象"] == "a" and parsed.sections["原因"] == "（待补充）"
    with pytest.raises(ExperienceError, match="does not match the file name"):
        Entry.from_text(text, tmp_path / "other.md")
    with pytest.raises(ExperienceError, match="status must be one of"):
        Entry.from_text(text.replace("status: 已验证", "status: 可能"), path)


def test_matched_triggers():
    entry = Entry(id="x-y-z", title="t", type="error-fix", triggers=["LB40/0053", "re:too small step", "attach"])
    assert entry.matched_triggers("FATAL /DIANA/AP/LB40/0053 ... Too Small Step in AUTOMA") == [
        "LB40/0053", "re:too small step"]


@needs_git
def test_add_update_approve_dispute_with_git_history(store):
    entry, commit = add(store)
    assert commit["committed"] and entry.status == "draft" and entry.source == "ai"
    assert entry.id == "convergence-" + entry.created.replace("-", "")  # a Chinese title gives a dated id
    assert (store.root / "README.md").is_file() and (store.root / ".git").is_dir()

    entry, commit = store.update(entry.id, add_evidence=["2026-10-02 作业 ab12 弧长法收敛"], sections={"验证": "弧长法后收敛"})
    assert commit["committed"] and entry.evidence == ["2026-10-02 作业 ab12 弧长法收敛"]
    assert entry.sections["验证"] == "弧长法后收敛" and entry.sections["解决"] == "改用弧长法"

    with pytest.raises(ExperienceError, match="user's confirmation"):
        store.approve(entry.id, " ")
    entry, _ = store.approve(entry.id, "同意，批准这条")
    assert entry.status == "verified" and entry.approval["note"] == "同意，批准这条"
    with pytest.raises(ExperienceError, match="use approve"):
        store.set_status(entry.id, "verified", "x")
    entry, _ = store.set_status(entry.id, "disputed", "新模型中弧长法也不收敛")
    assert entry.status == "disputed" and entry.dispute["note"] == "新模型中弧长法也不收敛"
    assert store.validate() == []

    log = git_log(store.root)
    for fragment in ("新增草稿", "更新", "批准为已验证", "原话：同意，批准这条", "标记为存疑", "初始化经验手册仓库"):
        assert fragment in log


@needs_git
def test_commits_only_the_changed_entry(store):
    first, _ = add(store)
    second, _ = add(store, title="attach() snaps to the nearest vertex", kind="api-usage", triggers=["attach"])
    assert second.id == "attach-snaps-to-the-nearest-vertex"
    path = store.entries_dir / f"{first.id}.md"
    path.write_text(path.read_text(encoding="utf-8").replace("软化段", "手工改动"), encoding="utf-8")
    store.update(second.id, tags=["荷载"])
    assert store.uncommitted() == [f"experiences/{first.id}.md"]


def test_invalid_files_are_reported_not_fatal(store):
    store.ensure()
    (store.entries_dir / "broken.md").write_text("no front matter", encoding="utf-8")
    add(store)
    entries, problems = store.load()
    assert len(entries) == 1 and problems and "broken.md" in problems[0]


def test_duplicates_and_similar(store):
    entry, _ = add(store)
    assert [e.id for e in store.likely_duplicates("素混凝土梁在荷载控制下过峰后发散")] == [entry.id]
    assert [e.id for e in store.likely_duplicates("完全不同的标题", triggers=["LB40/0053"])] == [entry.id]
    assert store.likely_duplicates("完全不同的标题", triggers=["abc"]) == []
    assert store.similar("LB40/0053 发散")[0][0].id == entry.id
    with pytest.raises(ExperienceError, match="already exists"):
        add(store, entry_id=entry.id)


def test_without_git_files_are_still_saved(tmp_path):
    store = ExperienceStore(tmp_path / "k", use_git=False)
    entry, commit = add(store)
    assert not commit["committed"] and "git" in commit["reason"]
    assert store.get(entry.id).title == entry.title

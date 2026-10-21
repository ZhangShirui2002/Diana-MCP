import shutil
import subprocess

import pytest

from diana_mcp import mining
from diana_mcp.experience import ExperienceStore

OUT_A = """\
 SEVERITY  : ABORT
 ERROR CODE: /DIANA/NL/IS41/0001
 ERRORMSG.A: Too small step in AUTOMA. Check your model, and decrease MINSIZ if it is correct.

 SEVERITY  : WARNING
 ERROR CODE: /DIANA/LB/EL30/0094
 ERRORMSG.W: One of the angles between edges at node 12 of element 7 is
 less than 10 degrees or larger than 170 degrees.

 SEVERITY  : WARNING
    50 WARNINGS SEEN
  FURTHER WARNINGS WILL BE SUPPRESSED

 Unexpected input MAXTHR
 Last line was
 DIANA-JOB ABORTED
"""
OUT_B = """\
 SEVERITY  : WARNING
 ERROR CODE: /DIANA/LB/EL30/0094
 ERRORMSG.W: One of the angles between edges at node 3 of element 9 is
 less than 10 degrees or larger than 170 degrees.

 Unexpected input PARDIS
 Unexpected input MAXTHR
"""


def test_normalize_and_keys():
    assert mining.normalize("node 12 of element 7, delta=0.35E-03 in A1-3D.dat") == "node <n> of element <n>, delta=<x> in <file>"
    # DIANA keywords stay, the user's own names do not
    assert mining.normalize("got 'POISON'. Line 25: NAME \"REBAR_GROUT\"") == "got 'POISON'. Line <n>: NAME <q>"
    assert mining.message_key("FATAL", "/DIANA/LB/FW/1002", "Integer expected, got 'POISON'.") == (
        "/DIANA/LB/FW/1002", "POISON")
    assert mining.message_key("ABORT", "/DIANA/NL/IS41/0001", "x") == ("/DIANA/NL/IS41/0001", "")
    assert mining.message_key("SYNTAX", "", "Unexpected input maxthr") == ("SYNTAX:Unexpected input", "MAXTHR")
    assert mining.message_key("SYNTAX", "", "Last line was") is None
    assert mining.message_key("WARNING", "", "50 WARNINGS SEEN FURTHER WARNINGS") == ("WARNING:<n> WARNINGS SEEN FURTHER", "")
    assert mining.message_key("FATAL", "", "Integer expected, got 'KAPCOH'. Line 3") == (
        "FATAL:Integer expected, got <q>.", "KAPCOH")
    assert mining.message_key("FATAL", "", "Integer expected, got 'ELEMEN'. Line 9")[0] == "FATAL:Integer expected, got <q>."
    assert mining.entry_id("/DIANA/NL/IS41/0001") == "msg-diana-nl-is41-0001"
    assert mining.entry_id("FATAL:Integer expected, got <q>.") == "msg-fatal-integer-expected-got"


@pytest.fixture
def logs(tmp_path):
    folder = tmp_path / "runs"
    (folder / "a").mkdir(parents=True)
    (folder / "a" / "one.out").write_text(OUT_A, encoding="utf-8")
    (folder / "two.out").write_text(OUT_B, encoding="utf-8")
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    return folder


def test_scan_groups_messages(logs):
    paths = mining.find_out_files([logs])
    assert [p.name for p in paths] == ["one.out", "two.out"]
    kinds, stats = mining.scan(paths)
    assert stats["files"] == 2 and stats["endings"] == {"aborted": 1, "unfinished": 1}
    angle = kinds["/DIANA/LB/EL30/0094"]
    assert angle.count == 2 and len(angle.files) == 2 and len(angle.texts) == 1  # numbers normalized away
    syntax = kinds["SYNTAX:Unexpected input"]
    assert dict(syntax.details) == {"MAXTHR": 2, "PARDIS": 1}
    assert "WARNING:<n> WARNINGS SEEN FURTHER" in kinds


def test_seed_creates_drafts_once_and_refreshes_statistics(logs, tmp_path, monkeypatch):
    use_git = shutil.which("git") is not None
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@e"), ("GIT_COMMITTER_NAME", "t"),
                       ("GIT_COMMITTER_EMAIL", "t@e")):
        monkeypatch.setenv(key, value)
    store = ExperienceStore(tmp_path / "knowledge", use_git=use_git)
    kinds, _ = mining.scan(mining.find_out_files([logs]))
    assert set(mining.seed(store, kinds, dry_run=True)["would_create"]) == {mining.entry_id(k) for k in kinds}

    report = mining.seed(store, kinds, titles={"/DIANA/NL/IS41/0001": "自动步长过小而中止"}, diana_version="10.5")
    assert report["created"][:2] == ["msg-syntax-unexpected-input", "msg-diana-nl-is41-0001"]  # severe first
    assert len(report["created"]) == 4
    entry = store.get("msg-diana-nl-is41-0001")
    assert entry.title == "自动步长过小而中止" and entry.status == "draft" and entry.source == "history-log"
    assert entry.type == "convergence" and entry.triggers == ["/DIANA/NL/IS41/0001"]
    assert "decrease MINSIZ" in entry.sections["解决"] and entry.evidence[0].startswith("历史日志：1 次，1 个 .out 文件")
    syntax = store.get("msg-syntax-unexpected-input")
    assert "| MAXTHR | 2 |" in syntax.sections["现象"] and syntax.triggers == ["Unexpected input"]
    if use_git:
        log = subprocess.run(["git", "-C", str(store.root), "log", "--format=%s"], capture_output=True, text=True,
                             encoding="utf-8").stdout.splitlines()
        assert log == ["knowledge: 从历史日志生成草稿", "knowledge: 初始化经验手册仓库"]  # one commit for all

    again = mining.seed(store, kinds)
    assert again["created"] == [] and again["updated"] == [] and len(again["unchanged"]) == 4
    (logs / "three.out").write_text(OUT_B, encoding="utf-8")
    kinds, _ = mining.scan(mining.find_out_files([logs]))
    refreshed = mining.seed(store, kinds)
    assert "msg-diana-lb-el30-0094" in refreshed["updated"]
    assert store.get("msg-diana-lb-el30-0094").evidence[0].startswith("历史日志：3 次，3 个 .out 文件")


def test_remote_logs_are_read_over_ssh_and_never_stored(logs, tmp_path, monkeypatch):
    """An ssh:// folder is listed and streamed over SSH: entries cite the remote path and the temporary copy
    that makes parsing possible is gone afterwards."""
    sample = logs / "a" / "one.out"
    tempdir = tmp_path / "temp"
    tempdir.mkdir()
    monkeypatch.setattr(mining.tempfile, "tempdir", str(tempdir))
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command[-1])
        if command[-1].startswith("find "):
            listing = f"{sample.stat().st_size} 1700000000.0 /work/diana_jobs/a/one.out\n"
            return subprocess.CompletedProcess(command, 0, listing, "")
        kwargs["stdout"].write(sample.read_bytes())  # the 'cat' of the remote file
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(mining.subprocess, "run", fake_run)
    items = mining.find_out_files(["ssh://engineer@compute-host/work/diana_jobs"])
    assert [i.label for i in items] == ["ssh://engineer@compute-host/work/diana_jobs/a/one.out"]

    kinds, stats = mining.scan(items)
    assert stats["files"] == 1
    assert kinds["/DIANA/NL/IS41/0001"].files == {"ssh://engineer@compute-host/work/diana_jobs/a/one.out"}
    assert not list(tempdir.iterdir())  # the streamed copy was deleted
    assert any(call.startswith("find ") for call in seen) and any(call.startswith("cat ") for call in seen)


def test_ssh_options_keep_a_windows_key_path(monkeypatch):
    monkeypatch.setenv("DIANA_MCP_SSH_OPTIONS", r'-i "C:\Users\me\.ssh\id_ed25519" -o IdentitiesOnly=yes')
    assert mining.ssh_options()[:4] == ["-i", r"C:\Users\me\.ssh\id_ed25519", "-o", "IdentitiesOnly=yes"]

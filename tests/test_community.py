import json
import subprocess
from pathlib import Path

import pytest

from diana_mcp.community import (
    CommunityError,
    CommunityStore,
    ContributionManager,
    _github_slug,
)
from diana_mcp.experience import ExperienceStore
from diana_mcp.knowledge import CommunityRetriever, Query


def _entry(store: ExperienceStore, *, entry_id: str = "private-path-lesson"):
    entry, _ = store.add(
        title="F:/Clients/Bridge-A 的网格问题",
        kind="modeling-pitfall",
        symptom="C:\\Users\\alice\\Projects\\Bridge-A\\model.dat 在 10.20.30.40 失败",
        cause="联系 alice@example.com；主机 WORKSTATION-ALICE",
        fix="把 C:\\Users\\alice\\Projects\\Bridge-A\\run.dcf 中的网格尺寸调小",
        verification="在 F:/Clients/Bridge-A/result.out 中复核",
        evidence=["F:/Clients/Bridge-A/result.out"],
        source="ai",
        entry_id=entry_id,
    )
    return entry


def test_prepare_is_non_destructive_and_redacts_a_public_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("COMPUTERNAME", "WORKSTATION-ALICE")
    private = ExperienceStore(tmp_path / "private", use_git=False)
    original = _entry(private)
    original_text = original.path.read_text(encoding="utf-8")

    manager = ContributionManager(private, tmp_path / "staging")
    report = manager.prepare([original.id])

    assert original.path.read_text(encoding="utf-8") == original_text
    assert report["entries"] == 1 and report["requires_human_review"] is True
    assert report["upload_performed"] is False
    preview = manager.preview(report["batch_id"])
    candidate = Path(preview["items"][0]["file"])
    public_text = candidate.read_text(encoding="utf-8")
    for secret in ("alice", "Bridge-A", "10.20.30.40", "alice@example.com", "WORKSTATION-ALICE"):
        assert secret.lower() not in public_text.lower()
    assert "<LOCAL_PATH>" in public_text and "community_status: candidate" in public_text
    assert "evidence: []" in public_text and "checks:" not in public_text
    assert preview["items"][0]["findings"]


def test_contribution_requires_the_exact_human_confirmation(tmp_path):
    private = ExperienceStore(tmp_path / "private", use_git=False)
    entry = _entry(private, entry_id="confirmation-gate")
    manager = ContributionManager(private, tmp_path / "staging")
    batch = manager.prepare([entry.id])["batch_id"]

    with pytest.raises(CommunityError, match="UPLOAD"):
        manager.contribute(batch, "同意")
    assert not (tmp_path / "staging" / batch / "UPLOADED.json").exists()


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8")
    return done.stdout.strip()


def test_community_store_syncs_a_read_only_git_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "test")
    _git(source, "config", "user.email", "test@example.invalid")
    candidate = source / "experiences" / "candidate"
    candidate.mkdir(parents=True)
    private = ExperienceStore(tmp_path / "seed", use_git=False)
    entry = _entry(private, entry_id="shared-mesh-lesson")
    text = entry.to_text().replace("status: draft", "status: draft\ncommunity_status: candidate\nconfidence: 0.2")
    (candidate / "shared-mesh-lesson.md").write_text(text, encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "seed")

    store = CommunityStore(tmp_path / "cache", url=str(source), branch=_git(source, "branch", "--show-current"))
    report = store.sync()

    assert report["synced"] and report["entries"] == 1 and report["commit"]
    assert store.entries()[0].extra["community_status"] == "candidate"
    assert store.status()["dirty"] is False


def test_community_retriever_keeps_provenance_and_trust(tmp_path):
    root = tmp_path / "community"
    folder = root / "experiences" / "community-confirmed"
    folder.mkdir(parents=True)
    private = ExperienceStore(tmp_path / "seed", use_git=False)
    entry = _entry(private, entry_id="community-mesh-lesson")
    text = entry.to_text().replace(
        "status: draft", "status: verified\ncommunity_status: community-confirmed\nconfidence: 0.82",
    )
    (folder / "community-mesh-lesson.md").write_text(text, encoding="utf-8")

    hits = CommunityRetriever(CommunityStore(root)).search(Query("网格问题 Bridge-A"), 5)

    assert hits and hits[0].source == "community"
    assert hits[0].meta["community_status"] == "community-confirmed"
    assert hits[0].meta["confidence"] == 0.82
    assert hits[0].meta["repository"]
    assert 0.8 < hits[0].meta["trust_factor"] < 1.1


def test_generated_index_can_raise_or_dispute_trust_without_rewriting_contributions(tmp_path):
    root = tmp_path / "community"
    folder = root / "experiences" / "candidate"
    folder.mkdir(parents=True)
    private = ExperienceStore(tmp_path / "seed", use_git=False)
    entry = _entry(private, entry_id="indexed-community-lesson")
    text = entry.to_text().replace("status: draft", "status: draft\ncommunity_status: candidate\nconfidence: 0.1")
    (folder / "indexed-community-lesson.md").write_text(text, encoding="utf-8")
    (root / "index.json").write_text(json.dumps({
        "format": 1,
        "entries": {"indexed-community-lesson": {
            "effective_status": "community-confirmed", "confidence": 0.78,
            "successful_confirmations": 3, "failed_confirmations": 0,
        }},
    }), encoding="utf-8")

    loaded = CommunityStore(root).entries()[0]

    assert loaded.extra["community_status"] == "community-confirmed"
    assert loaded.extra["confidence"] == 0.78
    assert loaded.extra["successful_confirmations"] == 3


def test_manifest_never_contains_original_private_text(tmp_path):
    private = ExperienceStore(tmp_path / "private", use_git=False)
    entry = _entry(private)
    manager = ContributionManager(private, tmp_path / "staging")
    batch = manager.prepare([entry.id])["batch_id"]
    manifest = json.loads((tmp_path / "staging" / batch / "manifest.json").read_text(encoding="utf-8"))
    encoded = json.dumps(manifest, ensure_ascii=False)
    assert "Bridge-A" not in encoded and "alice" not in encoded.lower()


def test_github_fork_slug_and_status_are_explicit(tmp_path):
    store = CommunityStore(tmp_path / "cache", push_url="git@github.com:someone/Diana-MCP-Knowledge.git")
    assert _github_slug(store.push_url) == "someone/Diana-MCP-Knowledge"
    assert _github_slug("https://github.com/ZhangShirui2002/Diana-MCP-Knowledge.git") == \
        "ZhangShirui2002/Diana-MCP-Knowledge"
    assert store.status()["contribution_repository"] == store.push_url

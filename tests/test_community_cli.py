import json

from diana_mcp.experience import ExperienceStore
from diana_mcp.knowledge_cli import main


def test_nested_community_cli_status_prepare_and_preview(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DIANA_MCP_KNOWLEDGE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("DIANA_MCP_COMMUNITY_DIR", str(tmp_path / "community"))
    store = ExperienceStore()
    entry, _ = store.add(title="网格局部过密", kind="modeling-pitfall", symptom="节点数异常增大",
                         entry_id="mesh-too-fine", source="ai")

    assert main(["community", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["synced"] is False and status["automatic_network_access"] is False

    assert main(["community", "redact", entry.id]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["entries"] == 1 and prepared["upload_performed"] is False

    assert main(["community", "preview", prepared["batch_id"]]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["required_confirmation"] == "UPLOAD " + prepared["batch_id"]
    assert "网格局部过密" in preview["items"][0]["text"]

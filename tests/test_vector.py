import json
import threading
import time

import pytest

from diana_mcp import config, knowledge, vector
from diana_mcp.community import CommunityStore
from diana_mcp.experience import ExperienceStore
from diana_mcp.knowledge import Context, Document, KnowledgeBase
from diana_mcp.manual import Manual, ManualStore

from test_manual import PAGES


def docs(*items):
    return [Document(f"{s}:{r}", s, r, t, x) for s, r, t, x in items]


def test_hash_embedder_is_normalized_and_deterministic():
    embedder = vector.HashEmbedder(64)
    a, b = embedder.embed(["LINPAG page layout", "LINPAG page layout"], "document")
    assert a == b and sum(v * v for v in a) == pytest.approx(1.0)
    assert embedder.name == "hash:64" and vector.HashEmbedder().name == "hash"


def test_index_update_reuses_unchanged_vectors_and_replaces_a_prefix(tmp_path):
    embedder = vector.HashEmbedder(64)
    index = vector.VectorIndex(tmp_path / "idx")
    first = index.update(docs(("manual", "a", "A", "tabular output LINPAG"), ("experience", "x", "X", "发散 荷载步")),
                         embedder)
    assert first == {"documents": 2, "embedded": 2, "reused": 0}
    index.save()
    loaded = vector.VectorIndex(tmp_path / "idx").load()
    assert loaded.ids == ["manual:a", "experience:x"] and loaded.model == "hash:64"
    again = loaded.update(docs(("experience", "y", "Y", "界面单元 法向刚度")), embedder, only_prefix="experience:")
    assert again == {"documents": 2, "embedded": 1, "reused": 1} and loaded.ids == ["experience:y", "manual:a"]
    position, score = loaded.search(embedder.embed(["LINPAG tabular"], "query")[0], 1)[0]
    assert loaded.ids[position] == "manual:a" and score > 0
    assert loaded.search([0.0] * 64, 0) == []


def test_meaningless_chunks_are_not_embedded():
    kept = Document("manual:Theory/Theoryse300.html#0", "manual", "Theory/Theoryse300.html", "TSCR", "shear " * 12)
    bare_keyword = Document("keyword:NlinstSteps:SIZES:1", "keyword", "NlinstSteps:SIZES", "SIZES", "SIZES")
    index_page = Document("manual:Theory/Theoryli6.html#3", "manual", "Theory/Theoryli6.html", "Index", "Arc " * 30)
    assert [vector.indexable(d) for d in (kept, bare_keyword, index_page)] == [True, False, False]


def test_vectors_of_another_model_are_never_reused(tmp_path):
    index = vector.VectorIndex(tmp_path / "idx")
    index.update(docs(("manual", "a", "A", "LINPAG")), vector.HashEmbedder(64))
    stats = index.update(docs(("manual", "a", "A", "LINPAG")), vector.HashEmbedder(32))
    assert stats["embedded"] == 1 and index.model == "hash:32" and len(index.vectors[0]) == 32


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("DIANA_MCP_VECTOR_MODEL")  # conftest switches the configured model off for other tests
    manuals = ManualStore(tmp_path / "manual")
    manuals._loaded["10.5"] = Manual("10.5", PAGES)
    experiences = ExperienceStore(tmp_path / "knowledge", use_git=False)
    experiences.add(title="Hordijk tension softening curve", kind="parameter", symptom="TENCRV HORDYK",
                    entry_id="hordijk-curve")
    return Context(config.DianaInstall(tmp_path / "Diana 10.5", "10.5"), experiences, manuals)


def test_factory_is_inactive_until_a_model_is_built(context, tmp_path):
    assert vector.factory(context) is None and vector.warm_up() is False
    report = vector.build(context, "hash")
    assert report["model"] == "hash" and report["documents"] >= 4 and report["active"]
    assert report["folder"].startswith(str(tmp_path / "models"))  # large files go to the models folder
    assert json.loads(vector.settings_path().read_text(encoding="utf-8"))["model"] == "hash"
    retriever = vector.factory(context)
    hits = retriever.search(knowledge.Query("Hordijk tension softening"), 3)
    assert ("experience", "hordijk-curve") in [(h.source, h.ref) for h in hits]
    assert vector.warm_up() is True and vector.status()["index_exists"]
    vector.disable()
    assert vector.factory(context) is None


def test_the_model_loads_once_and_only_when_needed(context, monkeypatch):
    loads = []
    real = vector.load_embedder
    monkeypatch.setattr(vector, "load_embedder", lambda spec, device=None: loads.append(spec) or real(spec, device))
    vector.build(context, "hash:48")
    assert loads == ["hash:48"]
    vector.build(context, "hash:48")  # nothing changed: the index is reused without loading the model
    assert loads == ["hash:48"]
    one, two = vector.factory(context), vector.factory(context)
    assert one.index is two.index  # the index is loaded once per process
    one.search(knowledge.Query("Hordijk"), 2)
    two.search(knowledge.Query("LINPAG"), 2)
    assert loads == ["hash:48", "hash:48"]  # the search model: loaded once, shared by both


def test_a_search_never_waits_for_a_slow_model(context, monkeypatch):
    vector.build(context, "hash:40")
    release = threading.Event()
    real = vector.load_embedder
    monkeypatch.setattr(vector, "load_embedder", lambda spec, device=None: release.wait(5) and real(spec, device))
    monkeypatch.setattr(vector, "_slow_to_load", lambda spec: True)
    retriever = vector.factory(context)
    with pytest.raises(vector.NotReady, match="loading in the background"):
        retriever.search(knowledge.Query("Hordijk"), 2)
    release.set()
    deadline = time.monotonic() + 5
    while ("hash:40", "cpu") not in vector._EMBEDDERS and time.monotonic() < deadline:
        time.sleep(0.02)
    assert retriever.search(knowledge.Query("Hordijk tension softening"), 2)


def test_environment_overrides_for_comparisons(context, monkeypatch):
    vector.build(context, "hash")
    monkeypatch.setenv("DIANA_MCP_VECTOR_MODEL", "none")
    assert vector.factory(context) is None
    monkeypatch.setenv("DIANA_MCP_VECTOR_MODEL", "hash")
    monkeypatch.setenv("DIANA_MCP_VECTOR_WEIGHT", "0.5")
    assert vector.factory(context).weight == 0.5
    assert vector.canonical("st:BAAI/bge-m3") == "sentence-transformers:BAAI/bge-m3"


def test_new_experiences_are_embedded_before_searching(context):
    vector.build(context, "hash")
    retriever = vector.factory(context)
    retriever.search(knowledge.Query("warm up"), 3)
    context.experiences.add(title="LINPAG zero removes page breaks", kind="workflow", symptom="LINPAG",
                            entry_id="linpag-zero")
    hits = retriever.search(knowledge.Query("LINPAG zero page breaks"), 3)
    assert hits[0].ref == "linpag-zero"
    reloaded = vector.VectorIndex(vector.index_for("hash").folder).load()
    assert "experience:linpag-zero" in reloaded.ids  # saved for the next server start
    assert vector.factory(context).index is retriever.index  # its own save does not force a reload


def test_synced_community_entries_incrementally_join_the_vector_index(context, tmp_path):
    vector.build(context, "hash:72")
    retriever = vector.factory(context)
    root = tmp_path / "community"
    (root / ".git").mkdir(parents=True)
    folder = root / "experiences" / "candidate"
    folder.mkdir(parents=True)
    source = context.experiences.get("hordijk-curve")
    text = source.to_text().replace(
        "id: hordijk-curve", "id: community-contact-status",
    ).replace(
        "title: Hordijk tension softening curve", "title: Contact status output interpretation",
    ).replace(
        "status: draft", "status: draft\ncommunity_status: candidate\nconfidence: 0.2",
    ).replace("TENCRV HORDYK", "CONTACT STATUS STICK SLIP")
    (folder / "community-contact-status.md").write_text(text, encoding="utf-8")
    context.community = CommunityStore(root)

    hits = retriever.search(knowledge.Query("CONTACT STATUS STICK SLIP"), 5)

    assert ("community", "community-contact-status") in [(h.source, h.ref) for h in hits]
    loaded = vector.VectorIndex(vector.index_for("hash:72").folder).load()
    assert "community:community-contact-status" in loaded.ids


def test_the_plugin_joins_the_fusion_and_can_be_switched_off(context, monkeypatch):
    vector.build(context, "hash")
    monkeypatch.setattr(knowledge, "_FACTORIES", list(knowledge._FACTORIES) + [vector.factory])
    names = [r.name for r in KnowledgeBase(context).retrievers()]
    assert "vector" in names
    monkeypatch.setenv("DIANA_MCP_DISABLE_RETRIEVERS", "vector")
    assert "vector" not in [r.name for r in KnowledgeBase(context).retrievers()]


def test_unknown_model_spec():
    with pytest.raises(ValueError, match="unknown embedding model"):
        vector.load_embedder("word2vec:x")

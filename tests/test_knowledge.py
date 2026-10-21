import json

import pytest

from diana_mcp import config, knowledge
from diana_mcp.experience import ExperienceStore
from diana_mcp.glossary import Glossary
from diana_mcp.knowledge import Context, Hit, KnowledgeBase, Query, chunk_text, fuse
from diana_mcp.manual import Manual, ManualStore
from diana_mcp.textindex import BM25, tokenize

from test_manual import PAGES


def test_tokenize_mixes_bigrams_and_codes():
    tokens = tokenize("荷载步长 FATAL /DIANA/NL/IS41/0001 LINEAR/ELASTI/YOUNG")
    assert {"荷载", "载步", "步长", "is41", "0001", "is41/0001", "linear/elasti/young"} <= set(tokens)
    assert tokenize("力") == ["力"]


def test_bm25_prefers_rare_terms_and_weights_fields():
    bm25 = BM25({"a": [("attach 荷载", 3.0), ("common words", 1.0)], "b": [("common words common", 1.0)]})
    assert bm25.search("attach")[0][0] == "a"
    assert bm25.search("nothing") == []


class Fixed:
    def __init__(self, name, weight, hits):
        self.name, self.weight, self._hits = name, weight, hits

    def search(self, query, limit):
        return self._hits[:limit]


def test_fuse_ranks_per_source_and_counts_each_key_once():
    manual = Fixed("manual", 1.0, [Hit("example", "E1", "e"), Hit("example", "E2", "e"), Hit("manual", "M1", "m")])
    keyword = Fixed("keyword", 0.8, [Hit("keyword", "K1", "k"), Hit("keyword", "K1", "k"), Hit("keyword", "K2", "k")])
    experience = Fixed("experience", 2.0, [Hit("experience", "X1", "x", meta={"status_key": "draft"}),
                                           Hit("experience", "X2", "x", meta={"status_key": "verified"})])
    fused = fuse([(manual, manual._hits), (keyword, keyword._hits), (experience, experience._hits)], limit=10)
    order = [h.ref for h in fused]
    assert order[:2] == ["X2", "X1"]  # a verified entry outranks a draft one place above it
    assert order.index("M1") < order.index("E1")  # manual pages are not pushed down by examples
    assert order.count("K1") == 1 and "status_key" not in fused[0].meta


def test_chunks_follow_section_headings():
    text = "intro\n53.3.3 Nonlinear Tension Softening\n" + "x" * 3200 + "\n53.3.4 Next Section Here\nshort"
    chunks = chunk_text(text, size=1500, overlap=200)
    assert chunks[0] == ("", "intro")
    assert [c[0] for c in chunks[1:4]] == ["53.3.3 Nonlinear Tension Softening"] * 3
    assert chunks[-1][0] == "53.3.4 Next Section Here"


@pytest.fixture
def base(tmp_path):
    install = config.DianaInstall(tmp_path / "Diana 10.5", "10.5")
    manuals = ManualStore(tmp_path / "manual")
    manuals._loaded["10.5"] = Manual("10.5", PAGES)
    experiences = ExperienceStore(tmp_path / "knowledge", use_git=False)
    experiences.add(title="TABULA 输出不分页要设 LINPAG 0", kind="workflow", symptom=".tb 每 66 行重复表头",
                    fix="LAYOUT LINPAG 0", triggers=["LINPAG"], entry_id="tabula-no-paging")
    glossary = Glossary([("Tabular output", "表格输出"), ("Lines per page", "每页行数")])
    return KnowledgeBase(Context(install, experiences, manuals, glossary), log_dir=tmp_path / "logs")


def test_search_translates_chinese_and_logs(base, tmp_path):
    result = base.search("每页行数怎么设")
    assert result["terms"] == [{"中文": "每页行数", "English": ["Lines per page"]}]
    assert result["searched_in_english"] == "Lines per page"
    refs = [(r["source"], r["ref"]) for r in result["results"]]
    assert ("手册", "Theory/Theoryse738.html") in refs
    english = base.search("LINPAG")
    assert [(r["source"], r["ref"]) for r in english["results"]][0] == ("经验", "tabula-no-paging")
    assert english["results"][0]["status"] == "未验证"
    only = base.search("LINPAG", sources=["manual"])
    assert {r["source"] for r in only["results"]} == {"手册"}
    base.feedback(english["search_id"], ["tabula-no-paging"], "对")
    log = [json.loads(l) for l in (tmp_path / "logs" / "search_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["query"] for r in log] == ["每页行数怎么设", "LINPAG", "LINPAG"]
    feedback = json.loads((tmp_path / "logs" / "feedback.jsonl").read_text(encoding="utf-8"))
    assert feedback["search_id"] == english["search_id"] and feedback["helpful"] == ["tabula-no-paging"]


def test_obsolete_entries_are_hidden_unless_asked(base):
    base.context.experiences.set_status("tabula-no-paging", "obsolete", "新版本默认不分页")
    assert all(r["source"] != "经验" for r in base.search("LINPAG")["results"])
    shown = base.search("LINPAG", include_obsolete=True)["results"]
    assert [r["status"] for r in shown if r["ref"] == "tabula-no-paging"] == ["过时"]
    assert shown[0]["ref"] != "tabula-no-paging"  # ranked below live knowledge


def test_plugins_register_and_failures_do_not_break_search(base, monkeypatch):
    calls = []

    class Vector:
        name, weight = "vector", 1.5

        def search(self, query, limit):
            calls.append(query.english_text)
            return [Hit("manual", "Theory/Theoryse350.html", "Discrete Cracking")]

    def broken(context):
        raise RuntimeError("model missing")

    monkeypatch.setattr(knowledge, "_FACTORIES", list(knowledge._FACTORIES))
    knowledge.register(lambda context: Vector())
    knowledge.register(broken)
    result = base.search("Hordijk softening")
    assert calls == ["Hordijk softening"]
    assert result["results"][0]["ref"] == "Theory/Theoryse350.html"  # both retrievers found it
    assert any("model missing" in p for p in result["problems"])


def test_plugins_load_from_the_environment(monkeypatch, tmp_path):
    module = tmp_path / "my_retriever.py"
    module.write_text("def factory(context):\n    return None\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DIANA_MCP_RETRIEVERS", "my_retriever:factory, missing_module:factory")
    monkeypatch.setattr(knowledge, "_FACTORIES", list(knowledge._FACTORIES))
    monkeypatch.setattr(knowledge, "_PLUGINS_LOADED", False)
    monkeypatch.setattr(knowledge, "_PLUGIN_ERRORS", [])
    errors = knowledge.load_plugins()
    assert len(errors) == 1 and errors[0].startswith("missing_module:factory")
    assert knowledge._FACTORIES[-1].__name__ == "factory"


def test_documents_cover_every_source(base):
    documents = list(base.context.documents())
    sources = {d.source for d in documents}
    assert {"experience", "manual"} <= sources
    experience = next(d for d in documents if d.source == "experience")
    assert experience.id == "experience:tabula-no-paging" and experience.lang == "zh"
    assert all(d.id.startswith(d.source + ":") for d in documents)


def test_query_english_text():
    assert Query("LINPAG").english_text == "LINPAG"
    assert Query("每页行数", english="Lines per page").english_text == "Lines per page"
    assert Query("中文没有术语").english_text == ""

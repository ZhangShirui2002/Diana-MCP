"""How well diana_knowledge_search finds the right knowledge: recall and MRR on a set of real questions.

Questions come from knowledge/eval/*.jsonl ({"query", "expect": ["experience:<id>", "manual:<page>", ...]}) and
from the feedback log (a search followed by diana_knowledge_feedback with the results that helped). Run it before
and after changing retrieval, e.g. when adding the vector retriever, to see whether it actually helps.

    python -m diana_mcp knowledge evaluate [extra.jsonl ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

from . import config, usage
from .api_reference import ApiReferenceStore
from .experience import ExperienceStore
from .glossary import GlossaryStore
from .knowledge import Context, KnowledgeBase
from .manual import ManualStore

SOURCE_KEYS = {"经验": "experience", "手册": "manual", "算例": "example", "关键字": "keyword", "API": "api"}


def load_items(files: Iterable[Path], log_folder: Path | None = None) -> list[dict[str, Any]]:
    items = []
    for path in files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                items.append({"query": item["query"], "expect": item["expect"], "origin": Path(path).name})
    searches = {r["id"]: r["query"] for r in usage.read("search_log.jsonl", log_folder) if "id" in r}
    for record in usage.read("feedback.jsonl", log_folder):
        query = searches.get(record.get("search_id"))
        helpful = [usage.key_of(ref) for ref in record.get("helpful") or []]
        if query and helpful:
            items.append({"query": query, "expect": helpful, "origin": "feedback"})
    return items


def evaluate(kb: KnowledgeBase, items: list[dict[str, Any]], depth: int = 10) -> dict[str, Any]:
    ranks, misses = [], []
    for item in items:
        result = kb.search(item["query"], limit=depth)
        keys = [f"{SOURCE_KEYS.get(r['source'], r['source'])}:{r['ref']}" for r in result["results"]]
        rank = next((i + 1 for i, key in enumerate(keys) if key in item["expect"]), None)
        ranks.append(rank)
        if rank is None or rank > 5:
            misses.append({"query": item["query"], "expect": item["expect"], "rank": rank, "top": keys[:3]})
    n = len(items) or 1
    return {
        "questions": len(items),
        "recall@1": round(sum(1 for r in ranks if r == 1) / n, 3),
        "recall@5": round(sum(1 for r in ranks if r and r <= 5) / n, 3),
        "recall@10": round(sum(1 for r in ranks if r) / n, 3),
        "mrr@10": round(sum(1 / r for r in ranks if r) / n, 3),
        "misses": misses,
    }


def default_base(log_folder: Path | None = None) -> KnowledgeBase:
    try:
        install = config.default_install()
    except FileNotFoundError:
        install = None
    glossary = None
    if install is not None:
        try:
            glossary = GlossaryStore().get(install)
        except (FileNotFoundError, OSError, ValueError):
            glossary = None
    # Evaluation searches must not count as usage: log them to a scratch folder.
    scratch = config.state_dir() / "knowledge" / "evaluation"
    return KnowledgeBase(Context(install, ExperienceStore(), ManualStore(), glossary, ApiReferenceStore()),
                         log_dir=scratch)


def main(paths: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    files = [Path(p) for p in paths] or sorted((config.knowledge_dir() / "eval").glob("*.jsonl"))
    items = load_items(files)
    if not items:
        print("没有评估问题：knowledge/eval/*.jsonl 为空，反馈日志中也没有记录")
        return 1
    from . import vector

    if vector.warm_up():  # the server loads the model in the background; here every question must use it
        status = vector.status()
        print(f"向量检索：{status['model']}（权重 {status['weight']}，查询设备 {status['query_device']}）")
    else:
        print("向量检索：未启用")
    report = evaluate(default_base(), items)
    print(f"{report['questions']} 个问题：recall@1 {report['recall@1']}，recall@5 {report['recall@5']}，"
          f"recall@10 {report['recall@10']}，MRR@10 {report['mrr@10']}")
    for miss in report["misses"]:
        where = f"第 {miss['rank']} 位" if miss["rank"] else "前 10 位之外"
        print(f"  未进前 5（{where}）：{miss['query']}  期望 {miss['expect'][0]}  实际前 3 {miss['top']}")
    return 0

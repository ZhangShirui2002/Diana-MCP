"""Periodic review of the experience handbook, for the AI to act on with the user.

Lists drafts worth proposing for approval (they helped, or a problem they were recommended for got solved), drafts
still missing an explanation, groups of similar entries that could be merged or generalized, entries whose
automatic verification failed or ran on an older DIANA version, and entries repeatedly reported as unhelpful.
Nothing is changed here: the AI proposes, the user decides.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .experience import SOURCES, STATUSES, TYPES, Entry, ExperienceStore
from .usage import Usage

PLACEHOLDER = "（待"


def _grams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _specific(trigger: str) -> bool:
    return len(trigger) >= 4 and bool(re.search(r"[\d/]", trigger))


def similar_groups(entries: list[Entry], threshold: float = 0.45) -> list[list[Entry]]:
    """Groups of entries with similar titles or a shared specific trigger (error code, path)."""
    parent = {e.id: e.id for e in entries}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    grams = {e.id: _grams(e.title) for e in entries}
    triggers = {e.id: {t.lower() for t in e.triggers if _specific(t)} for e in entries}
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if triggers[a.id] & triggers[b.id]:
                parent[find(a.id)] = find(b.id)
                continue
            if triggers[a.id] and triggers[b.id]:
                continue  # different error codes are different problems, however alike their titles read
            overlap = len(grams[a.id] & grams[b.id]) / max(1, len(grams[a.id] | grams[b.id]))
            if overlap >= threshold:
                parent[find(a.id)] = find(b.id)
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(find(entry.id), []).append(entry)
    return [g for g in groups.values() if len(g) > 1]


def _brief(entry: Entry, **extra: Any) -> dict[str, Any]:
    return {"id": entry.id, "title": entry.title, "status": STATUSES[entry.status], **extra}


def review(store: ExperienceStore, stats: dict[str, Usage], solved: dict[str, list[str]] | None = None,
           installed_version: str | None = None, limit: int = 15) -> dict[str, Any]:
    """``solved``: draft id -> ids of jobs whose problem it was recommended for and that were solved later."""
    entries = [e for e in store.entries() if e.status != "obsolete"]
    solved = solved or {}
    suggestions = []
    for entry in entries:
        if entry.status != "draft":
            continue
        use = stats.get(entry.id, Usage())
        reasons = []
        if use.helpful:
            reasons.append(f"{use.helpful} 次被标为有用")
        if solved.get(entry.id):
            reasons.append("出问题时被推荐，之后问题已解决（作业 " + "、".join(solved[entry.id][:3]) + "）")
        if reasons and PLACEHOLDER not in entry.sections.get("原因", "") + entry.sections.get("解决", ""):
            suggestions.append(_brief(entry, why="；".join(reasons)))
        elif reasons:
            suggestions.append(_brief(entry, why="；".join(reasons) + "（先补全原因或解决）"))
    needs_explanation = [
        _brief(e, source=SOURCES[e.source]) for e in entries
        if e.status == "draft" and PLACEHOLDER in e.sections.get("原因", "") and e.source == "ai"
    ]
    for entry in entries:  # history-log drafts that keep coming up deserve an explanation as well
        use = stats.get(entry.id, Usage())
        if entry.source == "history-log" and use.recommended >= 2 and PLACEHOLDER in entry.sections.get("原因", ""):
            needs_explanation.append(_brief(entry, source=SOURCES[entry.source], recommended=use.recommended))
    groups = [[_brief(e) for e in group] for group in similar_groups(entries)]
    failed = [_brief(e, detail=(e.checks or {}).get("detail", "")) for e in entries
              if e.checks and e.checks.get("result") == "fail"]
    stale = [_brief(e, checked_on=e.checks.get("diana_version")) for e in entries
             if e.checks and installed_version and str(e.checks.get("diana_version")) != installed_version]
    unhelpful = [_brief(e, unhelpful=stats[e.id].unhelpful) for e in entries
                 if e.id in stats and stats[e.id].unhelpful >= 2 and e.status != "disputed"]
    counts = {
        "status": dict(Counter(STATUSES[e.status] for e in store.entries())),
        "type": dict(Counter(TYPES[e.type] for e in entries)),
        "source": dict(Counter(SOURCES[e.source] for e in entries)),
    }
    out: dict[str, Any] = {"counts": counts}
    for key, items in (("approval_suggestions", suggestions), ("needs_explanation", needs_explanation),
                       ("similar_groups", groups), ("failed_checks", failed), ("stale_checks", stale),
                       ("reported_unhelpful", unhelpful)):
        if items:
            out[key] = items[:limit]
            if len(items) > limit:
                out[f"{key}_more"] = len(items) - limit
    return out

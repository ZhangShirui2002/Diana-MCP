"""One search over everything diana-mcp knows about DIANA, and the plugin interface for more retrievers.

Sources: the experience handbook (knowledge/, see experience.py), the user's manual with the shipped tutorials and
verification examples (manual.py), the data-model keywords (schema.py) and the script commands (api_reference.py).
A Chinese question is also run in English: DIANA terms in it are translated with the official GUI glossary.

Retrievers are plugins: anything with ``name``, ``weight`` and ``search(query, limit) -> list[Hit]`` can be
registered with ``register(factory)``, where ``factory(context)`` returns the retriever or None. External plugins
(e.g. a vector retriever) load from the ``diana_mcp.retrievers`` entry-point group or from DIANA_MCP_RETRIEVERS
("package.module:factory", comma-separated). Their input is the same normalized chunk stream the built-in ones
index: ``Context.documents()``. Results of all retrievers are merged by weighted reciprocal-rank fusion.

Every search is logged (query, translation, results) and ``feedback`` records which results actually helped; the
two logs are the evaluation set for choosing retrievers later.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

from . import config
from . import usage as usage_module
from .api_reference import ApiReference, ApiReferenceStore
from .community import CommunityStore
from .experience import STATUSES, TYPES, ExperienceStore
from .glossary import Glossary
from .manual import _HEADING, ManualStore
from .textindex import has_cjk

SOURCE_ZH = {"experience": "经验", "community": "社区经验", "manual": "手册", "example": "算例",
             "keyword": "关键字", "api": "API"}
RRF_K = 10  # small: the top hits of every source interleave near the top instead of one source filling the list
STATUS_BOOST = {"verified": 1.3, "draft": 1.0, "disputed": 0.6, "obsolete": 0.3}
SOURCE_FACTOR = {"example": 0.6}  # .dcf examples repeat keywords densely; prefer the manual's explanations
CHUNK_CHARS, CHUNK_OVERLAP = 1500, 200


@dataclass(frozen=True)
class Document:
    """A normalized chunk of knowledge, the unit a retriever indexes."""
    id: str  # "<source>:<ref>[#<n>]"
    source: str
    ref: str
    title: str
    text: str
    lang: str = "en"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    source: str
    ref: str
    title: str
    snippet: str = ""
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ref}"

    def view(self) -> dict[str, Any]:
        out = {"source": SOURCE_ZH.get(self.source, self.source), "ref": self.ref, "title": self.title}
        out.update({k: v for k, v in self.meta.items() if v not in (None, "", [])})
        if self.snippet:
            out["snippet"] = self.snippet
        return out


@dataclass
class Query:
    text: str
    english: str = ""
    terms: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def english_text(self) -> str:
        """What to search in English sources: the translation of a Chinese question, else the question."""
        if has_cjk(self.text):
            return self.english
        return self.text


class Retriever(Protocol):
    name: str
    weight: float

    def search(self, query: Query, limit: int) -> list[Hit]: ...


@dataclass
class Context:
    install: config.DianaInstall | None
    experiences: ExperienceStore
    manuals: ManualStore
    glossary: Glossary | None = None
    api_store: ApiReferenceStore | None = None
    log_dir: Path | None = None  # where search, feedback and recommendation logs live
    community: CommunityStore | None = None

    def usage(self) -> dict[str, usage_module.Usage]:
        """Usage statistics per experience id (see usage.py)."""
        return usage_module.experience_usage(self.log_dir)

    def manual(self):
        return self.manuals.get(self.install) if self.install else None

    def schema(self):
        if self.install is None:
            return None
        from .schema import load_schema
        return load_schema(self.install)

    def api_reference(self) -> ApiReference | None:
        """The cached script-command reference; never generated here (that needs a DianaIE run)."""
        if self.install is None or self.api_store is None:
            return None
        path = self.api_store.path_for(self.install.version)
        return ApiReference.load(path) if path.is_file() else None

    def documents(self, sources: Iterable[str] | None = None) -> Iterator[Document]:
        return iter_documents(self, sources)


# --- built-in retrievers ----------------------------------------------------------------------------------------

def _snippet(text: str, words: Iterable[str], width: int = 220) -> str:
    lowered = text.lower()
    positions = [lowered.find(w.lower()) for w in words if w and len(w) > 1]
    position = min((p for p in positions if p >= 0), default=0)
    start = max(0, position - 60)
    return re.sub(r"\s+", " ", text[start:start + width]).strip()


class ExperienceRetriever:
    name, weight = "experience", 1.6
    MIN_RELATIVE = 0.3  # drop entries that only share common words with the question
    MIN_COVERAGE = 0.5  # ... and entries that contain too little of it (words of an English question)
    MIN_COVERAGE_CJK = 0.25  # Chinese questions become many overlapping bigrams, so less of them match

    def __init__(self, store: ExperienceStore, usage: Callable[[], dict[str, Any]] | None = None):
        self.store = store
        self.usage = usage

    def search(self, query: Query, limit: int) -> list[Hit]:
        bm25, by_id = self.store.index()
        scores: dict[str, float] = {}
        texts = list(dict.fromkeys(t for t in (query.text, query.english) if t))
        for text in texts:
            for entry_id, score in bm25.search(text, limit * 2):
                scores[entry_id] = scores.get(entry_id, 0.0) + score
        needed = {t: self.MIN_COVERAGE_CJK if has_cjk(t) else self.MIN_COVERAGE for t in texts}
        scores = {i: s for i, s in scores.items() if any(bm25.coverage(i, t) >= needed[t] for t in texts)}
        if self.usage is not None:  # entries that helped before rise a little, reported misses sink
            stats = self.usage()
            scores = {i: s * stats[i].factor if i in stats else s for i, s in scores.items()}
        top = max(scores.values(), default=0.0)
        hits = []
        for entry_id, score in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
            if score < self.MIN_RELATIVE * top:
                break
            entry = by_id[entry_id]
            words = [t for t, _ in query.terms] + query.text.split()
            hits.append(Hit("experience", entry.id, entry.title, _snippet(entry.body, words), score, {
                "status": STATUSES[entry.status], "type": TYPES[entry.type], "status_key": entry.status}))
        return hits


class CommunityRetriever(ExperienceRetriever):
    """Adapter that keeps public provenance/trust distinct from the user's private experience handbook."""

    name, weight = "community", 1.05
    TRUST = {
        "candidate": 0.45,
        "community-confirmed": 0.85,
        "reproducibly-verified": 1.15,
        "disputed": 0.2,
        "obsolete": 0.05,
    }
    LOCAL_STATUS = {
        "candidate": "draft",
        "community-confirmed": "verified",
        "reproducibly-verified": "verified",
        "disputed": "disputed",
        "obsolete": "obsolete",
    }

    def __init__(self, store: CommunityStore):
        self.store = store
        self.usage = None

    def search(self, query: Query, limit: int) -> list[Hit]:
        entries = self.store.entries()
        bm25, by_id = ExperienceStore(self.store.root, use_git=False).index(entries)
        scores: dict[str, float] = {}
        texts = list(dict.fromkeys(t for t in (query.text, query.english) if t))
        for text in texts:
            for entry_id, score in bm25.search(text, limit * 3):
                scores[entry_id] = scores.get(entry_id, 0.0) + score
        needed = {t: self.MIN_COVERAGE_CJK if has_cjk(t) else self.MIN_COVERAGE for t in texts}
        scores = {i: s for i, s in scores.items() if any(bm25.coverage(i, t) >= needed[t] for t in texts)}
        adjusted = []
        factors: dict[str, float] = {}
        for entry_id, score in scores.items():
            entry = by_id[entry_id]
            community_status = str(entry.extra.get("community_status", "candidate"))
            confidence = float(entry.extra.get("confidence", 0.0))
            factor = self.TRUST.get(community_status, 0.2) * (0.75 + 0.5 * confidence)
            factors[entry_id] = factor
            adjusted.append((entry_id, score * factor))
        top = max((score for _, score in adjusted), default=0.0)
        hits = []
        labels = {
            "candidate": "候选（未验证）", "community-confirmed": "社区确认",
            "reproducibly-verified": "可重复验证", "disputed": "有争议", "obsolete": "已过时",
        }
        commit = self.store.status().get("commit")
        for entry_id, score in sorted(adjusted, key=lambda pair: -pair[1])[:limit]:
            if score < self.MIN_RELATIVE * top:
                break
            entry = by_id[entry_id]
            status = str(entry.extra.get("community_status", "candidate"))
            words = [t for t, _ in query.terms] + query.text.split()
            hits.append(Hit("community", entry.id, entry.title, _snippet(entry.body, words), score, {
                "community_status": status,
                "status": labels.get(status, status),
                "status_key": self.LOCAL_STATUS.get(status, "draft"),
                "confidence": float(entry.extra.get("confidence", 0.0)),
                "type": TYPES[entry.type],
                "repository": self.store.url,
                "commit": commit,
                "trust_factor": round(factors[entry_id], 3),
            }))
        return hits


class ManualRetriever:
    name, weight = "manual", 1.0

    def __init__(self, context: Context):
        self.context = context

    def search(self, query: Query, limit: int) -> list[Hit]:
        text = query.english_text
        manual = self.context.manual() if text else None
        if manual is None:
            return []
        hits, counts = [], {"manual": 0, "example": 0}
        for hit in manual.search(text, limit=limit * 4):  # examples and pages are ranked separately in fuse()
            source = "example" if hit.page.startswith("Examples/") else "manual"
            if counts[source] < limit:
                counts[source] += 1
                hits.append(Hit(source, hit.page, hit.title, hit.snippet, hit.score, {"section": hit.section}))
        return hits


class KeywordRetriever:
    name, weight = "keyword", 0.8

    def __init__(self, context: Context):
        self.context = context

    def search(self, query: Query, limit: int) -> list[Hit]:
        text = query.english_text
        schema = self.context.schema() if text else None
        if schema is None:
            return []
        hits = []
        for rank, entry in enumerate(schema.search(text, limit=limit)):
            where = "/".join(entry.dirs)
            title = f"{entry.keyword}" + (f"：{entry.title}" if entry.title else "")
            hits.append(Hit("keyword", f"{entry.define}:{entry.keyword}", title, "", float(limit - rank),
                            {"location": f"{entry.define} [{where}]" if where else entry.define,
                             "unit": entry.quantity or None}))
        return hits


class ApiRetriever:
    name, weight = "api", 0.8

    def __init__(self, context: Context):
        self.context = context

    def search(self, query: Query, limit: int) -> list[Hit]:
        text = query.english_text
        reference = self.context.api_reference() if text else None
        if reference is None:
            return []
        return [Hit("api", e.name, e.one_line()[:160], "", float(limit - rank))
                for rank, e in enumerate(reference.search(text, limit=limit))]


_FACTORIES: list[Callable[[Context], Any]] = [
    lambda c: ExperienceRetriever(c.experiences, c.usage),
    lambda c: CommunityRetriever(c.community) if c.community is not None and c.community.ready else None,
    ManualRetriever,
    KeywordRetriever,
    ApiRetriever,
]
_PLUGIN_ERRORS: list[str] = []
_PLUGINS_LOADED = False


def register(factory: Callable[[Context], Any]) -> Callable[[Context], Any]:
    """Add a retriever factory; ``factory(context)`` returns an object with name, weight and search(), or None."""
    if factory not in _FACTORIES:
        _FACTORIES.append(factory)
    return factory


def load_plugins() -> list[str]:
    """Register retrievers from the diana_mcp.retrievers entry points and DIANA_MCP_RETRIEVERS; returns errors."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return list(_PLUGIN_ERRORS)
    _PLUGINS_LOADED = True
    specs = [(ep.name, ep.load) for ep in entry_points(group="diana_mcp.retrievers")]
    for spec in filter(None, (s.strip() for s in os.environ.get("DIANA_MCP_RETRIEVERS", "").split(","))):
        module, _, attr = spec.partition(":")
        specs.append((spec, lambda m=module, a=attr: getattr(import_module(m), a)))
    for name, loader in specs:
        try:
            register(loader())
        except Exception as exc:  # a broken plugin must not break the search
            _PLUGIN_ERRORS.append(f"{name}: {type(exc).__name__}: {exc}")
    return list(_PLUGIN_ERRORS)


def fuse(results: list[tuple[Any, list[Hit]]], limit: int) -> list[Hit]:
    """Weighted reciprocal-rank fusion: each retriever contributes weight / (K + rank) per hit.

    Ranks count per (retriever, source), so the verification examples a manual search returns do not push its
    manual pages down, and a key counts once per list; SOURCE_FACTOR scales sources inside one retriever.
    """
    scores: dict[str, float] = {}
    best: dict[str, Hit] = {}
    for retriever, hits in results:
        ranks: dict[str, int] = {}
        seen: set[str] = set()
        for hit in hits:
            if hit.key in seen:
                continue
            seen.add(hit.key)
            rank = ranks[hit.source] = ranks.get(hit.source, 0) + 1
            boost = (STATUS_BOOST.get(hit.meta.get("status_key", ""), 1.0) *
                     SOURCE_FACTOR.get(hit.source, 1.0) * float(hit.meta.get("trust_factor", 1.0)))
            scores[hit.key] = scores.get(hit.key, 0.0) + getattr(retriever, "weight", 1.0) * boost / (RRF_K + rank)
            best.setdefault(hit.key, hit)
    ranked = sorted(scores, key=lambda k: -scores[k])[:limit]
    fused = []
    for key in ranked:
        hit = best[key]
        hit.score = round(scores[key] * 1000, 2)
        hit.meta.pop("status_key", None)
        fused.append(hit)
    return fused


# --- chunks for retrievers that index everything (plugin input) ------------------------------------------------

def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[tuple[str, str]]:
    """(section heading, text) chunks: split at manual section headings, long sections in overlapping windows."""
    marks = [m.start() for m in _HEADING.finditer(text)]
    starts = [0] + [m for m in marks if m > 0]
    pieces = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        section = text[start:end].strip()
        if not section:
            continue
        heading = _HEADING.match(section)
        title = heading.group(0).strip() if heading else ""
        position = 0
        while position < len(section):
            pieces.append((title, section[position:position + size]))
            if position + size >= len(section):
                break
            position += size - overlap
    return pieces


def _optional(load: Callable[[], Any]) -> Any:
    try:
        return load()
    except FileNotFoundError:  # a source this installation does not have
        return None


def iter_documents(context: Context, sources: Iterable[str] | None = None) -> Iterator[Document]:
    wanted = set(sources) if sources else {"experience", "community", "manual", "example", "keyword", "api"}
    if "experience" in wanted:
        for entry in context.experiences.entries():
            yield Document(f"experience:{entry.id}", "experience", entry.id, entry.title,
                           "\n".join([entry.title, " ".join(entry.triggers), " ".join(entry.tags), entry.body]),
                           lang="zh", meta={"status": entry.status, "type": entry.type})
    if "community" in wanted and context.community is not None and context.community.ready:
        commit = context.community.status().get("commit")
        for entry in context.community.entries():
            status = str(entry.extra.get("community_status", "candidate"))
            yield Document(f"community:{entry.id}", "community", entry.id, entry.title,
                           "\n".join([entry.title, " ".join(entry.triggers), " ".join(entry.tags), entry.body]),
                           lang="zh", meta={"community_status": status, "confidence": entry.extra.get("confidence"),
                                            "repository": context.community.url, "commit": commit})
    manual = _optional(context.manual) if wanted & {"manual", "example"} else None
    if manual is not None:
        for name, page in manual.pages.items():
            source = "example" if name.startswith("Examples/") else "manual"
            if source not in wanted:
                continue
            for n, (section, text) in enumerate(chunk_text(page["text"])):
                title = page["title"] + (f" — {section}" if section else "")
                yield Document(f"{source}:{name}#{n}", source, name, title, text, meta={"section": section})
    schema = _optional(context.schema) if "keyword" in wanted else None
    if schema is not None:
        for entry in schema.entries:
            text = " ".join(filter(None, [entry.keyword, entry.title, entry.text, entry.quantity, entry.domain]))
            yield Document(f"keyword:{entry.define}:{entry.keyword}:{entry.line}", "keyword",
                           f"{entry.define}:{entry.keyword}", entry.title or entry.keyword, text)
    reference = _optional(context.api_reference) if "api" in wanted else None
    if reference is not None:
        for name, entry in sorted(reference.entries.items()):
            yield Document(f"api:{name}", "api", name, name, entry.doc)


# --- the knowledge base -----------------------------------------------------------------------------------------

class KnowledgeBase:
    def __init__(self, context: Context, log_dir: Path | None = None):
        self.context = context
        self.log_dir = log_dir or context.log_dir or usage_module.log_dir()
        self.context.log_dir = self.log_dir
        self._retrievers: list[Any] | None = None
        self.plugin_errors: list[str] = []

    def retrievers(self) -> list[Any]:
        """Built-in and plugin retrievers; DIANA_MCP_DISABLE_RETRIEVERS (comma-separated names) switches some off,
        e.g. to compare search quality with and without the vector retriever."""
        if self._retrievers is None:
            self.plugin_errors = load_plugins()
            disabled = {n.strip() for n in os.environ.get("DIANA_MCP_DISABLE_RETRIEVERS", "").split(",") if n.strip()}
            built = []
            for factory in _FACTORIES:
                try:
                    retriever = factory(self.context)
                except Exception as exc:
                    self.plugin_errors.append(f"{getattr(factory, '__name__', factory)}: {exc}")
                    continue
                if retriever is not None and retriever.name not in disabled:
                    built.append(retriever)
            self._retrievers = built
        return self._retrievers

    def query(self, text: str) -> Query:
        query = Query(text.strip())
        if self.context.glossary is not None:
            query.english, query.terms = self.context.glossary.translate(query.text)
        return query

    def search(self, text: str, sources: Iterable[str] | None = None, limit: int = 10,
               include_obsolete: bool = False) -> dict[str, Any]:
        query = self.query(text)
        wanted = set(sources) if sources else None
        results, problems = [], []
        for retriever in self.retrievers():
            if wanted is not None and retriever.name not in wanted and not (
                    retriever.name == "manual" and "example" in wanted):
                continue
            try:
                hits = retriever.search(query, limit)
            except FileNotFoundError:
                continue  # e.g. no DIANA installation or manual on this machine
            except Exception as exc:
                problems.append(f"{retriever.name}: {type(exc).__name__}: {exc}")
                continue
            if wanted is not None:
                hits = [h for h in hits if h.source in wanted]
            if not include_obsolete:
                hits = [h for h in hits if h.meta.get("status_key") != "obsolete"]
            results.append((retriever, hits))
        fused = fuse(results, limit)
        search_id = uuid.uuid4().hex[:10]
        self._log("search_log.jsonl", {
            "id": search_id, "t": round(time.time(), 3), "query": query.text, "english": query.english,
            "terms": query.terms, "results": [{"key": h.key, "score": h.score} for h in fused],
        })
        out: dict[str, Any] = {"search_id": search_id, "query": query.text}
        if query.terms:
            out["terms"] = [{"中文": zh, "English": ens} for zh, ens in query.terms]
        if query.english and query.english != query.text:
            out["searched_in_english"] = query.english
        out["results"] = [h.view() for h in fused]
        if problems or self.plugin_errors:
            out["problems"] = problems + self.plugin_errors
        return out

    def feedback(self, search_id: str, helpful: list[str], note: str | None = None,
                 unhelpful: list[str] | None = None) -> dict[str, Any]:
        record = {"t": round(time.time(), 3), "search_id": search_id, "helpful": helpful,
                  "unhelpful": unhelpful or [], "note": note}
        self._log("feedback.jsonl", record)
        return {"recorded": True, **record}

    def _log(self, name: str, record: dict[str, Any]) -> None:
        usage_module.append(name, record, self.log_dir)

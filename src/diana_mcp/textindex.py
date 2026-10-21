"""Tokenizer and BM25 ranking shared by the knowledge-base retrievers.

Chinese has no spaces, so CJK runs become overlapping character bigrams (the usual approach without a word
segmenter); ASCII text becomes words plus compounds such as error codes (is41/0001) and parameter paths
(linear/elasti/young), so both "IS41" and "IS41/0001" match.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

_WORD = re.compile(r"[a-z0-9]+")
_COMPOUND = re.compile(r"[a-z0-9]+(?:[/._-][a-z0-9]+)+")
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
# English function words: in a small collection they are rare enough to look significant and drown real matches.
STOPWORDS = frozenset("""a an the of to in on at by for from with without into onto over under and or not no is are
was were be been being it its this that these those as if then than so do does did can could should would may might
must how what which when where why who whom there here i you we they he she my your our their me us them some any
all each per via also only just very""".split())


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens = [t for t in _WORD.findall(text) if t not in STOPWORDS]
    for compound in _COMPOUND.findall(text):
        parts = re.split(r"[/._-]", compound)
        tokens.append("/".join(parts))
        if len(parts) > 2:  # adjacent pairs, so "IS41/0001" matches inside "/DIANA/NL/IS41/0001"
            tokens.extend(f"{a}/{b}" for a, b in zip(parts, parts[1:]))
    for run in _CJK.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


class BM25:
    """Okapi BM25 over documents given as weighted fields: {doc_id: [(text, weight), ...]}."""

    def __init__(self, docs: dict[str, Iterable[tuple[str, float]]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: dict[str, Counter] = {}
        df: Counter = Counter()
        for doc_id, fields in docs.items():
            counts: Counter = Counter()
            for text, weight in fields:
                for token in tokenize(text or ""):
                    counts[token] += weight
            self.tf[doc_id] = counts
            df.update(counts.keys())
        n = len(self.tf)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.lengths = {d: sum(c.values()) for d, c in self.tf.items()}
        self.avg = sum(self.lengths.values()) / max(n, 1)

    def coverage(self, doc_id: str, query: str) -> float:
        """Share of the query's distinct tokens that occur in the document."""
        terms = set(tokenize(query))
        if not terms:
            return 0.0
        counts = self.tf.get(doc_id, {})
        return sum(1 for t in terms if counts.get(t)) / len(terms)

    def search(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        terms = [t for t in dict.fromkeys(tokenize(query)) if t in self.idf]
        scored = []
        for doc_id, counts in self.tf.items():
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * self.lengths[doc_id] / (self.avg or 1))
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    score += self.idf[term] * tf * (self.k1 + 1) / (tf + norm)
            if score > 0:
                scored.append((doc_id, score))
        scored.sort(key=lambda item: -item[1])
        return scored[:limit]

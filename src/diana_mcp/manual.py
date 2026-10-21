"""Full-text search in the DIANA user's manual of the local installation.

DIANA installs its complete documentation as share/Man/HTML/diana-<version>.qch, a Qt help file: an SQLite
database whose pages are qCompress'ed HTML (4-byte length prefix + zlib). The text is extracted once and cached
under the diana-mcp state directory. Like the API reference it is never shipped with this package: the manual
belongs to DIANA FEA BV.
"""
from __future__ import annotations

import html
import json
import math
import re
import sqlite3
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import config

CACHE_FORMAT = 2  # bump when the extracted content changes, so old caches are rebuilt
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")
# Shipped examples indexed next to the manual: DianaIE tutorial scripts and verification .dcf/.py files.
_EXAMPLES = (("Tutorials", ("*.py",)), ("Verify", ("*.dcf", "*.py")))
# Section headings as they appear in the text: '89.2.1 Node Selection', 'I.1.1.2 Options for Tabular Output'
_HEADING = re.compile(r"(?m)^\s*((?:[A-P]|\d+)(?:\.\d+)+|\d+)\s+[A-Z][^\n]{2,80}$")


def find_qch(install: config.DianaInstall) -> Path | None:
    folder = install.home / "share" / "Man" / "HTML"
    candidates = sorted(folder.glob("diana-*.qch"))
    return candidates[-1] if candidates else None


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(head|script|style)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li|pre|table|dt|dd)>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def extract_pages(qch: Path) -> list[dict]:
    """Title and plain text of every HTML page in a .qch file."""
    db = sqlite3.connect(f"file:{qch.as_posix()}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT n.Name, n.Title, d.Data FROM FileNameTable n JOIN FileDataTable d ON d.Id = n.FileId "
            "WHERE n.Name LIKE '%.html'"
        ).fetchall()
    finally:
        db.close()
    pages = []
    for name, title, blob in rows:
        markup = zlib.decompress(blob[4:]).decode("utf-8", errors="replace")
        heading = re.search(r"(?is)<title>(.*?)</title>", markup)
        if heading:
            title = html.unescape(re.sub(r"\s+", " ", heading.group(1))).strip()
        pages.append({"name": name, "title": title or name, "text": html_to_text(markup)})
    pages.sort(key=lambda p: p["name"])
    return pages


def extract_examples(install: config.DianaInstall) -> list[dict]:
    """The DianaIE tutorial scripts and verification command files shipped in <home>/share."""
    share = install.home / "share"
    pages = []
    for folder, patterns in _EXAMPLES:
        root = share / folder
        for pattern in patterns:
            for path in sorted(root.rglob(pattern)) if root.is_dir() else []:
                relative = path.relative_to(share).as_posix()
                kind = "DianaIE tutorial script" if folder == "Tutorials" else "verification example"
                pages.append({
                    "name": f"Examples/{relative}",
                    "title": f"{kind}: {relative}",
                    "text": path.read_text(encoding="utf-8", errors="replace"),
                    "source": str(path),
                })
    return pages


@dataclass
class Hit:
    page: str
    title: str
    score: float
    section: str
    snippet: str


class Manual:
    def __init__(self, version: str, pages: list[dict]):
        self.version = version
        self.pages = {p["name"]: p for p in pages}
        self._lower = {name.lower(): name for name in self.pages}
        self._terms: dict[str, Counter] = {}
        self._lengths: dict[str, int] = {}
        df: Counter = Counter()
        for name, page in self.pages.items():
            counts = Counter(_TOKEN.findall(page["text"].lower()))
            for token in _TOKEN.findall(page["title"].lower()):
                counts[token] += 5  # title words weigh like five mentions in the body
            self._terms[name] = counts
            self._lengths[name] = sum(counts.values())
            df.update(counts.keys())
        n = len(self.pages)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self._avg = sum(self._lengths.values()) / max(n, 1)

    @classmethod
    def load(cls, path: Path) -> Manual:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["version"], data["pages"])

    def search(self, query: str, limit: int = 8) -> list[Hit]:
        terms = [t for t in dict.fromkeys(_TOKEN.findall(query.lower())) if t in self._idf]
        phrase = query.strip().lower()
        scored = []
        for name, counts in self._terms.items():
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    score += self._idf[term] * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * self._lengths[name] / self._avg))
            if not score:
                continue
            if len(terms) > 1 and phrase in self.pages[name]["text"].lower():
                score *= 1.5
            if name.startswith("CumIdx") or name.endswith("li6.html"):
                score *= 0.3  # index pages list every keyword; rank real content first
            scored.append((score, name))
        scored.sort(reverse=True)
        return [self._hit(name, score, terms, phrase) for score, name in scored[:limit]]

    def _hit(self, name: str, score: float, terms: list[str], phrase: str) -> Hit:
        page = self.pages[name]
        text = page["text"]
        lowered = text.lower()
        position = lowered.find(phrase) if phrase else -1
        if position < 0:
            rarest = sorted(terms, key=lambda t: -self._idf.get(t, 0))
            position = next((p for p in (lowered.find(t) for t in rarest) if p >= 0), 0)
        start = max(0, position - 150)
        snippet = re.sub(r"\s+", " ", text[start:position + 250]).strip()
        return Hit(name, page["title"], round(score, 2), self.section_at(text, position), snippet)

    @staticmethod
    def section_at(text: str, position: int) -> str:
        headings = [m for m in _HEADING.finditer(text) if m.start() <= position]
        return headings[-1].group(0).strip() if headings else ""

    def resolve(self, page: str) -> str | None:
        page = page.strip().replace("\\", "/")
        if page in self.pages:
            return page
        for candidate in (page, page + ".html", "Theory/" + page, "Theory/" + page + ".html"):
            if candidate.lower() in self._lower:
                return self._lower[candidate.lower()]
        return None

    def read(self, page: str, find: str | None = None, offset: int = 0, max_chars: int = 6000) -> dict:
        name = self.resolve(page)
        if name is None:
            raise KeyError(page)
        text = self.pages[name]["text"]
        if find:
            found = text.lower().find(find.lower())
            if found < 0:
                raise LookupError(f"{find!r} does not occur on page {name}")
            offset = max(0, found - 200)
        offset = max(0, min(offset, len(text)))
        end = min(len(text), offset + max(500, max_chars))
        return {
            "page": name,
            "title": self.pages[name]["title"],
            "section": self.section_at(text, offset + 1),
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
            "text": text[offset:end],
        }


class ManualStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self._loaded: dict[str, Manual] = {}

    @property
    def _dir(self) -> Path:
        return self.directory or config.state_dir() / "manual"

    def get(self, install: config.DianaInstall) -> Manual:
        if install.version not in self._loaded:
            path = self._dir / f"diana-{install.version}.json"
            if not path.is_file() or json.loads(path.read_text(encoding="utf-8")).get("format") != CACHE_FORMAT:
                qch = find_qch(install)
                if qch is None:
                    raise FileNotFoundError(f"no manual (share/Man/HTML/diana-*.qch) in {install.home}")
                self._dir.mkdir(parents=True, exist_ok=True)
                pages = extract_pages(qch) + extract_examples(install)
                payload = {"format": CACHE_FORMAT, "version": install.version, "source": str(qch), "pages": pages}
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self._loaded[install.version] = Manual.load(path)
        return self._loaded[install.version]
